"""Phase 0 contracts: events, derive weights, POST, network model, snapshot, grids, API."""

import base64
import json
import struct
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from montney import grids, network
from montney.connectors.base import Context, utcnow
from montney.connectors.http import HttpConnector
from montney.store import Event, Obs, Store


def ctx(handler):
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=utcnow())


# ------------------------------------------------------------------ events
def test_events_upsert_keeps_first_seen(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    e = Event("ngtl_dop:123", "ngtl_dop", "outage", "Jenner maintenance",
              starts=datetime(2026, 11, 1, 14), ends=datetime(2026, 11, 7, 14),
              refs=["gas.ngtl.egat"], severity="warn")
    st.upsert_events([e])
    first = st.events()[0]["first_seen"]
    e.title = "Jenner maintenance (revised)"
    st.upsert_events([e])
    rows = st.events()
    assert len(rows) == 1 and rows[0]["title"].endswith("(revised)")
    assert rows[0]["first_seen"] == first
    assert st.events(ref="gas.ngtl.egat") and not st.events(ref="gas.nope")
    with pytest.raises(ValueError):
        st.upsert_events([Event("x:1", "x", "gossip", "t")])


def test_events_since_only_on_change_and_withdrawal(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    a = Event("dop:1", "ngtl_dop", "outage", "A", starts=datetime(2026, 11, 1), ends=datetime(2026, 11, 3))
    b = Event("dop:2", "ngtl_dop", "outage", "B", starts=datetime(2026, 11, 5), ends=datetime(2026, 11, 6))
    st.upsert_events([a, b], complete_scopes=[("ngtl_dop", "outage")])
    mark = datetime.fromisoformat(st.events()[0]["updated"])
    import time; time.sleep(1.1)
    st.upsert_events([a, b], complete_scopes=[("ngtl_dop", "outage")])   # identical
    assert st.events(since=mark) == []
    st.upsert_events([a], complete_scopes=[("ngtl_dop", "outage")])      # b cancelled
    changed = st.events(since=mark)
    assert [(c["id"], c["status"]) for c in changed] == [("dop:2", "withdrawn")]
    assert [x["id"] for x in st.events()] == ["dop:1"]                   # withdrawn hidden
    st.upsert_events([a, b], complete_scopes=[("ngtl_dop", "outage")])   # reinstated
    assert {x["id"] for x in st.events()} == {"dop:1", "dop:2"}


def test_events_states(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    now = datetime(2026, 11, 2, 12)
    st.upsert_events([
        Event("e:past", "s", "outage", "past", starts=datetime(2026, 10, 1), ends=datetime(2026, 10, 2)),
        Event("e:now", "s", "outage", "now", starts=datetime(2026, 11, 1), ends=datetime(2026, 11, 3)),
        Event("e:soon", "s", "outage", "soon", starts=datetime(2026, 11, 9), ends=datetime(2026, 11, 10)),
        Event("e:undated", "s", "bulletin", "undated", published=datetime(2026, 11, 2, 0)),  # TTL 48 h
    ])
    ids = lambda st_, at=now: sorted(x["id"] for x in st.events(state=st_, at=at))
    assert ids("ongoing") == ["e:now", "e:undated"]
    assert ids("upcoming") == ["e:soon"]
    assert ids("current") == ["e:now", "e:soon", "e:undated"]
    assert ids("past") == ["e:past"]


# ---------------------------------------------------------- derive / POST
def test_derive_weights_and_post_json():
    seen = {}

    def handler(req):
        seen["method"], seen["body"] = req.method, json.loads(req.content)
        return httpx.Response(200, json={"rows": [{"t": "2026-10-31 12:00", "cc": 3000, "sc": 100}]})

    spec = {"id": "x", "request": {"url": "https://e.x/api", "method": "POST",
                                   "json": {"day": "{today}"}},
            "extract": [{"path": "rows", "ts": {"field": "t", "tz": "UTC"}, "id": "p.{key}",
                         "unit": "MW", "fields": {"cc": {}, "sc": {}}}],
            "derive": [{"id": "p.burn", "weights": {"p.cc": 0.15, "p.sc": 0.2}, "unit": "MMcf/d"}]}
    res = HttpConnector(spec).run(ctx(handler))
    assert seen["method"] == "POST" and len(seen["body"]["day"]) == 10
    burn = [o for o in res.obs if o.series_id == "p.burn"][0]
    assert burn.value == pytest.approx(3000 * 0.15 + 100 * 0.2)


# ----------------------------------------------------------------- network
def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _node(fid, **p):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-110.0, 50.6]},
            "properties": {"id": fid, "kind": "node", "system": "gas", "type": "border",
                           "name": "Empress", "source": "CER key points CSV", **p}}


