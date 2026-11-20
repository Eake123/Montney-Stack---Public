"""Provincial gas, monthly: StatCan table 25-10-0086 (Natural gas supply and disposition, monthly), by vector, ONE
request (WDS getDataFromVectorsAndLatestNPeriods, ~5 KB JSON). Ontario + Québec since L-091; Alberta, BC, Saskatchewan
and Manitoba since L-106 (the wcan / mbnwo regions: Saskatchewan's production, demand and storage aren't public daily).

- `statcan.<pv>.production`   Marketable production (member 8; not gross withdrawals, which are raw gas before
                              processing shrink), monthly mean MMcf/d. AB, BC, SK (MB has none).
- `statcan.<pv>.deliveries`   Total deliveries to consumers (residential + commercial + industrial, incl. industrial
                              off transmission), monthly mean MMcf/d (10³m³ × 35.3147 ÷ 1000 ÷ days). Ontario's
                              daily demand can't be built from public daily data (Dawn's Gas Day Summary is behind a
                              reCAPTCHA), so this is Ontario's demand headline, ~2 months behind.
- `statcan.<pv>.deliveries.residential|commercial|industrial`  The split of deliveries (SK, MB, QC, ON), monthly mean
                              MMcf/d: gas_region_demand's inputs. Industrial includes gas burned for power generation.
- `statcan.<pv>.storage`      Closing inventory, storage (Bcf): Ontario = Dawn, Tecumseh and the other Ontario pools;
                              Québec = Pointe-du-Lac, Saint-Flavien; AB = the AECO hub fields and the others; BC =
                              Aitken Creek; SK = SaskEnergy's.
- `statcan.<pv>.storage_net_withdrawal`  Withdrawals − injections, monthly mean MMcf/d (+ withdrawal).
ts = the month's first day; as_of = StatCan's release time (a report vintage). Fails loudly if a vector is missing.
Manitoba reports deliveries only ("..": not available, for production and storage), so it has no other series.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, local_to_utc
from montney.store import Obs, SeriesMeta

WDS = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods"
GROUP = "StatCan gas supply and disposition (monthly)"
M3K_TO_MMCF = 35.3147 / 1000.0            # 10³m³ -> MMcf
# (province, measure) -> vector (table 25-10-0086, cubic metres, scalar thousands). Each measure's ids run in geography
# order (MB, SK, AB, BC: e.g. deliveries 1864626290..293), next to Ontario's and Québec's.
VECTORS = {("on", "deliveries"): 1864626289, ("qc", "deliveries"): 1864626288,
           ("on", "storage"): 1864626101, ("qc", "storage"): 1864626100,
           ("on", "withdrawals"): 1864626143, ("qc", "withdrawals"): 1864626142,
           ("on", "injections"): 1864626129, ("qc", "injections"): 1864626128,
           # L-106 (wcan, mbnwo)
           ("ab", "production"): 1864626186, ("bc", "production"): 1864626187, ("sk", "production"): 1864626185,
           ("ab", "deliveries"): 1864626292, ("bc", "deliveries"): 1864626293, ("sk", "deliveries"): 1864626291,
           ("mb", "deliveries"): 1864626290,
           ("ab", "storage"): 1864626104, ("bc", "storage"): 1864626105, ("sk", "storage"): 1864626103,
           ("ab", "withdrawals"): 1864626146, ("bc", "withdrawals"): 1864626147, ("sk", "withdrawals"): 1864626145,
           ("ab", "injections"): 1864626132, ("bc", "injections"): 1864626133, ("sk", "injections"): 1864626131,
           # the end-use split for gas_region_demand (Gas, 2026-11-13): residential, commercial and industrial
           # consumption (same table and request; ids run QC, ON, MB, SK)
           ("qc", "residential"): 1864626302, ("on", "residential"): 1864626303, ("mb", "residential"): 1864626304,
           ("sk", "residential"): 1864626305,
           ("qc", "commercial"): 1864626316, ("on", "commercial"): 1864626317, ("mb", "commercial"): 1864626318,
           ("sk", "commercial"): 1864626319,
           ("qc", "industrial"): 1864626330, ("on", "industrial"): 1864626331, ("mb", "industrial"): 1864626332,
           ("sk", "industrial"): 1864626333}
SECTORS = {"residential": "residential consumption", "commercial": "commercial consumption",
           "industrial": "industrial consumption (incl. gas for power generation)"}
NAME = {"on": "Ontario", "qc": "Québec", "ab": "Alberta", "bc": "British Columbia", "sk": "Saskatchewan",
        "mb": "Manitoba"}


def released(rel: str | None, fallback: datetime) -> datetime:
    """StatCan's releaseTime (Eastern, e.g. 2026-10-23T08:30) -> naive UTC."""
    from zoneinfo import ZoneInfo
    return local_to_utc(datetime.fromisoformat(rel), ZoneInfo("America/Toronto")) if rel else fallback


def per_day(m3k: float, month: datetime) -> float:
    return m3k * M3K_TO_MMCF / monthrange(month.year, month.month)[1]


