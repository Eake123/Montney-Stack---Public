"""WSC gauges (montney/connectors/weather_wsc.py) and the on-read flow percentile (weather_percentile.py).

Mocked OGC API payloads in the real response shape (GeoJSON features, `properties` as in docs/research/v2/weather.md
§3.1). Seeded reds: the datum factor, the null padding, the page cap, a gauge failure, LST parsing, and the
percentile's fail-closed paths.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import weather_wsc as wsc
from montney.connectors import weather_percentile as wp
from montney.connectors.base import Context

NOW = datetime(2026, 11, 1, 2, 0)
SPECS = load_specs(include_disabled=True)


def feature(stn, **props):
    return {"type": "Feature", "geometry": None, "properties": {"STATION_NUMBER": stn, **props}}


# real-time rows (values from the 2026-11-01 01:45Z pulls); DATETIME_LST is present but must be ignored
RT = {
    "07FA004": [feature("07FA004", DATETIME="2026-11-01T01:40:00Z", DATETIME_LST="2026-10-31T17:40:00-08:00",
                        LEVEL=1.34, DISCHARGE=1080.0),
                feature("07FA004", DATETIME="2026-11-01T01:45:00Z", DATETIME_LST="2026-10-31T17:45:00-08:00",
                        LEVEL=1.346, DISCHARGE=None)],
    "07EF002": [feature("07EF002", DATETIME="2026-11-01T00:25:00Z", LEVEL=39.286, DISCHARGE=None)],
    "05BH004": [feature("05BH004", DATETIME="2026-11-01T01:05:00Z", DATETIME_LST="2026-10-31T18:05:00-07:00",
                        LEVEL=0.945, DISCHARGE=58.0),
                feature("05BH005", DATETIME="2026-11-01T01:05:00Z", LEVEL=1.0, DISCHARGE=58.9)],  # other station
}


def handler_factory(rt=RT, daily=None, fail=(), seen=None):
    def h(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        stn = q.get("STATION_NUMBER") or q.get("bbox")       # real-time is queried by region bbox (L-081)
        if seen is not None:
            seen.append((req.url.path.split("/")[2], stn, dict(q)))
        if stn in fail:
            return httpx.Response(404, text="no such station")   # 4xx: no retry backoff in tests
        if "hydrometric-realtime" in req.url.path:
            feats = [f for rows in rt.values() for f in rows]     # every region sees every fixture row
        else:
            rows = (daily or {}).get(stn, [])
            off, lim = int(q["offset"]), int(q["limit"])
            feats = rows[off:off + lim]
        return httpx.Response(200, json={"type": "FeatureCollection", "features": feats,
                                         "numberReturned": len(feats)})
    return h


def ctx(h):
    return Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=NOW)


def rt_spec(stations):
    return {"id": "weather_wsc", "type": "python", "params": {"stations": stations, "lookback_h": 6}}


# ---------------------------------------------------------------- real-time
def test_realtime_series_units_and_datum(monkeypatch):
    seen = []
    res = wsc.WSCRealtime(rt_spec(["07FA004", "07EF002", "05BH004"])).fetch(ctx(handler_factory(seen=seen)))
    got = {(o.series_id, o.ts): o for o in res.obs}
    t = datetime(2026, 11, 1, 1, 40)
    assert got[("wsc.realtime.07fa004.discharge", t)].value == 1080.0
    assert got[("wsc.realtime.07fa004.elevation", t)].value == pytest.approx(1.34 + 405.891)   # HYDAT factor
    assert ("wsc.realtime.07fa004.discharge", datetime(2026, 11, 1, 1, 45)) not in got         # null Q skipped
    assert got[("wsc.realtime.07ef002.elevation", datetime(2026, 11, 1, 0, 25))].value == pytest.approx(669.304)
    assert got[("wsc.realtime.05bh004.level", datetime(2026, 11, 1, 1, 5))].value == 0.945      # gauge datum
    assert not any(o.series_id.startswith("wsc.realtime.05bh005") for o in res.obs)            # other station
    assert all(o.as_of == o.ts for o in res.obs)
    # UTC from DATETIME, never LST: 05BH004's LST is 18:05 -07:00, the stored ts must be 01:05
    assert ("wsc.realtime.05bh004.discharge", datetime(2026, 11, 1, 1, 5)) in got
    # one request per region (L-081), with the lookback window; overlapping regions don't duplicate rows
    assert [s[1] for s in seen] == [",".join(f"{x:g}" for x in r) for r in wsc.REGIONS]
    assert seen[0][2]["datetime"] == "2026-10-31T20:00:00Z/.."
    assert len(got) == len(res.obs)
    meta = {m.series_id: m for m in res.meta}
    assert meta["wsc.realtime.07fa004.discharge"].unit == "m3/s" and meta["wsc.realtime.07fa004.discharge"].freq == "5min"
    assert "CGVD2013" in meta["wsc.realtime.07ef002.elevation"].label
    assert "wsc.realtime.07ef002.discharge" not in meta                                         # lake: level only


def test_seeded_red_williston_without_its_factor_is_caught(monkeypatch):
    """Drop 07EF002's HYDAT factor: the series becomes a ~39 m `level`, and the elevation check must fail."""
    monkeypatch.setitem(wsc.GAUGES, "07EF002", wsc.GAUGES["07EF002"][:3] + (None,))
    res = wsc.WSCRealtime(rt_spec(["07EF002"])).fetch(ctx(handler_factory()))
    (o,) = res.obs
    assert o.series_id == "wsc.realtime.07ef002.level" and o.value == pytest.approx(39.286)
    assert not o.value == pytest.approx(669.304)


