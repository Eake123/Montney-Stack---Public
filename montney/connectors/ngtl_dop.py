"""NGTL Daily Operating Plan: area capability vs flow (series) + maintenance outages (events).

Same unauthenticated API as ngtl_csr / ngtl_gdsr (the backend of my.tccustomerexpress.com):

  GET /outages/publisheddates          publication timestamps (UTC), newest first
  GET /chart?start=YYYY-MM-DD&end=...  per area and gas day: flow, historicalFlow (one year
                                       earlier), capabilityVolume, firmContractEnergy
  GET /outages                         current publication's outage rows (each embeds its area)

Series (MMcf/d, freq D, ts = gas day):
  ngtl.dop.<area>.capability   as_of = DOP publication time, so each publication is a vintage
  ngtl.dop.<area>.flow         actual daily flow (past days only), as_of = ts
  ngtl.dop.<area>.flow_yago    same calendar day one year earlier, as_of = ts
for area in usjr, wgat, egat, osda (the only areas the chart covers).

We key capability vintages on the publication time, not the fetch time: the DOP is published
once per business day (~21:30 UTC), so polling hourly adds no duplicate vintages.

Events (kind "outage"), id "ngtl_dop:<outageId>:<area>". Severity rules:
  critical  impact text says "partial impact" / "full impact" / "curtail"
  warn      "potential impact", or the outage removes >= 10% of the area's base capability
  info      everything else (e.g. "No impact to FT-D anticipated")
refs = the area's network id (gas.ngtl.<area>). Local-area (LCLR/LCLD) and plant-turnaround
rows have no area node, so refs is empty and extra.local = true.
starts/ends: NGTL gas days run 08:00 MST to 08:00 MST (fixed 15:00 UTC; see ngtl_gasday.py), so
an outage on gas days D1..D2 runs from gas_day_start(D1) to gas_day_start(D2 + 1).

The outage list is the operator's complete current set, so fetch() declares
event_scopes = [(source, "outage")]: an outage that drops out of the republished list (cancelled
or finished early) is marked withdrawn.

Overlapping outages: each event's extra.capability_mmcfd is that row's own outage capability.
Area capability on a day is the MINIMUM across the overlapping rows, never base minus the sum of
the cuts. It is already in ngtl.dop.<area>.capability (TC's chart), so don't recompute it from
events. Example from 2026-11-01..07 at EGAT: Jenner 154,000, NPS 42 Zone 6 145,000 and then
135,000 10^3 m3/d; the chart shows 145,000 and then 135,000.

firmContractEnergy (EGAT/WGAT, in the chart) is TJ/d, not volume. It is not stored. Comparing it
with capability needs NGTL's heating value (38.5 MJ/m3 for the border areas, from /heatValues),
the same factor power uses for gas burn.

Units: TC publishes 10^3 m3/d. The payload layout is checked on every fetch; a changed layout
raises FormatDrift rather than silently storing nothing.
"""

from __future__ import annotations

import re
from datetime import timedelta

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import (
    E3M3_TO_MMCF, TC_API, TC_SITE, FormatDrift, gas_day_start_utc, parse_dt, cache_load, cache_save,
)
from montney.store import Event, Obs, SeriesMeta

# areaId -> acronym for the areas the chart returns. Verified against /areas on 2026-10-31
# and re-checked against the area objects embedded in /outages on every fetch.
CHART_AREAS = {1: "USJR", 2: "WGAT", 3: "EGAT", 7: "OSDA"}
# Areas that exist as network features (docs/ARCHITECTURE.md ids), for event refs.
AREA_REFS = {a: f"gas.ngtl.{a.lower()}" for a in ("USJR", "WGAT", "EGAT", "OSDA", "NEDA", "FHBC", "FHSK")}
AREA_NAMES = {"USJR": "Upstream James River", "WGAT": "West Gate", "EGAT": "East Gate",
              "OSDA": "Oil Sands Delivery Area"}
MEASURES = {
    "capability": ("capabilityVolume", "capability"),
    "flow": ("flow", "flow"),
    "flow_yago": ("historicalFlow", "flow one year earlier"),
}
GROUP = "NGTL / Daily Operating Plan"
DOP_STATE, FULL_EVERY_H = "ngtl_dop_state", 12.0   # L-081: <home>/cache; full re-read at least every 12 h


def series_id(area: str, measure: str) -> str:
    return f"ngtl.dop.{area.lower()}.{measure}"


def severity(impact: str, base: float | None, cap: float | None) -> str:
    t = (impact or "").lower()
    if re.search(r"partial impact|full impact|curtail", t):
        return "critical"
    if "potential impact" in t:
        return "warn"
    if base and cap is not None and base > 0 and (base - cap) / base >= 0.10:
        return "warn"
    return "info"


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


