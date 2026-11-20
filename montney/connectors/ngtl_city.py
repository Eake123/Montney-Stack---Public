"""City gas demand: Calgary, Edmonton, Lethbridge, Red Deer (utility) and Fort Saskatchewan (industrial) (PLAN 4b item 6).

**Where city gas is:** in TC's NGTL Station % Utilization *Delivery* files, the utility load sits on unnamed ATCO Gas supply
points "AG SP dd-nnn APN/APS" (the ATCO Pipelines → ATCO Gas handover meters). `dd` is an ATCO Gas district. Mapping
(inferred from five independent clues; there's no published table; docs in the PR):
  44 → Calgary (WGAT), 21 → Edmonton (EDM), 51 → Lethbridge (SLAT), 31 → Red Deer (ALEG)
The checks:
- the segment and gate names on TC's segment map;
- volume per head;
- each district fits its city's heating-degree days best (R² 0.99);
- no AG SP point appears in ST102 or Petrinex.

Districts 22 (Edmonton suburbs incl. Fort Sask town) and 40-43/45 (Calgary-area towns) are left out.

**Fort Saskatchewan (industrial)** = the named Heartland plants (FORT_SASK list; ~464 MMcf/d in Sep 2026).

**Units:** delivery files are **GJ**/month. MMcf/d = GJ/d ÷ HV (GJ per 10³m³ = MJ/m³) × 0.0353147. HV is TC's published area value
from the DOP `/heatValues` (WGAT 38.5 for Calgary and Lethbridge, NEDA 39.5 for Edmonton, Red Deer and Fort Sask).
- It's guarded to 37.5-39.8 MJ/m³ (L-032).
- When it's missing or out of range, the L-031 fallback 39.0 is used, and a warning flags it.

**Today's estimate:**
- **Utility cities: demand = a + b × HDD.** HDD is base 18 °C, today's mean temperature (Open-Meteo forecast, America/Edmonton day).
  - a, b are refit on every run: monthly TJ/d against monthly mean HDD (Open-Meteo archive) over the delivery files' months (2+ years).
  - The fit must have ≥ 12 months and R² ≥ 0.9, else NO-RESULT.
  - Jan 2024-Sep 2026: Calgary a 49.7, b 16.5 TJ/d per HDD, R² 0.991. Monthly residual P90 18 %; 2026 out of sample 18 %.
- **Fort Sask industrial: the latest TC month's level, carried forward.** Weather barely matters (R² 0.33). On a typical day
  the latest delivery month is M-2 (e.g. September on Nov 1), so the band is the M-2 error: P90 17.5 % (M-1 would be 11.4 %).
  - It isn't scaled by total intra-AB deliveries: they swing ~30 % with city heating load, which industrial load doesn't
    share. Scaling had put Fort Sask at ~600 MMcf/d on Oct 30, against 464 in Sep.
- **Bands:** Calgary/Edmonton ±20 %, Lethbridge/Red Deer ±15 %, Fort Sask ±18 %. These are monthly fits, so a single day can swing more.

Series (MMcf/d):
  ngtl.city.<city>.monthly        M, the district's (or plant list's) actual deliveries (report vintage = the file's As of)
  ngtl.city.<city>.demand_est     D, today's estimate (and yesterday's), as_of = fetch day

**AB intra demand: population vs industry** (PLAN step 5 item 2). The basis is NGTL intra-AB deliveries
(`ngtl.gdsr.intraprovincial.flow`, daily, measured).
- **Population** (`demand.ab.population_est`) = residential/commercial gas: every ATCO Gas supply point ("AG SP", all
  districts, ~1,000 points) plus the named town/county/co-op utility gates (MUNI_RE). Today's value = a + b × HDD, fitted
  like the cities, on the mean of Calgary's and Edmonton's HDD (AB's two big load centres).
- **Industry** (`demand.ab.industry_est`) = intra-AB − population, for the gas days GDSR has. Its parts:
  - oil sands: `ngtl.dop.osda.flow` (TC's Oil Sands Delivery Area, measured; = segments KIRB + LIEG + COLD + NLAT by
    volume, Sep 2025);
  - Heartland petrochem: `ngtl.city.fort_sask.demand_est` (EDM segment, not in OSDA);
  - power generation: `demand.ab.power_burn_est` = Power's AB implied gas burn (gas-day mean of the 5-min series) less
    the oil-sands and Cold Lake cogen (inside OSDA), Heartland cogen (inside Fort Sask) and Fort Nelson (BC);
  - other industrial: `demand.ab.other_industry_est` = industry − the three above.
"""

