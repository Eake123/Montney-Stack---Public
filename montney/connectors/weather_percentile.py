"""Flow-vs-normal percentile, computed on read (PLAN P5d; method in docs/research/v2/weather-p5d.md §3.2).

No climatology is stored. For a gauge and a day, the sample is that gauge's approved daily means
(`wsc.daily.<stn>.discharge`) in the reference years whose (month, day) falls within the day ±7 days. Today's value is
the trailing-24 h mean of the real-time discharge (`wsc.realtime.<stn>.discharge`), so a peaking release doesn't swing
the colour hourly. The rank is a mid-rank empirical CDF. Fails closed: too few samples or too little real-time data
gives NO-RESULT (p = None, band "no-result", drawn grey).

Pure functions; the read path (snapshot/API, orchestrator-owned) passes in the stored points.
"""

from __future__ import annotations

import threading
from array import array
from datetime import date, datetime, timedelta

YEARS = (1995, 2024)      # fixed reference window; re-base every 5 years (2030 -> 2000-2029)
HALF = 7                  # day-of-year window: ±7 days
MIN_N = 150               # 10 years x 15 days
MIN_RT_POINTS = 144       # half a day of 5-min readings in the trailing 24 h
_REF = 2001               # a non-leap year for the window arithmetic


def doy_key(d: date) -> tuple[int, int]:
    return (2, 28) if (d.month, d.day) == (2, 29) else (d.month, d.day)


def window_keys(d: date, half: int = HALF) -> set[tuple[int, int]]:
    m, dd = doy_key(d)
    c = date(_REF, m, dd)
    return {doy_key(c + timedelta(days=i)) for i in range(-half, half + 1)}


def sample(history: dict, d: date, years: tuple[int, int] = YEARS, half: int = HALF) -> list[float]:
    """Reference values for day `d`. `history` maps date or datetime -> value (None values skipped)."""
    keys = window_keys(d, half)
    return [v for k, v in history.items()
            if v is not None and years[0] <= k.year <= years[1] and doy_key(k) in keys]


def percentile(value: float | None, values: list[float], min_n: int = MIN_N) -> float | None:
    if value is None or len(values) < min_n:
        return None
    below = sum(1 for x in values if x < value)
    equal = sum(1 for x in values if x == value)
    return 100.0 * (below + 0.5 * equal) / len(values)


def band(p: float | None) -> str:
    if p is None:
        return "no-result"
    return "very low" if p < 10 else "low" if p < 25 else "normal" if p <= 75 else "high" if p <= 90 else "very high"


def trailing_mean(points: dict[datetime, float], now: datetime, hours: float = 24,
                  min_points: int = MIN_RT_POINTS) -> float | None:
    """Mean of the real-time points in (now - hours, now]. None (NO-RESULT) below `min_points`."""
    lo = now - timedelta(hours=hours)
    vals = [v for t, v in points.items() if lo < t <= now and v is not None]
    return sum(vals) / len(vals) if len(vals) >= min_points else None


def flow_percentile(history: dict, realtime: dict[datetime, float], now: datetime,
                    today: date | None = None) -> dict:
    """{value, p, band, n} for one gauge. `today` is the day whose window is used (default: now's date)."""
    v = trailing_mean(realtime, now)
    s = sample(history, today or now.date())
    p = percentile(v, s)
    return {"value": v, "p": p, "band": band(p), "n": len(s)}


# ---------------------------------------------------------------- per-gauge state for the snapshot
STALE_S = 3 * 3600            # newest real-time point older than this: "stale" (weather.md §7)
HIST_TTL_S = 24 * 3600        # the reference history (1995-2024) changes only with a HYDAT release: re-read once a day
THIN_TTL_S = 3600             # a station with too little history for ANY day (e.g. before the HYDAT backfill) is re-read
                              # after 1 h, not kept all day
# L-102: station -> (its reference-year daily values by month-day, how many, loaded at). One read per station per
# HIST_TTL_S per process, whatever the date: each day's +/-7-day sample is assembled from memory, so the Alberta-date
# rollover reads nothing (before, the cache was keyed by (station, day) and every new day re-read all 459 k rows).
_SAMPLE_CACHE: dict[str, tuple[dict[tuple[int, int], array], int, float]] = {}
_LOAD_LOCK = threading.Lock()


def _clock() -> float:
    import time
    return time.monotonic()


def _points(d: dict) -> dict[datetime, float]:
    return {datetime(1970, 1, 1) + timedelta(seconds=t): v for t, v in zip(d.get("ts", []), d.get("v", []))}


