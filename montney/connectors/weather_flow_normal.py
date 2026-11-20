"""River flow as a % of normal (Erik, L-066): current discharge / the historical median for the same day of year.

    pct = 100 * current / median,  where
    current = the trailing-24 h mean of the provisional real-time series (>= half a day of readings), and
    median  = the median of the stored APPROVED daily means (HYDAT, `wsc.daily.<stn>.discharge`) whose (month, day) lies
              within today's Alberta date +/- 7 days, over ALL stored years (1995 -> the latest approved day).

So the basis is provisional real-time / approved daily means. Each series' label carries the years used, n, and the
median itself, refreshed every run. NO-RESULT (no row written) when the history has fewer than 10 distinct years in the
window, when the median is under 1 m3/s (a % of almost nothing isn't meaningful), or when the real-time data are thin.
Never a guess.

Series (unit "%", stored as a percent: 84 means 84 %):
    wsc.realtime.<stn>.discharge_pct_normal   every WSC gauge with live discharge (weather_wsc)
    abrivers.<stn>.outflow_pct_normal         the TransAlta releases with a like-for-like WSC history station
                                              (weather_wsc.HISTORY_ONLY): Cascade, Ghost, Bearspaw, Barrier, Pocaterra,
                                              Bighorn, Brazeau. Horseshoe and Upper Kananaskis: NO-RESULT (none).
The id is the box row's flow series + "_pct_normal", so the map can pair them without a table.

Reads stored history read-only through gas_common.read_stored (the orchestrator's #162 ruling). The per-gauge median
is cached per Alberta day: the approved history only changes with a HYDAT release.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.connectors.weather_abrivers import STATIONS as AB_STATIONS, series_key
from montney.connectors.weather_percentile import window_keys
from montney.connectors.weather_wsc import GAUGES, HISTORY_ONLY
from montney.store import Obs, SeriesMeta

MIN_YEARS = 10
MIN_MEDIAN = 1.0                    # m3/s
HALF = 7
HIST_START = datetime(1995, 1, 1)   # the stored daily backfill starts here (weather_wsc_daily `start`)
_CACHE: dict[tuple[str, date], tuple[float, int, int, int] | None] = {}   # (wsc stn, day) -> (median, n, y0, y1)


def alberta_date(now: datetime) -> date:
    from zoneinfo import ZoneInfo
    from datetime import timezone
    return now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Edmonton")).date()


def normal(history: dict[datetime, float], day: date, half: int = HALF) -> tuple[float, int, int, int] | None:
    """(median, n, first year, last year) of the history within day +/- half (by month-day, all years), or None."""
    keys = window_keys(day, half)
    vals = [(t.year, v) for t, v in history.items()
            if v is not None and ((2, 28) if (t.month, t.day) == (2, 29) else (t.month, t.day)) in keys]
    years = {y for y, _ in vals}
    if len(years) < MIN_YEARS:
        return None
    med = statistics.median(v for _, v in vals)
    return (med, len(vals), min(years), max(years)) if med >= MIN_MEDIAN else None


def current(points: dict[datetime, float], now: datetime, min_points: int) -> float | None:
    v = [x for t, x in points.items() if now - timedelta(hours=24) < t <= now and x is not None]
    return sum(v) / len(v) if len(v) >= min_points else None


def targets() -> list[tuple[str, str, str, int, str, str]]:
    """(output id, current series, history series, min points in 24 h, display name, history label)."""
    from montney.connectors.weather_usgs import SITES as USGS, rt_id

    out = [(f"wsc.realtime.{s.lower()}.discharge_pct_normal", f"wsc.realtime.{s.lower()}.discharge",
            f"wsc.daily.{s.lower()}.discharge", 144, g[0], f"WSC {s} approved daily means")
           for s, g in GAUGES.items() if g[2]]
    for wsc_stn, (_, _, ab) in HISTORY_ONLY.items():
        k = series_key(ab)
        out.append((f"abrivers.{k}.outflow_pct_normal", f"abrivers.{k}.outflow", f"wsc.daily.{wsc_stn.lower()}.discharge",
                     12, f"{AB_STATIONS[ab][0]} release", f"WSC {wsc_stn} approved daily means"))
    # L-085: the Columbia hydro box (The Dalles, International Boundary). 15-min sites: half a day of readings.
    for s, (name, _, _, _) in USGS.items():
        out.append((f"{rt_id(s)}_pct_normal", rt_id(s), f"usgs.dv.{s}.discharge", 48, name,
                    f"USGS {s} daily means (recent days provisional)"))
    return out


class FlowVsNormal(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(oid, self.id, f"{name}: % of normal", "%", "1h", "Rivers / % of normal")
                for oid, _, _, _, name, _ in targets()]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        tg = targets()
        day = alberta_date(ctx.now)
        for k in [k for k in _CACHE if k[1] != day]:
            del _CACHE[k]
        need = sorted({h for _, _, h, _, _, _ in tg if (h, day) not in _CACHE})
        if need:
            # The last 30 days are never part of their own baseline (USGS daily values run to yesterday; HYDAT lags anyway).
            hist = read_stored(need, HIST_START, ctx.now - timedelta(days=30))
            for h in need:
                _CACHE[(h, day)] = normal(hist.get(h, {}), day)
        cur = read_stored([c for _, c, _, _, _, _ in tg], ctx.now - timedelta(hours=24), ctx.now)
        ts = ctx.now.replace(minute=0, second=0, microsecond=0)
        no_result = []
        for oid, cid, h, min_pts, name, hist_label in tg:
            nm, c = _CACHE.get((h, day)), current(cur.get(cid, {}), ctx.now, min_pts)
            basis = ("24 h mean of the AB Rivers hourly release" if cid.startswith("abrivers.") else
                     "24 h mean of provisional real-time") + f" / median of {hist_label}, {day:%b %d} +/-{HALF} d"
            if nm is None:
                label = f"{name}: % of normal: NO-RESULT ({hist_label}: under {MIN_YEARS} years or a median under {MIN_MEDIAN:g} m3/s)"
                no_result.append(oid)
            else:
                med, n, y0, y1 = nm
                med_s = f"{med:.0f}" if med >= 100 else f"{med:.3g}"
                label = f"{name}: % of normal ({basis}, {y0}-{y1}, n={n}, median {med_s} m3/s)"
                if c is not None:
                    res.obs.append(Obs(oid, ts, round(100.0 * c / med, 1), ts))
            res.meta.append(SeriesMeta(oid, self.id, label, "%", "1h", "Rivers / % of normal"))
        if no_result:
            res.warnings.append(f"% of normal NO-RESULT for {len(no_result)} series (history too short or median < 1 m3/s)")
        return res
