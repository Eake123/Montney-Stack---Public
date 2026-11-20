"""The pieces every EBB family shares (gas_ebb_common): one state -> region map, Dth -> MMcf/d, per-host pacing, the
weekly location cache, require_present and the no-state guard. The families' own tests (test_gas_ebb_km / _east /
_enbridge / _et / _bakken) cover them in use."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import gas_ebb_bakken, gas_ebb_common as common, gas_ebb_et, gas_ebb_km
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 18, 0)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))


def test_one_state_region_map_and_one_conversion():
    assert gas_ebb_et.state_regions is common.state_regions and gas_ebb_km.us_state_regions is common.state_regions
    m = common.state_regions()
    assert m["NY"] == "nyc" and m["NM"] == "sunbelt" and m["MI"] == "umw" and m["ND"] == "bakken"
    assert all(len(k) == 2 and k.isupper() for k in m)
    assert gas_ebb_et.mmcfd is common.mmcfd and common.mmcfd(1037.0) == 1.0 and common.DTH_PER_MMCF == 1037.0
    assert gas_ebb_bakken.require_states is gas_ebb_et.require_states


def test_host_wait_spaces_one_host_only():
    slept, last = [], {}
    assert common.host_wait("https://a.test/x", 1.0, last, slept.append) == "a.test" and slept == []   # never seen
    last["a.test"] = common.time.monotonic()
    common.host_wait("https://a.test/y?q=1", 1.0, last, slept.append)
    common.host_wait("https://b.test/y", 1.0, last, slept.append)                                    # another host
    assert len(slept) == 1 and 0 < slept[0] <= 1.0
    last["a.test"] -= 5
    common.host_wait("https://a.test/z", 1.0, last, slept.append)                                    # long ago
    assert len(slept) == 1


def test_paced_request_stamps_each_host(monkeypatch):
    waits = []
    monkeypatch.setattr(common, "_last_hit", {})
    monkeypatch.setattr(common.time, "sleep", waits.append)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))), now=NOW)
    for url in ("https://a.test/1", "https://b.test/1", "https://a.test/2"):
        assert common.paced_request(ctx, "GET", url, 0.5).text == "ok"
    assert set(common._last_hit) == {"a.test", "b.test"} and len(waits) == 1 and 0 < waits[0] <= 0.5
    assert ctx.n_requests == 3


def test_location_cache_week_and_early_retry(tmp_path):
    assert common.loc_cache_read("ebb_loc_x", "points") == ({}, None)
    assert common.loc_cache_due(None, NOW)
    common.loc_cache_write("ebb_loc_x", "points", NOW, {"1": ["NY", "LDC", "X"]})
    assert (tmp_path / "cache" / "ebb_loc_x.json").exists()
    locs, got = common.loc_cache_read("ebb_loc_x", "points")
    assert locs == {"1": ["NY", "LDC", "X"]} and got == NOW
    assert not common.loc_cache_due(got, NOW + timedelta(days=7)) and common.loc_cache_due(got, NOW + timedelta(days=8))
    assert not common.loc_cache_lacks(locs, {"1"}, got, NOW + timedelta(days=3))        # has it
    assert not common.loc_cache_lacks(locs, {"2"}, got, NOW + timedelta(hours=20))      # read today: no retry
    assert common.loc_cache_lacks(locs, {"1", "2"}, got, NOW + timedelta(hours=25))
    assert not common.loc_cache_lacks(locs, None, got, NOW + timedelta(days=3))


def test_require_present():
    common.require_present({"1", "2"}, ["1", "2", "3"], "p: override point(s)")
    with pytest.raises(FormatDrift, match=r"^p: override point\(s\) \['2', '9'\] missing from the payload$"):
        common.require_present({"9", "2", "1"}, {"1"}, "p: override point(s)")
    with pytest.raises(FormatDrift, match=r"segment\(s\) \[\('1', 'A'\)\] gone"):
        common.require_present({("1", "A")}, set(), "b: segment(s)", "gone")


def test_require_located():
    pts = [("1", "CITY GATE", 1037.0, True), ("2", "IDLE", 0.0, False), ("3", "OLD FUEL", -5.0, False)]
    common.require_located(pts, None, "p")                                    # located, zero, negative: pass
    with pytest.raises(FormatDrift, match=r"^p: 2 point\(s\) with scheduled gas but no state .*\['4', '5'\]$"):
        common.require_located(pts + [("5", "NEW B", 1.0, False), ("4", "NEW A", 9.0, False)], None, "p")
    common.require_located(pts + [("4", "CS #4 Bay City", 9.0, False)], r"\bCS\s*#", "p")      # unlocated_ok
    with pytest.raises(FormatDrift, match=r"\['4', '5'\]"):                                       # not matched
        common.require_located(pts + [("4", "CS #4 Bay City", 9.0, False), ("5", None, 2.0, False)], "^NEW", "p")


def test_ebb_connector_points_the_guard_accepts():
    board = {"pipe": "p", "unlocated_ok": "^KNOWN", "overrides": {
        "10": {"region": "nyc"}, "11": {"drop": True}, "12:D": {"state": "NY"}, "13": {"type": "LDC"}}}
    pts = [common.Pt("10", "A", "D", 5), common.Pt("11", "B", "R", 5), common.Pt("12", "C", "D", 5),
           common.Pt("13", "D", "D", 5), common.Pt("14", "E", "D", 5, state="NY"), common.Pt("15", "KNOWN F", "R", 5),
           common.Pt("16", "G", "S", 5), common.Pt("17", "H", "D", 0), common.Pt("12", "C", "R", 5)]
    assert common.unlocated(board, pts) == {"13", "12"}       # type-only override; 12's state is for deliveries only
    with pytest.raises(FormatDrift, match=r"p: 2 point\(s\) with scheduled gas but no state.*\['12', '13'\]"):
        common.aggregate({**board, "regions": ["nyc"]}, pts, [])
