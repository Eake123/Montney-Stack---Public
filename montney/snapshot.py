"""Current state of every network feature in one call (what the map polls every 60 s).

For each feature that references series (`value`, `capacity`), return the latest value, a
comparison value per the feature's compare rule, and a staleness flag:

    {"as_of": iso, "values": {feature_id: {"v", "ts", "prev", "prev_ts", "cap", "cap_ts",
                                           "unit", "freq", "compare", "stale", "series"}},
     "series": {series_id: {...same shape...}},   # for ?extra= ids (callouts not tied to a node)
     "gauges": {station: {"series", "value", "ts", "p", "band", "n", "state", "reason"}}}   # WSC flow-vs-normal (P5d)

Compare rules: "prev" = previous point (the right default for daily/gas-day series),
"1h" / "24h" / "7d" = value at or before latest minus that lag.
A feature's optional `target` series is returned as target/target_ts (at or before the value's ts).

Window means (#150 contract, L-033): a feature's `agg` property, or an `?extra=` token `<series>@1h` /
`<series>@24h`, adds to its entry
    "agg": {"window": "1h"|"24h", "start", "end" (epoch s), "mean", "n", "n_min", "reason"}
= the mean of the series' points with start < ts <= end, where end = now and start = now - window. It is computed here, from the
same latest-vintage points store.series() returns, so DuckDB and Postgres give the same numbers by construction. (The
rollups obs_fast_1h/_1d count every stored vintage, so a revised point would be counted twice; a rolling 24 h window sits
well inside raw retention.) Thin data is a NO-RESULT, never a mean of a few points dressed as a window: n must reach
n_min = ceil(AGG_MIN_COVERAGE x the points the series' freq implies over the window), else mean is None and reason
says why. A daily (or coarser than the window) series has no window mean.
"Current" means ts <= now (intraday), ts <= the current NGTL gas day for gas-day series (series meta
day == "gas"; rolls at 15:00 UTC), or ts <= today's Alberta date for other daily series. The first point after
that is returned as next/next_ts (and cap_next/cap_next_ts), e.g. tomorrow's DOP capability.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney.connectors.base import DEFAULT_TZ, utcnow
from montney import regions
from montney.connectors import weather_percentile

# NGTL gas day D starts 08:00 MST = a fixed 15:00 UTC (unchanged by Alberta's 2026-11-01 switch to
# permanent UTC-6, so 09:00 local). Source: TC bulletin 3374725064, cited in
# montney/connectors/ngtl_gasday.py. Daily series in this app are keyed by that gas day.
GAS_DAY_START_UTC_HOUR = 15


def alberta_date(now_utc: datetime) -> date:
    """Alberta's calendar date at the naive-UTC instant `now_utc`. Derived from the SAME instant as the gas day, never
    from a second clock read: a separate local_today() made calendar cutoffs follow the real clock while `now` was frozen
    (FLAKE_HISTORY 2026-11-01)."""
    return now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(DEFAULT_TZ)).date()


def current_gas_day(now_utc: datetime):
    return (now_utc - timedelta(hours=GAS_DAY_START_UTC_HOUR)).date()
from montney.store import Store

FREQ_SECONDS = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "30min": 1800,
                "1h": 3600, "h": 3600, "d": 86400, "w": 7 * 86400,   # weekly: EIA storage regions (Gas #405, L-106)
                "m": 31 * 86400,   # monthly (`freq: M`): stale only after ~3 months, not 3 hours (#160)
                "a": 366 * 86400}  # annual (`freq: A`, StatCan capacity: PLAN 9e): stale after ~3 years / its stale_after
LAG = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
AGG_WINDOW = {"1h": 3600, "24h": 86400}
# Sources that publish late by design: a value is stale only after this many seconds (not the default 3 × freq).
# rivers.alberta.ca releases are hourly but 2-5 h old when published (Weather, #144/#234), so hourly-stale froze every dam reach.
STALE_AFTER_BY_SOURCE = {"weather_abrivers": 6 * 3600, "weather_wsc": 5 * 3600, "power_statcan_gen": 130 * 86400,
                         "weather_usgs": 3 * 3600, "weather_columbia_hydro": 7 * 86400, "power_hq_hourly": 6 * 3600,
                         "gas_statcan_onqc": 130 * 86400, "gas_eia_midwest": 130 * 86400, "gas_eia_mist": 130 * 86400,
                         "gas_eia_states": 130 * 86400, "oil_statcan_provinces": 130 * 86400,
                         "gas_eia_storage_weekly": 14 * 86400,
                         "power_eia930_demand": 4 * 3600, "power_eia930_gen": 48 * 3600,
                         "power_bch_load": 5 * 86400, "power_saskpower": 3 * 86400,
                         "power_ieso_zonal": 48 * 3600}     # IESO zonal demand: the YTD file lands the next morning (Power #437)
# WSC real-time 1.5-4.4 h late; StatCan monthly gen ~2 months late (L-078); USGS 15-min fetched hourly (L-081) + ~1 h latency;
# USBR Hydromet storage posts 3-5+ days behind (Grand Coulee lag; Reviewer on #338) and NWRFC water-supply forecasts update ~daily (L-085);
# Hydro-Québec hourly production posts ~3 h behind (Power #361, L-091); EIA monthly state gas (Midwest L-095, PacNW Mist) ~3 months behind
# too (August posted Oct 30: ~94 d old on Nov 3); StatCan monthly Ontario/Québec gas is ~3 months
# behind like power_statcan_gen (Frontend on #364). L-106 (Gas #405): gas_eia_states (every state's monthly gas + crude)
# and oil_statcan_provinces (~3 months, as above); gas_eia_storage_weekly posts Thursdays for the week before (< 14 d).
# EIA-930 (Power #409): demand is 1-2 h behind (+ 1 h interval); gen by fuel and interchange post daily, <= 38 h behind.
# BC Hydro load posts weekdays, 1-3.5 days late; SaskPower one daily mean ~1.5 days late (Power #428)
# L-107: a source now says this itself, `stale_after: 4h` in its spec (docs/SOURCES.md), so a new feed doesn't need an
# edit here. The table above stays for the sources that predate it; a spec's value wins over the table.
_SPEC_STALE: dict[str, int] | None = None


def source_stale_after(source: str) -> int:
    """Seconds after which a sub-daily value from this source reads stale, when its source publishes late by design:
    the spec's `stale_after:` (90m / 4h / 2d), else the table above, else 0 (the default rule, 3 x freq)."""
    global _SPEC_STALE
    if _SPEC_STALE is None:
        from montney import connectors
        found: dict[str, int] = {}
        try:
            for sid, spec in connectors.load_specs(include_disabled=True).items():
                if spec.get("stale_after") is not None:
                    found[sid] = regions.parse_lag(spec["stale_after"], f"{sid}.stale_after")
        except Exception:               # an unreadable spec must not take the snapshot down: fall back to the table
            found = {}
        _SPEC_STALE = found
    return _SPEC_STALE.get(source) or STALE_AFTER_BY_SOURCE.get(source, 0)


