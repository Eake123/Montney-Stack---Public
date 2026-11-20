"""Peace River dams on the map (network/power/hydro.geojson, PLAN P5d).

BC Hydro publishes no plant MW, so these nodes show Weather's ESTIMATE (weather_peace_mw, merged #146): the value is
`power.hydro.<plant>.mw_est`, a 24 h mean of flow x head, and the box must always carry its ±2σ band (`mw_lo`, `mw_hi`)
and say "est., 24 h mean". It must never show a bare number. Nameplates are checked against cited BC Hydro values, and
against the estimator's own NAMEPLATE (its >105 % warning uses the same reference).
"""

from __future__ import annotations

import json
from pathlib import Path

from montney import network
from montney.connectors import build, load_specs
from montney.connectors.weather_peace_mw import NAMEPLATE

FILE = Path(__file__).resolve().parents[1] / "network" / "power" / "hydro.geojson"
# BC Hydro capacities, cited in each feature's nameplate_source (Wikipedia infoboxes citing BC Hydro, 2026-11-01)
CITED_MW = {"wac_bennett": 2907, "peace_canyon": 694, "site_c": 1100}


def dams() -> list[dict]:
    return [f["properties"] for f in json.loads(FILE.read_text(encoding="utf-8"))["features"]]


def series_of(p: dict) -> set[str]:
    out = {p["value"], p["estimate"]["lo"], p["estimate"]["hi"], *p["panel"]["series"]}
    return out | {x["series"] for x in p.get("callouts_extra", [])}


def callout_problems(p: dict) -> list[str]:
    """An estimate box must say so and always carry both band rows (priority 1 shows them whenever the box shows)."""
    bad = []
    if "est., 24 h mean" not in p.get("callout", {}).get("label", ""):
        bad.append(f"{p['id']}: callout label must say 'est., 24 h mean'")
    rows = {x["series"]: x for x in p.get("callouts_extra", [])}
    for k in ("lo", "hi"):
        r = rows.get(p["estimate"][k])
        if r is None or r.get("priority") != 1:
            bad.append(f"{p['id']}: band row {p['estimate'][k]} missing or not priority 1 (a bare number would show)")
    return bad


def test_hydro_features_validate():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    ids = {f["id"] for f in fc["features"]}
    assert {f"power.plant.hydro_{k}" for k in CITED_MW} <= ids
    for p in dams():
        assert p["type"] == "plant" and p["fuel"] == "hydro" and p["unit"] == "MW"
        assert p["source"].startswith("Wikipedia coordinates") and p["location_precision"] == "site"


def test_series_exist_in_the_weather_peace_spec():
    declared = {m.series_id for m in build(load_specs(include_disabled=True)["weather_peace_mw"]).declared_series()}
    for p in dams():
        missing = series_of(p) - declared
        assert not missing, (p["id"], missing)


def test_boxes_always_show_estimate_and_band():
    probs = [b for p in dams() for b in callout_problems(p)]
    assert dams() and probs == []


def test_nameplates_match_cited_and_estimator():
    for p in dams():
        k = p["id"].removeprefix("power.plant.hydro_")
        assert p["nameplate_mw"] == p["capacity_value"] == CITED_MW[k] == NAMEPLATE[k], k
        assert str(CITED_MW[k]) in p["nameplate_source"].replace(",", ""), k


def test_seeded_red_bare_number_is_caught():
    p = json.loads(json.dumps(dams()[0]))
    p["callouts_extra"] = [x for x in p["callouts_extra"] if not x["series"].endswith(".mw_lo")]
    assert any("band row" in b and "mw_lo" in b for b in callout_problems(p))
    p["callout"]["label"] = "GM Shrum"
    assert any("est., 24 h mean" in b for b in callout_problems(p))
    q = json.loads(json.dumps(dams()[1]))
    q["callouts_extra"][1]["priority"] = 3        # band shown only zoomed in: a bare number at lower zoom
    assert any("mw_hi" in b for b in callout_problems(q))