def test_network_validation(tmp_path):
    (tmp_path / "gas").mkdir()
    good = _node("gas.empress", value="ngtl.gdsr.empress.flow")
    link = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-118, 55], [-110, 50.6]]},
            "properties": {"id": "gas.ngtl.mainline", "kind": "link", "system": "gas", "type": "pipeline",
                           "name": "NGTL", "source": "schematic: key points", "schematic": True}}
    (tmp_path / "gas" / "a.geojson").write_text(json.dumps(_fc(good, link)))
    fc = network.load(tmp_path)
    assert [f["id"] for f in fc["features"]] == ["gas.empress", "gas.ngtl.mainline"]
    assert network.series_refs(fc) == {"gas.empress": {"value": "ngtl.gdsr.empress.flow"}}

    bad = _node("Gas Empress!")
    dup = _node("gas.empress")
    nolink = dict(link, properties={**link["properties"], "id": "gas.x"})
    del nolink["properties"]["schematic"]
    (tmp_path / "gas" / "b.geojson").write_text(json.dumps(_fc(bad, dup, nolink)))
    with pytest.raises(network.NetworkError) as e:
        network.load(tmp_path)
    msg = str(e.value)
    assert "id must match" in msg and "duplicate id" in msg and "schematic" in msg


def test_repo_network_files_are_valid():
    network.load()  # strict: CI fails on any invalid feature in network/


# ---------------------------------------------------------------- snapshot
def test_snapshot_compare_rules(tmp_path):
    from montney import snapshot
    st = Store(tmp_path / "t.duckdb")
    now = utcnow().replace(minute=0, second=0)
    st.write([Obs("ngtl.csr.empress", now - timedelta(hours=h), 4000 + h, now - timedelta(hours=h))
              for h in range(0, 30)])
    d1, d2 = (now - timedelta(days=2)).replace(hour=0), (now - timedelta(days=1)).replace(hour=0)
    st.write([Obs("ngtl.gdsr.empress.flow", d1, 4500, d1), Obs("ngtl.gdsr.empress.flow", d2, 4600, d2)])
    from montney.store import SeriesMeta
    st.upsert_meta([SeriesMeta("ngtl.csr.empress", "ngtl_csr", "E", "MMcf/d", "30min"),
                    SeriesMeta("ngtl.gdsr.empress.flow", "ngtl_gdsr", "E", "MMcf/d", "D")])
    snap = snapshot.build(st, {"gas.empress": {"value": "ngtl.csr.empress"},
                               "gas.empress.gd": {"value": "ngtl.gdsr.empress.flow"}},
                          {}, extra=["ngtl.csr.empress"])
    a = snap["values"]["gas.empress"]
    assert snap["values"]["gas.empress.gd"]["v"] == 4600
    assert a["v"] == 4000 and a["prev"] == 4024 and a["compare"] == "24h" and not a["stale"]
    b = snap["values"]["gas.empress.gd"]
    assert b["v"] == 4600 and b["prev"] == 4500 and b["compare"] == "prev"
    assert snap["series"]["ngtl.csr.empress"]["v"] == 4000


# ------------------------------------------------------------------- grids
def _grid(run, nx=3, ny=2, times=("2026-11-01T00:00:00Z",)):
    vals = list(range(len(times) * nx * ny))
    return {"var": "t2m", "unit": "degC", "model": "test", "run": run, "fetched": run,
            "lon0": -135.0, "lat0": 45.0, "dx": 0.25, "dy": 0.25, "nx": nx, "ny": ny,
            "times": list(times), "scale": 0.01, "offset": 0, "nodata": -32768,
            "data": base64.b64encode(struct.pack(f"<{len(vals)}h", *vals)).decode()}


