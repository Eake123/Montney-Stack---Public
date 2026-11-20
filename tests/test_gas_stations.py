"""The NGTL receipt-station layer (network/gas/stations.geojson, PLAN 5a) and its location arithmetic.

Reference coordinates below are hand-copied public facts (AER ST102 and BCER facility points), not derived from our
own output, so they're independent checks of the survey-grid conversion (Wellsite §4.3).
"""

from __future__ import annotations

import importlib.util
import json
import math

import pytest

from montney import network
from montney.connectors import ngtl_station_util as su

GAS = network.NETWORK_DIR / "gas"
_spec = importlib.util.spec_from_file_location("build_stations", GAS / "build_stations.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def km(a, b):
    return 6371 * math.hypot(math.radians(a[0] - b[0]), math.radians(a[1] - b[1]) * math.cos(math.radians(a[0])))


def stations():
    return json.loads((GAS / "stations.geojson").read_text(encoding="utf-8"))["features"]


# ------------------------------------------------------------------ DLS / NTS arithmetic vs independent points
@pytest.mark.parametrize("dls, ref", [
    ("04-33-078-19W6", (55.799434, -120.890812)),   # BCER facility 00018222, CGL Wilde Lake compressor station
    ("01-29-075-09W6", (55.521453, -119.348136)),   # AER ST102 ABMS0002107 Valhalla (LSD centre)
    ("01-30-075-04W6", (55.521511, -118.598518)),   # AER ST102 ABMS0002124 Woking
    ("12-16-075-10W6", (55.499618, -119.496520)),   # AER ST102 ABMS0002132 Bear River
])
def test_calibrated_dls_lands_within_a_kilometre_of_the_reference(dls, ref):
    assert km(bs.dls(dls), ref) < 1.0


def test_uncalibrated_dls_is_kilometres_off_so_the_calibration_matters():
    assert km(bs.dls("04-33-078-19W6", calibrated=False), (55.799434, -120.890812)) > 3.5


def test_nts_quarter_unit_arithmetic():
    lat, lon = bs.nts("A-005-B/103-I-02")          # LNG Canada's legal location, Kitimat
    assert 53.9 < lat < 54.1 and -128.8 < lon < -128.6
    assert bs.nts("not a location") is None and bs.dls("A-005-B/103-I-02") is None


# ------------------------------------------------------------------ the committed layer
def test_station_layer_validates_and_is_large_enough():
    fc = network.load()
    assert fc["errors"] == []
    feats = stations()
    assert len(feats) >= 300                       # 414 on 2026-11-01; fails closed if the build lost the AER join


def test_every_station_cites_its_location_and_flags_approximate_ones():
    for f in stations():
        p = f["properties"]
        assert p["id"] == f"gas.ngtl.station.{p['tc_station']}" and p["type"] == "meter"
        assert p["value"] == su.series_id(p["tc_station"], "receipt_est") and su.SERIES_PATTERN.match(p["value"])
        assert p["panel"]["series"] == [su.series_id(p["tc_station"], "receipt_est"), su.series_id(p["tc_station"]),
                                        su.series_id(p["tc_station"], "firm_pct")]
        assert p["source"].startswith(("AER ST102", "Petrinex BC")), p["id"]
        approx = p["location_quality"].endswith("_approx")
        assert approx == p["source"].startswith("Petrinex BC") == bool(p.get("schematic")), p["id"]
        assert p["band"] == 0.25 and p["note"].startswith("today's estimate")
        if "callout" in p:
            assert p["callout"]["priority"] == 3 and p["callout"]["label"].endswith("(today est., ±25 %)")


def test_existing_v1_nodes_are_not_drawn_twice():
    ids = {f["properties"]["id"] for f in stations()}
    assert not {f"gas.ngtl.station.{n}" for n in bs.EXISTING} & ids
    nodes = json.loads((GAS / "nodes.geojson").read_text(encoding="utf-8"))["features"]
    assert not ids & {f["properties"]["id"] for f in nodes}


def test_map_fixes_gordondale_and_wilde_lake_sit_on_their_meters():
    nodes = {f["properties"]["id"]: f for f in json.loads((GAS / "nodes.geojson").read_text(encoding="utf-8"))["features"]}
    g = nodes["gas.gordondale"]["geometry"]["coordinates"]
    assert km((g[1], g[0]), (55.817887, -119.762207)) < 0.05 and "ABMS0003886" in nodes["gas.gordondale"]["properties"]["source"]
    w = nodes["gas.willow_valley"]["geometry"]["coordinates"]
    assert km((w[1], w[0]), (55.799434, -120.890812)) < 0.05 and "00018222" in nodes["gas.willow_valley"]["properties"]["source"]
