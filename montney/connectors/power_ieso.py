"""Ontario (IESO) generation: total and by fuel (L-078, Erik: "partial IESO data ... just total Gen, and Gen split by type").

Two public, keyless IESO reports (https://reports-public.ieso.ca/public/), read by two specs:

* ``report: hourly`` (sources/power_ieso_hourly.yaml, ``mode: live``, never persisted): the box's hourly MW.
  ``GenOutputCapability/PUB_GenOutputCapability.xml`` is today's per-generator report, re-issued every hour
  (``_v1``, ``_v2`` ...), with each generator's ``FuelType`` and its hourly ``Output``. Yesterday's final file
  (``PUB_GenOutputCapability_<YYYYMMDD>.xml``) is read too, so the live window is the last ~24-48 h, even just
  after midnight. Once yesterday's file has all 24 hours it's cached under ``<home>/cache/ieso`` and read from
  disk (L-081: 1 request a day for it, not 24). The per-generator sum equals IESO's own fuel report exactly (2026-11-01: every fuel, 0 MWh off).
* ``report: daily`` (sources/power_ieso_daily.yaml, daily tier): the stored history, as **daily sums**
  (Erik, L-078: "save the data as daily sums"). ``GenOutputbyFuelHourly/PUB_GenOutputbyFuelHourly.xml`` is the
  year-to-date file (updated once a day). ``PUB_GenOutputbyFuelHourly_<YYYY>.xml`` is each past year's final file.
  Past-year files are cached under ``<home>/cache/ieso`` (they don't change), so the back-fill from ``from_year``
  (2020) costs one download per year, once.

"Partial": these are IESO's transmission-connected, market-participant generators only. Embedded (distribution-
connected) generation, mostly solar, isn't in either report.

Time. IESO publishes in **EST all year** (no daylight time), with hour-ending 1-24 for each date. That's one
fixed offset by design, named here (``IESO_TZ = Etc/GMT+5``, the "Etc" sign convention, i.e. UTC-5), like the
Open-Meteo city windows exception in AGENTS.md. Hourly ``ts`` is the hour's start in naive UTC; a daily ``ts`` is
the IESO (EST) date.

Series:
  ieso.gen.<fuel>.hourly, ieso.gen.total.hourly  MW, freq 1h (the hour's mean MW = its MWh), live only
  ieso.gen.<fuel>.daily, ieso.gen.total.daily    MWh/d, freq D (sum of the date's 24 hours), stored
with <fuel> in nuclear, gas, hydro, wind, solar, biofuel, other. ``CONTROL ACTIONS`` (in the fuel report) isn't
generation and isn't read.

Fail closed (FormatDrift, nothing stored): an unknown fuel, a file with none of the expected fuels, or a date
that isn't the file's. A date without all 24 hours of the six core fuels gets no daily value (a warning). OTHER
only exists in the fuel report from ~April 2025 (absent, not missing, before that): ``other.daily`` is written for
a date only when all 24 hours have it, and a partly-covered date still counts its OTHER hours in the total. In the hourly report, an hour still being filled in (fewer generators reporting than
the day's other hours, by more than ``LATE_HOUR_SLACK``) is left out until the next issue.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://reports-public.ieso.ca/public"
IESO_TZ = ZoneInfo("Etc/GMT+5")    # EST all year (IESO market time); "Etc" sign convention: UTC-5
FUELS = ("nuclear", "gas", "hydro", "wind", "solar", "biofuel", "other")
CORE = FUELS[:-1]                  # in every year; OTHER only appears in the fuel report from ~April 2025
NOT_GENERATION = {"CONTROL ACTIONS"}
GROUP = "IESO / Ontario generation by fuel"
LATE_HOUR_SLACK = 3                # generators an hour may be short of the day's most before it counts as unfinished
MAX_MW = 40_000                    # Ontario's whole fleet is ~38 GW installed: anything above is a parse error

_GEN = re.compile(r"<Generator>(.*?)</Generator>", re.S)
_OUT = re.compile(r"<Output>\s*<Hour>(\d+)</Hour>\s*(?:<EnergyMW>([^<]*)</EnergyMW>)?\s*</Output>")
_DAY = re.compile(r"<DailyData>\s*<Day>([^<]+)</Day>(.*?)</DailyData>", re.S)
_HOUR = re.compile(r"<HourlyData>\s*<Hour>(\d+)</Hour>(.*?)</HourlyData>", re.S)
_FUEL = re.compile(r"<Fuel>([^<]+)</Fuel>\s*<EnergyValue>.*?<Output>([^<]*)</Output>", re.S)
_CAP = re.compile(r"<Capability>\s*<Hour>(\d+)</Hour>\s*(?:<EnergyMW>([^<]*)</EnergyMW>)?\s*</Capability>")

# L-091: Ontario's major nuclear and gas plants, as groups of IESO generator names (GenOutputCapability <GeneratorName>,
# matched by prefix). Same request as the fuel sums: no extra fetch. network/power/ontario_plants.geojson places them.
PLANTS = {
    "bruce": ("BRUCEA-", "BRUCEB-"), "darlington": ("DARLINGTON-",), "pickering": ("PICKERING",),
    "portlands": ("PORTLANDS-",), "lennox": ("LENNOX-",), "napanee": ("NAPANEE-",), "goreway": ("SITHE GOREWAY-",),
    "halton_hills": ("HALTONHILLS-",), "st_clair": ("STCLAIRCGS",), "greenfield": ("GREENFIELD ENERGY CENTRE-",),
    "brighton_beach": ("BRIGHTON BEACH",), "york": ("YORKCGS-",), "thorold": ("THOROLDCGS",),
    "sarnia_regional": ("TASARNIA",),
}
PLANT_GROUP = "IESO / Ontario major plants"


def _fuel(name: str) -> str:
    f = name.strip().lower()
    if f not in FUELS:
        raise FormatDrift(f"IESO: unknown fuel {name!r} (expected {', '.join(f.upper() for f in FUELS)})")
    return f


def ieso_date(now_utc: datetime) -> date:
    """The IESO (EST) date at a naive-UTC instant."""
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(IESO_TZ).date()


def hour_start_utc(d: date, hour_ending: int) -> datetime:
    """IESO hour-ending ``h`` (EST) on date ``d`` -> the hour's start, naive UTC."""
    if not 1 <= hour_ending <= 24:
        raise FormatDrift(f"IESO: hour {hour_ending} outside 1-24")
    start = datetime(d.year, d.month, d.day, tzinfo=IESO_TZ) + timedelta(hours=hour_ending - 1)
    return start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_capability(text: str) -> tuple[date, dict[int, dict[str, float]], list[str]]:
    """GenOutputCapability -> (date, {hour_ending: {fuel: MW}}, warnings). Empty outputs count as 0, as IESO's own
    fuel report does; an hour still being filled in is dropped."""
    m = re.search(r"<Date>(\d{4}-\d{2}-\d{2})</Date>", text)
    if not m:
        raise FormatDrift("IESO GenOutputCapability: no <Date>")
    d = date.fromisoformat(m.group(1))
    sums: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    count: dict[int, int] = defaultdict(int)
    gens = _GEN.findall(text)
    if not gens:
        raise FormatDrift("IESO GenOutputCapability: no generators")
    for g in gens:
        fm = re.search(r"<FuelType>([^<]+)</FuelType>", g)
        outs = re.search(r"<Outputs>(.*?)</Outputs>", g, re.S)
        if not fm or not outs:
            raise FormatDrift("IESO GenOutputCapability: a generator without FuelType or Outputs")
        f = _fuel(fm.group(1))
        for h, v in _OUT.findall(outs.group(1)):
            sums[int(h)][f] += float(v) if v and v.strip() else 0.0
            count[int(h)] += 1
    warnings = []
    most = max(count.values())
    for h in sorted(count):
        if count[h] < most - LATE_HOUR_SLACK:
            warnings.append(f"IESO {d} hour {h}: {count[h]} of {most} generators reported; left out (unfinished)")
            del sums[h]
    return d, {h: dict(v) for h, v in sums.items()}, warnings


