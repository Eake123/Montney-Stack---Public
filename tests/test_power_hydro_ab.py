"""AB hydro: AESO CSD hydro assets at their dams (network/power/hydro_ab.geojson, P5b).

Ground truth is AESO's own published numbers, from the Current Supply & Demand report (Last Update Nov 1, 2026 09:22):
the HYDRO fuel MC is 896 MW and Bighorn (BIG) is 120 MW. Every CSD hydro asset must be mapped exactly once.
Weather's AB Rivers outflow (#144) is paired where one exists. The MW per m3/s ratio k is context only, with its NO-RESULT
state after 2026-11-01 (L-034, L-050): the map never shows a fitted k as a bare number.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from montney import network
from montney.connectors import build, load_specs
from montney.connectors.base import Context

FILE = Path(__file__).resolve().parents[1] / "network" / "power" / "hydro_ab.geojson"
FIX = Path(__file__).parent / "fixtures"
SPECS = load_specs(include_disabled=True)
AESO_HYDRO_MC = 896.0
AESO_HYDRO_ASSETS = {"BIG", "BOW1", "BRA", "CHIN", "DKSN", "ICP1", "OMRH", "RYMD", "TAY1"}   # CSD hydro block, Nov 1 2026


def collection() -> dict:
    return json.loads(FILE.read_text(encoding="utf-8"))


def nodes(ctx: bool = False) -> list[dict]:
    return [f["properties"] for f in collection()["features"] if bool(f["properties"].get("context")) == ctx]


def coverage_gaps(props: list[dict], unlocated: list) -> list[str]:
    if not props:
        return ["NO-RESULT: no hydro nodes"]
    assets = [a for p in props for a in p["aeso_assets"]] + [u["asset"] for u in unlocated]
    bad = [f"{a} listed twice" for a in set(assets) if assets.count(a) > 1]
    if set(assets) != AESO_HYDRO_ASSETS:
        bad.append(f"assets {sorted(set(assets) ^ AESO_HYDRO_ASSETS)} differ from AESO's hydro block")
    total = sum(p["nameplate_mw"] for p in props) + sum(u["mc_mw"] for u in unlocated)
    if abs(total - AESO_HYDRO_MC) > 0.5:
        bad.append(f"mapped {total} MW != AESO hydro MC {AESO_HYDRO_MC}")
    return bad


def k_problems(p: dict) -> list[str]:
    """k may appear only as context: status NO-RESULT with a reason and a 2024 range, never a bare number or a row."""
    ro = p.get("river_outflow")
    if not ro or "k_mw_per_m3s" not in ro:
        return []
    k = ro["k_mw_per_m3s"]
    bad = []
    if k.get("status") != "NO-RESULT" or len(k.get("reason", "")) < 40:
        bad.append(f"{p['id']}: k must carry status NO-RESULT and its reason")
    if any(isinstance(v, (int, float)) for v in k.values()):
        bad.append(f"{p['id']}: k has a bare number")
    if set(k.get("fit_2024", {})) - {"min", "max", "note", "source"} or not {"min", "max"} <= set(k.get("fit_2024", {})):
        bad.append(f"{p['id']}: the 2024 fit must be a min/max range with its source")
    if any("k" in r.get("label", "").split() or "per" in r.get("label", "") for r in p.get("callouts_extra", [])):
        bad.append(f"{p['id']}: a callout row shows k")
    return bad


def test_geojson_validates():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    ids = {f["id"] for f in fc["features"]}
    assert {p["id"] for p in nodes()} | {p["id"] for p in nodes(True)} <= ids
    for p in nodes() + nodes(True):
        assert p["type"] == "plant" and p["fuel"] == "hydro" and p["source"].startswith("Wikipedia coordinates"), p["id"]


def test_coverage_adds_up_to_aeso_hydro():
    assert coverage_gaps(nodes(), collection().get("unlocated", [])) == []


def test_series_exist():
    unit = SPECS["aeso_ets_units"]
    assert "hydro" in unit["params"]["fuels"] and "hydro" in unit["params"]["watch_assets"]["fuels"]
    derives = {d["id"] for d in unit["derive"]}
    abr = {m.series_id for m in build(SPECS["weather_abrivers"]).declared_series()}
    for p in nodes():
        if len(p["aeso_assets"]) == 1:
            a = p["aeso_assets"][0].lower()
            assert (p["value"], p["capacity"]) == (f"aeso.unit.{a}.tng", f"aeso.unit.{a}.mc"), p["id"]
        else:
            assert {p["value"], p["capacity"]} <= derives, p["id"]
        if "river_outflow" in p:
            assert p["river_outflow"]["series"] in abr, p["id"]
    for p in nodes(True):
        if "river_outflow" in p:
            assert p["river_outflow"]["series"] in abr, p["id"]


def test_shipped_path_emits_hydro_units():
    body = (FIX / "power_csd_sample.csv").read_text(encoding="utf-8")
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))),
                  now=__import__("datetime").datetime(2026, 10, 31, 20, 10))
    res = build(SPECS["aeso_ets_units"]).run(ctx)
    meta = {m.series_id: m for m in res.meta}
    assert meta["aeso.unit.thy1.tng"].extra["fuel"] == "hydro"
    assert [o.value for o in res.obs if o.series_id == "aeso.unit.thy1.tng"] == [30]


def test_nameplates_against_cited_values():
    by = {p["aeso_assets"][0]: p for p in nodes() if len(p["aeso_assets"]) == 1}
    assert by["BIG"]["nameplate_mw"] == 120.0 == by["BIG"]["nameplate_cited_mw"]      # AESO MC = Wikipedia/TransAlta
    # The cited list is in whole MW (±0.5), and AESO MC can sit above nameplate after an uprate (AESO published a
    # Raymond Reservoir RYMD MC increase in 2025). So the bar comes from the source's precision: within max(5 %, 1 MW).
    # This is not a tolerance for our own numbers; nameplate_mw is AESO's MC, unchanged.
    for p in nodes():
        assert abs(p["nameplate_cited_mw"] - p["nameplate_mw"]) <= max(0.05 * p["nameplate_mw"], 1.0), p["id"]
    bow = by["BOW1"]
    parts = {p["id"]: p for p in nodes(True)}
    assert set(bow["parts"]) == set(parts) and all(p["part_of"] == bow["id"] for p in parts.values())
    assert sum(p["nameplate_cited_mw"] for p in parts.values()) == bow["nameplate_cited_mw"] == 326
    assert all("value" not in p and "aeso_assets" not in p for p in parts.values())   # no MW of their own


def test_k_is_never_a_bare_number():
    paired = [p for p in nodes() if "river_outflow" in p]
    assert {p["aeso_assets"][0] for p in paired} == {"BIG", "BRA"}
    assert [b for p in nodes() for b in k_problems(p)] == []


def test_seeded_reds():
    ps = nodes()
    assert any("differ" in b or "!=" in b for b in coverage_gaps([p for p in ps if p["aeso_assets"] != ["BIG"]], []))
    assert coverage_gaps([], []) == ["NO-RESULT: no hydro nodes"]
    big = json.loads(json.dumps(next(p for p in ps if p["aeso_assets"] == ["BIG"])))
    big["river_outflow"]["k_mw_per_m3s"]["value"] = 0.58                     # a bare fitted k
    assert any("bare number" in b for b in k_problems(big))
    big2 = json.loads(json.dumps(next(p for p in ps if p["aeso_assets"] == ["BIG"])))
    big2["river_outflow"]["k_mw_per_m3s"]["status"] = "OK"
    assert any("NO-RESULT" in b for b in k_problems(big2))