def test_grid_save_rotates_and_validates(tmp_path):
    grids.save(_grid("2026-10-31T06:00:00Z"), tmp_path)
    grids.save(_grid("2026-10-31T12:00:00Z"), tmp_path)
    same = _grid("2026-10-31T12:00:00Z"); same["fetched"] = "2026-10-31T13:00:00Z"
    grids.save(same, tmp_path)  # same run re-fetched: new file, prev unchanged
    assert len(list(tmp_path.glob("t2m.*.json.gz"))) == 2
    cat = {(c["var"], c["prev"]): c["run"] for c in grids.catalog(tmp_path)}
    assert cat[("t2m", False)] == "2026-10-31T12:00:00Z"
    assert cat[("t2m", True)] == "2026-10-31T06:00:00Z"
    bad = _grid("x")
    bad["nx"] = 4
    with pytest.raises(grids.GridError):
        grids.save(bad, tmp_path)


# --------------------------------------------------------------------- API
def test_map_api(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from montney.server import create_app
    st = Store(tmp_path / "t.duckdb")
    st.upsert_events([Event("a:1", "a", "bulletin", "Weekend border capability")])
    grids.save(_grid("2026-10-31T12:00:00Z"))
    with TestClient(create_app(store=st, schedule=False)) as c:
        r = c.get("/api/network")
        assert r.status_code == 200 and r.json()["type"] == "FeatureCollection"
        assert c.get("/api/network", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        assert "values" in c.get("/api/snapshot").json()
        assert c.get("/api/events?kind=bulletin").json()[0]["title"].startswith("Weekend")
        g = c.get("/api/grid/t2m")
        assert g.status_code == 200 and g.json()["nx"] == 3
        assert c.get("/api/grid/u10").status_code == 404
        assert c.get("/static/map.js").headers["content-type"].startswith("text/javascript")


def _freeze_clock(monkeypatch, instant_utc: datetime):
    """Freeze the one clock that both utcnow() and local_today() read (base.datetime.now), so the
    UTC instant and the Alberta date come from the shipped code, not from the test."""
    import montney.connectors.base as base

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            aware = instant_utc.replace(tzinfo=timezone.utc)
            return aware.astimezone(tz) if tz is not None else aware.replace(tzinfo=None)

    monkeypatch.setattr(base, "datetime", _Frozen)


# (frozen UTC instant, its Alberta calendar date written by hand). None = the real clock.
# 03:00Z is the 00-06Z window where the UTC date is a day ahead of Alberta (UTC-6): this test was
# red on main in that window (docs/ci/FLAKE_HISTORY.md, 2026-11-01) because it built "today" in UTC.
_SNAPSHOT_CLOCKS = {
    "real-clock": None,
    "00-06Z-window": (datetime(2026, 11, 1, 3, 0), date(2026, 10, 31)),
    "after-06Z": (datetime(2026, 11, 1, 18, 0), date(2026, 11, 1)),
}


@pytest.mark.parametrize("clock", list(_SNAPSHOT_CLOCKS))
def test_snapshot_ignores_future_points_and_caps_at_value_time(tmp_path, monkeypatch, clock):
    from montney import snapshot
    from montney.store import SeriesMeta
    if _SNAPSHOT_CLOCKS[clock] is None:
        ab = datetime.now(ZoneInfo("America/Edmonton")).date()
    else:
        instant, ab = _SNAPSHOT_CLOCKS[clock]
        _freeze_clock(monkeypatch, instant)
    # Calendar-daily series are cut off at today's *Alberta* date, so "today" is that date.
    today = datetime(ab.year, ab.month, ab.day)
    st = Store(tmp_path / "t.duckdb")
    rows = []
    for d in range(-3, 180):          # DOP-style capability runs ~6 months forward
        t = today + timedelta(days=d)
        rows.append(Obs("ngtl.dop.egat.capability", t, 5000 + d, t))
    for d in range(-3, 1):            # actual flow only up to today
        t = today + timedelta(days=d)
        rows.append(Obs("ngtl.dop.egat.flow", t, 4500 + d, t))
    st.write(rows)
    st.upsert_meta([SeriesMeta("ngtl.dop.egat.capability", "x", "", "MMcf/d", "D"),
                    SeriesMeta("ngtl.dop.egat.flow", "x", "", "MMcf/d", "D")])
    snap = snapshot.build(st, {"gas.ngtl.egat": {"value": "ngtl.dop.egat.flow",
                                                 "capacity": "ngtl.dop.egat.capability"}}, {})
    e = snap["values"]["gas.ngtl.egat"]
    assert e["v"] == 4500 and e["cap"] == 5000 and e["cap_ts"] == e["ts"]
    assert e["cap_next"] == 5001                      # tomorrow's capability for a badge
    assert e["cap_current"] == 5000


def test_snapshot_cap_current_when_flow_lags(tmp_path, monkeypatch):
    from montney import snapshot
    from montney.store import SeriesMeta
    # Freeze BOTH clocks (utcnow and local_today read base.datetime): patching only snapshot.utcnow left the calendar
    # cutoff on the real Alberta date, so this went red once the real date passed 2026-10-31 (FLAKE_HISTORY 2026-11-01).
    _freeze_clock(monkeypatch, datetime(2026, 10, 31, 21, 0))   # 15:00 MDT Oct 31: calendar day Oct 31
    st = Store(tmp_path / "t.duckdb")
    st.write([Obs("ngtl.dop.egat.flow", datetime(2026, 10, 30), 5332.5, datetime(2026, 10, 30))])
    st.write([Obs("ngtl.dop.egat.capability", datetime(2026, 10, d), v, datetime(2026, 10, d))
              for d, v in ((30, 5226.6), (31, 5650.5))]
             + [Obs("ngtl.dop.egat.capability", datetime(2026, 11, 1), 5120.6, datetime(2026, 11, 1))])
    st.upsert_meta([SeriesMeta("ngtl.dop.egat.flow", "x", "", "MMcf/d", "D"),
                    SeriesMeta("ngtl.dop.egat.capability", "x", "", "MMcf/d", "D")])
    e = snapshot.build(st, {"gas.ngtl.egat": {"value": "ngtl.dop.egat.flow",
                                              "capacity": "ngtl.dop.egat.capability"}}, {})["values"]["gas.ngtl.egat"]
    assert (e["v"], e["cap"], e["cap_current"], e["cap_next"]) == (5332.5, 5226.6, 5650.5, 5120.6)


def test_snapshot_daily_follows_gas_day(tmp_path, monkeypatch):
    """Daily series are keyed by NGTL gas day (starts 15:00 UTC = 09:00 Alberta from 2026-11-01)."""
    from montney import snapshot
    from montney.store import SeriesMeta
    st = Store(tmp_path / "t.duckdb")
    st.write([Obs("westcoast.station2_south.capacity", datetime(2026, 11, d), 1600 + d, datetime(2026, 11, d))
              for d in (3, 4, 5)])
    st.upsert_meta([SeriesMeta("westcoast.station2_south.capacity", "x", "", "MMcf/d", "D", day="gas")])
    refs = {"gas.station2": {"value": "westcoast.station2_south.capacity"}}
    at = lambda t: (monkeypatch.setattr(snapshot, "utcnow", lambda: t),
                    snapshot.build(st, refs, {})["values"]["gas.station2"])[1]
    e = at(datetime(2026, 11, 4, 14, 59))   # 08:59 MT Nov 4: still gas day Nov 3
    assert e["v"] == 1603 and e["next"] == 1604 and e["day"] == "gas"
    e = at(datetime(2026, 11, 4, 15, 0))    # 09:00 MT: gas day Nov 4 begins
    assert e["v"] == 1604 and e["next"] == 1605
    e = at(datetime(2026, 11, 5, 2, 0))     # 20:00 MT Nov 4 (UTC date is Nov 5): still Nov 4
    assert e["v"] == 1604


def test_alberta_uses_bundled_tzdata_permanent_utc6():
    from zoneinfo import ZoneInfo
    import zoneinfo
    import montney  # noqa: F401  (resets TZPATH)
    assert zoneinfo.TZPATH == ()  # bundled tzdata only, never the OS copy
    off = datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("America/Edmonton")).utcoffset()
    assert off == timedelta(hours=-6)
    assert datetime(2027, 1, 15, 12, tzinfo=ZoneInfo("America/Edmonton")).utcoffset() == timedelta(hours=-6)


def test_scheduler_backoff_is_exponential_and_capped():
    from montney.runner import Scheduler
    w = Scheduler.next_wait
    assert w(3600, 0) == 3600                      # healthy: normal interval
    assert [w(3600, n) for n in (1, 2, 3)] == [300, 600, 1200]
    assert w(3600, 20) == 6 * 3600                 # capped
    assert w(3600, 5000) == 6 * 3600               # no float overflow
    assert w(60, 1) == 300                         # never faster than 5 min after a failure
    assert w(86400, 20) == 86400                   # daily sources cap at their interval


def test_snapshot_1h_compare_and_target(tmp_path):
    from montney import snapshot
    from montney.store import SeriesMeta
    st = Store(tmp_path / "t.duckdb")
    now = utcnow().replace(second=0, microsecond=0)
    st.write([Obs("ngtl.csr.linepack", now - timedelta(minutes=30 * k), 19700 - k, now - timedelta(minutes=30 * k))
              for k in range(6)])
    st.write([Obs("ngtl.csr.linepack_target", now - timedelta(minutes=30 * k), 20659, now - timedelta(minutes=30 * k))
              for k in range(6)])
    st.upsert_meta([SeriesMeta("ngtl.csr.linepack", "x", "", "MMcf", "30min"),
                    SeriesMeta("ngtl.csr.linepack_target", "x", "", "MMcf", "30min")])
    snap = snapshot.build(st, {"gas.ngtl.linepack": {"value": "ngtl.csr.linepack",
                                                     "target": "ngtl.csr.linepack_target"}},
                          {"gas.ngtl.linepack": "1h"})
    e = snap["values"]["gas.ngtl.linepack"]
    assert e["v"] == 19700 and e["prev"] == 19698 and e["compare"] == "1h"
    assert e["target"] == 20659 and e["target_ts"] == e["ts"]


def test_undated_alerts_expire_after_ttl(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    pub = datetime(2026, 11, 2, 6, 0)
    st.upsert_events([
        Event("a:1", "aeso_ets", "alert", "Keephills 2 offline.", published=pub),
        Event("b:1", "ngtl_bulletins", "bulletin", "Weekend border capability", published=pub),
        Event("o:1", "ngtl_dop", "outage", "open-ended outage", starts=pub),
    ])
    at = lambda h: sorted(x["id"] for x in st.events(state="ongoing", at=pub + timedelta(hours=h)))
    assert at(1) == ["a:1", "b:1", "o:1"]
    assert at(13) == ["b:1", "o:1"]          # alert TTL 12 h passed
    assert at(49) == ["o:1"]                 # bulletin TTL 48 h passed; outage stays open
    assert [x["id"] for x in st.events(state="past", at=pub + timedelta(hours=13))] == ["a:1"]


def test_calendar_daily_series_ignore_gas_day(tmp_path, monkeypatch):
    """Weather/AESO daily series use the Alberta calendar date, not the 09:00 gas-day rollover."""
    from datetime import date
    from montney import snapshot
    from montney.store import SeriesMeta
    monkeypatch.setattr(snapshot, "utcnow", lambda: datetime(2026, 11, 4, 8, 0))    # 02:00 MT Nov 4 (calendar day derives from it)
    st = Store(tmp_path / "t.duckdb")
    st.write([Obs("wx.alberta.hdd", datetime(2026, 11, d), 20 + d, datetime(2026, 11, d)) for d in (3, 4, 5)]
             + [Obs("ngtl.dop.egat.capability", datetime(2026, 11, d), 5000 + d, datetime(2026, 11, d)) for d in (3, 4, 5)])
    st.upsert_meta([SeriesMeta("wx.alberta.hdd", "w", "", "degC-d", "D"),
                    SeriesMeta("ngtl.dop.egat.capability", "g", "", "MMcf/d", "D", day="gas")])
    snap = snapshot.build(st, {}, {}, extra=["wx.alberta.hdd", "ngtl.dop.egat.capability"])["series"]
    assert snap["wx.alberta.hdd"]["v"] == 24             # calendar Nov 4
    assert snap["wx.alberta.hdd"]["day"] == "calendar"
    assert snap["ngtl.dop.egat.capability"]["v"] == 5003  # gas day still Nov 3 before 09:00 MT


def test_series_day_convention_roundtrip(tmp_path):
    from montney.store import SeriesMeta
    st = Store(tmp_path / "t.duckdb")
    st.upsert_meta([SeriesMeta("a.b", "s", "", "", "D", day="gas"), SeriesMeta("c.d", "s", "", "", "D")])
    m = st.meta(["a.b", "c.d"])
    assert m["a.b"]["day"] == "gas" and m["c.d"]["day"] == ""


def test_tabs_api_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from montney.server import create_app
    with TestClient(create_app(store=Store(tmp_path / "t.duckdb"), schedule=False)) as c:
        assert c.get("/api/tabs").status_code == 404


@pytest.mark.parametrize("instant,expect", [
    (datetime(2026, 11, 1, 5, 59), 100),   # 23:59 MDT Oct 31: still calendar day Oct 31
    (datetime(2026, 11, 1, 6, 1), 101),    # 00:01 Nov 1 (UTC-6): calendar day Nov 1
])
def test_calendar_cutoff_follows_the_snapshot_instant(tmp_path, monkeypatch, instant, expect):
    """The calendar-day cutoff comes from the same `now` as everything else in snapshot.build. Freezing only
    snapshot.utcnow must be enough. Seeded red: a separate real-clock local_today() gets one of these two wrong on any
    real date (both instants can't be "today")."""
    from montney import snapshot
    from montney.store import SeriesMeta
    monkeypatch.setattr(snapshot, "utcnow", lambda: instant)
    st = Store(tmp_path / "t.duckdb")
    st.write([Obs("wx.alberta.hdd", datetime(2026, 10, 31), 100, datetime(2026, 10, 31)),
              Obs("wx.alberta.hdd", datetime(2026, 11, 1), 101, datetime(2026, 11, 1))])
    st.upsert_meta([SeriesMeta("wx.alberta.hdd", "w", "", "degC-d", "D")])
    snap = snapshot.build(st, {}, {}, extra=["wx.alberta.hdd"])["series"]
    assert snap["wx.alberta.hdd"]["v"] == expect


def test_a_source_declares_its_own_stale_threshold(monkeypatch):
    """L-107: `stale_after: 4h` in a source spec is the snapshot's stale threshold for its sub-daily series (it used to
    need an entry in snapshot.STALE_AFTER_BY_SOURCE). The spec wins over the table; a bad value fails loudly in the
    spec tests, and an unreadable spec set falls back to the table instead of breaking the snapshot."""
    from montney import connectors, snapshot
    monkeypatch.setattr(snapshot, "_SPEC_STALE", None)
    monkeypatch.setattr(connectors, "load_specs", lambda include_disabled=False: {
        "late_src": {"id": "late_src", "stale_after": "4h"}, "weather_wsc": {"id": "weather_wsc", "stale_after": "90m"},
        "plain": {"id": "plain"}})
    assert snapshot.source_stale_after("late_src") == 4 * 3600
    assert snapshot.source_stale_after("weather_wsc") == 90 * 60                 # the spec wins over the table
    assert snapshot.source_stale_after("power_hq_hourly") == 6 * 3600            # the table still answers
    assert snapshot.source_stale_after("plain") == 0 and snapshot.source_stale_after("") == 0
    now = 1_800_000_000.0
    s = {"ts": [now - 3.5 * 3600], "v": [5.0]}
    late = snapshot.summarize(s, {"freq": "1h", "source": "late_src"}, None, now, now, sid="x.y")
    plain = snapshot.summarize(s, {"freq": "1h", "source": "plain"}, None, now, now, sid="x.y")
    assert late["stale"] is False and plain["stale"] is True                      # 3.5 h old: fresh under 4 h, stale under 3 h
    monkeypatch.setattr(snapshot, "_SPEC_STALE", None)

    def boom(include_disabled=False):
        raise OSError("specs unreadable")
    monkeypatch.setattr(connectors, "load_specs", boom)
    assert snapshot.source_stale_after("power_hq_hourly") == 6 * 3600 and snapshot.source_stale_after("late_src") == 0
    monkeypatch.setattr(snapshot, "_SPEC_STALE", None)


def test_every_spec_stale_after_parses():
    from montney import connectors, regions
    for sid, spec in connectors.load_specs(include_disabled=True).items():
        if spec.get("stale_after") is not None:
            assert regions.parse_lag(spec["stale_after"], sid) > 0, sid
