"""region_balance (L-106, docs/REGIONS.md §4): the ONE engine that turns every region file's parts into the standard
`region.<r>.*` series. Zero requests: it reads the inputs back from the store (read-only, gas_common.read_stored).

Rules (the contract):
- a line is written for a bucket only when EVERY part has a value in that bucket; a missing part is never a zero;
- a `none: true` line counts as 0 (the region is known to have none: PacNW production);
- buckets: gas = gas day (a daily part's own date; a sub-daily part's points are averaged per Alberta day, UTC-6
  fixed as in L-029), power = UTC hour (mean of the hour's points), oil = calendar month;
- `fill: month` holds a monthly part's value across every day / hour of its month; `fill: latest` also holds the
  newest month forward (EIA's 2-3 month lag), at most regions.LATEST_MAX_DAYS. Both mark the line est.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney import regions as R
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.store import Obs, SeriesMeta

GROUP = "Regions"
# power reaches back 7 days: BC Hydro posts load on weekdays only, up to ~4 days late (Power #428)
WINDOW = {"gas": timedelta(days=21), "power": timedelta(days=7), "oil": timedelta(days=400)}
# A one-off rebuild further back (after a source back-fills history older than the window): run the source once with
# MONTNEY_REGION_REBUILD_DAYS=<n> in the environment. The gas and power lines are then built and written over n days.
# Unset (the runner's normal state), the windows above apply. The store is change-only, so a rebuild writes only what
# differs.
REBUILD_ENV = "MONTNEY_REGION_REBUILD_DAYS"
REBUILD_MAX_DAYS = 400


def rebuild_days() -> int:
    raw = os.environ.get(REBUILD_ENV, "").strip()
    if not raw:
        return 0
    n = int(raw)                                  # a typo fails the run loudly rather than silently doing nothing
    if not 0 < n <= REBUILD_MAX_DAYS:
        raise ValueError(f"{REBUILD_ENV} must be 1..{REBUILD_MAX_DAYS} days, not {raw!r}")
    return n


# `gas: through: yesterday` (REGIONS §3): a region whose lines come from pipeline bulletin boards. The boards carry the
# current gas day at its latest intraday cycle, each pipe at a different one, so today's sum is not a balance (upne
# read -550 MMcf/d mid-cycle against +16 for the finished day; Reviewer on #503). Such a region's gas lines are
# written only for gas days that have ended on the NAESB clock: a gas day runs 09:00 to 09:00 Central.
NAESB_TZ = ZoneInfo("America/Chicago")
NAESB_DAY_START_H = 9


def current_gas_day(now_utc: datetime) -> datetime:
    """The gas day in progress at `now_utc` (naive UTC), as its bucket: midnight of the date it started on."""
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(NAESB_TZ) - timedelta(hours=NAESB_DAY_START_H)
    return datetime(local.year, local.month, local.day)


MONTH_LOOKBACK = timedelta(days=62)      # a `fill: month` part needs its month's point, which sits on the 1st
GAS_DAY_OFFSET = timedelta(hours=6)      # L-029: one named fixed UTC-6 offset for Alberta days


def _month(ts: datetime) -> datetime:
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _bucket(family: str, ts: datetime, subdaily: bool) -> datetime:
    if family == "power":
        return ts.replace(minute=0, second=0, microsecond=0)
    if family == "oil":
        return _month(ts)
    if subdaily:
        ts = ts - GAS_DAY_OFFSET
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def bucketed(family: str, part: R.Part, points: dict[datetime, float], buckets: set[datetime] | None
             ) -> dict[datetime, float]:
    """A part's value per bucket (sign and scale applied). With `fill: month`, the monthly value is held across
    `buckets` (the timeline of the line's other parts, or every bucket of the window)."""
    k = part.sign * part.scale
    if part.fill == "month":
        by_month = {_month(t): v for t, v in points.items()}
        return {b: by_month[_month(b)] * k for b in (buckets or ()) if _month(b) in by_month}
    if part.fill == "latest":
        # its own month where it exists, else the newest earlier month held forward (at most LATEST_MAX_DAYS past the
        # end of that month): EIA state data lags 2-3 months, and a held value is always marked est.
        by_month = sorted((_month(t), v) for t, v in points.items())
        out = {}
        for b in buckets or ():
            prior = [(m, v) for m, v in by_month if m <= _month(b)]
            if prior:
                m, v = prior[-1]
                end = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
                if b < end + timedelta(days=R.LATEST_MAX_DAYS):
                    out[b] = v * k
        return out
    if part.fill == "seasonal":
        # its own month where it exists, else the same calendar month of the newest earlier year (at most
        # SEASONAL_MAX_YEARS back): a demand-type flow keeps its season instead of carrying August into November
        by_month = {_month(t): v for t, v in points.items()}
        out = {}
        for b in buckets or ():
            m = _month(b)
            for back in range(0, R.SEASONAL_MAX_YEARS + 1):
                try:
                    cand = m.replace(year=m.year - back)
                except ValueError:
                    continue
                if cand in by_month:
                    out[b] = by_month[cand] * k
                    break
        return out
    subdaily = any(t.hour or t.minute for t in points)
    acc: dict[datetime, list[float]] = defaultdict(list)
    for t, v in points.items():
        acc[_bucket(family, t, subdaily)].append(v)
    return {b: sum(vs) / len(vs) * k for b, vs in acc.items()}


def line_values(family: str, line: R.Line, got: dict[str, dict[datetime, float]], window: set[datetime]
                ) -> dict[datetime, float]:
    """The line's sum per bucket, only where every part has a value. A `none` line is 0 everywhere in `window`."""
    if line.none:
        return {b: 0.0 for b in window}
    direct = [bucketed(family, p, got.get(p.series) or {}, None) for p in line.parts if not p.fill]
    timeline = set.intersection(*(set(d) for d in direct)) if direct else set(window)
    held = [bucketed(family, p, got.get(p.series) or {}, timeline) for p in line.parts if p.fill]
    cols = direct + held
    common = set.intersection(*(set(c) for c in cols)) if cols else set()
    return {b: sum(c[b] for c in cols) for b in common}


def span(reg: R.Region, family: str) -> timedelta:
    """How far back a region's family lines are built and written: the family WINDOW, or the section's `lag:` if that
    is longer. A `fill: month` storage line's newest month (EIA's August, read in November) is older than the 21-day gas
    window, so without this it would never be written (Reviewer on #436)."""
    lines = {"gas": reg.gas, "power": {**reg.power, **reg.fuels}, "oil": reg.oil}[family].values()
    longest = max([reg.lags.get(family, 0), *(ln.lag for ln in lines)])
    rebuild = timedelta(days=rebuild_days()) if family in ("gas", "power") else timedelta(0)
    return max(WINDOW[family], timedelta(seconds=longest), rebuild)


def _window(family: str, end: datetime, length: timedelta | None = None) -> set[datetime]:
    """Every bucket of the trailing window (for `none` lines and lines made only of `fill:` parts)."""
    start = end - (length or WINDOW[family])
    out, t = set(), _bucket(family, start, False)
    step = {"power": timedelta(hours=1), "gas": timedelta(days=1)}.get(family)
    while t <= end:
        out.add(t)
        t = t + step if step else (t.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def region_obs(reg: R.Region, got: dict[str, dict[datetime, float]], now: datetime, as_of: datetime) -> list[Obs]:
    out: list[Obs] = []
    have = {s for s, *_ in R.outputs(reg)}

    def emit(series: str, vals: dict[datetime, float], start: datetime):
        if series in have:
            out.extend(Obs(series, b, round(v, 4), as_of) for b, v in sorted(vals.items()) if start <= b <= now)

    # gas
    gwin = _window("gas", now, span(reg, "gas"))
    g = {k: line_values("gas", ln, got, gwin) for k, ln in reg.gas.items()}
    if reg.gas_through == "yesterday":           # finished gas days only: every line, so the balance is one day's
        today = current_gas_day(now)
        g = {k: {b: v for b, v in vals.items() if b < today} for k, vals in g.items()}
    gstart = now - span(reg, "gas")
    for k in ("production", "imports", "exports", "demand", "storage", "inventory"):
        if k in g and not reg.gas[k].none:
            emit(R.sid(reg.id, "gas", k), g[k], gstart)
    imp, exp = g.get("imports"), g.get("exports")
    if imp is not None and exp is not None:
        emit(R.sid(reg.id, "gas", "net_imports"), {b: imp[b] - exp[b] for b in set(imp) & set(exp)}, gstart)
    stand_in = any(reg.gas[k].stand_in for k in ("production", "imports", "exports", "demand") if k in reg.gas)
    if (R.sid(reg.id, "gas", "storage_implied") in have and "production" in g and "demand" in g
            and (imp is not None or exp is not None) and not stand_in):
        keys = set(g["production"]) & set(g["demand"])
        keys &= set(imp) if imp is not None else keys
        keys &= set(exp) if exp is not None else keys
        implied = {b: g["production"][b] + (imp[b] if imp is not None else 0.0)
                   - (exp[b] if exp is not None else 0.0) - g["demand"][b] for b in keys}
        emit(R.sid(reg.id, "gas", "storage_implied"), implied, gstart)
        if "storage" in g and not reg.gas["storage"].none:
            emit(R.sid(reg.id, "gas", "residual"),
                 {b: implied[b] - g["storage"][b] for b in set(implied) & set(g["storage"])}, gstart)
    # power
    pwin = _window("power", now, span(reg, "power"))
    p = {k: line_values("power", ln, got, pwin) for k, ln in reg.power.items()}
    pstart = now - span(reg, "power")
    for k in ("load", "generation", "interchange"):
        if k in p and not reg.power[k].none:
            emit(R.sid(reg.id, "power", k), p[k], pstart)
    for fuel, ln in reg.fuels.items():
        emit(f"region.{reg.id}.power.gen.{fuel}", line_values("power", ln, got, pwin), pstart)
    if {"load", "generation", "interchange"} <= set(p):
        keys = set(p["load"]) & set(p["generation"]) & set(p["interchange"])
        emit(R.sid(reg.id, "power", "residual"),
             {b: p["generation"][b] + p["interchange"][b] - p["load"][b] for b in keys}, pstart)
    # oil
    if "production" in reg.oil:
        emit(R.sid(reg.id, "oil", "production"),
             line_values("oil", reg.oil["production"], got, _window("oil", now)), now - WINDOW["oil"])
    return out


class RegionBalance(Connector):
    stored = staticmethod(read_stored)       # tests inject a fake
    load_regions = staticmethod(R.load)

    def declared_series(self) -> list[SeriesMeta]:
        metas = []
        for reg in self.load_regions().values():
            for series, label, unit, freq in R.outputs(reg):
                metas.append(SeriesMeta(series, self.id, label, unit, freq, GROUP, {"region": reg.id},
                                        day="gas" if ".gas." in series else ""))
        return metas

    def fetch(self, ctx: Context) -> FetchResult:
        regs = self.load_regions()
        res = FetchResult(meta=self.declared_series())
        now = ctx.now.replace(minute=0, second=0, microsecond=0)
        # Each input is read over its family's window only (a year of 5-minute AESO points would be wasted reads).
        need: dict[str, timedelta] = {}
        for reg in regs.values():
            for family, lines in (("gas", reg.gas.values()), ("power", [*reg.power.values(), *reg.fuels.values()]),
                                  ("oil", reg.oil.values())):
                for ln in lines:
                    for part in ln.parts:
                        back = span(reg, family) + (
                            timedelta(days=R.LATEST_MAX_DAYS + 62) if part.fill == "latest" else
                            timedelta(days=366 * R.SEASONAL_MAX_YEARS + 62) if part.fill == "seasonal" else
                            MONTH_LOOKBACK if part.fill or family == "oil" else timedelta(days=1))
                        need[part.series] = max(need.get(part.series, timedelta(0)), back)
        if not need:
            return res
        got: dict[str, dict[datetime, float]] = {}
        by_back: dict[timedelta, list[str]] = defaultdict(list)
        for series, back in need.items():
            by_back[back].append(series)
        for back, ids in sorted(by_back.items()):
            got.update(self.stored(sorted(ids), now - back, now + timedelta(days=1)))
        for reg in regs.values():
            res.obs += region_obs(reg, got, now, now)
        if not res.obs:
            res.warnings.append("no region line had all its parts stored yet")
        return res