MONTHLY_LOOKBACK_DAYS = 130     # > the 93-day monthly stale threshold, so a stale month still has a value to tag
ANNUAL_LOOKBACK_DAYS = 1200     # an annual point is stamped 1 Jan and lands ~10 months later (StatCan): ~3 years back
AGG_MIN_COVERAGE = 0.75         # n floor: 75 % of the points the series' freq implies over the window
_AGG_END_SHIFT_S = 0            # seeded-red switch: a window ending an hour early must be caught (tests)


def _freq_s(freq: str | None) -> int:
    return FREQ_SECONDS.get((freq or "").lower(), 3600)


def _value_at_or_before(ts: list[float], v: list, t: float):
    best = None
    for i in range(len(ts) - 1, -1, -1):
        if ts[i] <= t:
            best = i
            break
    return (v[best], ts[best]) if best is not None else (None, None)


def _cutoff(meta: dict, now_epoch: float, gasday_epoch: float, calday_epoch: float) -> float:
    """Latest timestamp that counts as 'current'. Daily series are dated at 00:00 of their day:
    gas-day series (meta day == "gas": NGTL GDSR/DOP, Westcoast) roll over at 15:00 UTC = 09:00
    Alberta; other daily series (weather, AESO outages) use the Alberta calendar date. Never
    UTC now (after 18:00 MT the UTC date is already tomorrow)."""
    if (meta.get("freq") or "").upper() != "D":
        return now_epoch
    return gasday_epoch if meta.get("day") == "gas" else calday_epoch


