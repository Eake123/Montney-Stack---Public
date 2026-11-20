"""Columbia dams on the map (network/power/hydro_columbia.geojson, PLAN step 5).

There is no live outflow gauge below Mica, Revelstoke or Keenleyside, so MW is NO-RESULT. The nodes show the reservoir
level (Weather's forebay series, docs/research/v2/weather-rivers-columbia.json `heads`) and the nameplate, labelled
"MW not published". Nothing on a node may read as output: no MW value, no `capacity_value` (the map would turn a level
over a capacity into a utilisation), no estimate band.
"""

from __future__ import annotations

import json
from pathlib import Path

from montney import network

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "network" / "power" / "hydro_columbia.geojson"
SPEC = json.loads((ROOT / "docs/research/v2/weather-rivers-columbia.json").read_text(encoding="utf-8"))
CITED_MW = {"mica": 2746, "revelstoke": 2480, "arrow_lakes": 185}   # docs/research/v2/power.md section 2.4


def dams() -> list[dict]:
    return json.loads(FILE.read_text(encoding="utf-8"))["features"]


def output_problems(p: dict) -> list[str]:
    """Anything that would let a Columbia node read as generation."""
    bad = []
    if p.get("unit") != "m" or not p["value"].startswith("wsc.realtime."):
        bad.append(f"{p['id']}: value must be a WSC level in m, not {p['value']} {p.get('unit')}")
    for k in ("capacity_value", "capacity", "estimate", "band", "share"):
        if k in p:
            bad.append(f"{p['id']}: {k} would imply output")
    if "MW not published" not in p["callout"]["label"]:
        bad.append(f"{p['id']}: label must say 'MW not published'")
    if p.get("mw") != "NO-RESULT" or any(x.get("unit") != "m" for x in p.get("callouts_extra", [])):
        bad.append(f"{p['id']}: MW must be NO-RESULT and every row a level")
    return bad


def test_nodes_show_levels_never_output():
    anchors = {a["node"]: a for r in SPEC["rivers"] for a in r["anchors"] if a["type"] == "dam"}
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    ids = {f["id"] for f in fc["features"]}
    feats = dams()
    assert {f["properties"]["id"] for f in feats} == {f"power.plant.hydro_{s}" for s in CITED_MW}
    for f in feats:
        p = f["properties"]
        slug = p["id"].removeprefix("power.plant.hydro_")
        assert p["id"] in ids and p["type"] == "plant" and p["fuel"] == "hydro"
        assert output_problems(p) == []
        assert p["value"] == SPEC["heads"][slug]["forebay"]                  # Weather's forebay level series
        assert f["geometry"]["coordinates"] == anchors[p["id"]]["lonlat"]    # the river's dam anchor, same point
        assert p["nameplate_mw"] == CITED_MW[slug] and f"{CITED_MW[slug]:,} MW" in p["callout"]["label"]


def test_seeded_red_output_on_a_node_is_caught():
    p = dams()[0]["properties"]
    assert output_problems({**p, "capacity_value": 2746})
    assert output_problems({**p, "value": "power.hydro.mica.mw_est", "unit": "MW"})
    assert output_problems({**p, "callout": {"label": "Mica", "priority": 2}})