def test_one_failing_region_is_a_warning_all_failing_raises():
    regions = [",".join(f"{x:g}" for x in r) for r in wsc.REGIONS]
    res = wsc.WSCRealtime(rt_spec(["07FA004", "05BH004"])).fetch(ctx(handler_factory(fail={regions[0]})))
    assert any(w.startswith("bbox ") for w in res.warnings)
    assert {o.series_id.split(".")[2] for o in res.obs} == {"07fa004", "05bh004"}     # the other regions still answer
    with pytest.raises(RuntimeError, match="every WSC region failed"):
        wsc.WSCRealtime(rt_spec(["07FA004"])).fetch(ctx(handler_factory(fail=set(regions))))


def test_unknown_station_is_rejected():
    with pytest.raises(ValueError, match="unknown WSC station"):
        wsc.WSCRealtime(rt_spec(["99ZZ999"]))


# ---------------------------------------------------------------- daily means
def daily_rows(stn, start, n, null_tail=0):
    d0 = date.fromisoformat(start)
    rows = [feature(stn, DATE=(d0 + timedelta(days=i)).isoformat(), DISCHARGE=1000.0 + i + 0.0000001)
            for i in range(n)]
    return rows + [feature(stn, DATE=(d0 + timedelta(days=n + i)).isoformat(), DISCHARGE=None)
                   for i in range(null_tail)]


def test_daily_pages_nulls_rounding(monkeypatch):
    monkeypatch.setattr(wsc, "PAGE", 3)
    seen = []
    spec = {"id": "weather_wsc_daily", "type": "python", "params": {"stations": ["07FD002", "07EF002"],
                                                                     "start": "1995-01-01"}}
    res = wsc.WSCDaily(spec).fetch(ctx(handler_factory(daily={"07FD002": daily_rows("07FD002", "2025-10-20", 5, 2)},
                                                       seen=seen)))
    vals = {o.ts: o.value for o in res.obs}
    assert len(vals) == 5 and vals[datetime(2025, 10, 20)] == 1000.0      # float32 noise rounded off
    assert max(vals) == datetime(2025, 10, 24)                           # null padding dropped
    assert all(o.as_of == o.ts and o.series_id == "wsc.daily.07fd002.discharge" for o in res.obs)
    assert [s[1] for s in seen] == ["07FD002"] * 3                       # 07EF002 has no Q; 7 rows / 3 per page
    assert [s[2]["offset"] for s in seen] == ["0", "3", "6"] and seen[0][2]["datetime"] == "1995-01-01/.."


