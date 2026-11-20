"""network/power/*.geojson must point at series our specs actually produce."""

import json
from pathlib import Path

from montney import network
from montney.connectors import load_specs

ROOT = Path(__file__).resolve().parents[1] / "network" / "power"
SPECS = load_specs(include_disabled=True)


def power_features():
    feats = []
    for f in sorted(ROOT.glob("*.geojson")):
        feats += json.loads(f.read_text(encoding="utf-8"))["features"]
    return [f["properties"] for f in feats]


def derived_ids():
    return {d["id"]: d for s in SPECS.values() for d in (s.get("derive") or [])}


def test_power_network_valid():
    fc = network.load()
    assert not fc["errors"]
    assert any(f["id"].startswith("power.") for f in fc["features"])


def test_plant_series_resolve():
    derives = derived_ids()
    for p in power_features():
        if p["type"] != "plant" or p.get("context") or "aeso_assets" not in p:
            continue   # non-AESO plants (BC hydro estimates): tests/test_power_hydro.py
        assets = [a.lower() for a in p["aeso_assets"]]
        for key, k in (("value", "tng"), ("capacity", "mc")):
            sid = p[key]
            if len(assets) == 1:
                assert sid == f"aeso.unit.{assets[0]}.{k}", p["id"]
            else:
                d = derives[sid]
                assert sorted(d["sum"]) == sorted(f"aeso.unit.{a}.{k}" for a in assets), p["id"]
                assert "aeso_ets_units" in {s for s, spec in SPECS.items()
                                            if any(x["id"] == sid for x in spec.get("derive") or [])}


def test_interties_and_anchor_series():
    props = {p["id"]: p for p in power_features()}
    assert props["power.intertie.bc"]["value"] == "aeso.intertie.british_columbia"
    assert props["power.intertie.matl"]["value"] == "aeso.intertie.montana"
    assert props["power.intertie.sk"]["value"] == "aeso.intertie.saskatchewan"
    assert all(props[i]["schematic"] is True for i in props if i.startswith(("power.intertie.", "power.tx.")))
    anchor = props["power.region.ab"]
    assert anchor["value"] in derived_ids()
    assert "callout" in anchor and anchor["callout"]["priority"] == 1


def test_every_feature_cites_its_geometry():
    for p in power_features():
        assert p["source"].startswith(("Wikipedia coordinates", "schematic:", "OpenStreetMap contributors (ODbL)",
                                       "AUC Decision", "Project page", "Alberta Major Projects", "Wind farm database",
                                       "EIA-860M")), p["id"]
        if p["kind"] == "node" and p["type"] == "plant":
            assert p["location_precision"] in {"site", "town", "area", "lld", "project_area"}