from __future__ import annotations

import calendar
import json
import re
import statistics as stats
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from montney import paths
from montney.connectors import ngtl_station_util as su
from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import E3M3_TO_MMCF, TC_API, FormatDrift, read_stored
from montney.store import Obs, SeriesMeta

GROUP = "Gas demand / cities"
HV_RANGE, HV_FALLBACK = (37.5, 39.8), 39.0
HDD_BASE = 18.0
MIN_MONTHS, MIN_R2 = 12, 0.9
CITIES = {   # id: (label, ATCO Gas district, TC heat-value area, lat, lon, band)
    "calgary": ("Calgary", "44", "WGAT", 51.05, -114.07, 0.20),
    "edmonton": ("Edmonton", "21", "NEDA", 53.55, -113.49, 0.20),
    "lethbridge": ("Lethbridge", "51", "WGAT", 49.69, -112.84, 0.15),
    "red_deer": ("Red Deer", "31", "NEDA", 52.27, -113.81, 0.15),
}
FORT_SASK = {31004, 31072, 31069, 31025, 31002, 31125, 31071, 31126, 31006, 31073, 31123, 31026, 31076, 32710, 31055,
             31019, 32731, 31064, 31023, 31010, 31011, 31344}
FORT_SASK_BAND = 0.18     # M-2 carry-forward error, P90 17.5 % (Reviewer, #204)
# Bands (PLAN step 5 item 2):
# - population: the HDD fit's out-of-sample monthly error, P90 24.0 % (Jul 2025-Sep 2026, n 15; in-sample 14.2 %, n 33,
#   R² 0.992) → ±24 %;
# - industry = intra (measured) − population: 24 % of ~950 MMcf/d (Nov) ≈ 230 on ~6.1 Bcf/d → ±4 %;
# - power: Power's own implied-burn band (heat rate and HHV high/low: +23 % / −17 % on Nov 1) → ±23 %;
# - other industrial: population 220 ⊕ Heartland 84 (±18 % of 464) ⊕ power 235 (±23 % of 1,023) = 333 on 1,392 (Nov 1)
#   → ±24 %. OSDA is measured.
POP_BAND, INDUSTRY_BAND, POWER_BAND, OTHER_BAND = 0.24, 0.04, 0.23, 0.24
# Population by city (Erik, L-072): the four utility cities' estimates (their ATCO Gas districts: the same city-gate
# residential/commercial basis as the population row, which counts every AG SP point) + "Rest of AB" = population − the
# four, so the parts sum exactly. Rest is 29-33 % of population every month (Apr 2025-Sep 2026). Its out-of-sample
# monthly error (population fit − the four city fits, trained on the months before): P90 20 % (n 18) → ±20 %.
REST_POP_BAND = 0.20
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
RECENT_DAYS = 92          # the forecast host's past_days limit: the fallback for recent months when the archive is down
POP_GROUP = "Gas demand / Alberta split"
MUNI_RE = re.compile(r"\b(TOWN|CITY|VILLAGE|COUNTY|CO-?OP|UTILITY|GAS CITY)\b")
INTRA, OSDA, FS_DEMAND = "ngtl.gdsr.intraprovincial.flow", "ngtl.dop.osda.flow", "ngtl.city.fort_sask.demand_est"
POWER_TOTAL = "power.ab.gas_burn.implied"
POWER_OUTSIDE = ("oilsands_athabasca", "oilsands_south", "cold_lake", "heartland_cogen", "fort_nelson")
GAS_DAY_START_UTC = 14            # 08:00 Alberta (UTC-6) = the NGTL gas day
OSDA_SEGMENTS = frozenset({"KIRB", "LIEG", "COLD", "NLAT"})    # = TC's OSDA by volume (Sep 2025)


