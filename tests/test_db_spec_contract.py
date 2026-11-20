"""The spec-contract cross-check (RETENTION.md "Spec contract", L-037 (2)): every declared `retention:` tier must
fit its series' freq and as_of mode. It runs over every spec in the repo (today: Power's AESO pins), and must be in
place BEFORE Gas and Weather add their keys. Seeded reds: each rule, broken on purpose, must be caught.
"""

from __future__ import annotations

import copy

import pytest

from montney.connectors import load_specs
from montney.db import tiercheck

SPECS = load_specs(include_disabled=True)


def test_every_declared_retention_fits_its_series():
    declared = {sid: s for sid, s in SPECS.items() if tiercheck.declares_retention(s)}
    checked = {sid: len(tiercheck._series(s)) for sid, s in declared.items()}
    problems = [p for s in declared.values() for p in tiercheck.problems(s)]
    print(f"{len(declared)} specs declare retention, {sum(checked.values())} series checked: {checked}")
    assert sum(checked.values()) > 0, "nothing checked (NO-RESULT is not a pass)"
    assert problems == [], problems


def _http(**block):
    b = {"path": "x", "ts": {"field": "t"}, "as_of": "ts", "freq": "5min",
         "fields": {"v": {"field": "v", "id": "test.red.value"}}}
    b.update(block)
    return {"id": "test_red", "type": "http", "mode": "cached", "retention": b.pop("_tier", "fast"), "extract": [b]}


REDS = [
    ("fast + as_of: fetch", _http(as_of="fetch"), "must be 'forecast'"),
    ("daily + freq 5min", _http(_tier="daily", freq="5min"), "doesn't fit freq"),
    ("hourly + freq D", _http(_tier="hourly", freq="D"), "doesn't fit freq"),
    ("fast + freq 1h", _http(freq="1h"), "doesn't fit freq"),
    ("forecast + as_of: ts", _http(_tier="forecast"), "needs a vintage per run"),
    ("unknown freq", _http(freq="fortnightly"), "unknown or missing"),
    ("missing freq", _http(freq=""), "unknown or missing"),
]


@pytest.mark.parametrize("name, spec, needle", REDS, ids=[r[0] for r in REDS])
def test_RED_each_contradiction_is_caught(name, spec, needle):
    got = tiercheck.problems(spec)
    print(name, got)
    assert any(needle in p for p in got), got


def test_RED_live_mode_and_tier_must_agree():
    live_spec = _http()
    live_spec["mode"] = "live"                                  # mode live, tier fast
    assert any("live <=> mode: live" in p for p in tiercheck.problems(live_spec))
    tier_live = _http(_tier="live")                             # tier live, mode cached
    assert any("live <=> mode: live" in p for p in tiercheck.problems(tier_live))


def test_RED_a_python_glob_map_that_contradicts_the_declared_freqs():
    """A real python spec (Power's outages) with its realized pattern switched to 'fast': realized is freq D."""
    spec = copy.deepcopy(SPECS["aeso_ets_outages"])
    spec["retention"] = {"default": "forecast", "aeso.outage.*.realized": "fast"}
    got = tiercheck.problems(spec)
    assert got and all("realized" in p and "doesn't fit freq" in p for p in got), got


def test_block_override_is_what_gets_checked():
    """aeso_price: the source says hourly; its forecast block (as_of: fetch) says forecast, which is consistent."""
    assert tiercheck.problems(SPECS["aeso_price"]) == []
    bad = copy.deepcopy(SPECS["aeso_price"])
    del bad["extract"][1]["retention"]                          # the forecast block falls back to 'hourly'
    assert any("as_of: fetch" in p for p in tiercheck.problems(bad))


@pytest.mark.parametrize("freq, cls", [("5min", "sub_hourly"), ("10min", "sub_hourly"), ("60min", "hourly"),
                                       ("1h", "hourly"), ("D", "daily_plus"), ("M", "daily_plus"), ("", None),
                                       ("weekly", None)])
def test_freq_classes(freq, cls):
    assert tiercheck.freq_class(freq) == cls