def parse_plants(text: str) -> tuple[date, dict[str, dict[int, tuple[float, float]]], list[str]]:
    """GenOutputCapability -> (date, {plant: {hour_ending: (output MW, capability MW)}}, warnings), summed over the
    plant's generators (PLANTS). Empty values count as 0, as in IESO's fuel report. A plant with no generator in the
    file is a warning (a renamed unit), never a silent 0."""
    m = re.search(r"<Date>(\d{4}-\d{2}-\d{2})</Date>", text)
    if not m:
        raise FormatDrift("IESO GenOutputCapability: no <Date>")
    out: dict[str, dict[int, list[float]]] = {k: defaultdict(lambda: [0.0, 0.0]) for k in PLANTS}
    found = set()
    for g in _GEN.findall(text):
        name = re.search(r"<GeneratorName>([^<]+)</GeneratorName>", g)
        if not name:
            continue
        plant = next((k for k, pre in PLANTS.items() if name.group(1).strip().upper().startswith(pre)), None)
        if plant is None:
            continue
        found.add(plant)
        outs = re.search(r"<Outputs>(.*?)</Outputs>", g, re.S)
        caps = re.search(r"<Capabilities>(.*?)</Capabilities>", g, re.S)
        for h, v in _OUT.findall(outs.group(1) if outs else ""):
            out[plant][int(h)][0] += float(v) if v and v.strip() else 0.0
        for h, v in _CAP.findall(caps.group(1) if caps else ""):
            out[plant][int(h)][1] += float(v) if v and v.strip() else 0.0
    warnings = [f"IESO: no generator for plant {k} ({', '.join(PLANTS[k])}) in GenOutputCapability"
                for k in PLANTS if k not in found]
    plants = {k: {h: (a, b) for h, (a, b) in v.items()} for k, v in out.items() if k in found}
    return date.fromisoformat(m.group(1)), plants, warnings