def test_seeded_red_page_cap_never_truncates_silently(monkeypatch):
    monkeypatch.setattr(wsc, "PAGE", 2)
    monkeypatch.setattr(wsc, "MAX_PAGES", 2)
    spec = {"id": "weather_wsc_daily", "type": "python", "params": {"stations": ["07FD002"]}}
    with pytest.raises(RuntimeError, match="more than 4 rows"):
        wsc.WSCDaily(spec).fetch(ctx(handler_factory(daily={"07FD002": daily_rows("07FD002", "2025-01-01", 9)})))


def test_shipped_specs():
    rt, dm = build(SPECS["weather_wsc"]), build(SPECS["weather_wsc_daily"])
    rt_ids = {m.series_id for m in rt.declared_series()}
    dm_ids = {m.series_id for m in dm.declared_series()}
    assert len(wsc.GAUGES) == 54 and len(rt_ids) == 100 and len(dm_ids) == 53
    assert {"wsc.realtime.07ef003.elevation", "wsc.realtime.07fa004.discharge", "wsc.realtime.05bh004.level",
            "wsc.realtime.05dd007.discharge"} <= rt_ids
    assert "wsc.daily.07ef002.discharge" not in dm_ids and "wsc.daily.07fa004.discharge" in dm_ids
    assert SPECS["weather_wsc"]["retention"] == "fast" and SPECS["weather_wsc_daily"]["retention"] == "daily"
    # the 4 stations with a HYDAT CGVD2013 factor (DECISIONS P5d)
    assert {s for s, g in wsc.GAUGES.items() if g[3] is not None} == {"07EF002", "07EF003", "07FA004", "07FD002",
                                                                     "08ND026", "08NE104", "08NH064"}


# ---------------------------------------------------------------- percentile (compute on read)
def history(value_of_year):
    d, out = date(1990, 1, 1), {}
    while d <= date(2025, 12, 31):
        out[datetime(d.year, d.month, d.day)] = value_of_year(d.year)
        d += timedelta(days=1)
    return out


def rt_points(value, hours=24, step_min=5):
    return {NOW - timedelta(minutes=step_min * i): value for i in range(int(hours * 60 / step_min))}


def test_flow_percentile_against_the_reference_window():
    h = history(lambda y: float(y))
    r = wp.flow_percentile(h, rt_points(2010.0), NOW, today=date(2026, 6, 15))
    assert r["n"] == 450 and r["p"] == pytest.approx(100 * (15 * 15 + 7.5) / 450) and r["band"] == "normal"
    assert wp.flow_percentile(h, rt_points(1e6), NOW)["band"] == "very high"
    assert wp.flow_percentile(h, rt_points(0.0), NOW)["band"] == "very low"


def test_window_wraps_and_feb_29():
    assert (12, 26) in wp.window_keys(date(2027, 1, 2)) and len(wp.window_keys(date(2027, 1, 2))) == 15
    assert wp.doy_key(date(2024, 2, 29)) == (2, 28)


def test_seeded_red_no_result_paths():
    h = history(lambda y: 1.0)
    thin = rt_points(1.0, hours=6)                                        # 72 points < 144
    assert wp.trailing_mean(thin, NOW) is None
    assert wp.flow_percentile(h, thin, NOW)["band"] == "no-result"
    seasonal = {d: v for d, v in h.items() if 5 <= d.month <= 10}
    r = wp.flow_percentile(seasonal, rt_points(1.0), NOW, today=date(2027, 1, 15))
    assert r["n"] == 0 and r["p"] is None and r["band"] == "no-result"
    stale = {t - timedelta(days=2): v for t, v in rt_points(1.0).items()}  # nothing in the last 24 h
    assert wp.trailing_mean(stale, NOW) is None