def window_mean(s: dict, meta: dict, agg: str, now_epoch: float) -> dict:
    """The mean over (now - window, now] with its n, n floor and window, or mean None with a reason (module doc)."""
    import math

    end = now_epoch - _AGG_END_SHIFT_S
    out = {"window": agg, "start": None, "end": end, "mean": None, "n": 0, "n_min": None, "reason": None}
    if agg not in AGG_WINDOW:
        out["reason"] = f"unknown window {agg!r} (one of {sorted(AGG_WINDOW)})"
        return out
    w = AGG_WINDOW[agg]
    out["start"] = end - w
    freq = meta.get("freq") or ""
    f = 86400 if freq.upper() == "D" else _freq_s(freq)
    if f >= w:
        out["reason"] = f"freq {freq or '?'} is not finer than the {agg} window: no window mean"
        return out
    pts = [v for t, v in zip(s["ts"], s["v"]) if end - w < t <= end and v is not None]
    n_min = max(2, math.ceil(AGG_MIN_COVERAGE * w / f))
    out["n"], out["n_min"] = len(pts), n_min
    if len(pts) < n_min:
        out["reason"] = (f"thin: n={len(pts)} < {n_min} ({AGG_MIN_COVERAGE:.0%} of the {w // f} points a {freq} series "
                         f"has in {agg})")
        return out
    out["mean"] = sum(pts) / len(pts)
    return out


def summarize(s: dict, meta: dict, compare: str | None, now_epoch: float,
              cutoff: float | None = None, sid: str = "") -> dict:
    cutoff = now_epoch if cutoff is None else cutoff
    k = len(s["ts"])
    while k and s["ts"][k - 1] > cutoff:
        k -= 1
    nxt = (s["v"][k], s["ts"][k]) if k < len(s["ts"]) else (None, None)
    ts, v = s["ts"][:k], s["v"][:k]
    freq = meta.get("freq") or ""
    daily = freq.upper() == "D"
    rule = compare or ("prev" if daily else "24h")
    out = {"v": None, "ts": None, "prev": None, "prev_ts": None, "unit": meta.get("unit", ""),
           "freq": freq, "day": (meta.get("day") or "calendar") if daily else "",
           "compare": rule, "stale": True, "next": nxt[0], "next_ts": nxt[1]}
    if not ts:
        return out
    out["v"], out["ts"] = v[-1], ts[-1]
    if rule == "prev":
        if len(ts) > 1:
            out["prev"], out["prev_ts"] = v[-2], ts[-2]
    else:
        out["prev"], out["prev_ts"] = _value_at_or_before(ts, v, ts[-1] - LAG[rule])
    if daily:
        # A gas day is published the next morning; stale if the newest day is > 3 days old.
        out["stale"] = now_epoch - ts[-1] > max(3 * 86400 + 86400, regions.stale_after(sid))   # + a region's `lag:`
    else:
        out["stale"] = now_epoch - ts[-1] > max(3 * _freq_s(freq), 3600, source_stale_after(meta.get("source", "")),
                                                regions.stale_after(sid))   # a region section's declared `lag` (L-106)
    return out


SHORT_WINDOW_H = 30        # sub-daily series: the latest point, its 24 h compare and the 1 h/24 h means fit in 30 h
LONG_WINDOW_DAYS = 15      # daily series, and any sub-daily series whose needed point isn't in the short window


def _subdaily(meta: dict) -> bool:
    f = (meta.get("freq") or "").lower()
    return f in FREQ_SECONDS and FREQ_SECONDS[f] < 86400


def _needs(refs: dict, compares: dict, aggs: dict | None, extra_tokens: list[str]) -> dict[str, dict]:
    """What summarize / window_mean / the capacity and target lookups read from each series: `lag` (s behind its own
    latest point), `prev` (its two latest points) and `at` (the value series it's read at: capacity and target)."""
    need: dict[str, dict] = {}

    def add(sid: str, lag: float = 0.0, prev: bool = False, at: str | None = None) -> None:
        n = need.setdefault(sid, {"lag": 0.0, "prev": False, "at": set()})
        n["lag"], n["prev"] = max(n["lag"], lag), n["prev"] or prev
        if at:
            n["at"].add(at)

    for fid, r in refs.items():
        if "value" in r:
            rule = compares.get(fid) or "24h"
            add(r["value"], max(LAG.get(rule, 0), AGG_WINDOW.get((aggs or {}).get(fid), 0)), prev=rule == "prev")
        for k in ("capacity", "target"):
            if k in r:
                add(r[k], at=r.get("value"))
    for tok in extra_tokens:
        sid, _, win = tok.partition("@")
        add(sid, max(LAG["24h"], AGG_WINDOW.get(win, 0)))
    return need