def _by_doy(d: dict) -> tuple[dict[tuple[int, int], array], int]:
    """A stored series' reference-year values grouped by month-day (Feb 29 with Feb 28, as `sample` does)."""
    idx: dict[tuple[int, int], array] = {}
    n = 0
    for k, v in _points(d).items():
        if v is not None and YEARS[0] <= k.year <= YEARS[1]:
            idx.setdefault(doy_key(k), array("d")).append(v)
            n += 1
    return idx, n


def _samples(store, stations: list[str], today: date) -> dict[str, list[float]]:
    """Reference samples for `today`: the same values as `sample(history, today)`, from the per-station month-day index."""
    now_c = _clock()

    def due(s: str) -> bool:
        c = _SAMPLE_CACHE.get(s)
        if c is None:
            return True
        age = now_c - c[2]
        return age > HIST_TTL_S or (c[1] < MIN_N and age > THIN_TTL_S)

    if any(due(s) for s in stations):
        # One loader at a time (L-102 outage): without the lock, every snapshot build that started while the cache was
        # empty (server restart) did its own 459 k-row read on the one shared connection, and the queue never drained.
        # Waiters re-check after the lock, so only the first does the read.
        with _LOAD_LOCK:
            missing = [s for s in stations if due(s)]
            if missing:
                ids = [f"wsc.daily.{s.lower()}.discharge" for s in missing]
                data = store.series(ids, start=datetime(YEARS[0], 1, 1) - timedelta(days=HALF),
                                    end=datetime(YEARS[1], 12, 31) + timedelta(days=HALF))
                loaded = _clock()
                for s, sid in zip(missing, ids):
                    _SAMPLE_CACHE[s] = (*_by_doy(data.get(sid, {})), loaded)
    keys = window_keys(today)
    return {s: [v for k in keys for v in _SAMPLE_CACHE[s][0].get(k, ())] for s in stations}


def gauge_states(store, now: datetime, today: date) -> dict[str, dict]:
    """Flow-vs-normal for every WSC gauge with discharge, as the snapshot exposes it (PLAN P5d).

    `today` is the Alberta date (the day whose ±7-day window is used). Per gauge:
      {"series", "value", "ts", "p", "band", "n", "state", "reason"}
    state: "ok" (p computed) | "seasonal" (a seasonal gauge out of its open-water window, with no data: "seasonal,
    no data", never an error) | "stale" (no real-time point in the last 3 h) | "no-result" (data, but too thin for a
    percentile: never a guess). p and band are set only when state is "ok"; consumers can rely on that.
    """
    from montney.connectors.weather_wsc import GAUGES, in_season

    stations = [s for s, g in GAUGES.items() if g[2]]
    samples = _samples(store, stations, today)
    rt_ids = [f"wsc.realtime.{s.lower()}.discharge" for s in stations]
    rt = store.series(rt_ids, start=now - timedelta(hours=24), end=now)
    out = {}
    for s, sid in zip(stations, rt_ids):
        pts = _points(rt.get(sid, {}))
        last = max(pts) if pts else None
        v = trailing_mean(pts, now)
        n = len(samples[s])
        p = percentile(v, samples[s], MIN_N)      # module value at call time
        quiet = last is None or (now - last).total_seconds() > STALE_S
        if quiet and not in_season(s, today):
            state, reason = "seasonal", "seasonal gauge: no data outside its open-water season"
        elif quiet:
            state, reason = "stale", "no real-time discharge in the last 3 h"
        elif p is None:
            state = "no-result"
            reason = (f"history n={n} < {MIN_N}" if n < MIN_N
                      else f"fewer than {MIN_RT_POINTS} real-time points in 24 h")
        else:
            state, reason = "ok", None
        if state != "ok":
            p = None          # a stale or thin gauge never shows a percentile
        out[s] = {"series": sid, "value": v, "ts": last.isoformat() + "Z" if last else None,
                  "p": p, "band": band(p), "n": n, "state": state, "reason": reason}
    return out


def snapshot_section(store, now: datetime, today: date) -> dict:
    """`gauge_states`, but a failure here never blanks the rest of the map snapshot: it's reported, not hidden."""
    import logging

    try:
        return gauge_states(store, now, today)
    except Exception as e:  # noqa: BLE001 - reported in the payload and the log
        logging.getLogger(__name__).warning("gauges: %s: %s", type(e).__name__, e)
        return {"_error": f"{type(e).__name__}: {e}"}