# ---------------------------------------------------------------- snapshot gauge states (stored data, compute on read)
@pytest.fixture
def three_gauges(monkeypatch, tmp_path):
    from montney.store import Obs, Store

    monkeypatch.setattr(wsc, "GAUGES", {k: wsc.GAUGES[k] for k in ("07FD002", "05BH005", "05BH004")})
    wp._SAMPLE_CACHE.clear()
    st = Store(tmp_path / "t.duckdb")
    rows = []
    d = date(1995, 1, 1)
    while d <= date(2024, 12, 31):          # 07FD002 + 05BH005 full 30 years (value = year), 05BH004 only 2020+
        ts = datetime(d.year, d.month, d.day)
        rows += [Obs("wsc.daily.07fd002.discharge", ts, float(d.year), ts),
                 Obs("wsc.daily.05bh005.discharge", ts, float(d.year), ts)]
        if d.year >= 2020:
            rows.append(Obs("wsc.daily.05bh004.discharge", ts, float(d.year), ts))
        d += timedelta(days=1)
    st.write(rows)
    yield st
    st.close()
    wp._SAMPLE_CACHE.clear()


def rt_obs(stn, value, end, hours=24):
    return [Obs_(f"wsc.realtime.{stn.lower()}.discharge", end - timedelta(minutes=5 * i), value)
            for i in range(int(hours * 12))]


def Obs_(sid, ts, v):
    from montney.store import Obs
    return Obs(sid, ts, v, ts)


def test_gauge_states_ok_seasonal_no_result(three_gauges):
    st, now = three_gauges, datetime(2026, 11, 2, 18, 0)
    st.write(rt_obs("07FD002", 2010.0, now)                                # live, mid-range
             + rt_obs("05BH004", 46.4, now)                                # live, but only 5 years of history
             + rt_obs("05BH005", 58.9, datetime(2026, 11, 1, 6, 55), hours=2))   # went quiet 06:55Z Nov 1
    g = wp.gauge_states(st, now, date(2026, 11, 2))
    assert g["07FD002"]["state"] == "ok" and g["07FD002"]["n"] == 450 and g["07FD002"]["band"] == "normal"
    assert g["07FD002"]["p"] == pytest.approx(100 * (15 * 15 + 7.5) / 450)
    assert g["05BH004"]["state"] == "no-result" and g["05BH004"]["p"] is None      # n = 75 < 150: never a guess
    assert "n=75 < 150" in g["05BH004"]["reason"] and g["05BH004"]["band"] == "no-result"
    assert g["05BH005"]["state"] == "seasonal" and g["05BH005"]["p"] is None       # not "stale" / error
    assert g["05BH005"]["series"] == "wsc.realtime.05bh005.discharge"


def test_seasonal_gauge_in_season_without_data_is_stale(three_gauges):
    g = wp.gauge_states(three_gauges, datetime(2026, 7, 1, 18, 0), date(2026, 7, 1))
    assert g["05BH005"]["state"] == "stale"                                        # a real gap in July
    assert g["07FD002"]["state"] == "stale"


def test_snapshot_exposes_gauges(three_gauges, monkeypatch):
    from montney import snapshot

    now = datetime(2026, 11, 2, 18, 0)
    monkeypatch.setattr(snapshot, "utcnow", lambda: now)
    three_gauges.write(rt_obs("07FD002", 2010.0, now))
    snap = snapshot.build(three_gauges, {}, {})
    assert set(snap["gauges"]) == {"07FD002", "05BH005", "05BH004"}
    assert snap["gauges"]["07FD002"]["state"] == "ok" and snap["gauges"]["05BH005"]["state"] == "seasonal"


