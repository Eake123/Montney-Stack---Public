"""Wind and solar farms on the map (network/power/{wind,solar}.geojson, P4c sprite animation).

Ground truth is AESO's own published numbers, from the Current Supply & Demand report, Last Update Nov 1, 2026 09:22:
* fuel table max capability (MC): WIND 5684 MW, SOLAR 1892 MW (wind matches the wind forecast's MCR);
* per-asset MC for two known assets: Blackspring Ridge (BSR1) 300 MW and Travers Solar (TVS1) 465 MW.

Every CSD wind/solar asset must be either on the map or in the file's `unlocated` list (each with a reason). That is
checked by the fuel totals here, and live by the `aeso_ets_units` fetch, which warns about any asset it doesn't
know (`watch_assets`). MONTNEY_LIVE_AESO=1 runs that live check as a failing test.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors.base import Context

ROOT = Path(__file__).resolve().parents[1] / "network" / "power"
FIX = Path(__file__).parent / "fixtures"
SPEC = load_specs(include_disabled=True)["aeso_ets_units"]
AESO_FUEL_MC = {"wind": 5684.0, "solar": 1892.0}
AESO_ASSET_MC = {"BSR1": 300.0, "TVS1": 465.0}
PRECISION = {"site", "lld", "project_area"}
SOURCES = ("OpenStreetMap contributors (ODbL)", "AUC Decision", "Project page", "Alberta Major Projects",
           "Wind farm database")


def collection(fuel: str) -> dict:
    return json.loads((ROOT / f"{fuel}.geojson").read_text(encoding="utf-8"))


def farms(fuel: str) -> list[dict]:
    return [f["properties"] for f in collection(fuel)["features"]]


def unlocated(fuel: str) -> dict[str, float]:
    return {u["asset"]: u["mc_mw"] for u in collection(fuel).get("unlocated", [])}


def coverage_gaps(fuel: str, props: list[dict], unl: dict) -> list[str]:
    """Mapped nameplate + unlocated MC must equal AESO's published fuel MC, with no asset counted twice."""
    if not props:
        return [f"NO-RESULT: no {fuel} features"]
    assets = [a for p in props for a in p["aeso_assets"]]
    bad = [f"{a} listed twice" for a in set(assets) if assets.count(a) > 1]
    bad += [f"{a} both mapped and unlocated" for a in set(assets) & set(unl)]
    total = sum(p["nameplate_mw"] for p in props) + sum(unl.values())
    if abs(total - AESO_FUEL_MC[fuel]) > 0.5:
        bad.append(f"{fuel}: mapped {sum(p['nameplate_mw'] for p in props)} + unlocated {sum(unl.values())}"
                   f" = {total} != AESO {AESO_FUEL_MC[fuel]}")
    return bad


@pytest.mark.parametrize("fuel", ["wind", "solar"])
def test_every_aeso_asset_is_mapped_or_listed(fuel):
    assert coverage_gaps(fuel, farms(fuel), unlocated(fuel)) == []


@pytest.mark.parametrize("fuel", ["wind", "solar"])
def test_unlocated_entries_carry_a_reason(fuel):
    for u in collection(fuel).get("unlocated", []):
        assert set(u) == {"asset", "mc_mw", "reason"} and len(u["reason"]) > 40, u


@pytest.mark.parametrize("fuel", ["wind", "solar"])
def test_farm_features(fuel):
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    ids = {f["id"] for f in fc["features"]}
    for p in farms(fuel):
        assert p["id"] in ids and p["id"].startswith(f"power.plant.{fuel}_")
        assert p["type"] == "plant" and p["fuel"] == fuel and p["unit"] == "MW"
        assert p["location_precision"] in PRECISION and p["source"].startswith(SOURCES), p["id"]
        if p["location_precision"] != "site":    # converted from a legal land description: say which
            assert p.get("lld") and "±2 km" in p["source"], p["id"]
        if "callout" in p:
            assert p["callout"]["priority"] in (2, 3) and p["nameplate_mw"] >= 100


def test_units_spec_emits_and_watches_wind_and_solar():
    assert {"wind", "solar"} <= set(SPEC["params"]["fuels"])
    assert {"wind", "solar"} <= set(SPEC["params"]["watch_assets"]["fuels"])   # hydro joined in P5b


def test_known_nameplates_match_aeso():
    by_asset = {p["aeso_assets"][0]: p for f in ("wind", "solar") for p in farms(f) if len(p["aeso_assets"]) == 1}
    for a, mw in AESO_ASSET_MC.items():
        assert by_asset[a]["nameplate_mw"] == mw, a


def test_seeded_red_dropped_or_double_counted_asset_is_caught():
    props = farms("wind")
    dropped = [p for p in props if p["aeso_assets"] != ["BSR1"]]
    assert any("!= AESO 5684.0" in b for b in coverage_gaps("wind", dropped, unlocated("wind")))
    assert any("both mapped and unlocated" in b for b in coverage_gaps("wind", props, {**unlocated("wind"), "BSR1": 300.0}))
    assert coverage_gaps("wind", [], {}) == ["NO-RESULT: no wind features"]


# ---- the live watch: a new AESO wind/solar asset the map doesn't account for is reported
def _ctx(body: str) -> Context:
    return Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))),
                   now=datetime(2026, 10, 31, 20, 10))


def _watching(tmp_path, mapped: list[str], unl: list[str], fuels=("wind", "solar")) -> dict:
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
                        "properties": {"aeso_assets": mapped}}],
          "unlocated": [{"asset": a, "mc_mw": 0, "reason": "test"} for a in unl]}
    f = tmp_path / "farms.geojson"
    f.write_text(json.dumps(fc), encoding="utf-8")
    p = {**SPEC["params"], "watch_assets": {"fuels": list(fuels), "network": [str(f)]}}
    return {**SPEC, "params": p}


def test_watch_reports_only_unaccounted_assets(tmp_path):
    # fixture assets: wind TWA1, TWB1; solar TSO1. TWA1 is mapped and TSO1 unlocated, so only TWB1 is new.
    res = build(_watching(tmp_path, ["TWA1"], ["TSO1"])).run(_ctx((FIX / "power_csd_sample.csv").read_text("utf-8")))
    new = [w for w in res.warnings if "neither mapped" in w]
    assert len(new) == 1 and "TWB1" in new[0] and "wind" in new[0]


def test_seeded_red_watch_off_or_unreadable_is_not_silent(tmp_path):
    body = (FIX / "power_csd_sample.csv").read_text("utf-8")
    off = build(_watching(tmp_path, [], [], fuels=())).run(_ctx(body))
    assert not [w for w in off.warnings if "neither mapped" in w]       # what a missing watch would look like
    spec = _watching(tmp_path, ["TWA1"], [])
    spec["params"]["watch_assets"]["network"] = [str(tmp_path / "missing.geojson")]
    res = build(spec).run(_ctx(body))
    assert any("watch list unreadable" in w for w in res.warnings)       # fails closed


@pytest.mark.skipif(os.environ.get("MONTNEY_LIVE_AESO") != "1", reason="live AESO check: set MONTNEY_LIVE_AESO=1")
def test_live_aeso_lists_no_unaccounted_wind_or_solar_asset():
    res = build(SPEC).run(Context(client=httpx.Client(follow_redirects=True), now=datetime.utcnow()))
    assert res.obs, "NO-RESULT: the live CSD fetch produced nothing"
    assert [w for w in res.warnings if "neither mapped" in w or "watch list" in w] == []