class NgtlDop(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for a in CHART_AREAS.values():
            for m, (_, label) in MEASURES.items():
                out.append(SeriesMeta(series_id(a, m), self.id, f"NGTL {AREA_NAMES[a]} ({a}) {label}",
                                      "MMcf/d", "D", GROUP, day="gas"))  # NGTL gas day, 15:00 UTC
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        base = p.get("api", TC_API)
        res = FetchResult(meta=self.declared_series())

        pubs = ctx.get(f"{base}/outages/publisheddates").json().get("data")
        if not isinstance(pubs, list) or not pubs:
            raise FormatDrift(f"{self.id}: /outages/publisheddates has no data list")
        pub = max(parse_dt(s) for s in pubs)
        # L-081: /outages/publisheddates is one small request. Only a new publication (or FULL_EVERY_H since the last
        # full read, for the chart's actual-flow rows) re-reads /chart and /outages. Tomorrow's capability therefore
        # shows within one run (≤ 1 h) of TC publishing it, whenever that is.
        state = cache_load(DOP_STATE)
        full_every = timedelta(hours=float(p.get("full_every_hours", FULL_EVERY_H)))
        last_full = parse_dt(state.get("full_at")) if state.get("full_at") else None
        if state.get("pub") == pub.isoformat() and last_full and ctx.now - last_full < full_every:
            res.warnings.append(f"no new DOP publication since {pub:%Y-%m-%d %H:%M}; chart and outages not re-read")
            return res

        today = local_today()
        start = today - timedelta(days=int(p.get("days_back", 7)))
        end = today + timedelta(days=int(p.get("days_forward", 16)))
        chart = ctx.get(f"{base}/chart", params={"start": start.isoformat(), "end": end.isoformat()}).json()
        res.obs.extend(self.chart_obs(chart, pub))

        outages = ctx.get(f"{base}/outages").json().get("data")
        if not isinstance(outages, list):
            raise FormatDrift(f"{self.id}: /outages has no data list")
        self.check_area_ids(outages)
        res.events.extend(self.outage_events(outages))
        res.event_scopes = [(self.id, "outage")]  # complete set: missing outages get withdrawn
        try:            # after the full read parsed; a failed run re-reads next time
            cache_save(DOP_STATE, {"pub": pub.isoformat(), "full_at": ctx.now.strftime("%Y-%m-%d %H:%M:%S")})
        except OSError as e:
            res.warnings.append(f"DOP state not written: {e}")
        return res

    # ------------------------------------------------------------------ chart
    def chart_obs(self, chart: dict, pub) -> list[Obs]:
        rows = chart.get("data") if isinstance(chart, dict) else None
        if not isinstance(rows, list) or not rows:
            raise FormatDrift(f"{self.id}: /chart returned no rows (needs start and end)")
        need = {"gasDay", "areaId", "capabilityVolume", "flow", "historicalFlow"}
        out: list[Obs] = []
        for r in rows:
            if not need <= set(r):
                raise FormatDrift(f"{self.id}: /chart row missing {sorted(need - set(r))}")
            area = CHART_AREAS.get(r["areaId"])
            if not area:
                continue
            day = parse_dt(r["gasDay"], ("%Y-%m-%d",))
            for m, (field, _) in MEASURES.items():
                v = _num(r.get(field))
                if v is None:
                    continue
                as_of = pub if m == "capability" else day
                out.append(Obs(series_id(area, m), day, v * E3M3_TO_MMCF, as_of))
        return out

    def check_area_ids(self, outages: list[dict]) -> None:
        for o in outages:
            a = o.get("area") or {}
            if a.get("id") in CHART_AREAS and a.get("dopAcronym") != CHART_AREAS[a["id"]]:
                raise FormatDrift(f"{self.id}: areaId {a['id']} is now {a.get('dopAcronym')!r}, "
                                  f"expected {CHART_AREAS[a['id']]!r}")

    # ---------------------------------------------------------------- outages
    def outage_events(self, outages: list[dict]) -> list[Event]:
        out = []
        for o in outages:
            area = ((o.get("area") or {}).get("dopAcronym") or "").strip().upper()
            if "outageId" not in o or "startDateTime" not in o:
                raise FormatDrift(f"{self.id}: outage row missing outageId/startDateTime")
            start = parse_dt(o["startDateTime"])
            end = parse_dt(o.get("endDateTime")) or start
            base = _num(o.get("areaBaseCapability"))
            if base is None:
                base = _num(o.get("localAreaBaseCapability"))
            cap = _num(o.get("flowCapability"))
            if cap is None:
                cap = _num(o.get("localAreaOutageCapability"))
            impact = (o.get("impact") or "").strip()
            desc = (o.get("description") or "").strip()
            to_mm = (lambda v: round(v * E3M3_TO_MMCF, 1) if v is not None else None)
            cut = (base - cap) if base is not None and cap is not None else None
            lines = [impact]
            if base is not None and cap is not None:
                lines.append(f"Capability {to_mm(cap):,.0f} MMcf/d vs base {to_mm(base):,.0f} "
                             f"(cut {to_mm(cut):,.0f} MMcf/d)")
            elif cap is not None:
                lines.append(f"Outage capability {to_mm(cap):,.0f} MMcf/d")
            if o.get("typicalFlow"):
                lines.append(f"Typical flow {o['typicalFlow']} 10³m³/d")
            ref = AREA_REFS.get(area)
            out.append(Event(
                id=f"{self.id}:{o['outageId']}:{area or 'na'}",
                source=self.id,
                kind="outage",
                title=f"{desc} ({area})" if area else desc,
                body="\n".join(x for x in lines if x),
                url=f"{TC_SITE}/#/OutagesList",
                starts=gas_day_start_utc(start.date()),
                ends=gas_day_start_utc(end.date() + timedelta(days=1)),
                refs=[ref] if ref else [],
                severity=severity(impact, base, cap),
                published=parse_dt(o.get("publishedDateTimeUtc")),
                extra={"area": area, "outage_id": o["outageId"], "local": ref is None,
                       "base_mmcfd": to_mm(base), "capability_mmcfd": to_mm(cap),
                       "cut_mmcfd": to_mm(cut), "typical_flow_e3m3d": o.get("typicalFlow")},
            ))
        return out