def group_of(station: int, name: str) -> str | None:
    if station in FORT_SASK:
        return "fort_sask"
    n = name.upper().strip()
    if n.startswith("AG SP "):
        dd = n[6:8]
        return next((c for c, v in CITIES.items() if v[1] == dd), None)
    return None


def cache_file(lat: float, lon: float) -> Path:
    """The data dir's cache of Open-Meteo archive daily mean temperatures for one point (never the repo)."""
    d = paths.home() / "cache" / "open_meteo_archive"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{lat:.2f}_{lon:.2f}.json"


def daily_temps(ctx: Context, res: FetchResult, name: str, lat: float, lon: float, first: date,
                today: date) -> dict[date, float]:
    """{local day: mean °C} from first to ~a week ago, for the HDD fit. Order:
    1. Open-Meteo's archive (archive-api host); every success is merged into the data-dir cache;
    2. if the archive fails (503 "overloaded" for a whole day on 2026-11-02): the cache, extended with the forecast
       host's own past days (api.open-meteo.com, `past_days`, back to ~92 days), a different service;
    3. nothing at all → {} and the caller fails closed (NO-RESULT)."""
    f = cache_file(lat, lon)
    try:
        cached = {date.fromisoformat(k): v for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        cached = {}
    try:
        arch = ctx.get(ARCHIVE, params={"latitude": lat, "longitude": lon, "start_date": first.isoformat(),
                                        "end_date": (today - timedelta(days=6)).isoformat(),
                                        "daily": "temperature_2m_mean", "timezone": "America/Edmonton"}).json()
        got = {date.fromisoformat(t): v for t, v in zip(arch["daily"]["time"], arch["daily"]["temperature_2m_mean"])
               if v is not None}
        cached.update(got)
        try:
            f.write_text(json.dumps({k.isoformat(): v for k, v in sorted(cached.items())}), encoding="utf-8")
        except OSError as e:
            res.warnings.append(f"{name}: could not write the archive cache ({e})")
        return got
    except Exception as e:  # noqa: BLE001
        res.warnings.append(f"{name}: Open-Meteo archive failed ({e.__class__.__name__}); using the cache "
                            f"({len(cached)} days) + the forecast host's past days")
    out = dict(cached)
    try:
        fc = ctx.get(FORECAST, params={"latitude": lat, "longitude": lon, "daily": "temperature_2m_mean",
                                       "timezone": "America/Edmonton", "past_days": RECENT_DAYS,
                                       "forecast_days": 1}).json()
        for t, v in zip(fc["daily"]["time"], fc["daily"]["temperature_2m_mean"]):
            d = date.fromisoformat(t)
            if v is not None and d < today and d not in out:
                out[d] = v
    except Exception as e:  # noqa: BLE001
        res.warnings.append(f"{name}: forecast host past days failed too ({e.__class__.__name__})")
    return out


def is_population(name: str, segment: str | None = None) -> bool:
    """Residential/commercial gates, outside the OSDA segments (those are in the oil-sands row; Reviewer on #240)."""
    if (segment or "").upper() in OSDA_SEGMENTS:
        return False
    n = name.upper().strip()
    return n.startswith("AG SP ") or bool(MUNI_RE.search(n))


def to_mmcfd(gj_per_day: float, hv: float) -> float:
    return gj_per_day / hv * E3M3_TO_MMCF


def fit(x: list[float], y: list[float]) -> tuple[float, float, float]:
    b, a = stats.linear_regression(x, y)
    return a, b, stats.correlation(x, y) ** 2


class NgtlCity(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for c, (label, dd, area, _, _, band) in [*CITIES.items(), ("fort_sask", ("Fort Saskatchewan (industrial)", "",
                                                                                  "NEDA", 0, 0, FORT_SASK_BAND))]:
            basis = f"ATCO Gas district {dd}" if dd else "Heartland industrial plants"
            out += [SeriesMeta(f"ngtl.city.{c}.monthly", self.id, f"{label} gas deliveries, monthly ({basis})", "MMcf/d",
                               "M", GROUP),
                    SeriesMeta(f"ngtl.city.{c}.demand_est", self.id, f"{label} gas demand today (est., ±{band:.0%})",
                               "MMcf/d", "D", GROUP, extra={"band": band})]
        out += [SeriesMeta("demand.ab.population.monthly", self.id, "Alberta residential/commercial gas deliveries, "
                           "monthly (all ATCO Gas supply points + town/county/co-op gates)", "MMcf/d", "M", POP_GROUP),
                SeriesMeta("demand.ab.population_est", self.id, f"Alberta population gas demand today: residential/"
                           f"commercial (est., ±{POP_BAND:.0%})", "MMcf/d", "D", POP_GROUP, extra={"band": POP_BAND}),
                SeriesMeta("demand.ab.population_rest_est", self.id, f"Alberta population gas demand outside Calgary, "
                           f"Edmonton, Red Deer and Lethbridge: population − the four cities (est., ±{REST_POP_BAND:.0%})",
                           "MMcf/d", "D", POP_GROUP, extra={"band": REST_POP_BAND}),
                SeriesMeta("demand.ab.industry_est", self.id, f"Alberta industrial gas demand: intra-AB − population "
                           f"(est., ±{INDUSTRY_BAND:.0%})", "MMcf/d", "D", POP_GROUP, extra={"band": INDUSTRY_BAND}),
                SeriesMeta("demand.ab.power_burn_est", self.id, "Alberta power generation gas burn outside oil sands and "
                           f"Heartland cogen (Power's implied burn, gas-day mean, ±{POWER_BAND:.0%})", "MMcf/d", "D",
                           POP_GROUP, extra={"band": POWER_BAND}),
                SeriesMeta("demand.ab.other_industry_est", self.id, "Alberta other industrial gas demand: industry − oil "
                           f"sands − Heartland − power (est., ±{OTHER_BAND:.0%})", "MMcf/d", "D", POP_GROUP,
                           extra={"band": OTHER_BAND})]
        return out

    def _hv(self, ctx: Context, res: FetchResult) -> dict[str, float]:
        try:
            rows = ctx.get(f"{self.spec.get('params', {}).get('api', TC_API)}/heatValues").json().get("data") or []
            hv = {r["acronym"]: float(r["value"]) for r in rows if r.get("endDateTimeUtc") is None}
        except Exception:  # noqa: BLE001
            hv = {}
        out = {}
        for area in {v[2] for v in CITIES.values()} | {"NEDA"}:
            v = hv.get(area)
            if v is None or not HV_RANGE[0] <= v <= HV_RANGE[1]:
                res.warnings.append(f"heat value {area} = {v!r}: using the L-031 fallback {HV_FALLBACK} MJ/m3")
                v = HV_FALLBACK
            out[area] = v
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        hv = self._hv(ctx, res)
        today = local_today()
        links = su.receipt_links(ctx.get(su.PAGE).text, su.PAGE, kind="Delivery")
        monthly: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))     # GJ per day
        cut_at: dict[date, datetime] = {}
        for year in sorted(y for y in links if y >= today.year - 2):
            rows, as_of = su.parse_receipts(su.read_sheet(links[year], ctx.get(links[year]).content), year,
                                            kind="delivery")
            for x in rows:
                if su._month_end(x["month"]) > as_of:
                    continue
                per_day = x["flow_e3m3"] / calendar.monthrange(x["month"].year, x["month"].month)[1]
                g = group_of(x["station"], x["name"])
                if g:
                    monthly[g][x["month"]] += per_day
                    cut_at[x["month"]] = as_of
                if is_population(x["name"], x.get("segment")):
                    monthly["ab_population"][x["month"]] += per_day
                    cut_at[x["month"]] = as_of
        if not monthly.get("calgary"):
            raise FormatDrift("no 'AG SP 44-' (Calgary) delivery points found: the delivery file layout or naming changed")
        area_of = {**{c: v[2] for c, v in CITIES.items()}, "fort_sask": "NEDA"}
        hv["AB"] = (hv["WGAT"] + hv["NEDA"]) / 2         # province-wide: the south and north area values, averaged
        for g, by_m in monthly.items():
            sid = "demand.ab.population.monthly" if g == "ab_population" else f"ngtl.city.{g}.monthly"
            for m, gj in by_m.items():
                res.obs.append(Obs(sid, datetime(m.year, m.month, 1), to_mmcfd(gj, hv[area_of.get(g, "AB")]), cut_at[m]))
        hdd_city: dict[str, dict[date, list[float]]] = {}
        fc_city: dict[str, dict[date, float]] = {}

        as_of = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        # utility cities: a + b × HDD
        for c, (label, dd, area, lat, lon, band) in CITIES.items():
            by_m = monthly.get(c, {})
            first = min(by_m) if by_m else today
            temps = daily_temps(ctx, res, c, lat, lon, first, today)
            if not temps:
                res.warnings.append(f"NO-RESULT {c}: no temperatures (archive down, nothing cached, forecast host down)")
                continue
            hdd_m: dict[date, list[float]] = defaultdict(list)
            for d, v in temps.items():
                if first <= d:
                    hdd_m[d.replace(day=1)].append(max(0.0, HDD_BASE - v))
            pairs = [(stats.mean(hdd_m[m]), gj / 1000.0) for m, gj in sorted(by_m.items())
                     if len(hdd_m.get(m, [])) >= 25]
            hdd_city[c] = hdd_m
            fc = None           # L-081: fetched once per city and reused below
            if c in ("calgary", "edmonton"):
                fc = ctx.get(FORECAST, params={"latitude": lat, "longitude": lon, "daily": "temperature_2m_mean",
                                               "timezone": "America/Edmonton", "past_days": 1, "forecast_days": 1}).json()
                fc_city[c] = {date.fromisoformat(t): max(0.0, HDD_BASE - v)
                              for t, v in zip(fc["daily"]["time"], fc["daily"]["temperature_2m_mean"]) if v is not None}
            if len(pairs) < MIN_MONTHS:
                res.warnings.append(f"NO-RESULT {c}: only {len(pairs)} months with HDD (need {MIN_MONTHS})")
                continue
            a, b, r2 = fit([p[0] for p in pairs], [p[1] for p in pairs])
            if r2 < MIN_R2:
                res.warnings.append(f"NO-RESULT {c}: HDD fit R² {r2:.2f} < {MIN_R2}")
                continue
            if fc is None:
                fc = ctx.get(FORECAST, params={"latitude": lat, "longitude": lon, "daily": "temperature_2m_mean",
                                               "timezone": "America/Edmonton", "past_days": 1, "forecast_days": 1}).json()
            for t, v in zip(fc["daily"]["time"], fc["daily"]["temperature_2m_mean"]):
                if v is None:
                    continue
                tj = a + b * max(0.0, HDD_BASE - v)
                d = date.fromisoformat(t)
                res.obs.append(Obs(f"ngtl.city.{c}.demand_est", datetime(d.year, d.month, d.day),
                                   to_mmcfd(tj * 1000.0, hv[area]), as_of))
            res.warnings.append(f"{c}: a {a:.1f} TJ/d, b {b:.2f} TJ/d per HDD, R² {r2:.3f}, n {len(pairs)}")

        # Fort Sask industrial: last month's level carried to today. Not scaled by intra-AB deliveries: those swing ~30 % with
        # city heating load (Sep 5,584 → late Oct ~7,200 MMcf/d), which the Heartland plants don't share.
        fs = monthly.get("fort_sask", {})
        if fs:
            last = max(fs)
            v = to_mmcfd(fs[last], hv["NEDA"])
            for d in (today - timedelta(days=1), today):
                res.obs.append(Obs("ngtl.city.fort_sask.demand_est", datetime(d.year, d.month, d.day), v, as_of))
            res.warnings.append(f"fort_sask: {v:,.0f} MMcf/d carried from {last:%Y-%m}")
        pop = self._population(ctx, res, monthly.get("ab_population", {}), hdd_city, fc_city, hv["AB"], as_of)
        self._industry(ctx, res, pop, as_of)
        return res

    def _population(self, ctx, res, by_m, hdd_city, fc_city, hv_ab, as_of) -> dict[date, float]:
        """AB residential/commercial today: a + b × HDD (mean of Calgary and Edmonton), fitted on monthly deliveries."""
        cal, edm = hdd_city.get("calgary", {}), hdd_city.get("edmonton", {})
        pairs = [((stats.mean(cal[m]) + stats.mean(edm[m])) / 2, gj / 1000.0) for m, gj in sorted(by_m.items())
                 if len(cal.get(m, [])) >= 25 and len(edm.get(m, [])) >= 25]
        if len(pairs) < MIN_MONTHS:
            res.warnings.append(f"NO-RESULT demand.ab.population_est: only {len(pairs)} months with HDD")
            return {}
        a, b, r2 = fit([p[0] for p in pairs], [p[1] for p in pairs])
        if r2 < MIN_R2:
            res.warnings.append(f"NO-RESULT demand.ab.population_est: HDD fit R² {r2:.2f} < {MIN_R2}")
            return {}
        resid = sorted(abs(a + b * x - y) / y for x, y in pairs)
        out = {}
        for d in sorted(set(fc_city.get("calgary", {})) & set(fc_city.get("edmonton", {}))):
            hdd = (fc_city["calgary"][d] + fc_city["edmonton"][d]) / 2
            out[d] = to_mmcfd((a + b * hdd) * 1000.0, hv_ab)
            res.obs.append(Obs("demand.ab.population_est", datetime(d.year, d.month, d.day), out[d], as_of))
            ts = datetime(d.year, d.month, d.day)
            cities = [o.value for o in res.obs if o.ts == ts and o.series_id in
                      {f"ngtl.city.{c}.demand_est" for c in CITIES}]
            if len(cities) == len(CITIES):          # Rest of AB = population − the four cities (L-072)
                res.obs.append(Obs("demand.ab.population_rest_est", ts, out[d] - sum(cities), as_of))
        res.warnings.append(f"population: a {a:.0f} TJ/d, b {b:.1f} TJ/d per HDD, R² {r2:.3f}, n {len(pairs)}, monthly "
                            f"residual P90 {resid[int(round(0.9 * (len(resid) - 1)))]:.1%}")
        return out

    def _industry(self, ctx, res, pop: dict[date, float], as_of) -> None:
        """Industry = intra-AB − population, and its parts, for the gas days GDSR has."""
        if not pop:
            return
        lo = datetime.combine(min(pop), datetime.min.time()) - timedelta(days=3)
        try:
            st = self.stored([INTRA, OSDA, POWER_TOTAL, *(f"power.plant.{p}.gas_burn.est" for p in POWER_OUTSIDE)],
                             lo, ctx.now)
        except Exception as e:  # noqa: BLE001
            res.warnings.append(f"NO-RESULT demand.ab.industry_est: store read failed ({e})")
            return
        intra = {t.date(): v for t, v in (st.get(INTRA) or {}).items()}
        osda = {t.date(): v for t, v in (st.get(OSDA) or {}).items()}
        fs = {o.ts.date(): o.value for o in res.obs if o.series_id == FS_DEMAND}

        def gas_day_mean(sid: str, d: date) -> float | None:
            start = datetime(d.year, d.month, d.day, GAS_DAY_START_UTC)
            vals = [v for t, v in (st.get(sid) or {}).items() if start <= t < start + timedelta(days=1)]
            return sum(vals) / len(vals) if len(vals) >= 12 else None          # ≥ 1 h of 5-min values

        for d in sorted(pop):
            if d not in intra:
                continue
            ts = datetime(d.year, d.month, d.day)
            industry = intra[d] - pop[d]
            res.obs.append(Obs("demand.ab.industry_est", ts, industry, as_of))
            o = next((osda[d - timedelta(days=i)] for i in range(3) if d - timedelta(days=i) in osda), None)
            total = gas_day_mean(POWER_TOTAL, d)
            excl = [gas_day_mean(f"power.plant.{p}.gas_burn.est", d) for p in POWER_OUTSIDE]
            power = total - sum(x or 0.0 for x in excl) if total is not None else None
            if power is not None:
                res.obs.append(Obs("demand.ab.power_burn_est", ts, power, as_of))
            if o is not None and power is not None and d in fs:
                res.obs.append(Obs("demand.ab.other_industry_est", ts, industry - o - fs[d] - power, as_of))
                res.warnings.append(f"AB demand {d}: intra {intra[d]:,.0f} = population {pop[d]:,.0f} + industry "
                                    f"{industry:,.0f} (oil sands {o:,.0f}, Heartland {fs[d]:,.0f}, power {power:,.0f}, "
                                    f"other {industry - o - fs[d] - power:,.0f})")
