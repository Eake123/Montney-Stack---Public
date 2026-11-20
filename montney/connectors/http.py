"""Declarative HTTP/JSON connector.

Everything is driven by the spec - no code per source. Full reference: docs/SOURCES.md.

    request:
      url: https://...
      params:  {startDate: "{today-3d}"}
      headers: {API-KEY: "{secret:AESO_API_KEY}"}
      method: POST            # optional; with json: {...} or form: {...} body (templated)
      iterate: {var: date, days_back: 3, days_forward: 0}   # optional: one request per day
    extract:
      - path: data              # dotted path into the JSON; a list = rows, an object = one row
        ts:    {field: lastUpdated, tz: "-07:00"}   # or {root: data.gasDay, date_only: true}
        as_of: ts | fetch | {root: data.asofDay, tz: "-07:00"}
        id: "ngtl.csr.{key}"     # series id template; {key} = field key, {row} = key_field value
        label: "{label}"
        from: e3m3/d             # unit the source publishes in
        unit: MMcf/d             # unit to store
        freq: 30min
        group: "NGTL / Current System"
        key_field: fuel_type     # optional: one set of series per distinct row value
        fields:
          total_receipts: {field: totalReceipts, label: Total receipts}
          empress:        {field: empressBorderFlow, abs: true}
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from montney import units
from montney.connectors.base import (
    DEFAULT_TZ, Connector, Context, FetchResult, local_today, parse_ts, render, slug, to_float,
)
from montney.store import Obs, SeriesMeta


def dig(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            obj = obj[int(part)] if part.isdigit() and int(part) < len(obj) else None
        else:
            obj = obj.get(part)
    return obj


class HttpConnector(Connector):
    def _requests(self) -> list[tuple[dict, dict]]:
        """Expand `iterate` into concrete (vars, request) pairs."""
        req = self.spec["request"]
        tz = self.spec.get("tz", DEFAULT_TZ)
        it = req.get("iterate")
        if not it:
            return [({}, req)]
        today = local_today(tz)
        fmt = it.get("format", "%Y-%m-%d")
        out = []
        for off in range(-int(it.get("days_back", 0)), int(it.get("days_forward", 0)) + 1):
            d = today + timedelta(days=off)
            out.append(({it.get("var", "date"): d.strftime(fmt)}, req))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        tz = self.spec.get("tz", DEFAULT_TZ)
        reqs = self._requests()
        seen_meta: dict[str, SeriesMeta] = {}
        errors = []
        for i, (vars, req) in enumerate(reqs):
            if i:
                time.sleep(float(req.get("pause", 0.3)))  # be polite to free endpoints
            url = render(req["url"], vars, tz)
            try:
                r = ctx.request(
                    str(req.get("method", "GET")).upper(),
                    url,
                    params=render(req.get("params") or {}, vars, tz),
                    headers=render(req.get("headers") or {}, vars, tz),
                    json=render(req["json"], vars, tz) if "json" in req else None,
                    data=render(req["form"], vars, tz) if "form" in req else None,
                    timeout=float(req.get("timeout", 30)),
                    retries=int(req.get("retries", 3)),
                )
                payload = r.json()
            except Exception as e:  # one bad day shouldn't sink the whole backfill
                errors.append(f"{vars or url}: {e}")
                continue
            for block in self.spec["extract"]:
                self._extract(block, payload, ctx, res, seen_meta)
        if errors and len(errors) == len(reqs):
            raise RuntimeError("; ".join(errors))
        res.warnings.extend(errors)
        res.meta = list(seen_meta.values())
        return res

    def _extract(self, block: dict, payload: Any, ctx: Context, res: FetchResult,
                 seen: dict[str, SeriesMeta]) -> None:
        data = dig(payload, block.get("path"))
        if data is None:
            return
        rows = data if isinstance(data, list) else [data]
        ts_cfg = block.get("ts") or {}
        as_cfg = block.get("as_of", "ts")
        key_field = block.get("key_field")

        root_ts = parse_ts(dig(payload, ts_cfg["root"]), ts_cfg) if "root" in ts_cfg else None
        root_as_of = None
        if isinstance(as_cfg, dict) and "root" in as_cfg:
            root_as_of = parse_ts(dig(payload, as_cfg["root"]), as_cfg)

        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = root_ts if root_ts is not None else parse_ts(row.get(ts_cfg.get("field")), ts_cfg)
            if ts is None:
                continue
            if as_cfg == "ts":
                as_of = ts
            elif as_cfg == "fetch":
                as_of = ctx.now.replace(second=0)
            elif root_as_of is not None:
                as_of = root_as_of
            else:
                as_of = parse_ts(row.get(as_cfg.get("field")), as_cfg) or ctx.now
            rowkey = slug(str(row.get(key_field, ""))) if key_field else ""
            rowname = str(row.get(key_field, "")) if key_field else ""

            for key, f in block["fields"].items():
                f = f if isinstance(f, dict) else {"field": f}
                val = to_float(dig(row, f.get("field", key)))
                if val is None:
                    continue
                src_u = f.get("from", block.get("from"))
                dst_u = f.get("unit", block.get("unit", src_u or ""))
                if src_u and dst_u and src_u != dst_u:
                    val = units.convert(val, src_u, dst_u)
                if f.get("abs"):
                    val = abs(val)
                val *= float(f.get("scale", 1.0))

                tvars = {"key": key, "row": rowkey, "row_name": rowname,
                         "label": f.get("label", key.replace("_", " "))}
                sid = render(f.get("id", block.get("id", "{key}")), tvars)
                res.obs.append(Obs(sid, ts, val, as_of))
                if sid not in seen:
                    seen[sid] = SeriesMeta(
                        sid, self.id, render(block.get("label", "{label}"), tvars),
                        dst_u or "", f.get("freq", block.get("freq", "")),
                        render(block.get("group", ""), tvars),
                        day=f.get("day", block.get("day", "")),
                    )

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for block in self.spec["extract"]:
            if block.get("key_field"):
                continue  # depends on the data
            for key, f in block["fields"].items():
                f = f if isinstance(f, dict) else {"field": f}
                tvars = {"key": key, "row": "", "row_name": "",
                         "label": f.get("label", key.replace("_", " "))}
                src_u = f.get("from", block.get("from"))
                out.append(SeriesMeta(
                    render(f.get("id", block.get("id", "{key}")), tvars), self.id,
                    render(block.get("label", "{label}"), tvars),
                    f.get("unit", block.get("unit", src_u or "")) or "",
                    f.get("freq", block.get("freq", "")), render(block.get("group", ""), tvars),
                    day=f.get("day", block.get("day", "")),
                ))
        for d in self.spec.get("derive") or []:
            out.append(SeriesMeta(d["id"], self.id, d.get("label", d["id"]), d.get("unit", ""),
                                  d.get("freq", ""), d.get("group", ""), day=d.get("day", "")))
        return out
