"""Read-only series access for other apps and agents (Krasnodar D29): `list_series()` and
`series(metric, start, end, freq)`, served as plain HTTP (`GET /api/read/*`) and as the read-only MCP `/mcp/read`
(montney/readmcp.py). Contract: docs/READ_API.md.

A curated allowlist of headline metrics named by Montney's own series ids, not the whole store. Every answer is
computed on request from what Montney already stores: no collection, no timers, no outside requests, no writes.

Days are America/Edmonton days. Montney's daily rollups of the 5-min and hourly tiers (obs_fast_1d, obs_hourly_1d)
are bucketed at a fixed UTC-6 (L-029). Alberta has been permanently UTC-6 since 2026-11-01, so that is exactly the
Edmonton day; before then a winter (MST, UTC-7) day is cut 1 h early. Documented, not re-cut.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from montney.connectors.base import utcnow

TZ = "America/Edmonton"
MAX_DAYS = 5 * 366                    # at most 5 years per call (D29)
DAY_OFFSET = timedelta(hours=6)       # the rollups' Alberta day: [D 06:00 UTC, D+1 06:00 UTC)
MIN_COVERAGE = 0.9                    # freq=day: a day needs >= 90 % of its samples, so partial days are left out
_GAS_DAY = ("gas day: the NGTL gas day that starts 09:00 Alberta time (15:00 UTC) on the date shown; "
            "latest NGTL Gas Day Summary value (NGTL revises recent days)")
_AESO = "AESO Current Supply Demand, 5-min samples; daily = mean of the Alberta day's samples (>= 90 % present)"


class ReadError(ValueError):
    """A bad request: unknown metric, bad dates, a freq the metric doesn't offer, or too long a range."""


# metric (= Montney series id) -> how it is served. `tier`: where it is read from - "fast" / "hourly" are daily
# means of the rollups, "daily" / "weekly" the stored points as they are. `per_day`: samples in a full day.
METRICS: dict[str, dict] = {
    "aeso.price.pool": dict(
        tier="hourly", per_day=24, unit="$/MWh", freqs=["day"],
        description="Alberta pool price (AESO). Daily = simple time mean of the day's 24 hourly prices "
                    "(not load-weighted); >= 90 % of hours present."),
    "aeso.csd.ail": dict(
        tier="fast", per_day=288, unit="MW", freqs=["day"],
        description="Alberta internal load. " + _AESO + "."),
    "aeso.gen.gas_total.net": dict(
        tier="fast", per_day=288, unit="MW", freqs=["day"],
        description="Alberta gas-fired net generation (all gas types). " + _AESO + "."),
    "aeso.gen.wind.net": dict(
        tier="fast", per_day=288, unit="MW", freqs=["day"],
        description="Alberta wind net generation. " + _AESO + "."),
    "aeso.gen.solar.net": dict(
        tier="fast", per_day=288, unit="MW", freqs=["day"],
        description="Alberta solar net generation. " + _AESO + "."),
    "ngtl.gdsr.field_receipts.flow": dict(
        tier="daily", unit="MMcf/d", freqs=["day"],
        description="NGTL field receipts (gas entering NGTL from Alberta/NE BC fields): the best daily proxy for "
                    "WCSB production on NGTL. " + _GAS_DAY + "."),
    "ngtl.gdsr.intraprovincial.flow": dict(
        tier="daily", unit="MMcf/d", freqs=["day"],
        description="NGTL intraprovincial deliveries: Alberta demand served by NGTL (utilities, industry, "
                    "oil sands, power). " + _GAS_DAY + "."),
    "ngtl.gdsr.empress.flow": dict(
        tier="daily", unit="MMcf/d", freqs=["day"],
        description="NGTL deliveries at the Empress border, east into the TC Mainline. " + _GAS_DAY + "."),
    "ngtl.gdsr.mcneill.flow": dict(
        tier="daily", unit="MMcf/d", freqs=["day"],
        description="NGTL deliveries at the McNeill border, south into Foothills / Northern Border (US). "
                    + _GAS_DAY + "."),
    "ngtl.gdsr.net_storage.flow": dict(
        tier="daily", unit="MMcf/d", freqs=["day"],
        description="NGTL net storage flow, Alberta storage: + = net withdrawal, - = net injection. "
                    + _GAS_DAY + "."),
    "ngtl.gdsr.eod_linepack.flow": dict(
        tier="daily", unit="MMcf", freqs=["day"],
        description="NGTL end-of-gas-day linepack (gas held in the pipe). " + _GAS_DAY + "."),
    "eia.storage.lower48.working_gas": dict(
        tier="weekly", unit="Bcf", freqs=["week"],
        description="US Lower 48 working gas in storage (EIA Weekly Natural Gas Storage Report). Weekly: one "
                    "point per report week, dated as stored (the week's date in EIA's table); freq=week only."),
    "wx.calgary.tmean.realized": dict(
        tier="daily", unit="degC", freqs=["day"],
        description="Calgary daily mean temperature, realized (Open-Meteo model analysis for days that have "
                    "ended; settles about a week later). The day is Calgary's local day."),
    "wx.calgary.hdd.realized": dict(
        tier="daily", unit="degC-d", freqs=["day"],
        description="Calgary heating degree days, base 18 degC, realized (from the daily mean above)."),
}

