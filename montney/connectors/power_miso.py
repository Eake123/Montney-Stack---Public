"""MISO headline: current demand and the fuel mix (L-096, Erik: "just the big headline numbers with the energy mix and
stuff. Dont make a lot of requests").

Source: MISO's public real-time JSON (keyless), https://public-api.misoenergy.org (the old DataBrokerServices.asmx
answers "no data" since the Dec 2025 move). Two small responses per run, interval 2 h = 1 request/h:

  /api/FuelMix    the latest 5-min interval: Coal, Natural Gas, Nuclear, Wind, Solar, Battery Storage, Other, Imports
                  (``ACT``, MW) and ``TotalMW``. TotalMW is every category but Battery Storage (2026-11-03:
                  77,005 + 187 = 77,192), so the box's "% of supply" uses the published total as is.
  /api/Snapshot   Current Demand (MW), Forecasted Peak Demand (MW, for the day), Marginal Energy Cost ($/MWh, the
                  system marginal energy component), and scheduled interchange (not read: the mix has Imports).

MISO publishes in EST all year (market time), like IESO: one fixed ``Etc/GMT+5`` (``MISO_TZ``). ``ts`` is the interval
stamp in naive UTC; the peak forecast is a daily value (``ts`` = the EST date). Live only (``mode: live``): Erik asked
for the headline, not a history, and MISO's first response has no back-fill.

Series (MW unless noted): miso.demand, miso.demand.peak_forecast (freq D), miso.price.mec ($/MWh),
miso.gen.<fuel> for coal, gas, nuclear, wind, solar, storage (signed), other, imports, and miso.gen.total (TotalMW).

Fail closed (FormatDrift, nothing stored): an unknown fuel category (map it first), a missing Snapshot item, or a
value that isn't a number. MISO renaming a Snapshot title fails the run rather than mislabelling a number.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://public-api.misoenergy.org/api"
MISO_TZ = ZoneInfo("Etc/GMT+5")          # EST all year (MISO market time); "Etc" sign convention: UTC-5
FUELS = {"coal": "Coal", "gas": "Natural Gas", "nuclear": "Nuclear", "wind": "Wind", "solar": "Solar",
         "storage": "Battery Storage", "other": "Other", "imports": "Imports"}
SNAPSHOT = {"miso.demand": "Current Demand (MW)", "miso.demand.peak_forecast": "Forecasted Peak Demand (MW)",
            "miso.price.mec": "Marginal Energy Cost ($/MWh)"}
GROUP = "MISO / demand and fuel mix"
MAX_MW = 200_000                          # MISO's record load is ~127 GW


def _num(s) -> float:
    try:
        return float(re.sub(r"[,$\s]", "", str(s)))
    except ValueError as e:
        raise FormatDrift(f"MISO: {s!r} is not a number") from e


def _utc(local: datetime) -> datetime:
    return local.replace(tzinfo=MISO_TZ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_fuelmix(j: dict) -> tuple[datetime, dict[str, float]]:
    rows = (j.get("Fuel") or {}).get("Type")
    if not rows or "TotalMW" not in j:
        raise FormatDrift("MISO FuelMix: no Fuel.Type rows or TotalMW")
    by_name = {v: k for k, v in FUELS.items()}
    out, stamps = {}, set()
    for r in rows:
        cat = str(r.get("CATEGORY", "")).strip()
        if cat not in by_name:
            raise FormatDrift(f"MISO FuelMix: unknown category {cat!r} (map it in power_miso.FUELS first)")
        out[by_name[cat]] = _num(r["ACT"])
        stamps.add(datetime.strptime(r["INTERVALEST"].strip(), "%Y-%m-%d %I:%M:%S %p"))
    if len(stamps) != 1:
        raise FormatDrift(f"MISO FuelMix: rows from {len(stamps)} intervals")
    out["total"] = _num(j["TotalMW"])
    for k, v in out.items():
        if not -MAX_MW <= v <= MAX_MW:
            raise FormatDrift(f"MISO FuelMix: {k} {v:,.0f} MW outside +-{MAX_MW:,}")
    return _utc(stamps.pop()), out


def parse_snapshot(items: list) -> dict[str, tuple[datetime, float]]:
    by_title = {str(x.get("t", "")).strip(): x for x in items or []}
    out = {}
    for sid, title in SNAPSHOT.items():
        x = by_title.get(title)
        if x is None:
            raise FormatDrift(f"MISO Snapshot: no {title!r} (titles: {sorted(by_title)})")
        local = datetime.strptime(re.sub(r"\s+E[SD]T$", "", x["d"].strip()), "%m/%d/%Y %I:%M:%S %p")
        ts = datetime(local.year, local.month, local.day) if sid.endswith("peak_forecast") else _utc(local)
        out[sid] = (ts, _num(x["v"]))
    if not 0 < out["miso.demand"][1] <= MAX_MW:
        raise FormatDrift(f"MISO Snapshot: demand {out['miso.demand'][1]:,.0f} MW")
    return out


class MisoHeadline(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "live", "tz": "EST (MISO market time)"}     # stated: the catalog is seeded from these (#292)
        names = {"coal": "coal", "gas": "natural gas", "nuclear": "nuclear", "wind": "wind", "solar": "solar",
                 "storage": "battery storage (+ discharge, - charge)", "other": "other", "imports": "imports",
                 "total": "total supply (published TotalMW: all categories but battery storage)"}
        return ([SeriesMeta(f"miso.gen.{k}", self.id, f"MISO {n}, latest 5-min", "MW", "1h", GROUP, extra=ex)
                 for k, n in names.items()]
                + [SeriesMeta("miso.demand", self.id, "MISO current demand, latest 5-min", "MW", "1h", GROUP, extra=ex),
                   SeriesMeta("miso.demand.peak_forecast", self.id, "MISO forecast peak demand for the day", "MW", "D",
                              GROUP, extra=ex),
                   SeriesMeta("miso.price.mec", self.id, "MISO marginal energy cost (system), latest 5-min", "$/MWh",
                              "1h", GROUP, extra=ex)])

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        ts, mix = parse_fuelmix(ctx.get(f"{BASE}/FuelMix").json())
        for k, v in mix.items():
            res.obs.append(Obs(f"miso.gen.{k}", ts, v, ts))
        for sid, (t, v) in parse_snapshot(ctx.get(f"{BASE}/Snapshot").json()).items():
            res.obs.append(Obs(sid, t, v, t))
        return res