def parse_fuel_hourly(text: str) -> dict[date, dict[int, dict[str, float]]]:
    """GenOutputbyFuelHourly -> {date: {hour_ending: {fuel: MWh}}}. A blank Output is left out (not 0)."""
    out: dict[date, dict[int, dict[str, float]]] = {}
    year = re.search(r"<DeliveryYear>(\d{4})</DeliveryYear>", text)
    for ds, body in _DAY.findall(text):
        d = date.fromisoformat(ds.strip())
        if year and d.year != int(year.group(1)):
            raise FormatDrift(f"IESO fuel report: day {d} in the {year.group(1)} file")
        hours = out.setdefault(d, {})
        for h, hb in _HOUR.findall(body):
            row = hours.setdefault(int(h), {})
            for name, v in _FUEL.findall(hb):
                if name.strip().upper() in NOT_GENERATION:
                    continue
                if v.strip():
                    row[_fuel(name)] = float(v)
    if not out:
        raise FormatDrift("IESO fuel report: no days")
    return out


def _check(v: float, what: str) -> float:
    if not -1_000 <= v <= MAX_MW:
        raise FormatDrift(f"IESO {what}: {v:,.0f} MW outside -1,000..{MAX_MW:,}")
    return v


class IesoGeneration(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.report = spec.get("params", {}).get("report", "daily")
        if self.report not in ("hourly", "daily"):
            raise ValueError(f"{self.id}: params.report must be hourly or daily, not {self.report!r}")

    def declared_series(self) -> list[SeriesMeta]:
        names = {f: f.title() for f in FUELS} | {"total": "Total"}
        plant = {k: k.replace("_", " ").title() for k in PLANTS}
        if self.report == "hourly":
            # The tier is stated, not left to the freq (1h -> "hourly"): the catalog is seeded from these metas at
            # startup, and a series seeded "hourly" can't then be written live (#290 bug).
            ex = {"tier": "live", "tz": "EST (IESO market time)"}
            return ([SeriesMeta(f"ieso.gen.{f}.hourly", self.id, f"Ontario (IESO) {n} generation, hourly", "MW", "1h",
                                GROUP, extra={**ex, "partial": "transmission-connected only"}) for f, n in names.items()]
                    + [SeriesMeta(f"ieso.plant.{k}.hourly", self.id, f"{n} output (IESO), hourly", "MW", "1h",
                                  PLANT_GROUP, extra={**ex, "generators": list(PLANTS[k])}) for k, n in plant.items()]
                    + [SeriesMeta(f"ieso.plant.{k}.capability.hourly", self.id,
                                  f"{n} available capability (IESO), hourly", "MW", "1h", PLANT_GROUP,
                                  extra={**ex, "generators": list(PLANTS[k])}) for k, n in plant.items()])
        return ([SeriesMeta(f"ieso.gen.{f}.daily", self.id, f"Ontario (IESO) {n} generation, daily sum", "MWh/d", "D",
                            GROUP, extra={"tier": "daily", "partial": "transmission-connected only",
                                          "day": "IESO date (EST)"}) for f, n in names.items()]
                + [SeriesMeta(f"ieso.plant.{k}.daily", self.id, f"{n} output (IESO), daily sum", "MWh/d", "D",
                              PLANT_GROUP, extra={"tier": "daily", "day": "IESO date (EST)",
                                                  "generators": list(PLANTS[k])}) for k, n in plant.items()])

    def fetch(self, ctx: Context) -> FetchResult:
        return self._hourly(ctx) if self.report == "hourly" else self._daily(ctx)

    # ---------------------------------------------------------------- live hourly MW
    def _yesterday(self, ctx: Context, d: date) -> str:
        """Yesterday's GenOutputCapability, read from IESO only until it's final (L-081). The live window is replaced on
        every fetch, so the file is still needed each hour; once it has all 24 hours it's cached under
        <home>/cache/ieso and read from disk (1 request a day, not 24). Older cached days are removed."""
        d_ = paths.home() / "cache" / "ieso"
        cache = d_ / f"PUB_GenOutputCapability_{d:%Y%m%d}.xml"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        text = ctx.get(f"{BASE}/GenOutputCapability/PUB_GenOutputCapability_{d:%Y%m%d}.xml", retries=1).text
        got, hours, _ = parse_capability(text)
        if got == d and sorted(hours) == list(range(1, 25)):     # final: every hour in, none left out as unfinished
            d_.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
            for old in d_.glob("PUB_GenOutputCapability_*.xml"):
                if old != cache:
                    old.unlink(missing_ok=True)
        return text

    def _hourly(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        today = ieso_date(ctx.now)
        texts = []
        try:                                                      # yesterday's final file: nice to have
            texts.append(self._yesterday(ctx, today - timedelta(days=1)))
        except Exception as e:                                    # noqa: BLE001 - today's file still shows the box
            res.warnings.append(f"IESO: yesterday's GenOutputCapability not read ({e}); today's hours only")
        texts.append(ctx.get(f"{BASE}/GenOutputCapability/PUB_GenOutputCapability.xml").text)
        for i, text in enumerate(texts):
            d, hours, warns = parse_capability(text)
            res.warnings += warns
            pd, plants, pwarn = parse_plants(text)
            if i == len(texts) - 1:
                res.warnings += pwarn
            for k, by_h in plants.items():
                for h in sorted(set(by_h) & set(hours)):               # the same complete hours as the fuel sums
                    ts = hour_start_utc(pd, h)
                    res.obs.append(Obs(f"ieso.plant.{k}.hourly", ts, _check(by_h[h][0], f"{pd} h{h} {k}"), ts))
                    res.obs.append(Obs(f"ieso.plant.{k}.capability.hourly", ts,
                                       _check(by_h[h][1], f"{pd} h{h} {k} cap"), ts))
            for h, row in sorted(hours.items()):
                ts = hour_start_utc(d, h)
                for f in FUELS:
                    res.obs.append(Obs(f"ieso.gen.{f}.hourly", ts, _check(row.get(f, 0.0), f"{d} h{h} {f}"), ts))
                res.obs.append(Obs("ieso.gen.total.hourly", ts, _check(sum(row.get(f, 0.0) for f in FUELS), f"{d} h{h}"), ts))
        if not res.obs:
            raise FormatDrift("IESO GenOutputCapability: no hours (NO-RESULT)")
        return res

    # ---------------------------------------------------------------- stored daily sums
    def _year_text(self, ctx: Context, year: int, this_year: int) -> str:
        if year == this_year:
            return ctx.get(f"{BASE}/GenOutputbyFuelHourly/PUB_GenOutputbyFuelHourly.xml", timeout=120).text
        cache = paths.home() / "cache" / "ieso" / f"PUB_GenOutputbyFuelHourly_{year}.xml"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        text = ctx.get(f"{BASE}/GenOutputbyFuelHourly/PUB_GenOutputbyFuelHourly_{year}.xml", timeout=120).text
        parse_fuel_hourly(text)                                   # never cache a file that doesn't parse
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text

    def _daily(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        res = FetchResult(meta=self.declared_series())
        this_year = ieso_date(ctx.now).year
        first = int(p.get("from_year", this_year))
        for year in range(first, this_year + 1):
            for d, hours in sorted(parse_fuel_hourly(self._year_text(ctx, year, this_year)).items()):
                ts = datetime(d.year, d.month, d.day)
                if sorted(hours) != list(range(1, 25)) or any(set(CORE) - set(r) for r in hours.values()):
                    res.warnings.append(f"IESO {d}: not all 24 hours of {', '.join(CORE)}; no daily sum")
                    continue
                day = {f: sum(hours[h][f] for h in hours) for f in CORE}
                n_other = sum("other" in r for r in hours.values())
                if n_other == 24:
                    day["other"] = sum(r["other"] for r in hours.values())
                elif n_other:                                     # counted in the total, but no partial "other" day
                    res.warnings.append(f"IESO {d}: OTHER in {n_other} of 24 hours; in the total, no other.daily")
                for f, v in day.items():
                    res.obs.append(Obs(f"ieso.gen.{f}.daily", ts, _check(v / 24, f"{d} {f}") * 24, ts))
                total = sum(day.values()) + (sum(r.get("other", 0.0) for r in hours.values()) if 0 < n_other < 24 else 0)
                res.obs.append(Obs("ieso.gen.total.daily", ts, _check(total / 24, f"{d}") * 24, ts))
        self._plant_days(ctx, res)
        if not res.obs:
            raise FormatDrift("IESO fuel report: no complete days (NO-RESULT)")
        return res

    def _plant_days(self, ctx: Context, res: FetchResult) -> None:
        """L-091: per-plant daily sums (MWh/d) from yesterday's final GenOutputCapability: 0 extra requests when the
        hourly source has cached it, else 1 a day. IESO keeps only recent daily files, so there's no 2020 back-fill
        for plants (the fuel totals above have it)."""
        y = ieso_date(ctx.now) - timedelta(days=1)
        try:
            text = self._yesterday(ctx, y)
        except Exception as e:                                    # noqa: BLE001 - the fuel sums still stand
            res.warnings.append(f"IESO: yesterday's GenOutputCapability not read for plant sums ({e})")
            return
        d, plants, warns = parse_plants(text)
        res.warnings += warns
        ts = datetime(d.year, d.month, d.day)
        for k, by_h in plants.items():
            if sorted(by_h) != list(range(1, 25)):
                res.warnings.append(f"IESO {d}: plant {k} has {len(by_h)} of 24 hours; no daily sum")
                continue
            mw = sum(v[0] for v in by_h.values()) / 24
            res.obs.append(Obs(f"ieso.plant.{k}.daily", ts, _check(mw, f"{d} {k}") * 24, ts))
