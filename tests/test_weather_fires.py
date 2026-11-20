"""CWFIS wildfire alerts: hotspot cells, perimeters, severity by distance, withdrawal."""

from datetime import datetime

import httpx
import pytest

from montney.connectors.base import Context
from montney.connectors.fire_cwfis import CWFISFires, dist_km, poly_dist_km, simplify
from montney.store import Store

NODE = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-120.0, 56.0]},
        "properties": {"id": "gas.test.plant", "kind": "node", "name": "Test Plant"}}
LINK = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-118.0, 55.0], [-118.0, 57.0]]},
        "properties": {"id": "power.tx.test", "kind": "link", "name": "Test Line"}}


def hs(lon, lat, frp, when="2026-10-31T20:00:00Z", agency="BC"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"lon": lon, "lat": lat, "frp": frp, "rep_date": when, "agency": agency}}


HOTSPOTS = [
    hs(-120.02, 56.01, 50.0, "2026-10-31T09:00:00Z"),  # cell A: 3 strong points ~1.5 km from the plant
    hs(-120.03, 56.02, 80.0),
    hs(-120.01, 56.03, 20.0),
    hs(-119.75, 56.05, 50.0),   # B: single strong, ~16 km from the plant -> warn
    hs(-118.05, 56.35, 3.0),    # C: single weak, ~3 km from the line -> critical, downgraded to warn
    hs(-130.0, 50.0, 40.0),     # D: > 100 km from anything -> dropped (context_km)
    hs(-120.32, 56.01, 1.0),    # E: cluster of small detections ~20 km from the plant: pile burns,
    hs(-120.33, 56.02, 2.0),    #    warn downgraded to info
]
PERIM = {"type": "Feature", "id": "m3_polygons_current.42",
         "geometry": {"type": "Polygon", "coordinates": [[[-120.1, 55.9], [-119.9, 55.9], [-119.9, 56.1],
                                                          [-120.1, 56.1], [-120.1, 55.9]]]},
         "properties": {"hcount": 120, "firstdate": "2026-10-20T00:00:00Z",
                        "lastdate": "2026-10-30T10:00:00Z", "area": 1520.5}}


def make(hotspots, perims=(PERIM,), **params):
    conn = CWFISFires({"id": "weather_fires", "params": params})
    conn.load_features = lambda: [NODE, LINK]
    seen = []

    def handler(req):
        q = req.url.params
        seen.append(q["typeName"])
        assert q["srsName"] == "EPSG:4326" and "CQL_FILTER" in q
        feats = list(hotspots) if q["typeName"] == "public:hotspots_last24hrs" else list(perims)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": feats})

    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 10, 31, 21))
    return conn, ctx, seen


def by_id(res):
    return {e.id: e for e in res.events}


def test_distances():
    assert dist_km(-120.0, 56.0, NODE["geometry"]) == pytest.approx(0, abs=1e-6)
    assert dist_km(-119.0, 56.0, NODE["geometry"]) == pytest.approx(62.2, abs=1)   # 1 deg lon at 56 N
    assert dist_km(-118.1, 56.0, LINK["geometry"]) == pytest.approx(6.2, abs=0.2)   # to the segment
    assert poly_dist_km(PERIM["geometry"], NODE["geometry"]) == 0.0                 # node inside
    assert poly_dist_km(PERIM["geometry"], LINK["geometry"]) == pytest.approx(1.9 * 62.2 / 1.0, rel=0.05)


def test_hotspot_cells_and_severity():
    conn, ctx, seen = make(HOTSPOTS)
    res = conn.fetch(ctx)
    assert seen == ["public:hotspots_last24hrs", "public:m3_polygons_current"]
    assert res.event_scopes == [("weather_fires", "alert")]
    hs_ev = [e for e in res.events if ":hs:" in e.id]
    assert len(hs_ev) == 4  # A's three points share a cell, as do E's two; D is out of context
    ev = {e.extra["count"]: e for e in hs_ev if e.extra["count"] == 3}[3]
    assert ev.severity == "critical" and ev.refs == ["gas.test.plant"]
    assert ev.extra["max_frp"] == 80.0 and len(ev.extra["points"]) == 3
    assert ev.extra["nearest"]["id"] == "gas.test.plant" and ev.extra["nearest"]["km"] < 5
    assert ev.starts == datetime(2026, 10, 31, 9) and ev.ends == datetime(2026, 11, 1, 20)
    assert "3 detections" in ev.title and "Test Plant" in ev.title
    sev = {(round(e.extra["lon"], 2)): e for e in hs_ev}
    assert sev[-119.75].severity == "warn" and sev[-119.75].refs == ["gas.test.plant"]
    assert sev[-118.05].severity == "warn" and sev[-118.05].refs == ["power.tx.test"]  # weak: down one
    assert -130.0 not in sev
    (pile,) = [e for e in hs_ev if e.extra["count"] == 2]  # max FRP 2: likely pile burns
    assert pile.severity == "info" and pile.refs == ["gas.test.plant"]
    assert pile.extra["points"] == [] and pile.extra["count"] == 2  # no points for info cells
    assert all(e.kind == "alert" and e.source == "weather_fires" for e in res.events)


