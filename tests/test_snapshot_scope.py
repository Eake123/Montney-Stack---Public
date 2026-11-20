"""L-081 PR 2: the zoom-scoped snapshot carries what can show at a tier, boxes in full and drawn-only features lite.

The rule (montney/scope.py) mirrors web/map.js featureTier/featureShown and web/style.js:
- a node shows from overrides.minzoom ?? tiers[overrides.tier ?? callout.priority ?? 2]; merged boxes from their target's;
- links and areas are drawn at every zoom (coloured by util), so they're always in;
- lite = [v, stale, cap] with cap = cap_current ?? cap, None for a capacity value (else util would be wrong).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from montney import scope, snapshot
from montney.store import Obs, SeriesMeta, Store


def _f(fid, kind="node", callout=None, **p):
    props = {"id": fid, "kind": kind, "value": f"s.{fid}", **p}
    if callout is not None:
        props["callout"] = callout
    return {"type": "Feature", "id": fid, "properties": props, "geometry": {"type": "Point", "coordinates": [0, 0]}}


FC = {"type": "FeatureCollection", "features": [
    _f("overview", callout={"priority": 0}),
    _f("breakout", callout={"priority": 1}),
    _f("detail", callout={"priority": 3}),
    _f("plant"),                                      # no callout: tier 2, drawn only
    _f("pipe", kind="link"),                          # a link: drawn at every zoom
    _f("merged", callout={"priority": 3}),            # merged into `overview` below its merge_until
    _f("early", callout={"priority": 3}),             # minzoom override between tiers 1 and 2
]}
CFG = {"overrides": {"merged": {"merge_into": "overview", "merge_until": 7}, "early": {"minzoom": 6.5}}}
REFS = {f["id"]: {"value": f["properties"]["value"]} for f in FC["features"]}


@pytest.mark.parametrize("tier, hot, lite", [
    (0, {"overview", "merged"}, {"pipe"}),
    (1, {"overview", "merged", "breakout", "early"}, {"pipe"}),
    (2, {"overview", "merged", "breakout", "early"}, {"pipe", "plant"}),
    (3, {"overview", "merged", "breakout", "early", "detail"}, {"pipe", "plant"}),
])
def test_split_mirrors_the_map(tier, hot, lite):
    h, li = scope.split(FC, CFG, REFS, tier)
    assert (h, li) == (hot, lite)


def test_tiers_come_from_map_json_when_present():
    assert scope.tiers({}) == scope.DEFAULT_TIERS
    cfg = {"tiers": {"1": 5, "0": 0, "2": 6, "3": 6.4}}
    assert scope.tiers(cfg) == [0, 5, 6, 6.4]
    assert "breakout" in scope.split(FC, cfg, REFS, 1)[0]                   # tier 1 now from 5
    assert "detail" not in scope.split(FC, cfg, REFS, 2)[0]                 # tier 2 spans [6, 6.4)
    assert "detail" in scope.split(FC, cfg, REFS, 3)[0]
    # a note or a typo in map.json never breaks the snapshot (Frontend, #319): notes skipped, junk -> defaults
    assert scope.tiers({"tiers": {"_doc": "zoom per tier", **cfg["tiers"]}}) == [0, 5, 6, 6.4]
    assert scope.tiers({"tiers": {"0": 0, "1": "six"}}) == scope.DEFAULT_TIERS
    assert scope.tiers({"tiers": {"_doc": "only a note"}}) == scope.DEFAULT_TIERS


def test_lite_entry_carries_cap_for_util():
    e = {"v": -50.0, "stale": False, "cap": 100.0, "cap_current": 80.0}
    assert scope.lite_entry(e, None) == [-50.0, False, 80.0]
    assert scope.lite_entry({**e, "cap_current": None}, None) == [-50.0, False, 100.0]
    assert scope.lite_entry(e, "capacity") == [-50.0, False, None]


# ------------------------------------------------------------------ the endpoint, on the real network files
T0 = datetime(2026, 11, 3, 18, 0, 30)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(snapshot, "utcnow", lambda: T0)
    from montney import network
    from montney.server import create_app
    fc = network.load(strict=False)
    refs = network.series_refs(fc)
    st = Store(tmp_path / "t.duckdb")
    sids = sorted({r["value"] for r in refs.values() if "value" in r})
    st.upsert_meta([SeriesMeta(s, "test_src", s, "MW", "5min") for s in sids])
    st.write([Obs(s, T0 - timedelta(minutes=5), float(i + 1), T0) for i, s in enumerate(sids)])
    with TestClient(create_app(store=st, schedule=False)) as c:
        yield c


def test_scoped_snapshot_agrees_with_the_full_one(client):
    full = client.get("/api/snapshot").json()["values"]
    for z in range(4):
        s = client.get("/api/snapshot", params={"z": z}).json()
        assert s["scope"]["z"] == z
        assert not set(s["values"]) & set(s["lite"])
        for fid, e in s["values"].items():
            assert e == full[fid], fid                                  # boxes: the full entry, unchanged
        for fid, (v, stale, cap) in s["lite"].items():
            f = full[fid]
            assert (v, stale) == (f["v"], f["stale"]), fid
            assert cap == (f.get("cap_current") if f.get("cap_current") is not None else f.get("cap")) or cap is None
    z0, z3 = (client.get("/api/snapshot", params={"z": z}).json() for z in (0, 3))
    assert len(z0["values"]) + len(z0["lite"]) < len(z3["values"]) + len(z3["lite"]) <= len(full)


def test_features_param_is_the_click_detail(client):
    full = client.get("/api/snapshot").json()["values"]
    ids = sorted(full)[:3]
    s = client.get("/api/snapshot", params={"features": ",".join(ids)}).json()
    assert s["values"] == {k: full[k] for k in ids} and s["gauges"] == {} and s["series"] == {}


def test_a_bad_override_is_ignored_not_a_500(caplog):
    """Reviewer on #340: a non-numeric override minzoom (or tier) in web/map.json must not 500 every scoped snapshot.
    The bad value is ignored (logged once) and the feature keeps its tier's minzoom, so it's never left out."""
    bad = {"overrides": {"early": {"minzoom": "soon"}, "detail": {"tier": "x"}, "plant": "not a dict",
                         "breakout": {"minzoom": "6"}}}                     # a numeric string is fine (JS coerces it)
    mz = scope.minzooms(FC, bad)
    assert mz["early"] == scope.DEFAULT_TIERS[3]                            # its callout priority 3, not "soon"
    assert mz["detail"] == scope.DEFAULT_TIERS[3] and mz["breakout"] == 6.0
    assert scope.split(FC, bad, REFS, 3)[0] >= {"early", "detail", "breakout"}
    assert caplog.text.count("'soon' is not a number") == 1
    scope.minzooms(FC, bad)
    assert caplog.text.count("'soon' is not a number") == 1                 # once, not every snapshot


def test_shared_util_series_once_per_tier():
    """L-106 (Power #422): inventory plants read their BA + fuel's est. utilisation (`util_est`). A tier's snapshot adds
    each such series once to `series` for the nodes drawn at that tier (scope.shared_series), so the client's `extra`
    (capped at 400) doesn't grow with 4,784 plants."""
    fc = {"type": "FeatureCollection", "features": [
        _f("p1", util_est="eia930.x.util.coal", callout={"priority": 2}), _f("p2", util_est="eia930.x.util.coal", callout={"priority": 2}),
        _f("p3", util_est="eia930.y.util.wind", callout={"priority": 3}), _f("p4"),
        _f("l1", kind="link", util_est="eia930.z.util.gas")]}     # links don't take one: plants are nodes
    assert scope.shared_series(fc, {}, 0) == scope.shared_series(fc, {}, 1) == []
    assert scope.shared_series(fc, {}, 2) == ["eia930.x.util.coal"]
    assert scope.shared_series(fc, {}, 3) == ["eia930.x.util.coal", "eia930.y.util.wind"]


def test_scoped_snapshot_carries_util_est_series(client):
    from montney import network
    fc = network.load(strict=False)
    util = {f["properties"]["util_est"] for f in fc["features"] if f["properties"].get("util_est")}
    if not util:
        pytest.skip("no inventory plants with util_est in network/")
    s3 = client.get("/api/snapshot", params={"z": 3}).json()
    s0 = client.get("/api/snapshot", params={"z": 0}).json()
    assert util <= set(s3["series"]) and not util & set(s0["series"])
