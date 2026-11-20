"""Texas (ERCOT) generation outages, hourly (PLAN 9e: the tx region page's `outages` group).

Source: ERCOT's public dashboard JSON (keyless, the same host and terms as power_ercot),
``https://www.ercot.com/api/1/services/read/dashboards/generation-outages.json`` (~0.5 MB): MW of generation out, every
5 minutes for the last 7 days (``previous`` = the 6 days before, ``current`` = today), each row:

  {"deliveryTime": "2026-11-13 15:21:00-0600", "dstFlag": "N",
   "Combined": {"unplanned": 22438, "planned": 14067, "total": 36505},
   "Dispatchable": {...}, "Renewable": {...}}

keyed by its epoch in milliseconds (checked against ``deliveryTime``). ``types`` lists the groups (Dispatchable,
Renewable); Combined is their sum. Stamps carry their own UTC offset, so nothing is guessed (fall-back safe).

Requests: one conditional GET an hour (``interval: 1h``, ``ctx.get_if_changed``; the file is re-issued every 5 min, so
most are 200s): ~12 MB/day. The 7 days in each file back-fill a gap by themselves.

Series (MW, freq 1h, ts = hour start, naive UTC; the mean of the hour's twelve 5-min readings, complete hours only):
  ercot.outage.<group>.<kind>   group in combined, dispatchable, renewable; kind in planned, unplanned, total
Fail closed (FormatDrift): a body without ``current``/``previous`` objects, an unknown outage group, a row without a
group or kind, a value outside 0..MAX_MW, a stamp that disagrees with its key, or no complete hour.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.connectors.power_ercot import BASE, PER_HOUR, _num, _obj, _stamp
from montney.store import Obs, SeriesMeta

URL = BASE + "generation-outages.json"
GROUPS = {"Combined": "combined", "Dispatchable": "dispatchable", "Renewable": "renewable"}
KINDS = ("planned", "unplanned", "total")
GROUP_NAMES = {"combined": "all generation", "dispatchable": "dispatchable (thermal, hydro, storage)",
               "renewable": "renewable (wind, solar)"}
MAX_MW = 150_000                 # ERCOT's installed capacity is ~210 GW; ~35 GW out is normal in autumn
LABEL = "ERCOT / Texas generation outages"


def parse(payload) -> dict[datetime, dict[str, float]]:
    """{5-min stamp: {"<group>.<kind>": MW}} from both days' blocks."""
    types = payload.get("types") if isinstance(payload, dict) else None
    if not isinstance(types, list) or set(types) - set(GROUPS):
        raise FormatDrift(f"ERCOT outages: unknown outage groups {types!r} (map them in GROUPS)")
    out: dict[datetime, dict[str, float]] = {}
    for block in ("previous", "current"):
        for key, row in _obj(payload, block, dict, "outages").items():
            if not isinstance(row, dict) or "deliveryTime" not in row:
                raise FormatDrift(f"ERCOT outages: a {block} row without deliveryTime")
            try:
                epoch = int(key)
            except ValueError:
                raise FormatDrift(f"ERCOT outages: key {key!r} isn't an epoch") from None
            ts = _stamp(row["deliveryTime"], epoch)
            vals = {}
            for g, slug in GROUPS.items():
                part = row.get(g)
                if not isinstance(part, dict):
                    raise FormatDrift(f"ERCOT outages {row['deliveryTime']}: no {g} block")
                for k in KINDS:
                    v = _num(part.get(k), f"outages {g}.{k}", 0, MAX_MW)
                    if v is None:
                        raise FormatDrift(f"ERCOT outages {row['deliveryTime']}: {g}.{k} is blank")
                    vals[f"{slug}.{k}"] = v
            out[ts] = vals
    return out


def hourly(points: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    """5-min readings -> {UTC hour start: mean} for hours with all twelve readings."""
    by: dict[datetime, list[dict]] = defaultdict(list)
    for ts, row in points.items():
        by[ts.replace(minute=0, second=0, microsecond=0)].append(row)
    return {h: {k: sum(r[k] for r in rows) / len(rows) for k in rows[0]}
            for h, rows in by.items() if len(rows) == PER_HOUR}


class ErcotOutages(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "tz": "UTC offsets in the feed (Central prevailing time)",
              "lag": "~5 min (5-min readings); an hour lands ~5 min after it ends (+ the hourly poll)"}
        return [SeriesMeta(f"ercot.outage.{g}.{k}", self.id, f"ERCOT generation out, {GROUP_NAMES[g]}: {k}, hourly mean",
                           "MW", "1h", LABEL, extra=ex) for g in GROUPS.values() for k in KINDS]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        r = ctx.get_if_changed(URL, timeout=60)
        if r is None:                                   # unchanged since the last written run: nothing new
            return res
        try:
            payload = r.json()
        except ValueError:
            raise FormatDrift("ERCOT outages: the body isn't JSON (blocked by the site's firewall?)") from None
        hours = hourly(parse(payload))
        if not hours:
            raise FormatDrift("ERCOT outages: no complete hour (NO-RESULT)")
        for h, row in sorted(hours.items()):
            for k, v in row.items():
                res.obs.append(Obs(f"ercot.outage.{k}", h, round(v, 1), h))
        return res
