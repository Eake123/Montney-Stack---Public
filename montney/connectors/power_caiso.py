"""California (CAISO) demand and generation by fuel (L-090, Erik: "add California ... limiting the amount of requests
... power demand ... energy mix"). The same shape as Ontario (power_ieso.py, L-078): hourly display, daily sums stored.

Source: CAISO "Today's Outlook" CSVs, keyless, 5-minute rows in Pacific prevailing time (America/Los_Angeles):
  https://www.caiso.com/outlook/current/{fuelsource,demand}.csv           today, re-issued every 5 min
  https://www.caiso.com/outlook/history/<YYYYMMDD>/{fuelsource,demand}.csv one final file per past day (2020 on)
fuelsource columns: Solar, Wind, Geothermal, Biomass, Biogas, Small hydro, Coal, Nuclear, Natural gas, Large hydro,
Batteries (+ discharging, - charging), Imports (net), Other. demand: "Current demand" (MW).

Requests (L-081, docs/ARCHITECTURE-REQUESTS.md §3):
* ``report: hourly`` (sources/power_caiso_hourly.yaml, ``mode: live``, 1 h): today's two files each run (2 req/h).
  Yesterday's two history files are read until final (288 rows), then served from ``<home>/cache/caiso``.
* ``report: daily`` (sources/power_caiso_daily.yaml, 1 d): yesterday's two files, plus a BOUNDED back-fill of
  ``backfill_per_run`` older days per run (2 requests each) toward ``from_date``. A history day is one request per
  file (there's no bulk file), so the 2020 back-fill is spread over runs, not done in one burst. Each finished day's
  sums go into ``<home>/cache/caiso/daily_sums.json`` (a few hundred KB), and every run re-emits all of them, so a
  ``montney try`` that filled the cache can't leave the store short. The store's change-only writes drop the repeats.

Groups (stored, MW hourly / MWh per day): solar, wind, geothermal, biomass (biomass + biogas), hydro (small + large),
gas (natural gas), nuclear, batteries (signed), imports (net), other (coal + other). Totals: ``gen.total`` = every
group but imports (in-state generation), ``supply.total`` = gen.total + imports, and ``demand``. Demand isn't exactly
supply (within about -740/+330 MW on 2026-11-03; CAISO's demand nets out some losses and pumping), so the box's
shares are "% of supply".

Series:
  caiso.gen.<group>.hourly, caiso.gen.total.hourly, caiso.supply.total.hourly, caiso.demand.hourly
      MW, freq 1h: the mean of the hour's twelve 5-min rows (complete hours only), live only
  caiso.gen.<group>.daily, caiso.gen.total.daily, caiso.supply.total.daily, caiso.demand.daily
      MWh/d, freq D: sum of the Pacific date's 5-min rows x 5/60 h (a day needs >= 276 rows)

Fail closed (FormatDrift, nothing stored): an unknown column (CAISO added a fuel: map it first), a file without its
header, or no complete hour. A row with a blank cell (the rest of today) is skipped, never read as 0.
CAISO's history files list 24 clock hours even on the November fall-back day (288 rows on 2026-11-01), so that day's
sum covers 24 of 25 h (~4 % low; each such day gets a warning), and the repeated hour isn't double-counted.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from montney import paths
from montney.connectors.aeso_ets import LocalClock
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://www.caiso.com/outlook"
TZ = "America/Los_Angeles"
GROUPS = {"solar": ("solar",), "wind": ("wind",), "geothermal": ("geothermal",), "biomass": ("biomass", "biogas"),
          "hydro": ("small hydro", "large hydro"), "gas": ("natural gas",), "nuclear": ("nuclear",),
          "batteries": ("batteries",), "imports": ("imports",), "other": ("coal", "other")}
COLUMN = {c: g for g, cols in GROUPS.items() for c in cols}
NAMES = {"solar": "solar", "wind": "wind", "geothermal": "geothermal", "biomass": "biomass + biogas",
         "hydro": "hydro (small + large)", "gas": "natural gas", "nuclear": "nuclear",
         "batteries": "batteries (+ discharge, - charge)", "imports": "net imports", "other": "other (incl. coal)"}
TOTALS = {"caiso.gen.total": "in-state generation (all fuels, no imports)",
          "caiso.supply.total": "supply (in-state generation + net imports)", "caiso.demand": "demand"}
GROUP = "CAISO / California demand and generation by fuel"
STEP_H = 5 / 60
FULL_DAY_ROWS = 288
MIN_DAY_ROWS = 276                       # a 23-hour (spring-forward) day
MAX_MW = 80_000                          # CAISO's record demand is ~52 GW: anything above is a parse error


def pacific_date(now_utc: datetime) -> date:
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(TZ)).date()


def day_hours(d: date) -> float:
    """Hours in the Pacific date ``d`` (23, 24 or 25)."""
    tz, utc = ZoneInfo(TZ), ZoneInfo("UTC")
    a = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(utc)
    b = (datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)).replace(tzinfo=None)
    return (b.replace(tzinfo=tz).astimezone(utc) - a).total_seconds() / 3600


def _rows(text: str, what: str) -> list[dict]:
    r = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not r or "Time" not in r[0]:
        raise FormatDrift(f"CAISO {what}: no Time header")
    return r


def _stamps(d: date, rows: list[dict]) -> list[datetime]:
    """'HH:MM' Pacific rows of date ``d`` -> naive UTC. A trailing '00:00' (the next day's first point) is dropped."""
    locals_ = []
    for r in rows:
        hh, mm = (int(x) for x in r["Time"].strip().split(":"))
        locals_.append(datetime(d.year, d.month, d.day, hh, mm))
    while len(locals_) > 1 and locals_[-1] <= locals_[0]:
        locals_.pop()
        rows.pop()
    return LocalClock(TZ).series(locals_)


def parse_day(d: date, fuel_text: str, demand_text: str) -> dict[datetime, dict[str, float]]:
    """One day's two files -> {5-min UTC stamp: {group..., gen, supply, demand}}, complete rows only."""
    fuel = _rows(fuel_text, "fuelsource")
    cols = [c for c in fuel[0] if c != "Time"]
    unknown = [c for c in cols if c.strip().lower() not in COLUMN]
    if unknown:
        raise FormatDrift(f"CAISO fuelsource: unknown column(s) {unknown} (map them in power_caiso.GROUPS first)")
    out: dict[datetime, dict[str, float]] = {}
    for ts, r in zip(_stamps(d, fuel), fuel):
        if any((r.get(c) or "").strip() == "" for c in cols):
            continue                                              # the rest of today: not data
        g = defaultdict(float)
        for c in cols:
            g[COLUMN[c.strip().lower()]] += float(r[c])
        row = {k: g.get(k, 0.0) for k in GROUPS}
        row["gen"] = sum(v for k, v in row.items() if k != "imports")
        row["supply"] = row["gen"] + row["imports"]
        out[ts] = row
    dem = _rows(demand_text, "demand")
    if "Current demand" not in dem[0]:
        raise FormatDrift("CAISO demand: no 'Current demand' column")
    for ts, r in zip(_stamps(d, dem), dem):
        v = (r.get("Current demand") or "").strip()
        if v and ts in out:
            out[ts]["demand"] = float(v)
    for ts, row in out.items():
        for k, v in row.items():
            if not -MAX_MW <= v <= MAX_MW:
                raise FormatDrift(f"CAISO {ts}: {k} {v:,.0f} MW outside +-{MAX_MW:,}")
    return {ts: row for ts, row in out.items() if "demand" in row}


def _sid(key: str, kind: str) -> str:
    if key == "gen":
        return f"caiso.gen.total.{kind}"
    if key == "supply":
        return f"caiso.supply.total.{kind}"
    if key == "demand":
        return f"caiso.demand.{kind}"
    return f"caiso.gen.{key}.{kind}"


KEYS = (*GROUPS, "gen", "supply", "demand")


def hourly_means(points: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    by_hour: dict[datetime, list[dict]] = defaultdict(list)
    for ts, row in points.items():
        by_hour[ts.replace(minute=0)].append(row)
    return {h: {k: sum(r[k] for r in rows) / len(rows) for k in KEYS}
            for h, rows in by_hour.items() if len(rows) == 12}


class CaisoGeneration(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.report = spec.get("params", {}).get("report", "daily")
        if self.report not in ("hourly", "daily"):
            raise ValueError(f"{self.id}: params.report must be hourly or daily, not {self.report!r}")

    def declared_series(self) -> list[SeriesMeta]:
        names = {**{k: NAMES[k] for k in GROUPS}, "gen": TOTALS["caiso.gen.total"],
                 "supply": TOTALS["caiso.supply.total"], "demand": TOTALS["caiso.demand"]}
        if self.report == "hourly":     # the tier is stated: the catalog is seeded from these metas (#292)
            return [SeriesMeta(_sid(k, "hourly"), self.id, f"California (CAISO) {n}, hourly mean", "MW", "1h", GROUP,
                               extra={"tier": "live", "tz": TZ}) for k, n in names.items()]
        return [SeriesMeta(_sid(k, "daily"), self.id, f"California (CAISO) {n}, daily sum", "MWh/d", "D", GROUP,
                           extra={"tier": "daily", "day": "Pacific date"}) for k, n in names.items()]

    @staticmethod
    def _cache() -> "paths.Path":
        d = paths.home() / "cache" / "caiso"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _history(self, ctx: Context, d: date, *, keep: bool) -> tuple[str, str]:
        """A past day's two files. ``keep``: cache them once final (yesterday, for the hourly window), else don't."""
        c = self._cache()
        f, m = c / f"{d:%Y%m%d}_fuelsource.csv", c / f"{d:%Y%m%d}_demand.csv"
        if f.exists() and m.exists():
            return f.read_text(encoding="utf-8"), m.read_text(encoding="utf-8")
        fuel = ctx.get(f"{BASE}/history/{d:%Y%m%d}/fuelsource.csv", retries=1).text
        dem = ctx.get(f"{BASE}/history/{d:%Y%m%d}/demand.csv", retries=1).text
        if keep and len(parse_day(d, fuel, dem)) >= MIN_DAY_ROWS:
            for old in c.glob("*_fuelsource.csv"):
                old.unlink(missing_ok=True)
            for old in c.glob("*_demand.csv"):
                old.unlink(missing_ok=True)
            f.write_text(fuel, encoding="utf-8")
            m.write_text(dem, encoding="utf-8")
        return fuel, dem

    def fetch(self, ctx: Context) -> FetchResult:
        return self._hourly(ctx) if self.report == "hourly" else self._daily(ctx)

    def _hourly(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        today = pacific_date(ctx.now)
        points: dict[datetime, dict[str, float]] = {}
        try:                                                      # yesterday: nice to have for the window
            points.update(parse_day(today - timedelta(days=1), *self._history(ctx, today - timedelta(days=1), keep=True)))
        except Exception as e:                                    # noqa: BLE001 - today's file still shows the box
            res.warnings.append(f"CAISO: yesterday's history not read ({e}); today's hours only")
        points.update(parse_day(today, ctx.get(f"{BASE}/current/fuelsource.csv").text,
                                ctx.get(f"{BASE}/current/demand.csv").text))
        for h, row in sorted(hourly_means(points).items()):
            for k in KEYS:
                res.obs.append(Obs(_sid(k, "hourly"), h, round(row[k], 1), h))
        if not res.obs:
            raise FormatDrift("CAISO: no complete hour (NO-RESULT)")
        return res

    def _daily(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        res = FetchResult(meta=self.declared_series())
        start = date.fromisoformat(str(p.get("from_date", "2020-01-01")))
        per_run = int(p.get("backfill_per_run", 0))
        yesterday = pacific_date(ctx.now) - timedelta(days=1)
        store = self._cache() / "daily_sums.json"
        sums: dict[str, dict[str, float]] = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        missing = [yesterday - timedelta(days=i) for i in range((yesterday - start).days + 1)]
        missing = [d for d in missing if d.isoformat() not in sums]
        todo = missing[: 1 + per_run] if missing and missing[0] == yesterday else missing[:per_run]
        for d in todo:
            try:
                pts = parse_day(d, *self._history(ctx, d, keep=d == yesterday))
            except FormatDrift:
                raise
            except Exception as e:                                # noqa: BLE001 - a missing day: try again next run
                res.warnings.append(f"CAISO {d}: history not read ({e})")
                continue
            if len(pts) < MIN_DAY_ROWS:
                res.warnings.append(f"CAISO {d}: {len(pts)} complete 5-min rows (< {MIN_DAY_ROWS}); no daily sum yet")
                continue
            sums[d.isoformat()] = {k: round(sum(r[k] for r in pts.values()) * STEP_H, 1) for k in KEYS}
            if day_hours(d) > 24 and len(pts) <= FULL_DAY_ROWS:     # the fall-back day: CAISO lists 24 clock hours
                res.warnings.append(f"CAISO {d}: a 25-hour day but the file has 24 h of rows; its daily sums are "
                                    f"~4 % low (the repeated hour isn't published)")
        store.write_text(json.dumps(sums, sort_keys=True), encoding="utf-8")
        left = len([d for d in missing if d.isoformat() not in sums])
        if left:
            res.warnings.append(f"CAISO back-fill: {left} days to {start} still to read ({per_run} per run)")
        for ds, row in sorted(sums.items()):
            ts = datetime.fromisoformat(ds)
            for k in KEYS:
                res.obs.append(Obs(_sid(k, "daily"), ts, row[k], ts))
        if not res.obs:
            raise FormatDrift("CAISO: no complete day (NO-RESULT)")
        return res
