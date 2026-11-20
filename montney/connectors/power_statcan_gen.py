"""Saskatchewan and Manitoba generation: total and by type, monthly (L-078).

Neither SaskPower nor Manitoba Hydro publishes a live (or hourly/daily) generation feed. The best public source is
**Statistics Canada table 25-10-0015-01**, "Electric power generation, monthly generation by type of electricity",
read through the keyless WDS API (``getDataFromCubePidCoordAndLatestNPeriods``). It's monthly, released about two
months after the month (Aug 2026 came out 2026-10-30), so it's labelled as monthly with its month, never as live.

Class of producer: "Total all classes" (utilities + industry). Types read (StatCan's own categories):

  total    Total all types of electricity generation
  fossil   Total electricity production from non-renewable combustible fuels (coal + natural gas + oil: StatCan
           suppresses the steam / combustion-turbine / internal-combustion split for SK and MB, so coal vs gas can't
           be told apart from this table)
  biomass  Total electricity production from biomass
  hydro    Hydraulic turbine
  wind     Wind power turbine
  solar    Solar
  (no nuclear or tidal: neither province has any, and WDS has no such series for them: it answers FAILED)
  other    Other types of electricity generation

The parts add up to the total exactly (SK Aug 2026: 387,351 + 1,704,059 + 5,455 + 133,203 + 6,365 + 9,649 =
2,246,082 MWh). A month where they don't (a suppressed or revised value) gets a warning.

Stored as the month's **mean MW** (MWh / hours in the month), so the boxes compare with Alberta's and Ontario's MW:

  statcan.gen.<prov>.<type>    MW (monthly mean), freq M, ts = first of the month, prov in sk, mb

Fail closed: a WDS error, or no data points at all, raises FormatDrift (nothing stored). A suppressed value
("..", value null) is left out, never stored as 0.
"""

from __future__ import annotations

import calendar
from datetime import datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods"
TABLE = 25100015
PROVINCES = {"sk": (9, "Saskatchewan"), "mb": (8, "Manitoba")}
TYPES = {"total": 1, "fossil": 12, "biomass": 13, "hydro": 2, "wind": 8, "solar": 9, "other": 10}
LABELS = {"total": "total", "fossil": "fossil (coal + gas + oil)", "biomass": "biomass", "hydro": "hydro",
          "wind": "wind", "solar": "solar", "other": "other"}
GROUP = "StatCan / Generation by type (monthly)"
TOLERANCE_MWH = 1.0


def mean_mw(mwh: float, month: datetime) -> float:
    return mwh / (calendar.monthrange(month.year, month.month)[1] * 24)


def parse(payload: list, first_year: int = 2020) -> tuple[dict[tuple[str, str], dict[datetime, float]], list[str]]:
    """WDS response -> {(prov, type): {month: MWh}}, warnings."""
    geo = {g: k for k, (g, _) in PROVINCES.items()}
    typ = {t: k for k, t in TYPES.items()}
    out: dict[tuple[str, str], dict[datetime, float]] = {}
    warnings = []
    for item in payload:
        if item.get("status") != "SUCCESS":
            raise FormatDrift(f"StatCan WDS: {item.get('status')} {item.get('object')}")
        o = item["object"]
        g, cls, t = (int(x) for x in o["coordinate"].split(".")[:3])
        if cls != 1 or g not in geo or t not in typ:
            raise FormatDrift(f"StatCan WDS: unexpected coordinate {o['coordinate']}")
        pts = {}
        for p in o.get("vectorDataPoint", []):
            if p.get("value") is None:                     # suppressed (".."): never a 0
                continue
            if p.get("scalarFactorCode", 0) != 0:
                raise FormatDrift(f"StatCan WDS: scalar factor {p['scalarFactorCode']} (expected units, MWh)")
            m = datetime.strptime(p["refPer"], "%Y-%m-%d")
            if m.year >= first_year:
                pts[m] = float(p["value"])
        if pts:
            out[(geo[g], typ[t])] = pts
    if not out:
        raise FormatDrift("StatCan WDS: no data points (NO-RESULT)")
    for prov in PROVINCES:
        total = out.get((prov, "total"), {})
        for m, v in sorted(total.items()):
            parts = [out[(prov, t)].get(m) for t in TYPES if t != "total" and (prov, t) in out]
            if any(x is None for x in parts) or abs(sum(parts) - v) > TOLERANCE_MWH:
                warnings.append(f"StatCan {prov} {m:%Y-%m}: types don't add up to the total "
                                f"({sum(x or 0 for x in parts):,.0f} vs {v:,.0f} MWh)")
    return out, warnings


class StatcanGeneration(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(f"statcan.gen.{p}.{t}", self.id, f"{name} {LABELS[t]} generation (StatCan, monthly mean)",
                           "MW", "M", GROUP, extra={"table": "25-10-0015-01", "stored": "MWh / hours in the month"})
                for p, (_, name) in PROVINCES.items() for t in TYPES]

    def fetch(self, ctx: Context) -> FetchResult:
        first = int(self.spec.get("params", {}).get("from_year", 2020))
        n = (ctx.now.year - first) * 12 + ctx.now.month + 1
        body = [{"productId": TABLE, "coordinate": f"{g}.1.{t}.0.0.0.0.0.0.0", "latestN": n}
                for g, _ in PROVINCES.values() for t in TYPES.values()]
        data, warnings = parse(ctx.post(URL, json=body).json(), first)
        res = FetchResult(meta=self.declared_series(), warnings=warnings)
        for (prov, t), pts in sorted(data.items()):
            for m, mwh in sorted(pts.items()):
                res.obs.append(Obs(f"statcan.gen.{prov}.{t}", m, mean_mw(mwh, m), m))
        return res