def test_a_gauge_failure_never_blanks_the_snapshot(monkeypatch, tmp_path):
    from montney import snapshot
    from montney.store import Store

    st = Store(tmp_path / "t.duckdb")
    monkeypatch.setattr(wp, "gauge_states", lambda *a: (_ for _ in ()).throw(RuntimeError("reader down")))
    snap = snapshot.build(st, {}, {})
    assert snap["gauges"] == {"_error": "RuntimeError: reader down"} and "values" in snap
    st.close()


def test_seeded_red_seasonal_rule_off_reads_stale(three_gauges, monkeypatch):
    """Without the seasonal window, the winter-quiet 05BH005 would read "stale" (an error). The check must see it."""
    monkeypatch.setattr(wsc, "SEASONAL", {})
    g = wp.gauge_states(three_gauges, datetime(2026, 11, 2, 18, 0), date(2026, 11, 2))
    assert g["05BH005"]["state"] == "stale"


def test_seeded_red_thin_history_would_guess(three_gauges, monkeypatch):
    """Drop the n >= 150 floor and 05BH004 gets a percentile from 5 years: the no-result rule is what prevents that."""
    now = datetime(2026, 11, 2, 18, 0)
    three_gauges.write(rt_obs("05BH004", 46.4, now))
    monkeypatch.setattr(wp, "MIN_N", 0)
    g = wp.gauge_states(three_gauges, now, date(2026, 11, 2))
    assert g["05BH004"]["state"] == "ok" and g["05BH004"]["p"] is not None       # a guess from n = 75


def test_stale_gauge_never_shows_a_percentile(three_gauges):
    """A gauge with a full 24 h of data that ended 4 h ago reads stale, and p is None (Reviewer, #142)."""
    now = datetime(2026, 11, 2, 18, 0)
    three_gauges.write(rt_obs("07FD002", 2010.0, now - timedelta(hours=4), hours=20))
    g = wp.gauge_states(three_gauges, now, date(2026, 11, 2))["07FD002"]
    assert g["state"] == "stale" and g["p"] is None and g["band"] == "no-result"
    assert g["value"] == 2010.0          # the real 24 h mean is still reported; only the percentile is withheld


