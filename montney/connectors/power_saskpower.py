"""SaskPower: system demand, generation, net interchange and generation by type (L-106, PLAN 8c wcan).

Source (keyless): https://www.saskpower.com/ignitionapi/PowerUseDashboard/GetPowerUseDashboardData, the JSON behind
SaskPower's "Where your power comes from" page (~8 KB, one request). It carries **one 24-hour average**: the last full
Saskatchewan day (``asOfDate`` = that day's end, 23:59:59 CST = UTC-6; Saskatchewan has no daylight time), about
1.5 days behind real time. No history: each call returns only the latest day, so the source polls twice a day.

Identity (checked 2026-11-05's day): generation 2,958 + netInterchange -61 = systemDemand 2,897, so ``netInterchange``
is **net imports** (+ import, - export; the page shows a negative as "exported"). It's one net number with no split by
tie (AB, MB, North Dakota).

Hourly region lines (regions/wcan.yaml) need hourly parts, so each daily mean is **held across that day's 24 hours**
(freq 1h). The meta says so, and the region lines that use it are marked estimate. Never read these as hourly
measurements.

Series (MW, freq 1h, the day's mean held across its hours):
  saskpower.demand, saskpower.generation, saskpower.net_imports (+ import)
  saskpower.gen.<fuel>    wind, hydro, solar, coal, gas, other (FUELS keys)
  <each of the above>_best  the same, then the latest day's mean HELD FORWARD from that day's end up to the current hour
                          (at most HOLD_MAX past it), so the wcan region lines reach "now" instead of waiting ~1.5-2 days
                          for SaskPower's next day (PLAN 9 item 1). Est.: the held hours are overwritten by the real
                          daily mean when that day is published (same ts / as_of). Polled hourly so the held tail
                          follows the clock.
Fail closed (FormatDrift): a missing field, an unknown generation type, or demand != generation + net interchange by
more than TOLERANCE_MW.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://www.saskpower.com/ignitionapi/PowerUseDashboard/GetPowerUseDashboardData"
TYPES = {"wind": "wind", "hydro": "hydro", "solar": "solar", "coal": "coal", "gas": "gas", "natural gas": "gas",
         "other": "other"}
TOLERANCE_MW = 5
GROUP = "SaskPower / daily supply mix"
MAX_MW = 10_000
HOLD_MAX = timedelta(hours=72)            # beyond this the held value is too old to show as "now"


def _date(s: str) -> datetime:
    m = re.match(r"/Date\((-?\d+)\)/", str(s))
    if not m:
        raise FormatDrift(f"SaskPower: asOfDate {s!r}")
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).replace(tzinfo=None)


def parse(payload: dict) -> tuple[datetime, dict[str, float]]:
    """-> (the day's first UTC hour start, {key: daily mean MW}). Keys: demand, generation, net_imports, gen.<fuel>."""
    d = (payload or {}).get("PowerCacheData")
    if not isinstance(d, dict) or not all(k in d for k in ("asOfDate", "generation", "netInterchange",
                                                            "systemDemand", "generationByType")):
        raise FormatDrift("SaskPower: no PowerCacheData with asOfDate / generation / netInterchange / systemDemand")
    end = _date(d["asOfDate"]) + timedelta(seconds=1)              # 23:59:59 CST -> the next 00:00 CST
    vals = {"demand": float(d["systemDemand"]), "generation": float(d["generation"]),
            "net_imports": float(d["netInterchange"])}
    for g in d["generationByType"]:
        t = str(g.get("type", "")).strip().lower()
        if t not in TYPES:
            raise FormatDrift(f"SaskPower: unknown generation type {g.get('type')!r} (map it in TYPES)")
        key = f"gen.{TYPES[t]}"
        vals[key] = vals.get(key, 0.0) + float(g["totalGenerationForType"])
    if abs(vals["generation"] + vals["net_imports"] - vals["demand"]) > TOLERANCE_MW:
        raise FormatDrift(f"SaskPower: generation {vals['generation']} + net interchange {vals['net_imports']} != "
                          f"demand {vals['demand']}")
    for k, v in vals.items():
        if not -MAX_MW <= v <= MAX_MW:
            raise FormatDrift(f"SaskPower: {k} {v} MW")
    return end - timedelta(hours=24), vals


class SaskPower(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "held": "the day's 24 h mean, held across its hours (SaskPower publishes only that)",
              "lag": "~1.5 days"}
        keys = ["demand", "generation", "net_imports"] + [f"gen.{f}" for f in ("wind", "hydro", "solar", "coal", "gas",
                                                                               "other")]
        names = {"demand": "system demand", "generation": "generation", "net_imports": "net imports (+ import)"}
        label = {k: f"SaskPower {names.get(k, k.replace('gen.', '') + ' generation')}" for k in keys}
        best = {**ex, "quality": "estimate", "held": "the day's mean held across its hours, then the latest day's mean "
                "held forward to the current hour (at most 72 h past its day)"}
        return ([SeriesMeta(f"saskpower.{k}", self.id, f"{label[k]} (daily mean held hourly)", "MW", "1h", GROUP,
                            extra=ex) for k in keys]
                + [SeriesMeta(f"saskpower.{k}_best", self.id, f"{label[k]}, best (daily mean, held forward to now; est.)",
                              "MW", "1h", GROUP, extra=best) for k in keys])

    def fetch(self, ctx: Context) -> FetchResult:
        start, vals = parse(ctx.get(URL, timeout=60).json())
        res = FetchResult(meta=self.declared_series())
        for h in range(24):
            ts = start + timedelta(hours=h)
            for k, v in vals.items():
                res.obs.append(Obs(f"saskpower.{k}", ts, v, ts))
        now = ctx.now.replace(minute=0, second=0, microsecond=0)
        end = min(now, start + timedelta(hours=24) + HOLD_MAX)
        ts = start
        while ts <= end:
            for k, v in vals.items():
                res.obs.append(Obs(f"saskpower.{k}_best", ts, v, ts))
            ts += timedelta(hours=1)
        return res