def _last_at_or_before(ts: list[float], t: float) -> int:
    k = len(ts)
    while k and ts[k - 1] > t:
        k -= 1
    return k          # number of points <= t


def _read_window(store: Store, ids: list[str], meta: dict, cut: dict, need: dict, now: datetime) -> dict:
    """Reading 15 days of every series was ~100k rows of 5-min data per build for one latest value each (L-102).
    Sub-daily series read SHORT_WINDOW_H. The short window runs contiguously to `end`, so any point it holds at or before
    a time t is the same point a 15-day read finds; a series re-reads 15 days only when a point it needs (its compare
    point, its two latest, or its value at the value's timestamp) isn't in the short window. Output is unchanged."""
    end = now + timedelta(days=3)
    short = [sid for sid in ids if _subdaily(meta.get(sid, {}))]
    long_ = [sid for sid in ids if sid not in set(short)]
    data = store.series(short, start=now - timedelta(hours=SHORT_WINDOW_H), end=end) if short else {}
    if long_:
        data.update(store.series(long_, start=now - timedelta(days=LONG_WINDOW_DAYS), end=end))

    def latest(sid: str) -> float | None:
        if sid not in data:
            return None
        k = _last_at_or_before(data[sid]["ts"], cut.get(sid, float("inf")))
        return data[sid]["ts"][k - 1] if k else None

    redo = []
    for sid in short:
        ts, n = data[sid]["ts"], need.get(sid, {"lag": 0.0, "prev": False, "at": set()})
        k = _last_at_or_before(ts, cut[sid])
        ok = k >= (2 if n["prev"] else 1) and (_last_at_or_before(ts, ts[k - 1] - n["lag"]) > 0 if k else False)
        for v in n["at"]:            # capacity/target: the point at or before the value's ts, and at the cutoff
            tv = latest(v)
            if tv is None and v in set(short):   # the value's own latest may be older than the short window
                ok = False
            ok = ok and _last_at_or_before(ts, tv if tv is not None else cut[sid]) > 0
        if not ok:
            redo.append(sid)
    if redo:
        data.update(store.series(redo, start=now - timedelta(days=LONG_WINDOW_DAYS), end=end))
    return data


FAR_LOOKBACK_DAYS = 400         # latest_far: a monthly line held to the end of its month, a feed that's weeks late
FAR_LOOKBACK_SUBDAILY_DAYS = 14


def latest_far(store: Store, ids: list[str]) -> dict[str, dict]:
    """The newest stored point of series that have nothing in the snapshot's normal window (15 days daily, 30 hours
    sub-daily): a storage line built from a monthly value is written to the end of its month, months ago (Frontend on
    #530: 7 regions' measured-storage rows had a series and no value). Read further back and summarized by the same
    rules, so `stale` is the same judgement. Only series with a point come back."""
    if not ids:
        return {}
    now = utcnow()
    now_epoch = (now - datetime(1970, 1, 1)).total_seconds()
    ep = lambda d: (datetime(d.year, d.month, d.day) - datetime(1970, 1, 1)).total_seconds()  # noqa: E731
    gasday_epoch, calday_epoch = ep(current_gas_day(now)), ep(alberta_date(now))
    meta = store.meta(ids)
    out: dict[str, dict] = {}
    for sub, days in ((True, FAR_LOOKBACK_SUBDAILY_DAYS), (False, FAR_LOOKBACK_DAYS)):
        batch = [s for s in ids if s in meta and _subdaily(meta[s]) == sub]
        if not batch:
            continue
        data = store.series(batch, start=now - timedelta(days=days), end=now + timedelta(days=3))
        for sid in batch:
            s = data.get(sid) or {}
            if s.get("ts"):
                m = meta[sid]
                e = summarize(s, m, None, now_epoch, _cutoff(m, now_epoch, gasday_epoch, calday_epoch), sid=sid)
                if e.get("v") is not None:
                    out[sid] = e
    return out