def test_perimeter_event():
    conn, ctx, _ = make([])
    (ev,) = conn.fetch(ctx).events
    assert ev.id == "weather_fires:m3:42" and ev.severity == "critical"
    assert ev.refs == ["gas.test.plant"]
    assert ev.extra["geometry"]["type"] == "Polygon" and ev.extra["area_ha"] == 1520.5
    assert ev.ends == datetime(2026, 11, 6, 10)  # lastdate + perimeter_days


def test_simplify_caps_vertices():
    ring = [[-120 + i * 1e-4, 56 + (i % 7) * 1e-4] for i in range(2000)]
    g = simplify({"type": "Polygon", "coordinates": [ring + [ring[0]]]}, 300)
    r = g["coordinates"][0]
    assert len(r) <= 302 and r[0] == r[-1]
    assert all(len(str(x).split(".")[-1]) <= 3 for v in r for x in v)


def test_empty_season_and_withdrawal(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    conn, ctx, _ = make(HOTSPOTS)
    res = conn.fetch(ctx)
    st.upsert_events(res.events, complete_scopes=res.event_scopes)
    assert len(st.events()) == 5
    conn, ctx, _ = make([], perims=())  # fire season over: nothing in the feed
    res = conn.fetch(ctx)
    assert res.events == [] and not res.warnings
    st.upsert_events(res.events, complete_scopes=res.event_scopes)
    assert st.events() == []
    assert {e["status"] for e in st.events(include_withdrawn=True)} == {"withdrawn"}


def test_shipped_spec_loads():
    from montney.connectors import build, load_specs
    spec = load_specs(include_disabled=True)["weather_fires"]
    conn = build(spec)
    assert isinstance(conn, CWFISFires) and conn.lon_min == -140 and conn.perimeter_days == 7


SCHEMATIC = {"type": "Feature",
             "geometry": {"type": "LineString", "coordinates": [[-121.0, 57.0], [-121.0, 58.0]]},
             "properties": {"id": "gas.test.schematic", "kind": "link", "name": "Test Pipeline",
                            "schematic": True}}


def test_schematic_link_is_only_a_corridor():
    conn, ctx, _ = make([hs(-121.01, 57.5, 90.0), hs(-121.6, 57.5, 90.0)], perims=())
    far_node = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.5, 57.5]},
                "properties": {"id": "gas.test.far", "kind": "node", "name": "Far Node"}}  # ~84 km
    conn.load_features = lambda: [SCHEMATIC, far_node]
    ev = {round(e.extra["lon"], 1): e for e in conn.fetch(ctx).events}
    on_line = ev[-121.0]  # 0.6 km from a drawn line: critical if it were surveyed
    assert on_line.severity == "info" and on_line.refs == ["gas.test.schematic"]
    assert "near the Test Pipeline corridor (schematic)" in on_line.title
    assert " km" not in on_line.title.split("(max FRP")[0]  # no distance to a drawn line
    assert on_line.extra["nearest"]["schematic"] is True
    off = ev[-121.6]  # ~36 km: outside the 25 km corridor, kept (context) but no refs
    assert off.severity == "info" and off.refs == [] and "corridor" not in off.title
    # a surveyed node nearby drives severity and is the one named
    conn, ctx, _ = make([hs(-121.01, 57.5, 90.0)], perims=())
    node = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-121.02, 57.5]},
            "properties": {"id": "gas.test.station", "kind": "node", "name": "Test Station"}}
    conn.load_features = lambda: [SCHEMATIC, node]
    (e,) = conn.fetch(ctx).events
    assert e.severity == "critical" and "1 km from Test Station" in e.title
    assert set(e.refs) == {"gas.test.station", "gas.test.schematic"}