HOW_TO_USE = f"""Montney Stack read-only series (Krasnodar D29). Two calls:
- list_series(): the metrics, each with description (incl. how its day is aggregated), unit, first_date,
  last_date and freqs. freqs is the truth: a weekly-only metric refuses freq="day".
- series(metric, start, end, freq="day"): sorted [["YYYY-MM-DD", number], ...] for {TZ} days, start/end
  inclusive ISO dates, at most 5 years per call. Days without a value are left out (never zero-filled); for the
  daily means of 5-min/hourly data, a day needs >= 90 % of its samples, so today is never included.
Values are the latest stored vintage for each date: a past value can change on a later call (NGTL, EIA revise),
and as_of is the newest fetch behind the answer. Actuals only: no forecasts. Gas flows are MMcf/d, linepack MMcf,
storage Bcf, power MW, price $/MWh. NGTL metrics are gas days, dated by the day the gas day starts.
Day boundaries: the 5-min/hourly daily means use a fixed UTC-6 day; Alberta is UTC-6 all year since 2026-11-01,
so before then a winter day is cut 1 h early. No gas prices (AECO, Station 2) are in Montney.
Data is licensed for Erik's personal use: don't publish it."""


def _parse_day(s: str, name: str) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        raise ReadError(f"{name} must be an ISO date (YYYY-MM-DD), not {s!r}") from None


def _metric(metric: str) -> dict:
    m = METRICS.get(metric)
    if m is None:
        raise ReadError(f"unknown metric {metric!r}; list_series() names the {len(METRICS)} metrics")
    return m


def _round(v: float) -> float:
    return round(float(v), 3)


def _day_means(store, metric: str, m: dict, start: date, end: date) -> tuple[list, str | None]:
    """[(date, mean)] for complete days of a 5-min/hourly series, and the newest as_of behind them."""
    lo = datetime.combine(start, datetime.min.time()) + DAY_OFFSET
    hi = datetime.combine(end, datetime.min.time()) + DAY_OFFSET
    need = MIN_COVERAGE * m["per_day"]
    if hasattr(store, "day_rollup"):                      # Postgres: the obs_*_1d rollups (kept beyond raw retention)
        rows, as_of = store.day_rollup(metric, m["tier"], lo, hi)
        pts = [((ts - DAY_OFFSET).date(), s / n) for ts, s, n in rows if n and n >= need]
        return pts, as_of
    # DuckDB (dev / tests): the same rule over the raw points
    d = store.series([metric], lo, hi + timedelta(days=1) - timedelta(microseconds=1)).get(metric) or {}
    acc: dict[date, list] = {}
    for t, v in zip(d.get("ts", []), d.get("v", [])):
        if v is None:
            continue
        day = (datetime(1970, 1, 1) + timedelta(seconds=t) - DAY_OFFSET).date()
        a = acc.setdefault(day, [0.0, 0])
        a[0] += v
        a[1] += 1
    pts = [(k, s / n) for k, (s, n) in sorted(acc.items()) if n >= need and start <= k <= end]
    return pts, d.get("as_of")


def _stored(store, metric: str, start: date, end: date) -> tuple[list, str | None]:
    """[(date, value)] of a daily/weekly series as stored (latest vintage per date)."""
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end, datetime.min.time())
    if hasattr(store, "daily_points"):                    # Postgres: obs_daily alone, not the all-tier union
        rows, as_of = store.daily_points(metric, lo, hi)
        return [(t.date(), v) for t, v in rows if v is not None], as_of
    d = store.series([metric], lo, hi).get(metric) or {}
    pts = [((datetime(1970, 1, 1) + timedelta(seconds=t)).date(), v)
           for t, v in zip(d.get("ts", []), d.get("v", [])) if v is not None]
    return pts, d.get("as_of")


def _points(store, metric: str, m: dict, start: date, end: date) -> tuple[list, str | None]:
    if m["tier"] in ("fast", "hourly"):
        return _day_means(store, metric, m, start, end)
    return _stored(store, metric, start, end)


def series(store, metric: str, start: str, end: str, freq: str = "day") -> dict:
    m = _metric(metric)
    if freq not in m["freqs"]:
        raise ReadError(f"{metric} offers freq {m['freqs']}, not {freq!r} (list_series().freqs is the truth)")
    d0, d1 = _parse_day(start, "start"), _parse_day(end, "end")
    if d1 < d0:
        raise ReadError(f"end {d1} is before start {d0}")
    if (d1 - d0).days + 1 > MAX_DAYS:
        raise ReadError(f"at most 5 years per call ({MAX_DAYS} days); ask for {d0}..{d1} in pieces")
    pts, as_of = _points(store, metric, m, d0, d1)
    source = (store.meta([metric]).get(metric) or {}).get("source")
    return {"metric": metric, "unit": m["unit"], "tz": TZ, "freq": freq,
            "points": [[k.isoformat(), _round(v)] for k, v in pts], "source": source, "as_of": as_of}


def list_series(store, today: date | None = None) -> list[dict]:
    """Every metric with its first/last date (over the stored history, at most the last ~20 years)."""
    today = today or (utcnow() - DAY_OFFSET).date()
    out = []
    for metric, m in METRICS.items():
        if hasattr(store, "series_bounds") and m["tier"] in ("daily", "weekly"):
            first, last = store.series_bounds(metric)
        else:
            pts, _ = _points(store, metric, m, today - timedelta(days=20 * 366), today)
            first, last = (pts[0][0], pts[-1][0]) if pts else (None, None)
        out.append({"metric": metric, "description": m["description"], "unit": m["unit"],
                    "first_date": first.isoformat() if first else None,
                    "last_date": last.isoformat() if last else None, "freqs": list(m["freqs"])})
    return out