def build(store: Store, refs: dict[str, dict[str, str]], compares: dict[str, str],
          extra: list[str] | None = None, units: dict[str, str] | None = None,
          aggs: dict[str, str] | None = None, gauges: bool = True) -> dict:
    """`aggs`: feature id -> "1h"|"24h" (a feature's `agg` property). `extra` items may be "<series>@<window>".
    `gauges=False` leaves out the WSC flow-vs-normal section (a click's detail snapshot doesn't need it)."""
    now = utcnow()
    now_epoch = (now - datetime(1970, 1, 1)).total_seconds()
    ep = lambda d: (datetime(d.year, d.month, d.day) - datetime(1970, 1, 1)).total_seconds()
    gasday_epoch, calday_epoch = ep(current_gas_day(now)), ep(alberta_date(now))
    extra_tokens = list(extra or [])
    extra_sids = {t: t.split("@", 1)[0] for t in extra_tokens}
    ids = sorted({sid for r in refs.values() for sid in r.values()} | set(extra_sids.values()))
    # Fetch a little past now so the next forward point (e.g. DOP capability for tomorrow, Westcoast
    # capacity for D+1) is available as `next`; summarize() never reports a future point as current.
    meta = store.meta(ids) if ids else {}
    # Monthly series (freq M) publish weeks after the month ends, so their latest point is ~2 months old: read them over
    # MONTHLY_LOOKBACK_DAYS, or a fresh month would be outside the 15-day window and read as stale (step 4b, #189 review).
    monthly = [sid for sid in ids if (meta.get(sid, {}).get("freq") or "").upper() == "M"]
    annual = [sid for sid in ids if (meta.get(sid, {}).get("freq") or "").upper() == "A"]
    others = [sid for sid in ids if sid not in set(monthly) | set(annual)]
    cut = {sid: _cutoff(meta.get(sid, {}), now_epoch, gasday_epoch, calday_epoch) for sid in ids}
    data = _read_window(store, others, meta, cut, _needs(refs, compares, aggs, extra_tokens), now) if others else {}
    if monthly:
        data.update(store.series(monthly, start=now - timedelta(days=MONTHLY_LOOKBACK_DAYS), end=now + timedelta(days=3)))
    if annual:                  # PLAN 9e: annual capacity (StatCan) read nothing in the 130-day monthly window
        data.update(store.series(annual, start=now - timedelta(days=ANNUAL_LOOKBACK_DAYS), end=now + timedelta(days=3)))
    values = {}
    for fid, r in refs.items():
        entry = {"series": r.get("value")}
        if "value" in r:
            entry.update(summarize(data[r["value"]], meta.get(r["value"], {}),
                                   compares.get(fid), now_epoch, cut[r["value"]], sid=r["value"]))
            if units and units.get(fid):
                entry["unit"] = units[fid]
            if aggs and aggs.get(fid):
                entry["agg"] = window_mean(data[r["value"]], meta.get(r["value"], {}), aggs[fid], now_epoch)
        if "capacity" in r:
            cs = data[r["capacity"]]
            ref_t = entry.get("ts") if entry.get("ts") is not None else cut[r["capacity"]]
            entry["cap"], entry["cap_ts"] = _value_at_or_before(cs["ts"], cs["v"], ref_t)
            # Flow is often published a day behind capacity (e.g. NGTL DOP), so cap at the value's
            # ts would never show today's capability; cap_current is the newest one at the cutoff.
            entry["cap_current"], entry["cap_current_ts"] = _value_at_or_before(
                cs["ts"], cs["v"], cut[r["capacity"]])
            later = [(t, v) for t, v in zip(cs["ts"], cs["v"]) if t > cut[r["capacity"]]]
            entry["cap_next"], entry["cap_next_ts"] = (later[0][1], later[0][0]) if later else (None, None)
        if "target" in r:
            ts_ = data[r["target"]]
            ref_t = entry.get("ts") if entry.get("ts") is not None else cut[r["target"]]
            entry["target"], entry["target_ts"] = _value_at_or_before(ts_["ts"], ts_["v"], ref_t)
        values[fid] = entry
    series = {}
    for tok in extra_tokens:
        sid = extra_sids[tok]
        e = summarize(data[sid], meta.get(sid, {}), None, now_epoch, cut[sid], sid=sid)
        if "@" in tok:
            e["agg"] = window_mean(data[sid], meta.get(sid, {}), tok.split("@", 1)[1], now_epoch)
        series[tok] = e
    # P5d: flow-vs-normal per WSC gauge, computed on read (Weather's module; the day window is the Alberta date).
    g = weather_percentile.snapshot_section(store, now, alberta_date(now)) if gauges else {}
    return {"as_of": now.isoformat() + "Z", "values": values, "series": series, "gauges": g}