class GasStatcanProvinces(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for pv, nm in NAME.items():
            has = {m for (p, m) in VECTORS if p == pv}
            if "production" in has:
                out.append(SeriesMeta(f"statcan.{pv}.production", self.id, f"{nm} marketable gas production (StatCan, "
                                      "monthly mean)", "MMcf/d", "M", GROUP))
            out.append(SeriesMeta(f"statcan.{pv}.deliveries", self.id, f"{nm} gas demand: StatCan deliveries to "
                                  "consumers (monthly mean)", "MMcf/d", "M", GROUP))
            out += [SeriesMeta(f"statcan.{pv}.deliveries.{m}", self.id, f"{nm} gas {SECTORS[m]} (StatCan, monthly "
                               "mean)", "MMcf/d", "M", GROUP) for m in SECTORS if m in has]
            if "storage" in has:
                out += [SeriesMeta(f"statcan.{pv}.storage", self.id, f"{nm} gas storage inventory (StatCan, month end)",
                                   "Bcf", "M", GROUP),
                        SeriesMeta(f"statcan.{pv}.storage_net_withdrawal", self.id, f"{nm} storage net withdrawal "
                                   "(StatCan, monthly mean; + withdrawal)", "MMcf/d", "M", GROUP)]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        n = int(p.get("months", 24))
        body = [{"vectorId": v, "latestN": n} for v in VECTORS.values()]
        try:
            data = {int(x["object"]["vectorId"]): x["object"]["vectorDataPoint"]
                    for x in ctx.post(p.get("url", WDS), json=body, timeout=60).json() if x.get("status") == "SUCCESS"}
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"StatCan WDS: not the expected JSON ({e})") from e
        missing = [k for k, v in VECTORS.items() if v not in data]
        if missing:
            raise FormatDrift(f"StatCan WDS: no data for {missing}")
        pts = {k: {datetime.fromisoformat(d["refPer"]): (float(d["value"]), d.get("releaseTime"))
                   for d in data[v] if d.get("value") is not None} for k, v in VECTORS.items()}
        res = FetchResult(meta=self.declared_series())
        for (pv, m), got in pts.items():
            for ts, (val, rel) in got.items():
                if m in ("production", "deliveries"):
                    res.obs.append(Obs(f"statcan.{pv}.{m}", ts, per_day(val, ts), released(rel, ctx.now)))
                elif m in SECTORS:
                    res.obs.append(Obs(f"statcan.{pv}.deliveries.{m}", ts, per_day(val, ts), released(rel, ctx.now)))
                elif m == "storage":
                    res.obs.append(Obs(f"statcan.{pv}.storage", ts, val * M3K_TO_MMCF / 1000.0, released(rel, ctx.now)))
        for pv in NAME:
            wd, inj = pts.get((pv, "withdrawals"), {}), pts.get((pv, "injections"), {})
            for ts in sorted(set(wd) & set(inj)):
                res.obs.append(Obs(f"statcan.{pv}.storage_net_withdrawal", ts, per_day(wd[ts][0] - inj[ts][0], ts),
                                   released(wd[ts][1], ctx.now)))
        return res


# ------------------------------------------------------------------ crude oil by province (L-106 8b)
# StatCan table 25-10-0063-01 "Supply and disposition of crude oil and equivalent", monthly, cubic metres (scalar 0).
# ONE WDS request for all of it. "Crude oil production" = net field production (light/medium, heavy, non-upgraded crude
# bitumen) + synthetic crude: Alberta's oil sands are in it (Aug 2026: bitumen 11.24 + synthetic 6.67 + conventional
# 2.83 = 20.75 million m³). "Equivalent products" = condensate + pentanes plus, kept as its own series (BC's Montney
# liquids are mostly here: 136 kbbl/d vs 11 kbbl/d of crude, Aug 2026). Manitoba has no equivalent-products series.
# The four western provinces don't sum to Canada: Newfoundland and Labrador's offshore is the gap.
OIL_VECTORS = {("ab", "crude_production"): 107757710, ("sk", "crude_production"): 107757636,
               ("bc", "crude_production"): 107757784, ("mb", "crude_production"): 107757562,
               ("ca", "crude_production"): 107757044,
               ("ab", "condensate_production"): 107757728, ("sk", "condensate_production"): 107757654,
               ("bc", "condensate_production"): 107757802, ("ca", "condensate_production"): 107757062}
OIL_NAME = {"ab": "Alberta", "sk": "Saskatchewan", "bc": "British Columbia", "mb": "Manitoba", "ca": "Canada"}
OIL_LABEL = {"crude_production": "crude oil production (incl. bitumen and synthetic)",
             "condensate_production": "condensate + pentanes plus production"}
M3_TO_BBL = 6.28981
OIL_GROUP = "StatCan crude oil supply (monthly)"


class OilStatcanProvinces(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(f"statcan.{pv}.{m}", self.id, f"{OIL_NAME[pv]} {OIL_LABEL[m]} (StatCan, monthly mean)",
                           "bbl/d", "M", OIL_GROUP) for pv, m in OIL_VECTORS]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        body = [{"vectorId": v, "latestN": int(p.get("months", 24))} for v in OIL_VECTORS.values()]
        try:
            data = {int(x["object"]["vectorId"]): x["object"]["vectorDataPoint"]
                    for x in ctx.post(p.get("url", WDS), json=body, timeout=60).json() if x.get("status") == "SUCCESS"}
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"StatCan WDS: not the expected JSON ({e})") from e
        missing = [k for k, v in OIL_VECTORS.items() if v not in data]
        if missing:
            raise FormatDrift(f"StatCan WDS (crude oil): no data for {missing}")
        res = FetchResult(meta=self.declared_series())
        for (pv, m), v in OIL_VECTORS.items():
            for d in data[v]:
                if d.get("value") is None:          # suppressed ('x'): skipped, never a 0
                    continue
                ts = datetime.fromisoformat(d["refPer"])
                bbl_d = float(d["value"]) * M3_TO_BBL / monthrange(ts.year, ts.month)[1]
                res.obs.append(Obs(f"statcan.{pv}.{m}", ts, bbl_d, released(d.get("releaseTime"), ctx.now)))
        return res
