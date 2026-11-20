"""Installed generating capacity by type for AB, BC, SK, MB, ON and QC, annual, and the four Canadian regions' sums
(PLAN 9, region page `capacity_by_fuel`).

Source (keyless): **Statistics Canada table 25-10-0022-01**, "Installed plants, annual generating capacity by type of
electricity generation", read through the WDS API (``getDataFromCubePidCoordAndLatestNPeriods``, one POST of 72
coordinates, ~50 KB). Class of producer "Total all classes" (utilities + industry). Values are kilowatts, stored as MW.
Annual, released ~10 months after the year (2025 came out 2026-10-19): ts = 1 January of the year (StatCan's own
reference period), so the newest value is ~1.8 years old when it lands and ~2.8 years old just before the next.

StatCan splits by **type of generation** (turbine), not fuel. What maps to our FUELS keys does; the combustion
plants come only by prime mover, which mixes fuels (coal, gas, oil, biomass), so they keep StatCan's split, with labels
that say so:

  hydro           Hydraulic turbine (pumped storage, e.g. Ontario's Beck PGS, is inside it)
  wind            Wind power turbine
  solar           Solar power
  geothermal      Geothermal
  nuclear         Nuclear steam turbine
  other           Tidal power turbine + Other non-combustibles
  combustion      Total thermal - nuclear = the three below (coal + gas + oil + biomass)
  thermal_steam   Conventional steam turbine (coal, gas, oil and biomass boilers)
  thermal_ct      Combustion turbine (almost all gas: simple and combined cycle)
  thermal_ic      Internal combustion turbine (reciprocating engines: diesel, gas)
  total           Total installed capacity
  (no battery storage: the table has no storage type)

A type a province doesn't have has no WDS vector (it answers FAILED): stored as 0, a real "none installed", and the
identity check below catches a vector that disappears for any other reason. A suppressed point ("..", value null) is
left out, never stored as 0.

Series (MW, freq A, ts = 1 Jan of the year):
  cap.<prov>.<key>     prov in ab bc sk mb on qc
  cap.wcan.<key>       AB + BC + SK (all of BC: StatCan has no split for the Lower Mainland that the map clips to pnw)
  cap.sont.<key>       ON (all of Ontario: StatCan has no split for the north the map clips to mbnwo)
  cap.mbnwo.<key>      MB (Manitoba only: northwestern Ontario's plants are inside cap.sont / cap.on)
  (qc: cap.qc.<key> is the province and the region at once)
A region sum is written for a year only when no member's value is suppressed.

Fail closed (FormatDrift, nothing stored): a WDS status other than SUCCESS or FAILED; no data at all; a scalar factor
other than units; a negative value; a province total outside 500 MW - 100 GW (catches a kW -> MW change); the newest
year's parts not adding to its total (within 0.5 %). An older year that doesn't add up is a warning.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods"
TABLE = 25100022
PROVINCES = {"qc": (6, "Québec"), "on": (7, "Ontario"), "mb": (8, "Manitoba"), "sk": (9, "Saskatchewan"),
             "ab": (10, "Alberta"), "bc": (11, "British Columbia")}
# StatCan type member ids (dimension 3)
TYPES = {"total": 1, "hydro": 2, "wind": 3, "tidal": 4, "solar": 5, "thermal": 6, "thermal_steam": 7, "nuclear": 8,
         "thermal_ct": 9, "thermal_ic": 10, "other_noncomb": 11, "geothermal": 12}
TOTAL_PARTS = ("hydro", "wind", "tidal", "solar", "geothermal", "other_noncomb", "thermal")
THERMAL_PARTS = ("thermal_steam", "nuclear", "thermal_ct", "thermal_ic")
# stored key -> the StatCan types it sums
STORED = {"total": ("total",), "hydro": ("hydro",), "wind": ("wind",), "solar": ("solar",),
          "geothermal": ("geothermal",), "nuclear": ("nuclear",), "other": ("tidal", "other_noncomb"),
          "combustion": ("thermal_steam", "thermal_ct", "thermal_ic"), "thermal_steam": ("thermal_steam",),
          "thermal_ct": ("thermal_ct",), "thermal_ic": ("thermal_ic",)}
LABELS = {"total": "total", "hydro": "hydro", "wind": "wind", "solar": "solar", "geothermal": "geothermal",
          "nuclear": "nuclear", "other": "other (tidal + other non-combustible)",
          "combustion": "combustion (coal + gas + oil + biomass)",
          "thermal_steam": "conventional steam (coal, gas, oil, biomass)",
          "thermal_ct": "combustion turbine (mostly gas)", "thermal_ic": "internal combustion (diesel, gas engines)"}
REGIONS = {"wcan": (("ab", "bc", "sk"), "AB · BC · SK"), "sont": (("on",), "S. Ontario (all of Ontario)"),
           "mbnwo": (("mb",), "Manitoba (MB only)")}
GROUP = "StatCan / Installed capacity by type (annual)"
MIN_TOTAL_MW, MAX_TOTAL_MW = 500, 100_000
TOLERANCE = 0.005


def parse(payload: list) -> tuple[dict[tuple[str, str], dict[datetime, float | None]], set[tuple[str, str]]]:
    """WDS response -> ({(prov, statcan type): {year: MW or None (suppressed)}}, {(prov, type) with no vector})."""
    geo = {g: k for k, (g, _) in PROVINCES.items()}
    typ = {t: k for k, t in TYPES.items()}
    out: dict[tuple[str, str], dict[datetime, float | None]] = {}
    absent: set[tuple[str, str]] = set()
    for item in payload:
        o = item.get("object") or {}
        try:
            g, cls, t = (int(x) for x in str(o.get("coordinate", "")).split(".")[:3])
        except ValueError:
            raise FormatDrift(f"StatCan WDS: bad coordinate {o.get('coordinate')!r}") from None
        if cls != 1 or g not in geo or t not in typ:
            raise FormatDrift(f"StatCan WDS: unexpected coordinate {o['coordinate']}")
        key = (geo[g], typ[t])
        if item.get("status") == "FAILED":          # no such vector: the province has none of this type
            absent.add(key)
            continue
        if item.get("status") != "SUCCESS":
            raise FormatDrift(f"StatCan WDS: {item.get('status')} {o}")
        pts: dict[datetime, float | None] = {}
        for p in o.get("vectorDataPoint", []):
            if p.get("scalarFactorCode", 0) != 0:
                raise FormatDrift(f"StatCan WDS: scalar factor {p['scalarFactorCode']} (expected units, kW)")
            year = datetime.strptime(p["refPer"], "%Y-%m-%d")
            v = p.get("value")
            if v is not None and float(v) < 0:
                raise FormatDrift(f"StatCan WDS {o['coordinate']} {year:%Y}: negative capacity {v}")
            pts[year] = None if v is None else float(v) / 1000.0          # kW -> MW
        out[key] = pts
    if not out:
        raise FormatDrift("StatCan WDS: no data points (NO-RESULT)")
    return out, absent


def province_values(raw, absent) -> tuple[dict[tuple[str, str], dict[datetime, float]], list[str]]:
    """{(prov, stored key): {year: MW}} (suppressed points left out), warnings. Raises FormatDrift."""
    years = sorted({y for pts in raw.values() for y in pts})
    if not years:
        raise FormatDrift("StatCan WDS: no years")
    warnings: list[str] = []

    def get(prov, t, y):                      # MW, 0.0 for no vector, None for suppressed / missing
        if (prov, t) in absent:
            return 0.0
        return raw.get((prov, t), {}).get(y)

    out: dict[tuple[str, str], dict[datetime, float]] = defaultdict(dict)
    for prov in PROVINCES:
        for y in years:
            total = get(prov, "total", y)
            if total is None:
                continue
            if not MIN_TOTAL_MW <= total <= MAX_TOTAL_MW:
                raise FormatDrift(f"StatCan {prov} {y:%Y}: total {total:,.0f} MW out of range (unit change?)")
            for whole, parts in (("total", TOTAL_PARTS), ("thermal", THERMAL_PARTS)):
                w = get(prov, whole, y)
                s = sum(get(prov, t, y) or 0.0 for t in parts)       # a suppressed part reads 0 here
                if w is not None and abs(s - w) > max(1.0, TOLERANCE * w):
                    msg = f"StatCan {prov} {y:%Y}: {whole} parts add to {s:,.0f} MW, not {w:,.0f}"
                    if y == years[-1]:
                        raise FormatDrift(msg)
                    warnings.append(msg)
            for key, types in STORED.items():
                vals = [get(prov, t, y) for t in types]
                if all(v is not None for v in vals):
                    out[(prov, key)][y] = round(sum(vals), 3)
    return out, warnings


class CapacityCA(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "daily", "table": "25-10-0022-01", "lag": "~10 months after the year (annual)",
              "stored": "kW / 1000", "class": "total all classes of electricity producer"}
        out = [SeriesMeta(f"cap.{p}.{k}", self.id, f"{name} installed {LABELS[k]} capacity (StatCan, annual)", "MW",
                          "A", GROUP, extra={**ex, "region": "qc" if p == "qc" else
                                             next(r for r, (m, _) in REGIONS.items() if p in m)})
               for p, (_, name) in PROVINCES.items() for k in STORED]
        out += [SeriesMeta(f"cap.{r}.{k}", self.id, f"{name} installed {LABELS[k]} capacity (StatCan, annual)", "MW",
                           "A", GROUP, extra={**ex, "region": r, "members": "+".join(m).upper()})
                for r, (m, name) in REGIONS.items() for k in STORED]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        first = int(self.spec.get("params", {}).get("from_year", 2015))
        n = max(1, ctx.now.year - first + 1)
        body = [{"productId": TABLE, "coordinate": f"{g}.1.{t}.0.0.0.0.0.0.0", "latestN": n}
                for g, _ in PROVINCES.values() for t in TYPES.values()]
        raw, absent = parse(ctx.post(URL, json=body, timeout=60).json())
        vals, warnings = province_values(raw, absent)
        res = FetchResult(meta=self.declared_series(), warnings=warnings)
        for (prov, key), pts in sorted(vals.items()):
            for y, v in sorted(pts.items()):
                if y.year >= first:
                    res.obs.append(Obs(f"cap.{prov}.{key}", y, v, y))
        for r, (members, _) in REGIONS.items():
            for key in STORED:
                years = set.intersection(*(set(vals.get((p, key), {})) for p in members))
                for y in sorted(years):
                    if y.year >= first:
                        res.obs.append(Obs(f"cap.{r}.{key}", y, round(sum(vals[(p, key)][y] for p in members), 3), y))
        return res
