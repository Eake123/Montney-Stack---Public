"""Station ↔ plant links: the volume-fingerprint rule (build_links.match) and the committed plants.geojson (PLAN 5a).

Only the volume-matched high tier is drawn (DECISIONS P5a / L-044). The synthetic plants and stations below are made up.
"""

from __future__ import annotations

import importlib.util
import json

from montney import network
from montney.connectors import gas_petrinex as gp

GAS = network.NETWORK_DIR / "gas"
_spec = importlib.util.spec_from_file_location("build_links", GAS / "build_links.py")
bl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bl)

A = (55.00, -118.00)
NEAR = (55.005, -118.00)        # ~0.56 km from A
FAR = (55.40, -118.00)          # ~44 km from A


def plant(mmcfd, ll=A, name="ALPHA GAS PLANT"):
    return {"mmcfd": mmcfd, "name": name, "ll": ll}


def station(mmcfd, ll=NEAR, name="ALPHA SOUTH (1)"):
    return {"mmcfd": mmcfd, "name": name, "ll": ll}


def test_unique_fingerprint_close_and_named_is_shown():
    links = bl.match({"ABGP0000001": plant(100.0)}, {1: station(102.0)})
    assert len(links) == 1 and links[0]["station"] == 1 and links[0]["volume_match_pct"] == -1.96
    assert links[0]["score"] >= bl.MIN_SCORE


def test_outside_three_percent_is_not_a_fingerprint():
    assert bl.match({"ABGP0000001": plant(100.0)}, {1: station(103.2)}) == []      # −3.1 %


def test_ambiguous_fingerprint_is_not_drawn():
    two = {1: station(101.0), 2: station(99.5, ll=(55.01, -118.0), name="BETA (2)")}
    assert bl.match({"ABGP0000001": plant(100.0)}, two) == []                       # two stations within ±3 %
    two_plants = {"ABGP0000001": plant(100.0), "ABGP0000002": plant(101.5, ll=(55.002, -118.0), name="BETA PLANT")}
    assert bl.match(two_plants, {1: station(100.5)}) == []                           # the station has two candidates


def test_far_small_or_low_score_is_not_drawn():
    assert bl.match({"ABGP0000001": plant(100.0)}, {1: station(100.0, ll=FAR)}) == []          # ≥ 30 km
    assert bl.match({"ABGP0000001": plant(4.9)}, {1: station(4.9)}) == []                      # < 5 MMcf/d
    # ~20 km, no shared name, not the only station nearby, small plant: score −1 + 1 (nearest) = 0 < 4
    mid = (55.18, -118.0)
    assert bl.match({"ABGP0000001": plant(10.0, name="ZETA")}, {1: station(10.0, ll=mid, name="OMEGA (1)")}) == []


def _features():
    return json.loads((GAS / "plants.geojson").read_text(encoding="utf-8"))["features"]


def test_committed_layer_is_the_volume_matched_tier_only():
    feats = _features()
    nodes = {f["properties"]["id"]: f["properties"] for f in feats if f["properties"]["kind"] == "node"}
    links = [f["properties"] for f in feats if f["properties"]["kind"] == "link"]
    assert len(links) >= 50                                   # 78 on 2026-11-01; fails closed if the build broke
    stations = {f["properties"]["id"] for f in json.loads((GAS / "stations.geojson").read_text(encoding="utf-8"))["features"]}
    for p in links:
        assert abs(p["volume_match_pct"]) <= 3.0 and p["km"] < 30 and p["score"] >= bl.MIN_SCORE
        assert p["confidence"] == 0.95 and p["schematic"] is True and p["source"].startswith("schematic:")
        assert p["from"] in nodes and p["to"] in stations and gp.SERIES_PATTERN.match(p["value"])
    assert {p["from"] for p in links} == set(nodes)            # every plant dot has its link, and only linked plants
    for p in nodes.values():
        assert p["type"] == "gas_processing" and p["source"].startswith("AER ST102") and gp.SERIES_PATTERN.match(p["value"])
        assert p["band"] == gp.PLANT_BAND == 0.31 and p["note"].startswith("last-month context, not live")


def test_layer_validates():
    assert network.load()["errors"] == []