def test_thin_sample_is_reread_after_the_ttl(monkeypatch, tmp_path):
    """Before the HYDAT backfill, the sample is empty. It must not stick for the whole day (Reviewer, #142)."""
    from montney.store import Store

    monkeypatch.setattr(wsc, "GAUGES", {"07FD002": wsc.GAUGES["07FD002"]})
    wp._SAMPLE_CACHE.clear()
    clock = [1000.0]
    monkeypatch.setattr(wp, "_clock", lambda: clock[0])
    st = Store(tmp_path / "t.duckdb")
    today, now = date(2026, 11, 2), datetime(2026, 11, 2, 18, 0)
    st.write(rt_obs("07FD002", 2010.0, now))
    assert wp.gauge_states(st, now, today)["07FD002"]["n"] == 0                 # backfill not run yet
    d, rows = date(1995, 1, 1), []
    while d <= date(2024, 12, 31):                                              # ...the backfill lands
        ts = datetime(d.year, d.month, d.day)
        rows.append(Obs_("wsc.daily.07fd002.discharge", ts, float(d.year)))
        d += timedelta(days=1)
    st.write(rows)
    clock[0] += 60
    assert wp.gauge_states(st, now, today)["07FD002"]["n"] == 0                 # within the TTL: cached
    clock[0] += wp.THIN_TTL_S
    g = wp.gauge_states(st, now, today)["07FD002"]
    assert g["n"] == 450 and g["state"] == "ok"                                 # re-read after the TTL
    clock[0] += 10 * wp.THIN_TTL_S
    monkeypatch.setattr(st, "series", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-read a full sample")))
    assert len(wp._samples(st, ["07FD002"], today)["07FD002"]) == 450          # a full history doesn't expire hourly,
    assert len(wp._samples(st, ["07FD002"], date(2026, 11, 3))["07FD002"]) == 450   # a new day reads nothing (L-102)
    clock[0] += wp.HIST_TTL_S
    with pytest.raises(AssertionError, match="re-read a full sample"):
        wp._samples(st, ["07FD002"], today)                                     # ...until the daily refresh
    st.close()
    wp._SAMPLE_CACHE.clear()


def test_indexed_sample_equals_the_full_history_filter(monkeypatch, tmp_path):
    """L-102: the per-station month-day index must give exactly `sample(history, day)` (the reviewed P5d definition),
    including the windows that cross New Year and Feb 29, for any day, from one read."""
    from montney.store import Store

    wp._SAMPLE_CACHE.clear()
    st = Store(tmp_path / "t.duckdb")
    d, rows, hist = date(1994, 12, 1), [], {}
    while d <= date(2025, 1, 31):                         # one value a day, all distinct, a little outside YEARS too
        ts = datetime(d.year, d.month, d.day)
        hist[ts] = float(d.toordinal())
        rows.append(Obs_("wsc.daily.07fd002.discharge", ts, hist[ts]))
        d += timedelta(days=1)
    st.write(rows)
    for day in (date(2026, 1, 1), date(2026, 1, 3), date(2028, 2, 29), date(2026, 3, 1), date(2026, 11, 4),
                date(2026, 12, 29), date(2026, 12, 31)):
        assert sorted(wp._samples(st, ["07FD002"], day)["07FD002"]) == sorted(wp.sample(hist, day)), day
    st.close()
    wp._SAMPLE_CACHE.clear()


def test_seeded_red_thin_sample_cached_all_day(monkeypatch, tmp_path):
    """With an infinite TTL (the pre-review behaviour), the post-backfill read never happens."""
    from montney.store import Store

    monkeypatch.setattr(wsc, "GAUGES", {"07FD002": wsc.GAUGES["07FD002"]})
    monkeypatch.setattr(wp, "THIN_TTL_S", float("inf"))
    wp._SAMPLE_CACHE.clear()
    st = Store(tmp_path / "t.duckdb")
    today, now = date(2026, 11, 2), datetime(2026, 11, 2, 18, 0)
    assert wp.gauge_states(st, now, today)["07FD002"]["n"] == 0
    st.write([Obs_("wsc.daily.07fd002.discharge", datetime(y, 11, 2), 1.0) for y in range(1995, 2025)])
    assert wp.gauge_states(st, now, today)["07FD002"]["n"] == 0                 # stuck: the bug the TTL fixes
    st.close()
    wp._SAMPLE_CACHE.clear()


class _SlowStore:
    """A store whose history read takes a while and counts how often it ran (the L-102 outage shape)."""
    def __init__(self):
        self.reads = 0

    def series(self, ids, start=None, end=None):
        import time
        self.reads += 1
        time.sleep(0.2)
        t = (datetime(2001, 11, 4) - datetime(1970, 1, 1)).total_seconds()
        return {i: {"ts": [t], "v": [1.0]} for i in ids}


def _herd(store, n=8):
    import threading
    ts = [threading.Thread(target=wp._samples, args=(store, ["07FD002"], date(2026, 11, 4))) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def test_concurrent_cold_builds_read_the_history_once():
    """L-102 outage: 8 snapshot builds that start on an empty cache (a server restart) must share ONE history read."""
    wp._SAMPLE_CACHE.clear()
    st = _SlowStore()
    _herd(st)
    assert st.reads == 1
    wp._SAMPLE_CACHE.clear()


def test_seeded_red_no_lock_reads_once_per_build(monkeypatch):
    import contextlib
    wp._SAMPLE_CACHE.clear()
    monkeypatch.setattr(wp, "_LOAD_LOCK", contextlib.nullcontext())
    st = _SlowStore()
    _herd(st)
    assert st.reads > 1                       # the herd: every cold build did its own read
    wp._SAMPLE_CACHE.clear()
