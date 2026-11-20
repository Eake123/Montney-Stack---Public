"""Frontend (web/): the single map page serves with the right content types, its config is valid,
and dev fixtures can only load in ?dev=1 mode."""

import base64
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from montney import paths
from montney.store import Store

WEB = paths.WEB_DIR


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from montney.server import create_app
    with TestClient(create_app(store=Store(tmp_path / "t.duckdb"), schedule=False)) as c:
        yield c


@pytest.mark.parametrize("path,ctype", [
    ("/", "text/html"),
    ("/static/map.js", "text/javascript"),
    ("/static/lib.js", "text/javascript"),
    ("/static/weather.js", "text/javascript"),
    ("/static/callouts.js", "text/javascript"),
    ("/static/placement.js", "text/javascript"),
    ("/static/sprites.js", "text/javascript"),
    ("/static/status.js", "text/javascript"),
    ("/static/sysmenu.js", "text/javascript"),
    ("/static/gauges.js", "text/javascript"),
    ("/static/sprites/app-icon/favicon-32.png", "image/png"),
    ("/static/rivers.js", "text/javascript"),
    ("/static/sprites/nodes.json", "application/json"),
    ("/static/sprites/nodes.png", "image/png"),
    ("/static/panel.js", "text/javascript"),
    ("/static/style.js", "text/javascript"),
    ("/static/map.css", "text/css"),
    ("/static/map.json", "application/json"),
    ("/static/vendor/maplibre/maplibre-gl.mjs", "text/javascript"),
    ("/static/vendor/maplibre/maplibre-gl-worker.mjs", "text/javascript"),
    ("/static/vendor/maplibre/maplibre-gl.css", "text/css"),
    ("/static/vendor/uPlot.iife.min.js", "text/javascript"),
])
def test_assets_serve_with_content_type(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200, path
    assert r.headers["content-type"].startswith(ctype), (path, r.headers["content-type"])


def test_index_is_the_map_page(client):
    html = client.get("/").text
    assert '<script type="module" src="/static/map.js">' in html
    assert "gridstack" not in html and "app.js" not in html
    # every local asset the page references exists
    for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert client.get(ref).status_code == 200, ref


def test_module_imports_resolve():
    """Static ES-module imports between web/ files point at files that exist (no build step to catch it)."""
    for f in WEB.glob("*.js"):
        for spec in re.findall(r'^\s*import\s[^;]*?from\s+"(\.[^"]+)"', f.read_text(encoding="utf-8"), re.M):
            assert (f.parent / spec).resolve().exists(), (f.name, spec)


def test_dev_fixtures_only_load_in_dev_mode():
    # Real mode must never show synthetic data: nothing outside web/dev/ imports it statically,
    # and the single dynamic import sits behind the ?dev=1 flag.
    for f in WEB.glob("*.js"):
        src = f.read_text(encoding="utf-8")
        assert not re.search(r'^\s*import\s[^;]*from\s+"\./dev/', src, re.M), f.name
        if "dev/devapi.js" in src:
            assert f.name == "lib.js" and re.search(r"if \(DEV\) \{\s*devApi \?\?= await import\(\"\./dev/devapi\.js\"\)", src)
    assert "fixtures" not in (WEB / "index.html").read_text(encoding="utf-8")


def test_map_config_is_valid():
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    v = cfg["view"]
    (w, s), (e, n) = v["maxBounds"]
    # PLAN 10a (Erik: "zoom over all of America and northern Mexico"): generous bounds, open ocean at the edges; the
    # contiguous US, northern Mexico to 20° N and the Canada we show are inside. The weather grids end inside them
    # (lon -140..-66.5, lat 25.5..65): the field fades out over its 1° edge feather there, the basemap goes on.
    assert -180 <= w <= -141 and e >= -57 and s <= 20 and 63 <= n <= 80 and w < e and s < n
    assert w <= v["center"][0] <= e and s <= v["center"][1] <= n
    ids = set()
    for c in cfg["callouts"]:
        assert c["id"] not in ids
        ids.add(c["id"])
        a = c["anchor"]
        assert isinstance(a, str) or (len(a["at"]) == 2 and a["label"]), c["id"]
        assert c.get("tier", 2) in (0, 1, 2, 3)
        assert c["rows"] and all(("series" in r) != ("feature" in r) for r in c["rows"]), c["id"]
    ov = cfg.get("overrides", {})
    for fid, o in ov.items():
        assert re.match(r"^[a-z0-9][a-z0-9_.\-]+$", fid) and set(o) <= {"prefer", "tier", "hide", "label", "rows_before", "rank",
                                                                       "minzoom", "maxzoom", "sprite", "title", "section",
                                                                       "merge_into", "merge_until", "merge_order", "box_minzoom",
                                                                       "outer_title", "cue"}, fid
        # merged boxes (PLAN step 6): merge into another overridden feature, never itself, below a numeric zoom (or
        # "outer", L-087: the last view.outer_span zooms); heads are text
        if "merge_into" in o:
            assert o["merge_into"] != fid and o["merge_into"] in ov and "merge_into" not in ov[o["merge_into"]], fid
        zoomish = lambda z: isinstance(z, (int, float)) or z == "outer"
        assert zoomish(o.get("merge_until", 0)) and "merge_until" not in o or "merge_into" in o, fid
        assert zoomish(o.get("box_minzoom", 0)) and isinstance(o.get("minzoom", 0), (int, float)), fid   # the server reads minzoom
        assert all(isinstance(o[k], str) and o[k].strip() for k in ("title", "section", "outer_title") if k in o), fid
    assert cfg["gas_day"]["start_hour_utc"] == 15  # NGTL gas day: 08:00 MST = fixed 15:00 UTC
    layer_ids = [l["id"] for l in cfg["layers"]]
    assert len(layer_ids) == len(set(layer_ids))


def test_overview_zoom_levels():
    """L-106 8a (docs/REGIONS.md §5): fully zoomed out, Erik sees the generated region boxes (one per region with lines)
    and the region outlines, nothing else. The region boxes hand off at zoom 6 (map.json `systems.region`, no region id
    in JS) to each region's detail: the old hand-built overview boxes are tier-1 detail boxes now, or retired."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov = cfg["overrides"]
    feats = {}
    for f in sorted((paths.REPO_ROOT / "network").glob("*/*.geojson")):
        for ft in json.loads(f.read_text(encoding="utf-8"))["features"]:
            feats[ft["properties"]["id"]] = ft["properties"]

    def tier(fid):
        return ov.get(fid, {}).get("tier", feats[fid].get("callout", {}).get("priority", 2))

    region_boxes = {fid for fid, p in feats.items() if p.get("type") == "region" and p.get("system") == "region"
                    and p.get("callout")}
    assert len(region_boxes) >= 7, region_boxes            # fails closed if the generated boxes can't be read
    assert cfg["systems"]["region"]["maxzoom"] == cfg["tiers"]["1"] == 6
    overview_feats = {fid for fid, p in feats.items() if p.get("callout") and not p.get("context")
                      and not ov.get(fid, {}).get("hide") and tier(fid) == 0 and not ov.get(fid, {}).get("minzoom")}
    assert overview_feats == region_boxes
    # no map.json box and no gauge box is left at tier 0
    assert [c["id"] for c in cfg["callouts"] if c.get("tier", 2) == 0] == []
    assert all(s.get("tier", cfg["gauge_boxes"].get("tier", 3)) >= 1 for s in cfg["gauge_boxes"]["stations"].values())
    # the hand-merged overview boxes (Alberta stack, PacNW, California, Ontario, Québec, Midwest, MB/QC hydro) are
    # tier-1 detail from zoom 6; the TC overview boxes, which only bridged to the tier-1 TC boxes, are retired
    for fid in ("gas.production.total", "gas.ab_demand", "gas.kitimat", "gas.huntingdon", "power.region.aeso_load",
                "gas.empress", "gas.mcneill", "gas.alberta_bc", "gas.pnw.imports", "gas.cal.demand", "gas.ont.demand",
                "gas.qc.demand", "gas.midwest.chicago", "power.hydro_summary.mb", "power.hydro_summary.qc"):
        assert ov[fid]["tier"] == 1, fid
    for fid in ("gas.tc.overview.sk_demand", "gas.tc.overview.mb_demand", "gas.tc.overview.mainline", "gas.tc.overview.ontario"):
        assert ov[fid] == {"tier": 1, "hide": True}, fid
    ids = {c["id"] for c in cfg["callouts"]}
    assert not ids & {"c.sk_outer", "c.mb_outer", "c.bc_demand_outer", "c.storage", "c.exports", "c.bc_exports"}
    # no merge config that only applied below zoom 6 (dead now): what still merges does so above the tier-1 zoom
    for k, o in [*ov.items(), *((c["id"], c) for c in cfg["callouts"])]:
        if "merge_until" in o:
            assert isinstance(o["merge_until"], (int, float)) and o["merge_until"] > 6, k
        assert "outer_title" not in o and o.get("minzoom") != "outer" and o.get("box_minzoom") != "outer", k
    # the section heads and the lines a folded region box keeps (map.json `sections`), by standard line (REGIONS.md §4)
    sec = cfg["sections"]
    assert {k: v["head"] for k, v in sec.items() if k != "_doc"} == {"gas": "Gas · MMcf/d", "power": "Power · MW", "oil": "Oil · bbl/d"}
    # every region box carries the six v2 rows (montney/regions.py box_v2) and nothing of the old layout; its own value
    # is one of its rows
    for fid in region_boxes:
        p = feats[fid]
        assert len(p["box_rows"]) == 6 and all(r["section"] in sec for r in p["box_rows"]), fid
        assert "callouts_extra" not in p and "panel" not in p, fid
        assert "value" not in p or p["value"] in {r["series"] for r in p["box_rows"]}, fid


def test_old_board_is_gone():
    for f in ("app.js", "widgets.js", "app.css", "vendor/gridstack-all.js"):
        assert not (WEB / f).exists(), f
    assert not list((paths.REPO_ROOT / "tabs").glob("*.json"))


def test_attribution_carries_required_credits():
    style = (WEB / "style.js").read_text(encoding="utf-8")
    for needle in ("OpenFreeMap", "OpenMapTiles", "OpenStreetMap", "Open Government Licence – Canada",
                   "U.S. Geological Survey", "ETOPO1", "ArcticDEM", "NOAA GFS", "Open-Meteo", "TC Energy", "AESO"):
        assert needle in style, needle


def test_time_label_is_mt_not_iana_abbr():
    # IANA labels Alberta's permanent UTC-6 "CST" from 2026-11-01; the UI must say MT, never the raw abbr.
    lib = (WEB / "lib.js").read_text(encoding="utf-8")
    assert 'abbr: "MT"' in lib and "tz.abbr" not in lib


# ---- P4a callout stability: placement.js logic run under node (the shipped module, not a Python port)
NODE = shutil.which("node")


def _data_url(src: str) -> str:
    return "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()


def _node(script: str, module: str = "placement.js"):
    """Run `script` as an ES module with `P` bound to web/<module> (a module with no imports). It must print one
    JSON line."""
    if not NODE:
        if os.environ.get("CI"):
            pytest.fail("node is required for the web module tests in CI")
        pytest.skip("node not installed")
    src = (WEB / module).read_text(encoding="utf-8")
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    prog = f"const P = await import({json.dumps(url)});\n{script}"
    # the program goes in on stdin: as an -e argument it can pass Windows' 32 k command-line limit
    r = subprocess.run([NODE, "--input-type=module"], input=prog, capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


HYST_JS = """
const bounds = [6, 6.75, 7.5];
const levels = (ze) => bounds.filter((b) => ze >= b).length;
function walk(zs, h) {           // returns level flips and the zooms where each flip happened
  let ze = null, lv = null, flips = [];
  for (const z of zs) {
    ze = P.levelZoom(z, ze, bounds, h);
    const l = levels(ze);
    if (lv != null && l !== lv) flips.push([z, l]);
    lv = l;
  }
  return flips;
}
const jitter = [];
for (const b of bounds) for (let i = 0; i < 20; i++) jitter.push(b - 0.07, b + 0.07);
const up = [], down = [];
for (let i = 0; i <= 400; i++) up.push(+(4 + i * 0.01).toFixed(2));
for (let i = 0; i <= 400; i++) down.push(+(8 - i * 0.01).toFixed(2));
console.log(JSON.stringify({
  H: P.HYST,
  jitter: walk([5.5, ...jitter], P.HYST), jitterNoHyst: walk([5.5, ...jitter], 0),
  up: walk(up, P.HYST), down: walk(down, P.HYST),
  far: P.levelZoom(8, 4, bounds), mid: P.levelZoom(6.4, 6.3, bounds), first: P.levelZoom(6.01, null, bounds),
  past: levels(P.levelZoom(6.08, 5.95, bounds)), back: levels(P.levelZoom(5.92, 6.05, bounds)),
}));
"""


def test_zoom_level_hysteresis():
    """PLAN P4a: ~0.15 zoom of hysteresis at each zoom-level boundary, so boxes and dots don't flicker."""
    r = _node(HYST_JS)
    h = r["H"]
    assert 0.1 <= h <= 0.2
    # The jitter fixture really crosses the boundaries: with no hysteresis it flips on every step (fails closed).
    assert len(r["jitterNoHyst"]) >= 100, r["jitterNoHyst"][:5]
    # With hysteresis, wobbling ±0.07 around 6, 6.75 and 7.5 only changes level when moving between boundaries:
    # 5.5 -> 6.07 holds below 6, then 6.68 is a real move past 6 and 7.43 a real move past 6.75 (2 flips, no flicker).
    assert [l for _, l in r["jitter"]] == [1, 2], r["jitter"]
    # A steady zoom changes level exactly once per boundary, h/2 past it in the direction of travel.
    assert [(round(z, 2), l) for z, l in r["up"]] == [(round(b + h / 2, 2), i + 1) for i, b in enumerate([6, 6.75, 7.5])]
    assert [(round(z, 2), l) for z, l in r["down"]] == [(round(b - h / 2 - 0.01, 2), 2 - i) for i, b in enumerate([7.5, 6.75, 6])]
    # Away from a boundary the level zoom is the zoom; past the band it always switches.
    assert r["far"] == 8 and r["mid"] == 6.4 and r["first"] == 6.01
    assert r["past"] == 1 and r["back"] == 0


SLOT_JS = """
const W = 800, H = 600;
const mk = (rects) => (r, strict = true) => r.x < 4 || r.y < 4 || r.x + r.w > W - 4 || r.y + r.h > H - 4 ||
  rects.some((b) => (strict || !b.soft) && r.x < b.x + b.w && b.x < r.x + r.w && r.y < b.y + b.h && b.y < r.y + r.h);
const p = { x: 400, y: 300 }, w = 120, h = 32, prefer = ["E", "NE", "SE", "W"];
const free = P.chooseSlot({ p, w, h, prefer, hit: mk([]) });
// sticky: last slot W@64 still free, so keep it although E@34 (preferred) is free too
const kept = P.chooseSlot({ p, w, h, prefer, last: { dir: "W", L: 64 }, hit: mk([]) });
// forced: a box covers the outer edge of W@64 (not W@34), so move, keeping the W side at the other length first
const wRect = P.slotRect(p, "W", 64, w, h);
const forced = P.chooseSlot({ p, w, h, prefer, last: { dir: "W", L: 64 }, hit: mk([{ ...wRect, w: 24 }]) });
// a soft obstacle (a minor dot) doesn't move a lenient (tier 0/1) box, but does move a strict one
const dot = { x: wRect.x + 10, y: wRect.y + 5, w: 18, h: 18, soft: true };
const soft = P.chooseSlot({ p, w, h, prefer, last: { dir: "W", L: 64 }, hit: mk([dot]), lenient: true });
const strict = P.chooseSlot({ p, w, h, prefer, last: { dir: "W", L: 64 }, hit: mk([dot]) });
// handoff: start nearest the partner's centre (up-left), not at the preferred E
const near = P.chooseSlot({ p, w, h, prefer, near: { x: 300, y: 240 }, hit: mk([]) });
const none = P.chooseSlot({ p, w, h, prefer, hit: () => true });
const t = new P.Tween(0, 200); t.set(100, 1000);
const t0 = new P.Tween(0, 0); t0.set(100, 1000);
console.log(JSON.stringify({ free, kept, forced, soft, strict, near, none,
  tw: [t.at(1000), t.at(1100), t.at(1200), t.at(5000), t.busy(1100), t.busy(1200)], t0: t0.at(1000) }));
"""


def test_sticky_slots_and_handoff():
    """PLAN P4a: a box keeps its side and leader length unless a hard collision forces a move; a handoff starts
    nearest its partner; moves glide over ~200 ms."""
    r = _node(SLOT_JS)
    assert (r["free"]["dir"], r["free"]["L"]) == ("E", 34)
    assert (r["kept"]["dir"], r["kept"]["L"], r["kept"]["kept"]) == ("W", 64, True)
    assert (r["forced"]["dir"], r["forced"]["L"], r["forced"]["kept"]) == ("W", 34, False)
    assert (r["soft"]["dir"], r["soft"]["L"], r["soft"]["kept"]) == ("W", 64, True)
    assert not (r["strict"]["dir"] == "W" and r["strict"]["L"] == 64)
    assert r["near"]["dir"] == "NW", r["near"]
    assert r["none"] is None
    a, mid, end, late, busy_mid, busy_end = r["tw"]
    assert a == 0 and 50 < mid < 100 and end == 100 and late == 100 and busy_mid and not busy_end
    assert r["t0"] == 100


# ---- P4c node sprites: web/sprites.js mapping against the real network and Art's atlas (L-028, docs/art/NODES.md)
def test_node_sprites_map_to_atlas():
    """Every network node gets an atlas kind (regions keep their ring), every image the map can ask for exists at
    16 and 32 px, compound fuels are never guessed, and the glow levels follow NODES.md."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    manifest = json.loads((WEB / "sprites" / "nodes-manifest.json").read_text(encoding="utf-8"))
    atlas = set(json.loads((WEB / "sprites" / "nodes.json").read_text(encoding="utf-8")))
    atlas2 = set(json.loads((WEB / "sprites" / "nodes@2x.json").read_text(encoding="utf-8")))
    nodes = [ft["properties"] for f in sorted((paths.REPO_ROOT / "network").glob("*/*.geojson"))
             for ft in json.loads(f.read_text(encoding="utf-8"))["features"] if ft["properties"].get("kind") == "node"]
    assert len(nodes) >= 40, len(nodes)   # fails closed if the network can't be read
    overrides = {k: v["sprite"] for k, v in cfg.get("overrides", {}).items() if "sprite" in v}
    script = f"""
const nodes = {json.dumps(nodes)}, ov = {json.dumps(overrides)};
const kinds = nodes.filter((p) => p.type !== "region").map((p) => [p.id, P.spriteKind(p, ov[p.id])]);
const glow = [[null, null], [0, 0], [5, 0.01], [50, 1/3], [51, 0.34], [100, 2/3], [101, 0.67], [500, 1.4], [5, null],
  [-5, 5/300], [-200, 200/300]]
  .map(([v, u]) => P.spriteState("plant-cogen", v, u));
console.log(JSON.stringify({{ kinds, glow,
  states: Object.fromEntries([...new Set(kinds.map(([, k]) => k))].map((k) => [k, [...new Set([null, 0, 0.2, 0.5, 0.9].map((u) => P.spriteState(k, u == null ? null : 10, u)))]])),
  compound: P.spriteKind({{ type: "plant", fuel: "cogeneration + simple_cycle" }}),
  hyphen: P.spriteKind({{ type: "plant", fuel: "Gas-fired steam" }}), bogus: P.spriteKind({{ type: "plant", fuel: "fusion" }}),
  override: P.spriteKind({{ type: "plant", fuel: "cogeneration + simple_cycle" }}, "plant-cogen"),
  primary: P.spriteKind({{ type: "plant", fuel: "cogeneration + simple_cycle", fuel_primary: "simple_cycle" }}),
  badge: [P.needsBadge("plant-cogen", null, null, false), P.needsBadge("plant-cogen", 10, 0.5, false),
          P.needsBadge("receipt", 10, null, false), P.needsBadge("receipt", 10, null, true)],
  center: [...P.CENTER_KINDS], big: P.SPRITE_32_Z,
  css: [[16, 1], [16, 1.25], [16, 1.5], [16, 2], [32, 1.25], [32, 1.5]].map(([b, pr]) => +P.spriteCss(b, pr).toFixed(3)) }}));
"""
    r = _node(script, "sprites.js")
    assert len(r["kinds"]) == len([n for n in nodes if n["type"] != "region"]) > 0
    missing = [(i, k) for i, k in r["kinds"] if k not in manifest]
    assert not missing, missing
    for kind, states in r["states"].items():
        for st in states:
            for size in (16, 32):
                key = f"{kind}-{size}-{st}"
                assert key in atlas and key in atlas2, key
                assert manifest[kind]["sizes"][str(size)][st] == key
    for size in (16, 32):
        assert f"unknown-{size}-static" in atlas
    for kind in manifest:   # anchors agree with the manifest
        assert (kind in r["center"]) == (manifest[kind]["anchor"] == "center"), kind
    # NODES.md: measured zero -> glow0, <= 1/3 -> glow1, <= 2/3 -> glow2, above -> glow3; missing -> glow0 + badge
    # ... and negative MW (a plant drawing power) doesn't glow: STYLE.md max(MW, 0), though util = |v|/cap
    assert r["glow"] == ["glow0", "glow0", "glow1", "glow1", "glow2", "glow2", "glow3", "glow3", "glow0", "glow0", "glow0"]
    # the sprite's real CSS size, which the badge, the selection ring and the callout keep-out use
    assert r["css"] == [16, 12.8, 21.333, 16, 25.6, 42.667]
    assert r["compound"] == "plant-unknown" and r["bogus"] == "plant-unknown" and r["override"] == "plant-cogen"
    assert r["hyphen"] == "plant-gas-fired-steam"
    # Power's fuel_primary (#60) picks a compound plant's sprite; the real network's two compound plants get theirs
    assert r["primary"] == "plant-simple-cycle"
    kinds = dict(r["kinds"])
    assert kinds["power.plant.genesee"] == "plant-combined-cycle" and kinds["power.plant.rainbow_lake"] == "plant-simple-cycle"
    assert r["badge"] == [True, False, False, True]
    assert r["big"] == 8
    for fid, spr in overrides.items():
        assert spr in manifest, (fid, spr)


# ---- P4c item 3: gas flanges as an overlay under LOG L-028 G1-G7 (docs/art/PIPELINE_FLANGES.md)
# G1 golden: the data layers' paint on main (563b5c6) before any pixel treatment. The overlay must leave it as is.
G1_GOLDEN = json.loads(r'''{"gas-casing": {"line-color": "#020504", "line-width": ["interpolate", ["linear"], ["abs", ["coalesce", ["get", "v"], 0]], 0, 4, 8000, 11]}, "gas": {"line-width": ["interpolate", ["linear"], ["abs", ["coalesce", ["get", "v"], 0]], 0, 1.5, 8000, 7], "line-color": ["case", ["<", ["coalesce", ["get", "util"], -1], 0], "#9fb3a9", ["interpolate", ["linear"], ["coalesce", ["get", "util"], -1], 0, "#7f948a", 0.6, "#eef6f2", 0.85, "#ffb547", 1, "#ff4d6d"]]}, "power-casing": {"line-color": "#020504", "line-width": 4}, "power": {"line-width": 2, "line-dasharray": [2, 1.2], "line-color": ["case", ["<", ["coalesce", ["get", "util"], -1], 0], "#b9a6db", ["interpolate", ["linear"], ["coalesce", ["get", "util"], -1], 0, "#7d68a8", 0.6, "#c9a2ff", 0.85, "#ffb547", 1, "#ff4d6d"]]}, "areas-line": {"line-width": ["case", ["<", ["coalesce", ["get", "util"], -1], 0.85], 1, 1.6], "line-dasharray": [2, 2], "line-color": ["case", ["<", ["coalesce", ["get", "util"], -1], 0], "rgba(211,236,224,0.35)", ["interpolate", ["linear"], ["coalesce", ["get", "util"], -1], 0, "rgba(211,236,224,0.35)", 0.85, "rgba(255,181,71,0.8)", 1, "rgba(255,77,109,0.9)"]]}}''')
STYLE_STUBS = {"TERRARIUM": '"stub"', "BREAKOUT_Z": "6"}


def _network_layers(src=None):
    """The shipped `networkLayers()` from web/style.js, run under node with its imports stubbed (an import this test
    doesn't know fails loudly). `src` lets a test feed an edited copy."""
    src = src if src is not None else (WEB / "style.js").read_text(encoding="utf-8")

    real = {name: "data:text/javascript;base64," + base64.b64encode((WEB / f"{name}.js").read_bytes()).decode()
            for name in ("sprites", "gauges")}

    def stub(m):   # sprites.js and gauges.js have no imports, so use the real modules; stub the browser-bound ones
        if m.group(2) in real:
            return f'import {{{m.group(1)}}} from "{real[m.group(2)]}";'
        return "\n".join(f"const {n.strip()} = {STYLE_STUBS[n.strip()]};" for n in m.group(1).split(","))
    src = re.sub(r'^import \{([^}]*)\} from "\./([a-z]+)\.js";', stub, src, flags=re.M)
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "style.js gained an import the test doesn't stub"
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    return _node(f"""const S = await import({json.dumps(url)});
console.log(JSON.stringify({{ layers: S.networkLayers(), casing: S.CASING_W, min: S.FLANGE_MIN_STROKE,
  spacing: S.FLANGE_SPACING, dot: {{ "1": S.dotPaint(1, 8, 1), "1.25": S.dotPaint(1, 8, 1.25), "z7_1.5": S.dotPaint(1, 7, 1.5) }},
  anim_on: S.dotPaint(1, 7, 1, {{ on: true, t: 12.34 }}), anim_off: S.dotPaint(1, 7, 1) }}));""")


def _ev(e, p):
    """Evaluate the MapLibre expression subset the network layers use, for one feature's properties `p`."""
    if not isinstance(e, list):
        return e
    op, a = e[0], e[1:]
    if op == "get":
        return p.get(a[0])
    if op == "has":
        return a[0] in p
    if op == "literal":
        return a[0]
    if op == "coalesce":
        for x in a:
            v = _ev(x, p)
            if v is not None:
                return v
        return None
    if op == "abs":
        return abs(_ev(a[0], p))
    if op == "floor":
        return math.floor(_ev(a[0], p))
    if op in ("*", "+"):
        vals = [_ev(x, p) for x in a]
        return math.prod(vals) if op == "*" else sum(vals)
    if op == "-":
        return _ev(a[0], p) - _ev(a[1], p) if len(a) == 2 else -_ev(a[0], p)
    if op == "%":
        return math.fmod(_ev(a[0], p), _ev(a[1], p))
    if op == "!":
        return not _ev(a[0], p)
    if op == "all":
        return all(_ev(x, p) for x in a)
    if op in ("<", ">", ">=", "<=", "==", "!="):
        x, y = _ev(a[0], p), _ev(a[1], p)
        return {"<": lambda: x < y, ">": lambda: x > y, ">=": lambda: x >= y, "<=": lambda: x <= y,
                "==": lambda: x == y, "!=": lambda: x != y}[op]()
    if op == "case":
        for i in range(0, len(a) - 1, 2):
            if _ev(a[i], p):
                return _ev(a[i + 1], p)
        return _ev(a[-1], p)
    if op == "step":
        x, out = _ev(a[0], p), a[1]
        for i in range(2, len(a), 2):
            if x >= a[i]:
                out = a[i + 1]
        return out
    if op == "interpolate":
        assert a[0] == ["linear"]
        x, stops = _ev(a[1], p), a[2:]
        if x <= stops[0]:
            return stops[1]
        for i in range(0, len(stops) - 2, 2):
            x0, y0, x1, y1 = stops[i], stops[i + 1], stops[i + 2], stops[i + 3]
            if x <= x1:
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return stops[-1]
    raise AssertionError(f"expression op not handled: {op}")


def _flange_h(w):
    """PIPELINE_FLANGES.md: casing <= 4 -> 6; 4 < w <= 6 -> 8; 6 < w <= 8 -> 10; 8 < w <= 10 -> 12; 10 < w <= 11 -> 14."""
    for top, h in ((4, 6), (6, 8), (8, 10), (10, 12), (11, 14)):
        if w <= top + 1e-9:
            return h
    raise AssertionError(w)


def test_pipeline_overlay_guard_g1_to_g6():
    """L-028: the data layers keep their paint (G1); flanges are a separate symbol overlay, never a line-pattern, on
    gas only (round 1); flange height tracks the casing width from the same |flow| with exact interval boundaries;
    off for zero, missing and stale flow (G5) and where the casing is narrower than 3 px (G4); width monotonic (G3)."""
    r = _network_layers()
    layers = {l["id"]: l for l in r["layers"]}
    for lid, paint in G1_GOLDEN.items():                     # G1: unchanged data channel
        assert layers[lid]["paint"] == paint, lid
    for l in r["layers"]:                                     # never a line-pattern
        assert "line-pattern" not in l.get("paint", {}), l["id"]
    fl = layers["gas-flanges"]
    assert fl["type"] == "symbol" and fl["source"] == "net"
    ids = [l["id"] for l in r["layers"]]
    assert ids.index("gas") < ids.index("gas-flanges") < ids.index("nodes")
    lay = fl["layout"]
    assert lay["symbol-placement"] == "line" and lay["symbol-spacing"] == r["spacing"] == 64
    assert lay["icon-size"] == 1 and lay["icon-rotation-alignment"] == "map" and lay["icon-pitch-alignment"] == "viewport"
    assert lay["icon-keep-upright"] is True   # the asymmetric collar's light row stays on top in both directions
    # line-placed overlays: gas flanges and (round 2) the power joints, nothing else
    assert sorted(l["id"] for l in r["layers"] if (l.get("layout") or {}).get("symbol-placement") == "line") == ["gas-flanges", "power-joints"]
    sprites = json.loads((WEB / "sprites" / "flanges.json").read_text(encoding="utf-8"))

    def gas(v, **kw):
        return {"kind": "link", "system": "gas", "v": v, "util": 0.5, "stale": False, **kw}
    b = lambda w: (w - 4) / 7 * 8000   # the flow at which the casing is exactly w px
    flows = [1e-9, 1, 1000, b(6), b(6) + 1e-3, 3000, b(8), b(8) + 1e-3, b(10), b(10) + 1e-3, 7999, 8000, 9000, -3000, -8000]
    for v in flows:
        cw = _ev(layers["gas-casing"]["paint"]["line-width"], gas(v))
        img = _ev(lay["icon-image"], gas(v))
        assert img in sprites, img
        assert int(img.rsplit("-", 1)[1]) == _flange_h(cw), (v, cw, img)
    show = lambda p: _ev(fl["filter"], p)
    assert show(gas(1000)) and show(gas(-1000))
    assert not show(gas(0)) and not show(gas(None)) and not show(gas(1000, stale=True))           # G5
    assert not show({**gas(1000), "system": "power"}) and not show({**gas(1000), "kind": "node"})  # gas links only
    # G3: width monotonic in |v| for the base line
    w = [_ev(layers["gas"]["paint"]["line-width"], gas(v)) for v in (0, 2000, 4000, 8000, 12000)]
    assert w == sorted(w) and w[0] < w[-1]
    # G4: today the casing is never under 3 px, so the overlay always has room ...
    assert r["casing"][0] >= r["min"] == 3
    # ... and if the casing got narrower, the overlay switches off for those flows (exercised on an edited copy)
    narrow = _network_layers((WEB / "style.js").read_text(encoding="utf-8")
                             .replace("export const CASING_W = [4, 11];", "export const CASING_W = [1, 11];"))
    nl = {l["id"]: l for l in narrow["layers"]}
    assert not _ev(nl["gas-flanges"]["filter"], gas(500)) and _ev(nl["gas-flanges"]["filter"], gas(4000))


def test_alert_pins_sit_off_the_real_sprite():
    """Alert pins split by the pinned feature's anchor (circle-translate can't vary per feature) and sit off the
    sprite's top-left corner by its real CSS size (spriteCss: 32 px art = 25.6 CSS px at 125 %, 16 = 21.33 at 150 %)."""
    r = _network_layers()
    layers = {l["id"]: l for l in r["layers"]}
    pin_b, pin_c = layers["event-pins"], layers["event-pins-c"]
    for anchor, want in (("bottom", "event-pins"), ("center", "event-pins-c"), (None, "event-pins")):
        p = {"severity": "warn", **({"anchor": anchor} if anchor else {})}
        assert [lid for lid, l in (("event-pins", pin_b), ("event-pins-c", pin_c)) if _ev(l["filter"], p)] == [want], anchor
    for key, s in (("1", 32), ("1.25", 25.6), ("z7_1.5", 16 * 2 / 1.5)):
        d = r["dot"][key]
        assert d["event-pins"]["circle-translate"] == pytest.approx([-(s / 2 + 5), -(s - 3)]), key
        assert d["event-pins-c"]["circle-translate"] == pytest.approx([-(s / 2 + 5), -(s / 2 - 3)]), key
        assert d["node-sel"]["circle-radius"] == pytest.approx(s / 2 + 4), key


# ---- P4c motion: wind rotors and compressor ticks (docs/art/STYLE.md "Silhouettes and data" + "Budget", L-005)
ANIM_JS = """
const span = (u) => {   // frame changes per 1-s window over 10 s of the shared clock, sampled every 1 ms
  let prev = P.frameAt(u, 0), changes = [], seq = [prev];
  for (let i = 1; i <= 10000; i++) { const f = P.frameAt(u, i / 1000); if (f !== prev) { changes.push(i); if (seq.length < 6) seq.push(f); } prev = f; }
  let worst = 0;
  for (let i = 0; i < changes.length; i++) { let n = 0; while (i + n < changes.length && changes[i + n] - changes[i] < 1000) n++; worst = Math.max(worst, n); }
  return { total: changes.length, worst, seq };
};
const S = (k, v, u, st) => P.animSpeed(k, v, u, st);
console.log(JSON.stringify({
  frames: P.ANIM_FRAMES, tick: P.ANIM_TICK_MS, kinds: [...P.ANIM_KINDS],
  wind: [S("plant-wind", 50, 0.25, false), S("plant-wind", 500, 1.7, false), S("plant-wind", 0, 0, false),
         S("plant-wind", -5, 5 / 300, false), S("plant-wind", 50, 0.25, true), S("plant-wind", null, null, false),
         S("plant-wind", 50, null, false), S("plant-cogen", 50, 0.25, false)],
  comp: [S("compressor", 300, 0.3, false), S("compressor", -300, 0.3, false), S("compressor", 0, 0, false),
         S("compressor", -300, 0.3, true), S("compressor", 9000, 1.4, false)],
  badge: [P.needsBadge("plant-wind", null, null, false), P.needsBadge("compressor", 10, null, false), P.needsBadge("plant-wind", 5, 0.1, false)],
  rate: Object.fromEntries([1, 0.5, 0.1, -1, -0.3, 1.0].map((u) => [String(u), span(u)])),
  at: Object.fromEntries([0.25, 0.6, 1, -0.4, -1].map((u) => [String(u), P.frameAt(u, 12.34)])),
}));
"""


def test_sprite_motion_rules():
    """Wind: cycles/s = clamp(max(MW,0)/ref, 0, 1), so MW <= 0 stops it. Compressor: same on |flow|, signed. Stale or
    missing freezes (with `?`). Never more than 4 frame updates a second (4 frames per cycle, |u| <= 1, 4 Hz clock)."""
    r = _node(ANIM_JS, "sprites.js")
    assert r["frames"] == 4 and r["tick"] >= 250 and set(r["kinds"]) == {"plant-wind", "compressor"}
    assert r["wind"] == [0.4375, 1, 0, 0, 0, 0, 0, 0]      # 0.25 + 0.75·util; MW <= 0 stopped; stale/missing frozen; not wind 0
    assert r["comp"] == [0.3, -0.3, 0, 0, 1]               # signed direction; zero and stale static; clamped at the reference
    assert r["badge"] == [True, True, False]
    for u, s in r["rate"].items():
        assert s["worst"] <= 4, (u, s)                     # STYLE.md budget: <= 4 frame updates/s per feature
        assert s["total"] == round(4 * abs(float(u)) * 10) or abs(s["total"] - 4 * abs(float(u)) * 10) <= 1, (u, s)
    assert r["rate"]["1"]["seq"][:5] == [0, 1, 2, 3, 0] and r["rate"]["-1"]["seq"][:5] == [3, 2, 1, 0, 3]   # reverse flow
    # the shipped MapLibre expressions agree with frameAt, and only moving features use the frame layers
    lay = _network_layers()
    layers = {l["id"]: l for l in lay["layers"]}
    anim_ids = [f"nodes-anim-{k}" for k in range(4)]
    atlas = set(json.loads((WEB / "sprites" / "nodes.json").read_text(encoding="utf-8")))
    for k, lid in enumerate(anim_ids):
        L = layers[lid]
        assert L["type"] == "symbol" and not _ev(L["filter"], {"kind": "node", "spr_kind": "plant-wind", "spr_u": 0})
        assert _ev(L["filter"], {"kind": "node", "spr_kind": "plant-wind", "spr_u": 0.3})
        for kind in r["kinds"]:
            for size in (16, 32):
                assert f"{kind}-{size}-frame{k}" in atlas
    for u, want in r["at"].items():
        p = {"kind": "node", "spr_kind": "plant-wind", "spr_u": float(u), "zmin": 0, "context": False}
        on = [_ev(lay["anim_on"][lid]["icon-opacity"], p) for lid in anim_ids]
        assert on == [1 if k == want else 0 for k in range(4)], (u, on, want)
        assert _ev(lay["anim_on"]["nodes"]["icon-opacity"], p) == 0      # the static image steps aside while moving
        assert [_ev(lay["anim_off"][lid]["icon-opacity"], p) for lid in anim_ids] == [0, 0, 0, 0]
        assert _ev(lay["anim_off"]["nodes"]["icon-opacity"], p) == 1     # off: historical / reduced motion -> static
    still = {"kind": "node", "spr_kind": "plant-wind", "spr_u": 0, "zmin": 0, "context": False}
    assert _ev(lay["anim_on"]["nodes"]["icon-opacity"], still) == 1


# ---- P4c pixel callout frame + leaders (Art #41, approved L-041 Q10; docs/art/CALLOUT_PROPOSAL.md)
def _css_rule(css: str, selector: str) -> str:
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


SMOKE_JS = """
const S = (k, v, u, st = false, est = false) => P.smokeSpec(k, v, u, st, est);
console.log(JSON.stringify({
  full: S("plant-combined-cycle", 800, 0.93), low: S("plant-gas-fired-steam", 180, 0.21),
  none: [S("plant-cogen", 0, 0), S("plant-cogen", -5, 0.02), S("plant-cogen", 300, 0.5, true), S("plant-cogen", 300, 0.5, false, true),
         S("plant-cogen", null, null), S("plant-wind", 50, 0.3), S("plant-hydro", 50, 0.3), S("gas-processing", 50, 0.3)],
}));
"""


def test_smoke_only_from_fresh_measured_gas_output():
    """Gas-plant smoke (PLAN 4b, docs/art/SMOKE.md): a plume only for a gas-fired plant with fresh, measured, positive
    output; the thin puff below a third of reference; nothing (not a frozen puff) for zero, negative, stale, estimated,
    missing, or a non-gas kind."""
    r = _node(SMOKE_JS, "sprites.js")
    assert r["full"]["variant"] == "normal" and 0.9 < r["full"]["u"] <= 1
    assert r["low"]["variant"] == "low" and 0.25 < r["low"]["u"] < 0.5
    assert r["none"] == [None] * 8


# docs/REGIONS.md §4 `FUELS`: the only fuel keys (montney/regions.py on the Regions branch). Every one gets a sprite.
REGION_FUELS = ["gas", "coal", "nuclear", "hydro", "pumped_storage", "wind", "solar", "battery", "oil", "biomass",
                "geothermal", "other"]
FUELS_JS = """
const fuels = %s, K = (f) => P.spriteKind({ type: "plant", fuel: f });
const B = (v, st = false, est = false) => P.spriteState("plant-battery", v, v == null ? null : Math.abs(v) / 100, st, est);
const S = (k, v, u, st = false, est = false) => P.smokeSpec(k, v, u, st, est);
console.log(JSON.stringify({
  kinds: Object.fromEntries(fuels.map((f) => [f, K(f)])), upper: K("Pumped Storage"),
  glow: ["plant-gas", "plant-coal", "plant-oil"].map((k) => [0.1, 0.5, 0.9].map((u) => P.spriteState(k, 10, u))),
  est: P.spriteState("plant-coal", 60, 0.6, false, true),
  statics: ["plant-biomass", "plant-geothermal", "plant-pumped-storage"].map((k) => P.spriteState(k, 50, 0.5)),
  battery: [B(40), B(-25), B(0), B(null), B(40, true), B(-25, false, true), P.spriteState("plant-battery", NaN, null)],
  bbadge: [P.needsBadge("plant-battery", null, null, false), P.needsBadge("plant-battery", -25, 0.25, false)],
  smoke: ["plant-gas", "plant-coal", "plant-oil"].map((k) => S(k, 300, 0.5)),
  nosmoke: [S("plant-coal", 300, 0.5, false, true), S("plant-oil", 300, 0.5, true), S("plant-battery", 40, 0.4),
            S("plant-geothermal", 40, 0.4), S("plant-biomass", 40, 0.4)],
  offset: P.SMOKE_OFFSET,
  isEst: [P.isEstimate({ quality: "estimate" }), P.isEstimate({ band: 0.1 }), P.isEstimate({ estimate: { lo: "a", hi: "b" } }),
          P.isEstimate({ quality: "measured" }), P.isEstimate({})],
}));
"""


def test_regions_fuels_map_to_sprites():
    """L-106 (Art #404, docs/art/NODES.md): every Regions FUELS key draws its own plant sprite (`other` the unknown-fuel
    one), and every state the map can pick exists at 16 and 32 px. Gas/coal/oil glow by utilisation thirds and smoke
    only for fresh measured output, at Art's stack offsets. A battery takes its direction from fresh, measured, signed net
    MW only (> 0 discharge, < 0 charge), otherwise its static rest image, never a glow or a timer. An estimate keeps a
    steady glow but never smokes or sets a battery direction."""
    try:
        from montney.regions import FUELS   # the source of truth once the Regions paradigm is on main
        assert list(FUELS) == REGION_FUELS
    except ImportError:
        pass
    manifest = json.loads((WEB / "sprites" / "nodes-manifest.json").read_text(encoding="utf-8"))
    atlas = set(json.loads((WEB / "sprites" / "nodes.json").read_text(encoding="utf-8")))
    atlas2 = set(json.loads((WEB / "sprites" / "nodes@2x.json").read_text(encoding="utf-8")))
    r = _node(FUELS_JS % json.dumps(REGION_FUELS), "sprites.js")
    k = r["kinds"]
    assert k["other"] == "plant-unknown" and r["upper"] == "plant-pumped-storage"
    assert len(set(k.values())) == len(REGION_FUELS), k     # no two fuels share a sprite
    assert {f: k[f] for f in ("gas", "coal", "oil", "battery", "biomass", "geothermal", "pumped_storage")} == {
        "gas": "plant-gas", "coal": "plant-coal", "oil": "plant-oil", "battery": "plant-battery",
        "biomass": "plant-biomass", "geothermal": "plant-geothermal", "pumped_storage": "plant-pumped-storage"}
    for kind in k.values():
        for st in manifest[kind]["states"]:
            for size in (16, 32):
                key = f"{kind}-{size}-{st}"
                assert key in atlas and key in atlas2 and manifest[kind]["sizes"][str(size)][st] == key, key
    assert r["glow"] == [["glow1", "glow2", "glow3"]] * 3
    assert r["est"] == "glow2" and r["statics"] == ["static"] * 3
    assert r["battery"] == ["discharge", "charge", "static", "static", "static", "static", "static"]
    # the battery's three stills, plus Art's timed charge0..3 / discharge0..3 (#538, L-108: test_plant_motion_rule_every_kind)
    assert set(manifest["plant-battery"]["states"]) == {"static", "charge", "discharge"} | {f"{s}{i}" for s in ("charge", "discharge") for i in range(4)}
    assert r["bbadge"] == [True, False]
    assert all(s and s["variant"] == "normal" for s in r["smoke"]) and r["nosmoke"] == [None] * 5
    assert r["offset"]["plant-coal"] == {"16": [3, -12], "32": [6, -24]}
    assert r["offset"]["plant-oil"] == {"16": [4, -12], "32": [8, -24]}
    assert r["offset"]["plant-gas"] == {"16": [-4, -12], "32": [-8, -24]}
    assert r["isEst"] == [True, True, True, False, False]
    style = (WEB / "style.js").read_text(encoding="utf-8")
    assert 'id: "node-est"' in style and '["get", "spr_est"]' in style and '"text-field": "est."' in style


FIRE_JS = """
const now = Date.parse("2026-11-02T06:00:00Z") / 1000, h = (x) => new Date((now - x * 3600) * 1000).toISOString().slice(0, 19);
const sq = [[[-120, 56], [-119.9, 56], [-119.9, 56.1], [-120, 56]]];
const ev = [
  { id: "hot", extra: { type: "hotspots", lon: -120.1, lat: 56.2 }, published: h(3) },
  { id: "old-hot", extra: { type: "hotspots", lon: -121, lat: 56 }, published: h(30) },
  { id: "perim", extra: { type: "perimeter", lon: -119.95, lat: 56.05, lastdate: h(20), geometry: { type: "Polygon", coordinates: sq } } },
  { id: "old-perim", extra: { type: "perimeter", lon: -118, lat: 55, lastdate: h(96), geometry: { type: "Polygon", coordinates: sq } } },
  { id: "outage", extra: {}, published: h(1) },
];
const f = P.fireFeatures(ev, now);
console.log(JSON.stringify(f.map((x) => [x.properties.id, x.geometry.type, x.properties.fire || null, x.properties.lastdate || null])));
"""


def test_wildfire_flames_only_for_active_fires():
    """Wildfire (Art's WILDFIRE.md): a small flame for a hotspot cell detected in the last 24 h, a large one for a perimeter
    CWFIS updated in the last 48 h; an older perimeter keeps its outline and date but no flame (no stale or historical
    flames); anything that isn't a CWFIS fire draws nothing."""
    r = _node(FIRE_JS, "sprites.js")
    assert ["hot", "Point", "hotspot", None] in r
    assert ["perim", "Polygon", None, "2026-11-01"] in r and ["perim", "Point", "perimeter", None] in r
    assert ["old-perim", "Polygon", None, "2026-10-29"] in r and not any(x[0] == "old-perim" and x[1] == "Point" for x in r)
    assert not any(x[0] in ("old-hot", "outage") for x in r)


def test_callout_frame_keeps_p4a_box_and_leader_semantics():
    """The frame is a non-interactive overlay: the box keeps P4a's padding, min-width and 1 px border (so its measured
    size, hit target and collision footprint don't change) and the translucent background Erik kept. Leaders are the
    approved 1 px #7f948a on a 3 px #020504 casing, and region leaders stay dashed."""
    css = (WEB / "map.css").read_text(encoding="utf-8")
    box = _css_rule(css, ".callout")
    assert "padding: 3px 7px 4px" in box and "min-width: 104px" in box and "border: 1px solid" in box
    assert "background: rgba(6, 11, 9, .84)" in box and "backdrop-filter: blur(3px)" in box
    frame = _css_rule(css, ".callout::before")
    assert "pointer-events: none" in frame and "position: absolute" in frame and "inset: -1px" in frame
    assert 'url("sprites/callout-frame.png") 4 / 4px' in frame and "image-rendering: pixelated" in frame
    for f in ("callout-frame.png", "callout-frame@2x.png"):
        assert (WEB / "sprites" / f).exists(), f"{f} is Art's production export (web/sprites/**)"
    assert "stroke: #7f948a; stroke-width: 1" in _css_rule(css, ".leader")
    assert "stroke: #020504; stroke-width: 3" in _css_rule(css, ".leader-case")
    assert "stroke-dasharray: 3 3" in _css_rule(css, ".leader.region, .leader-case.region")
    js = (WEB / "callouts.js").read_text(encoding="utf-8")
    assert 'class="leader-case${reg}"' in js and 'class="leader${reg}"' in js   # both strokes carry the region dash
    # endcap per Art's handoff: 4x4 outline, 2x2 #d3ece0 centre
    assert 'width="4" height="4" class="cap-case"' in js and 'width="2" height="2" class="cap"' in js
    assert "fill: #d3ece0" in _css_rule(css, ".cap") and "fill: #020504" in _css_rule(css, ".cap-case")



def test_callout_frame_corners_are_clipped():
    """The frame's stepped corners: the box clips exactly the frame's 3 transparent pixels at each corner, so the map
    shows there (not a square of the translucent background), and hover stays visible inside the clip."""
    css = (WEB / "map.css").read_text(encoding="utf-8")
    m = re.search(r"\.callout \{ clip-path: polygon\(([^;]*)\); \}", css)
    assert m, "no stepped clip-path on .callout"
    pts = [" ".join(p.split()) for p in m.group(1).split(",")]
    assert len(pts) == 20
    assert pts[:5] == ["0 2px", "1px 2px", "1px 1px", "2px 1px", "2px 0"]           # top-left: (0,0) (1,0) (0,1) cut
    assert pts[15:] == ["2px 100%", "2px calc(100% - 1px)", "1px calc(100% - 1px)", "1px calc(100% - 2px)", "0 calc(100% - 2px)"]
    assert "inset 0 0 0 3px var(--jade)" in _css_rule(css, ".callout:hover")   # an outside shadow would be clipped away



BAND_JS = """
// the real Callouts.update() with a minimal DOM: placement and drawing are stubbed, the row HTML is the shipped code
globalThis.document = { createElement: () => ({ dataset: {}, style: {}, addEventListener() {}, innerHTML: "" }) };
class T extends C.Callouts { place() {} render() { return false; } }
const snap = { values: { "gas.kitimat": { v: 1300, prev: 1250, compare: "prev", unit: "MMcf/d", stale: false },
                         "gas.plain": { v: 1300, prev: 1250, compare: "prev", unit: "MMcf/d", stale: false } }, series: {} };
const out = {};
for (const [id, band] of [["gas.kitimat", 0.1], ["gas.plain", undefined]]) {
  const t = new T({ map: { on() {} }, list: () => [{ id: `f:${id}`, anchor: id,
    rows: [{ feature: id, label: id, unit: "MMcf/d", band, priority: 1 }] }], featureName: (x) => x },
    { appendChild() {} }, null);
  t.update(snap);
  out[id] = t.els.get(`f:${id}`).innerHTML;
}
console.log(JSON.stringify(out));
"""


def test_feature_band_renders_as_an_estimate():
    """A feature's own `band` (e.g. Gas's gas.kitimat, #162, band 0.10) reaches its callout value row, so the box shows
    "≈ 1,300 ±130": the same estimate marker map.json rows get. A feature without `band` is unchanged."""
    js = (WEB / "map.js").read_text(encoding="utf-8")
    row = re.search(r"const own = (?:!selfRow && )?p\.value && \(ownDaily \?[^\n]*\n\s*:\s*\{([^}]*)\}", js).group(1)   # L-078: or its daily fallback
    assert "band: p.band" in row
    extra = re.search(r"for \(const x of (?:p\.callouts_extra \|\| \[\]|extras)\) \{[^{]*?const r = \{([^}]*)\}", js).group(1)
    assert "band: x.band" in extra          # a callouts_extra row's own band renders "≈ … ±" too (not a "±" typed in its label)
    lib = "data:text/javascript;base64," + base64.b64encode((WEB / "lib.js").read_bytes()).decode()
    place = "data:text/javascript;base64," + base64.b64encode((WEB / "placement.js").read_bytes()).decode()
    src = (WEB / "callouts.js").read_text(encoding="utf-8")
    src = (src.replace('from "./lib.js"', f"from {json.dumps(lib)}").replace('from "./placement.js"', f"from {json.dumps(place)}")
           .replace('from "./regionlayout.js"', f"from {json.dumps(_data_url((WEB / 'regionlayout.js').read_text(encoding='utf-8')))}"))
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "callouts.js gained an import the test doesn't inline"
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    out = _node('globalThis.location = { search: "" };\nconst C = await import(' + json.dumps(url) + ");\n" + BAND_JS)
    k, plain = out["gas.kitimat"], out["gas.plain"]
    assert '<span class="est" title="Estimate ±10%">≈</span>' in k and '<span class="u bd">±130</span>' in k
    assert 'class="est"' not in plain and "±" not in plain
    assert re.sub(r"\s+", " ", k).count("1,300") == 1 and "1,300" in plain


def test_power_joints_overlay_never_covers_a_line():
    """Round 2 (Art #129): pixel joints on transmission and intertie lines, as an overlay drawn BELOW every data line
    layer. The wire runs through each joint's transparent corridor, and crossing lines draw over the joint, so no
    data-line pixel can change (G1). Intertie and transmission use their own joint; map-aligned, 96 px, 16 px."""
    r = _network_layers()
    ids = [l["id"] for l in r["layers"]]
    layers = {l["id"]: l for l in r["layers"]}
    pj = layers["power-joints"]
    line_layers = [l["id"] for l in r["layers"] if l["type"] == "line"]
    assert set(line_layers) >= {"areas-line", "gas-casing", "gas", "power-casing", "power"}
    assert ids.index("power-joints") < min(ids.index(x) for x in line_layers)   # below EVERY line layer (areas too)
    lay = pj["layout"]
    assert pj["type"] == "symbol" and lay["symbol-placement"] == "line" and lay["symbol-spacing"] == 96
    assert lay["icon-rotation-alignment"] == "map" and lay["icon-pitch-alignment"] == "viewport"
    assert lay["icon-keep-upright"] is True and lay["icon-size"] == 1 and "icon-offset" not in lay
    atlas = json.loads((WEB / "sprites" / "power-lines.json").read_text(encoding="utf-8"))
    link = lambda sys_, typ: {"kind": "link", "system": sys_, "type": typ}
    assert _ev(pj["filter"], link("power", "transmission")) and _ev(pj["filter"], link("power", "intertie"))
    assert not _ev(pj["filter"], link("gas", "pipeline")) and not _ev(pj["filter"], {"kind": "node", "system": "power"})
    assert _ev(lay["icon-image"], link("power", "intertie")) == "line/intertie-joint-16"
    assert _ev(lay["icon-image"], link("power", "transmission")) == "line/power-joint-16"
    for key in ("line/intertie-joint-16", "line/power-joint-16"):
        assert key in atlas
    assert r["dot"]["1"]["power-joints"]["icon-opacity"] == 1   # dims with the network off "now" (k)



# ---- status dots: storage budget + runner heartbeat (PLAN roadmap step 2; server.py /api/status `budget`, `runner`)
STATUS_JS = """
const ok = { id: "a", name: "A", last: { ok: true } }, bad = { id: "a", name: "A", last: { ok: false } }, never = { id: "a", name: "A" };
const D = (s, b) => P.sourceDot(s, b, () => "5m");
console.log(JSON.stringify({
  ok: D(ok, { a: "ok" }), warn: D(ok, { a: "warn" }), blocked: D(ok, { a: "blocked" }),
  errWarn: D(bad, { a: "warn" }).cls, errBlocked: D(bad, { a: "blocked" }).cls, err: D(bad, {}).cls,
  noBudget: D(ok, undefined).cls, neverWarn: D(never, { a: "warn" }).cls, never: D(never, {}).cls,
  running: P.runnerBadge({ state: "running", age_s: 3 }), stale: P.runnerBadge({ state: "stale", age_s: 120 }),
  down: P.runnerBadge({ state: "down", reason: "no runner has ever run" }), none: P.runnerBadge(undefined),
}));
"""


def _panel_module():
    """web/panel.js as a data URL with its imports inlined (lib.js; callouts.js with lib.js and placement.js)."""
    du = lambda src: "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    lib = du((WEB / "lib.js").read_text(encoding="utf-8"))
    place = du((WEB / "placement.js").read_text(encoding="utf-8"))
    co = (WEB / "callouts.js").read_text(encoding="utf-8")
    co = (co.replace('from "./lib.js"', f"from {json.dumps(lib)}").replace('from "./placement.js"', f"from {json.dumps(place)}")
          .replace('from "./regionlayout.js"', f"from {json.dumps(_data_url((WEB / 'regionlayout.js').read_text(encoding='utf-8')))}"))
    tie = (WEB / "tieout.js").read_text(encoding="utf-8").replace('from "./lib.js"', f"from {json.dumps(lib)}")   # L-079
    pa = (WEB / "panel.js").read_text(encoding="utf-8")
    pa = (pa.replace('from "./lib.js"', f"from {json.dumps(lib)}").replace('from "./callouts.js"', f"from {json.dumps(du(co))}")
          .replace('from "./tieout.js"', f"from {json.dumps(du(tie))}"))
    for src in (co, tie, pa):
        assert not re.search(r'^import [^\n]*from "\./', src, re.M), "a panel import the test doesn't inline"
    return du(pa)


PANEL_JS = """
const H = (v, spec) => PA.headlineHtml(v, undefined, "MW", spec);
console.log(JSON.stringify({
  band: H(1300, { band: 0.1 }),
  bandNull: H(null, { band: 0.1 }),
  est: H(1808, { estimate: { lo: 1540, hi: 2075, basis: "24 h mean ending at ts, flow x head, ±2σ band" } }),
  estNoRange: H(1808, { estimate: { lo: null, hi: 2075, basis: "b" } }),
  plain: H(1300, {}),
}));
"""


def test_plant_panel_names_its_fuel():
    """PLAN 10d (Erik: "for the powerplants, make the fuel type obvious in this tab"): a plant's panel says its fuel in the
    header ("OIL PLANT"), beside the name (a chip with its map sprite) and as the first fact, in the region page's fuel
    words. Every plant source: AESO (`fuel`, a compound plant's `fuel_primary`), Ontario, EIA-860M and OpenStreetMap
    (`fuel_group`); a gas plant's technology rides along; nothing known says "fuel unknown", never a guess."""
    plants = [f["properties"] for fn in sorted((paths.REPO_ROOT / "network" / "power").glob("*.geojson"))
              for f in json.loads(fn.read_text(encoding="utf-8"))["features"] if f["properties"].get("type") == "plant"]
    r = _node("""const cases = [{ fuel: "oil", fuel_group: "oil" }, { fuel: "combined_cycle", fuel_group: "gas" },
  { fuel: "cogeneration + simple_cycle", fuel_primary: "simple_cycle" }, { fuel: "combined_cycle + gas-fired steam", fuel_primary: "combined_cycle" },
  { fuel: "gas_fired_steam" }, { fuel: "pumped_storage", fuel_group: "pumped_storage" }, { fuel: "hydro" }, {}, { fuel: "peat" }, { fuel: "wind + battery" }];
const net = """ + json.dumps([{k: p.get(k) for k in ("fuel", "fuel_primary", "fuel_group")} for p in plants]) + """;
console.log(JSON.stringify({ cases: cases.map((c) => P.plantFuel(c)), unknown: net.filter((p) => !P.plantFuel(p).key).length,
  words: [...new Set(net.map((p) => P.plantFuel(p).word))].sort(), all: P.FUEL_WORDS }));""", "sprites.js")
    c = r["cases"]
    assert c[0] == {"key": "oil", "word": "Oil", "tech": None}
    assert c[1] == {"key": "gas", "word": "Gas", "tech": "combined cycle"}
    assert c[2] == {"key": "gas", "word": "Gas", "tech": "cogeneration + simple cycle"}          # AESO compound plant
    assert c[3]["tech"] == "combined cycle + gas-fired steam" and c[4]["tech"] == "gas-fired steam"
    assert c[5]["word"] == "Pumped storage" and c[6]["word"] == "Hydro"
    assert [x["word"] for x in c[7:]] == ["fuel unknown"] * 3 and all(x["key"] is None for x in c[7:])   # never a guess
    assert r["unknown"] == 0 and len(plants) > 1000                          # every plant on the map has a known fuel today
    regions_fuels = re.search(r"^FUELS = \(([^)]*)\)", (paths.REPO_ROOT / "montney" / "regions.py").read_text(encoding="utf-8"), re.M).group(1)
    assert r["all"] == re.findall(r'"([a-z_]+)"', regions_fuels)            # the region page's fuel words, same order
    assert set(r["words"]) <= {w.replace("_", " ").capitalize() for w in r["all"]}
    url = _panel_module()
    h = _node('globalThis.location = { search: "" };\nconst PA = await import(' + json.dumps(url) + """);
console.log(JSON.stringify([PA.fuelChip({ key: "oil", word: "Oil", sprite: { x: 2, y: 110, w: 16, h: 16 } }), PA.fuelChip({ key: null, word: "fuel unknown", sprite: null })]));""")
    assert 'class="fuelchip"' in h[0] and "background-position:-2px -110px" in h[0] and ">Oil</span>" in h[0]
    assert 'class="fuelchip unk"' in h[1] and "fuel unknown" in h[1] and "fspr" not in h[1]
    pa = (WEB / "panel.js").read_text(encoding="utf-8")
    assert 'fu && ["Fuel",' in pa and pa.index('fu && ["Fuel",') < pa.index('["Value is", "published capacity')   # the first fact
    js = (WEB / "map.js").read_text(encoding="utf-8")
    of = js[js.index("function openFeature("):js.index("function openCallout(")]
    assert 'p.type === "plant" ? plantFuel(p) : null' in of and '`${fuel.word} plant`' in of and '"power plant · fuel unknown"' in of
    assert 'if (atlas === "nodes") S.nodeAtlas = index' in js


def test_plant_out_of_service_and_readings():
    """PLAN 10c follow-up (Power on #536/#537): IESO lists Portlands' units with output 0 and capability 0 every hour since
    2026-11-07 (Pickering B likewise): the panel says "Out of service (IESO capability 0)", with no capacity or utilisation
    rows, and the sprite is still (glow0 / static) with no `?` badge: known to be off, not unknown. 0 MW with capability
    above 0 reads "Idle, available". Each source's own word for its capacity series (map.json `capacity_words`). A feature
    `basis` (the 15 US nuclear plants' NRC daily level, #537) shows under the headline, which stays plain (no "est")."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    words = {k: v for k, v in cfg["capacity_words"].items() if not k.startswith("_")}
    assert all(isinstance(v, dict) and v.get("word") and isinstance(v.get("live"), bool) for v in words.values())
    r = _node("""const W = """ + json.dumps(cfg["capacity_words"]) + """, plant = { type: "plant" };
console.log(JSON.stringify({
  off: [P.outOfService(plant, { v: 0, cap: 0 }), P.outOfService(plant, { v: null, cap_current: 0, cap: 0 }), P.outOfService(plant, { v: 0, cap: 0, stale: true }),
        P.outOfService(plant, { v: 0, cap: 868 }), P.outOfService(plant, { v: 5, cap: 0 }), P.outOfService(plant, { v: 0 }), P.outOfService({ type: "hub" }, { v: 0, cap: 0 })],
  idle: [P.notProducing(plant, { v: 0, cap: 868 }), P.notProducing(plant, { v: 0, cap: 0 }), P.notProducing(plant, { v: 3, cap: 868 })],
  words: ["ieso.plant.portlands.capability.hourly", "aeso.unit.egc1.mc", "nrc.plant.eia_6099.capacity", "x.y"].map((s) => P.capacityInfo(s, W)),
  zero: ["ieso.plant.york.capability.hourly", "aeso.unit.big.mc", "nrc.plant.eia_1715.capacity", "x.y"].map((s) => P.zeroText(P.capacityInfo(s, W), s.startsWith("nrc") ? 768.5 : 466, "MW")),
  sprite: [P.spriteState("plant-combined-cycle", 0, null, false, false, true), P.needsBadge("plant-combined-cycle", 0, null, false, true),
           P.spriteState("plant-nuclear", 0, null, false, false, true), P.needsBadge("plant-nuclear", 0, null, false, true),
           P.needsBadge("plant-combined-cycle", 0, null, false, false), P.needsBadge("plant-combined-cycle", 0, null, true, true),
           P.animSpeed("plant-wind", 0, null, false)],
}));""", "sprites.js")
    assert r["off"] == [True, True, False, False, False, False, False]   # stale says nothing; a missing capability isn't 0
    assert r["idle"] == [True, False, False]
    assert [w["word"] for w in r["words"]] == ["IESO capability", "AESO max capability", "net summer capacity", "capacity"]
    assert [w["live"] for w in r["words"]] == [True, False, False, False]   # only IESO publishes availability hour by hour
    # Reviewer on #539: 0 MW is "idle, available" only against live availability; AESO MC and NRC capacity are static
    # ratings (Palisades: 0 of 768.5 MW for 30 days, restarting), so they read "not producing", NRC in its own words
    assert r["zero"] == ["Idle, available (IESO capability 466 MW)", "Not producing (0 of 466 MW AESO max capability)",
                         "Not producing (0 % on NRC's daily report)", "Not producing (0 of 466 MW capacity)"]
    assert r["sprite"][:4] == ["glow0", False, "static", False]          # still, no `?`: known off, not unknown
    assert r["sprite"][4] is True and r["sprite"][5] is True               # unknown output keeps its badge; stale too
    url = _panel_module()
    h = _node('globalThis.location = { search: "" };\nconst PA = await import(' + json.dumps(url) + """);
console.log(JSON.stringify([PA.headlineHtml(1180, undefined, "MW", { estimate: { range: false, basis: "NRC daily power level ...", reading: true } }),
  PA.headlineHtml(0, 0, "MW", {})]));""")
    assert h[0] == '<span class="big">1,180</span> <span class="u">MW</span>' and h[1].startswith('<span class="big">0</span>')
    pa = (WEB / "panel.js").read_text(encoding="utf-8")
    for row in ('!off && e?.cap_current != null && ["Capability today"', '!off && e?.cap != null && ["Capacity"', '!off && util != null && ["Utilization"'):
        assert row in pa, row
    assert 'class="pstate${spec.plantState.off ? " off" : ""}"' in pa
    js = (WEB / "map.js").read_text(encoding="utf-8")
    of = js[js.index("function openFeature("):js.index("function openCallout(")]
    assert "`Out of service (${ci.word} 0)`" in of and "zeroText(ci, entry.cap_current ?? entry.cap" in of and "if (!est && p.basis) est = { range: false, basis: p.basis, reading: true }" in of
    assert "spriteProps(f.properties, o, v, util, stale, outOfService(f.properties, e))" in js
    # every plant capacity series on the map has its source's word (no bare "capacity" for a source we read)
    caps = {f["properties"]["capacity"].split(".")[0] + "." for fn in (paths.REPO_ROOT / "network" / "power").glob("*.geojson")
            for f in json.loads(fn.read_text(encoding="utf-8"))["features"] if f["properties"].get("type") == "plant" and f["properties"].get("capacity")}
    assert caps <= set(words), caps - set(words)


def test_panel_headline_never_bare_for_an_estimate():
    """The detail panel's big number (Reviewer on #163): a feature with `band` reads "≈ v ±abs"; one with an `estimate`
    block (Power's Peace dam nodes, #148) reads "est v [lo–hi]" with the basis below; a plain feature is unchanged."""
    url = _panel_module()
    r = _node('globalThis.location = { search: "" };\nconst PA = await import(' + json.dumps(url) + ");\n" + PANEL_JS)
    assert r["band"] == ('<span class="est" title="Estimate ±10%">≈</span> <span class="big">1,300</span> '
                         '<span class="u">±130</span> <span class="u">MW</span>')
    assert r["bandNull"].startswith('<span class="est"') and "±" not in r["bandNull"].split("</span>", 1)[1]
    assert r["est"] == ('<span class="est" title="Estimate">est</span> <span class="big">1,808</span> '
                        '<span class="u">[1,540–2,075]</span> <span class="u">MW</span>')
    assert r["estNoRange"].startswith('<span class="est" title="Estimate">est</span>') and "[band n/a]" in r["estNoRange"]
    assert r["plain"] == '<span class="big">1,300</span> <span class="u">MW</span>'
    # wiring: the panel uses it with the spec; openFeature passes the feature's band and its estimate's lo/hi values and
    # basis; the snapshot request includes every estimate's lo/hi series, so the range is there to show
    pa = (WEB / "panel.js").read_text(encoding="utf-8")
    # sh is shown(e): the window mean when the entry has agg (#158), else the latest point
    assert "headlineHtml(sh.v, sh.v === 0 && spec.plantState ? 0 : row.decimals, unit, spec)" in pa and 'class="basis">${esc(spec.estimate.basis)}' in pa
    js = (WEB / "map.js").read_text(encoding="utf-8")
    of = js[js.index("function openFeature("):js.index("function openCallout(")]
    assert "band: p.band, estimate: est" in of and "S.snap?.series?.[p.estimate.lo]?.v" in of and "basis: p.estimate.basis" in of
    # every tier asks for them (L-081: a click on an estimated lite feature opens its range; ?features= has no series)
    assert "ids.add(f.properties.estimate.lo).add(f.properties.estimate.hi)" in js[js.index("function extraIds(t)"):js.index("async function loadSnapshot()")]
    # every estimate block in the network names lo/hi series and a basis (Power's dam nodes today)
    blocks = [f["properties"]["estimate"] for fn in sorted((paths.REPO_ROOT / "network").glob("*/*.geojson"))
              for f in json.loads(fn.read_text(encoding="utf-8"))["features"] if "estimate" in f["properties"]]
    assert len(blocks) >= 3 and all(b.get("lo") and b.get("hi") and b.get("basis") for b in blocks)


def test_status_dots_budget_and_runner():
    """Each budget state and runner state renders distinctly. Precedence: blocked > fetch error > warn > ok (blocked
    means nothing new is stored). The runner badge shows only when it isn't running, and never in DuckDB mode."""
    r = _node(STATUS_JS, "status.js")
    assert r["ok"]["cls"] == "ok" and "budget" not in r["ok"]["title"]
    assert r["warn"]["cls"] == "warn" and "80 %" in r["warn"]["title"]
    assert r["blocked"]["cls"] == "blocked" and "writes are stopped" in r["blocked"]["title"]
    assert (r["errBlocked"], r["errWarn"], r["err"]) == ("blocked", "err", "err")
    assert (r["noBudget"], r["neverWarn"], r["never"]) == ("ok", "warn", "")
    assert r["running"] is None and r["none"] is None
    assert r["stale"]["cls"] == "stale" and r["stale"]["text"] == "RUNNER BUSY" and "120 s" in r["stale"]["title"]
    assert r["down"]["cls"] == "down" and r["down"]["text"] == "RUNNER DOWN" and "has ever run" in r["down"]["title"]
    css = (WEB / "map.css").read_text(encoding="utf-8")
    for sel in (".dot.warn", ".dot.blocked", ".runner.stale", ".runner.down"):
        assert re.search(r"(?m)^" + re.escape(sel) + r"\s*\{", css), sel
    assert "border-radius: 1px" in _css_rule(css, ".dot.blocked")   # blocked differs by shape, not only colour
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="runner"' in html


# ---- river gauges (PLAN roadmap step 4, P5d): stations + flow-vs-normal encoding (web/gauges.js, web/gauges.geojson)
def _wsc_gauges():
    import ast
    tree = ast.parse((paths.REPO_ROOT / "montney/connectors/weather_wsc.py").read_text(encoding="utf-8"))
    for node in tree.body:
        tgt = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if getattr(tgt, "id", None) == "GAUGES":
            return ast.literal_eval(node.value)
    raise AssertionError("GAUGES not found in weather_wsc.py")


def test_gauge_stations_match_wsc_and_snapshot():
    """One marker per discharge gauge the snapshot reports, with WSC coordinates that agree with Weather's
    independently fetched station tables (docs/research/v2/weather-notes-*.md) to within 0.02 deg."""
    q = {s for s, g in _wsc_gauges().items() if g[2]}
    assert len(q) >= 20, len(q)   # fails closed if GAUGES can't be read
    fc = json.loads((WEB / "gauges.geojson").read_text(encoding="utf-8"))
    st = [f["properties"]["station"] for f in fc["features"]]
    assert sorted(st) == sorted(q) and len(st) == len(set(st))
    notes = {}
    for f in sorted((paths.REPO_ROOT / "docs/research/v2").glob("weather-notes-*.md")):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*(\d{2}[A-Z]{2}\d{3})\s*\|[^|]*\|\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\|", line)
            if m:
                notes.setdefault(m.group(1), (float(m.group(2)), float(m.group(3))))
    checked = 0
    for f in fc["features"]:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        assert f["id"] == p["id"] == f"gauge.{p['station'].lower()}" and "api.weather.gc.ca" in p["source"]
        if p["station"] not in notes:
            continue   # a gauge Weather added later without a notes table (e.g. the AB rivers): WSC metadata only
        nlat, nlon = notes[p["station"]]
        assert abs(nlat - lat) < 0.02 and abs(nlon - lon) < 0.02, (p["station"], (lat, lon), (nlat, nlon))
        checked += 1
    assert checked >= 28   # every gauge Weather's tables list is cross-checked (the original 28)


GAUGE_JS = """
const ok = (p, band) => ({ value: 812.4, p, band, n: 420, state: "ok", reason: null });
const S = { veryLow: ok(4, "very low"), low: ok(18, "low"), normal: ok(50, "normal"), high: ok(82, "high"), veryHigh: ok(96, "very high"),
  seasonal: { value: null, p: null, band: "no-result", n: 300, state: "seasonal", reason: "seasonal gauge: no data outside its open-water season" },
  stale: { value: 55.2, p: null, band: "no-result", n: 400, state: "stale", reason: "no real-time discharge in the last 3 h" },
  noResult: { value: 12.34, p: null, band: "no-result", n: 90, state: "no-result", reason: "history n=90 < 150" },
  missing: undefined };
console.log(JSON.stringify({ style: Object.fromEntries(Object.entries(S).map(([k, g]) => [k, P.gaugeStyle(g)])),
  lines: Object.fromEntries(Object.entries(S).map(([k, g]) => [k, P.gaugeLines(g)])), bands: P.BAND_COLOR, ring: P.GAUGE_RING,
  okNoBand: P.gaugeStyle({ state: "ok", band: "no-result" }) }));
"""


def test_gauge_states_encode_distinctly():
    """Colour only for state ok (by percentile band); seasonal hollow; stale dimmed; no-result neutral with its reason;
    the callout shows m3/s, "percentile NN (band)" and n. Band colours never reuse an identity or alert colour (L-028 G2)."""
    r = _node(GAUGE_JS, "gauges.js")
    st, ln, bands = r["style"], r["lines"], r["bands"]
    for k, band in (("veryLow", "very low"), ("low", "low"), ("normal", "normal"), ("high", "high"), ("veryHigh", "very high")):
        assert st[k] == {"fill": bands[band], "fillOpacity": 1, "opacity": 1}, k
        assert ln[k] == ["812 m³/s", f"percentile {round({'veryLow': 4, 'low': 18, 'normal': 50, 'high': 82, 'veryHigh': 96}[k])} ({band})", "n 420"]
    assert len(set(bands.values())) == 5
    assert st["seasonal"]["fillOpacity"] == 0 and st["seasonal"]["opacity"] == 1 and ln["seasonal"][0] == "seasonal, no data"
    assert st["stale"]["opacity"] < 0.5 and st["stale"]["fill"] not in bands.values() and "stale" in ln["stale"]
    assert st["noResult"]["opacity"] == 1 and st["noResult"]["fill"] not in bands.values()
    assert ln["noResult"] == ["12.3 m³/s", "no result", "history n=90 < 150", "n 90"]
    assert r["okNoBand"]["fill"] not in bands.values()       # an "ok" without a known band never gets a band colour
    assert st["missing"]["fillOpacity"] == 0 and ln["missing"] == ["no data yet"]
    assert not any("percentile" in x for k in ("seasonal", "stale", "noResult") for x in ln[k])   # no percentile unless ok
    reserved = {"#3dffb8", "#c9a2ff", "#b392f0", "#ffb547", "#ff4d6d"}   # receipt, intertie, storage, warn, critical
    assert not ({c.lower() for c in bands.values()} | {r["ring"].lower()}) & reserved
    lay = {l["id"]: l for l in _network_layers()["layers"]}["gauges"]
    assert lay["type"] == "circle" and lay["source"] == "gauges" and lay["paint"]["circle-color"] == ["get", "g_fill"]
    assert lay["paint"]["circle-stroke-color"] == r["ring"]
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    assert any(l["id"] == "gauges" for l in cfg["layers"])


# ---- window means in callouts (#151 contract, pairs with Power's #153 burn rows @1h)
def _node_callouts(script: str):
    """Run `script` with `C` bound to the real web/callouts.js, its imports (lib.js, placement.js) inlined as data URLs.
    lib.js reads `location` at load, so the program stubs it first."""
    def data_url(src):
        return "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    lib = data_url((WEB / "lib.js").read_text(encoding="utf-8"))
    place = data_url((WEB / "placement.js").read_text(encoding="utf-8"))
    src = (WEB / "callouts.js").read_text(encoding="utf-8")
    src = (src.replace('from "./lib.js"', f"from {json.dumps(lib)}").replace('from "./placement.js"', f"from {json.dumps(place)}")
           .replace('from "./regionlayout.js"', f"from {json.dumps(_data_url((WEB / 'regionlayout.js').read_text(encoding='utf-8')))}"))
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "callouts.js gained an import the test doesn't inline"
    return _node('globalThis.location = { search: "" };\nconst C = await import(' + json.dumps(data_url(src)) + ");\n" + script,
                 "placement.js")



def test_river_pct_normal():
    """Step 7 (Erik; Weather #266): a dam outflow / WSC discharge row shows its `_pct_normal` sibling as "· 84 % of normal";
    any other series, or a missing or stale sibling, shows nothing (never 0)."""
    r = _node_callouts("""
const sn = (e) => ({ series: { "abrivers.tau_001.outflow_pct_normal": e } });
console.log(JSON.stringify({
  ids: [C.pctNormalId("abrivers.tau_001.outflow"), C.pctNormalId("wsc.realtime.07fa004.discharge"), C.pctNormalId("aeso.unit.big.tng"),
        C.pctNormalId("abrivers.tau_001.outflow@24h")],
  ok: C.normalHtml("abrivers.tau_001.outflow", sn({ v: 116.4, stale: false })),
  high: C.normalHtml("abrivers.tau_001.outflow", sn({ v: 131, stale: false })),
  low: C.normalHtml("abrivers.tau_001.outflow", sn({ v: 64.2, stale: false })),
  stale: C.normalHtml("abrivers.tau_001.outflow", sn({ v: 90, stale: true })),
  none: C.normalHtml("abrivers.tau_001.outflow", sn(undefined)), mw: C.normalHtml("aeso.unit.big.tng", sn({ v: 90 })),
}));""")
    assert r["ids"] == ["abrivers.tau_001.outflow_pct_normal", "wsc.realtime.07fa004.discharge_pct_normal", None, None]
    assert ">· 116 % of normal</span>" in r["ok"] and "var(--muted)" in r["ok"]          # 75-125 %: untinted
    assert "#5aa0dc" in r["high"] and "#a9a46c" in r["low"]                                 # gauge high / low colours
    assert r["stale"] == r["none"] == r["mw"] == ""


def test_compact_rules():
    """L-069 (Erik: one code path, no hardcoded boxes): compact mode comes from the map's size alone; a compact box keeps
    its title, its first row and each section's first row (a merged box keeps both totals); the box budget scales with area."""
    r = _node_callouts("""
const merged = [{ head: "Alberta demand", title: true }, { head: "Gas", section: true }, { series: "a" }, { series: "a1" },
                { head: "Power", section: true }, { series: "b" }, { series: "b1" }, { series: "b2" }];
// a total with plain merged parts (L-070 exports): the parts' heads start no lead, so it folds to the total
const parts = [{ series: "tot" }, { head: "Empress" }, { series: "e" }, { head: "McNeill" }, { series: "m" }];
console.log(JSON.stringify({ merged: C.leadRows(merged), plain: C.leadRows([{ series: "x" }, { series: "y" }, { series: "z" }]),
  parts: C.leadRows(parts),
  sizes: [C.isCompact(412, 915), C.isCompact(915, 412), C.isCompact(1536, 730), C.isCompact(1280, 800)],
  budget: [C.boxBudget(412, 915), C.boxBudget(200, 200)] }));""")
    assert r["merged"] == [True, False, True, False, False, True, False, False]
    assert r["plain"] == [True, False, False]
    assert r["parts"] == [True, False, False, False, False]
    assert r["sizes"] == [True, True, False, False]           # phone portrait / landscape compact; desktop (1.25×) not
    assert r["budget"] == [8, 3]


def test_row_tree():
    """L-073 (Erik: nested split-out boxes): a box's rows form a tree from `depth` (Gas's contract) or the "·" marks: the
    first row is the root, a row is at most one level deeper than the row before, and its parent is the nearest row above
    one level up. A section head starts a new tree; the title head isn't part of one."""
    r = _node_callouts("""
const t = (rows) => C.rowTree(rows);
console.log(JSON.stringify({
  explicit: t([{ series: "tot" }, { series: "ab", depth: 1 }, { series: "ab1", depth: 2 }, { series: "ngtl", depth: 1 },
               { series: "field", depth: 2 }, { series: "usjr", depth: 3 }, { series: "rest", depth: 3 }, { series: "gord", depth: 2 }]),
  dots: t([{ series: "d" }, { label: "Population" }, { label: "· Calgary" }, { label: "· Edmonton" }, { label: "Industry" }, { label: "· OSDA" }]),
  clamp: t([{ series: "tot" }, { label: "· Empress" }, { label: "· McNeill" }, { series: "x", depth: 3 }]),
  merged: t([{ head: "Alberta demand", title: true }, { head: "Gas", section: true }, { series: "g" }, { label: "Pop" },
             { head: "Power", section: true }, { series: "p" }, { label: "Wind" }]) }));""")
    assert r["explicit"] == {"depth": [0, 1, 2, 1, 2, 3, 3, 2], "parent": [-1, 0, 1, 0, 3, 4, 4, 3]}
    assert r["dots"] == {"depth": [0, 1, 2, 2, 1, 2], "parent": [-1, 0, 1, 1, 0, 4]}
    assert r["clamp"] == {"depth": [0, 1, 1, 2], "parent": [-1, 0, 0, 2]}      # "·" under the headline is depth 1; never skips a level
    assert r["merged"] == {"depth": [None, None, 0, 1, None, 0, 1], "parent": [-1, -1, -1, 2, -1, -1, 5]}


def test_search_ranking():
    """L-076 (Erik: a search bar for all the nodes, wells included): prefix beats word-prefix beats substring beats
    in-order fuzzy; other keys (id, operator) count a little less; results grouped by type in best-hit order."""
    lib = "data:text/javascript;base64," + base64.b64encode((WEB / "lib.js").read_text(encoding="utf-8").encode()).decode()
    src = (WEB / "search.js").read_text(encoding="utf-8").replace('from "./lib.js"', f"from {json.dumps(lib)}")
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "search.js gained an import the test doesn't inline"
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    r = _node('globalThis.location = { search: "" };\nconst S = await import(' + json.dumps(url) + """);
const items = [{ label: "Empress Solar", group: "Power plant" }, { label: "Empress", group: "Power plant" },
  { label: "NGTL to Empress", group: "Gas pipeline" }, { label: "East Gate (Empress / McNeill)", group: "Gas area" },
  { label: "Columbia River at Birchbank", group: "River gauge", keys: ["08NE049"] },
  { label: "Resthaven 8-11-60-3W6 GP", group: "Top producers", keys: ["PEMBINA GAS INFRASTRUCTURE INC."] }];
console.log(JSON.stringify({
  norm: S.norm("Kakwa 5-26 (Pembina) é"),
  empress: S.find(items, "empress").map((x) => x.label),
  fuzzy: S.find(items, "brchbank").map((x) => x.label), byId: S.find(items, "08ne049").map((x) => x.label),
  byOperator: S.find(items, "pembina").map((x) => x.label), none: S.find(items, "zzz").length, blank: S.find(items, "  ").length,
  groups: S.grouped(S.find(items, "empress")).map(([g, l]) => [g, l.length]) }));""", "placement.js")
    assert r["norm"] == "kakwa 5 26 pembina e"
    assert r["empress"][:2] == ["Empress", "Empress Solar"] and r["empress"][2:] == ["NGTL to Empress", "East Gate (Empress / McNeill)"]
    assert r["fuzzy"] == ["Columbia River at Birchbank"] and r["byId"] == ["Columbia River at Birchbank"]
    assert r["byOperator"] == ["Resthaven 8-11-60-3W6 GP"] and r["none"] == 0 and r["blank"] == 0
    assert r["groups"] == [["Power plant", 2], ["Gas pipeline", 1], ["Gas area", 1]]


def test_tieout_waterfall():
    """L-079 (Erik: "AB + BC should tie out"): the waterfall reads every term at the balance's day, sums multi-series terms
    (the carried AB + BC remainders), signs each bar, and has no total if any term is missing that day."""
    lib = "data:text/javascript;base64," + base64.b64encode((WEB / "lib.js").read_text(encoding="utf-8").encode()).decode()
    src = (WEB / "tieout.js").read_text(encoding="utf-8").replace('from "./lib.js"', f"from {json.dumps(lib)}")
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "tieout.js gained an import the test doesn't inline"
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    r = _node('globalThis.location = { search: "" };\nconst T = await import(' + json.dumps(url) + """);
const d = { P: { ts: [1, 2], v: [20121.2, 20694.6] }, A: { ts: [1, 2], v: [1089.7, 1089.7] }, B: { ts: [1, 2], v: [-265.6, 1082.9] },
  E: { ts: [1, 2], v: [11126.1, 11375.3] }, L: { ts: [1, 2], v: [1456.8, 1780.6] }, D: { ts: [1], v: [7139.5] } };
const cfg = { bars: [{ series: "P", sign: 1 }, { series: ["A", "B"], sign: -1 }, { series: "E", sign: -1 }, { series: "L", sign: -1 },
  { series: "D", sign: -1 }] };
const w1 = T.waterfall(cfg, d, 1), w2 = T.waterfall(cfg, d, 2);
const h = T.chartColumns({ chart: [{ series: "dI", history: "mI" }] }, { mI: { ts: [10, 20, 30], v: [1, 2, 3] }, dI: { ts: [30, 31], v: [33, 34] } });
console.log(JSON.stringify({ left: Math.round(w1.left * 10) / 10, carried: w1.bars[1].c, missing: w2.left, sum: T.termAt(d, ["A", "B"], 1), h }));""", "placement.js")
    assert r["left"] == -425.3            # 20,121.2 − 824.1 − 11,126.1 − 1,456.8 − 7,139.5: Gas's 2026-11-01 numbers
    assert abs(r["carried"] + 824.1) < 1e-9 and abs(r["sum"] - 824.1) < 1e-9
    assert r["missing"] is None           # day 2 has no demand point: no total rather than a wrong one
    assert r["h"] == {"xs": [10, 20, 30, 31], "cols": [[1, 2, 33, 34]]}   # monthly history, then daily; daily wins on a shared ts


def test_tieout_breakdown():
    """L-080 item 4 (Erik: "Include the estimation when I click the unaccounted for amount and what it assumes it is"):
    map.json's breakdown is Gas #313's parts and they sum to the raw error (Gas's 2026-11-01: 1,104 = 1,017 + 374 − 94 −
    266 + 73); a part's parts follow it at depth 1; bands are absolute, relative, a series, or none when measured."""
    lib = "data:text/javascript;base64," + base64.b64encode((WEB / "lib.js").read_text(encoding="utf-8").encode()).decode()
    src = (WEB / "tieout.js").read_text(encoding="utf-8").replace('from "./lib.js"', f"from {json.dumps(lib)}")
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))["tieouts"]["ngtl.csr.net_storage"]
    bd = cfg["raw"]["breakdown"]
    assert cfg["raw"]["series"] == "balance.ab_bc.residual_est"     # the parts sum to this one (Gas #313), not the metered error
    P = "balance.ab.unaccounted."
    vals = {"ab_offsystem_est": 1017, "ngtl_basis_est": 374, "ngtl_closure": -94, "bc_remainder_est": -266, "unexplained_est": 73,
            "unexplained_band_est": 510, "gas_alberta_est": 403, "atco_pipelines_est": 337, "tc_mainline_suffield_est": 111,
            "pioneer_brazeau_est": 72, "suncor_albersun_est": 44, "altagas_cold_lake_est": 27, "other_systems_est": 23}
    data = {P + k: {"ts": [1], "v": [v]} for k, v in vals.items()}
    r = _node('globalThis.location = { search: "" };\nconst T = await import(' + json.dumps(url) + """);
const bd = """ + json.dumps(bd) + ", data = " + json.dumps(data) + """;
const a = T.breakdownRows(bd, data, 1), gone = T.breakdownRows(bd, {}, 1);
console.log(JSON.stringify({ sum: a.sum, rows: a.rows.map((x) => [x.series, x.depth, x.band == null ? null : Math.round(x.band), x.kids, !!x.assume]),
  gone: gone.sum }));""", "placement.js")
    assert r["sum"] == 1104
    assert r["gone"] is None                                         # no part that day: no sum rather than a wrong one
    rows = {s.removeprefix(P): (d, b, k, a) for s, d, b, k, a in r["rows"]}
    assert set(rows) == set(vals) - {"unexplained_band_est"}
    assert [s.removeprefix(P) for s, d, *_ in r["rows"] if d == 0] == ["ab_offsystem_est", "ngtl_basis_est", "ngtl_closure",
                                                                         "bc_remainder_est", "unexplained_est"]
    assert rows["ab_offsystem_est"] == (0, 407, 7, True)            # ±40 %, its seven systems folded under it
    assert sum(vals[k] for k, (d, *_) in rows.items() if d == 1) == vals["ab_offsystem_est"]
    assert rows["tc_mainline_suffield_est"][1] == 44                # each system ±40 % (Gas addendum)
    assert rows["ngtl_basis_est"][1] == 50 and rows["bc_remainder_est"][1] == 300
    assert rows["ngtl_closure"][1] is None                          # measured
    assert rows["unexplained_est"][1] == 510                        # its own band series
    assert all(a for k, (d, b, kids, a) in rows.items() if d == 0)  # every part says what it assumes
    # orchestrator (L-080 item 4): each component's confidence (Gas's report §1), and a link to and summary of the report
    conf = {p["series"].removeprefix(P): p.get("confidence") for q in bd["parts"] for p in [q, *q.get("parts", [])]}
    assert all(conf[k] for k in conf if k != "unexplained_est"), conf
    assert conf["pioneer_brazeau_est"].startswith("low") and conf["ngtl_basis_est"] == "high"
    assert bd["report"]["url"].endswith("docs/research/gas-residual.md") and bd["report"]["summary"]
    assert '"conf"' in src and "bd.report.url" in src                # rendered, not just configured


def test_power_rules():
    """L-080 (battery): hidden beats saver beats idle; saver follows the user's choice, else small touch screens and
    battery; the wind field draws fewer particles on a small screen and in saver, at a capped pixel ratio."""
    p = _node("""console.log(JSON.stringify({
  states: [P.powerState({ hidden: true, saver: true, sinceInput: 0 }), P.powerState({ hidden: false, saver: true, sinceInput: 0 }),
           P.powerState({ hidden: false, saver: false, sinceInput: P.IDLE_MS + 1 }), P.powerState({ hidden: false, saver: false, sinceInput: 5 })],
  saver: [P.saverOn("1", false, false), P.saverOn("0", true, true), P.saverOn(null, true, false), P.saverOn(null, false, true), P.saverOn(null, false, false)],
  rates: P.RATES }));""", "power.js")
    assert p["states"] == ["hidden", "saver", "idle", "active"]
    assert p["saver"] == [True, False, True, True, False]
    assert p["rates"]["hidden"] == {"wind": 0, "river": 0, "clock": 0}
    assert p["rates"]["saver"]["clock"] == 1    # saver keeps sprites turning slowly: spin means output (Reviewer, #306)
    assert p["rates"]["active"]["wind"] > p["rates"]["idle"]["wind"] > p["rates"]["saver"]["wind"] > 0
    w = _node("""console.log(JSON.stringify({ desk: P.windCount(1536, 730, false), phone: P.windCount(412, 915, false), phoneSaver: P.windCount(412, 915, true),
  dpr: [P.windDpr(1.25, false), P.windDpr(2.625, false), P.windDpr(2.625, true)] }));""", "weather.js")
    assert w["desk"] == 2600 and w["phone"] < 900 and w["phoneSaver"] < w["phone"]
    assert w["dpr"] == [1.25, 2, 1]

AGG_JS = """
const now = 1793559600;
const snap = {
  values: { "power.plant.x": { v: 610, prev: 600, compare: "prev", unit: "MW", stale: false,
                               agg: { window: "24h", start: now - 86400, end: now, mean: 575, n: 280, n_min: 216, reason: null } } },
  series: {
    "p.est@1h": { v: 57, prev: 50, compare: "prev", unit: "MMcf/d", stale: false,
                  agg: { window: "1h", start: now - 3600, end: now, mean: 42, n: 12, n_min: 9, reason: null } },
    "p.lo@1h":  { v: 31, prev: 30, compare: "prev", unit: "MMcf/d", stale: false,
                  agg: { window: "1h", start: now - 3600, end: now, mean: null, n: 3, n_min: 9,
                         reason: "n 3 < 9 (75 % of the 12 points 5-min data has in 1h)" } },
    "p.hi": { v: 88, prev: 80, compare: "prev", unit: "MMcf/d", stale: false },
    "q.raw@1h": { v: 7, prev: 7, compare: "prev", unit: "MW", stale: false,
                  agg: { window: "1h", start: now - 3600, end: now, mean: 6, n: 12, n_min: 9, reason: null } },
  } };
const row = (r) => C.rowHtml(r, 0, snap, r.label || "x");
console.log(JSON.stringify({
  mean: row({ series: "p.est@1h", label: "gas burn est." }),
  noresult: row({ series: "p.lo@1h", label: "burn low" }),
  plain: row({ series: "p.hi", label: "burn high" }),
  rowAgg: row({ series: "q.raw", agg: "1h", label: "raw" }),
  feature: row({ feature: "power.plant.x", label: "plant" }),
  keys: [C.rowKey({ series: "q.raw", agg: "1h" }), C.rowKey({ series: "p.est@1h" }), C.rowKey({ series: "p.hi" }),
         C.rowKey({ series: "p.est@1h", agg: "1h" }), C.rowKey({ feature: "f" }) ?? null],
  base: [C.baseSeries("p.est@1h"), C.baseSeries("p.hi")],
  shown: [C.shown(snap.series["p.est@1h"]), C.shown(snap.series["p.lo@1h"]), C.shown(snap.series["p.hi"]), C.shown(undefined)],
}));
"""


def _val(html):
    return re.search(r'class="v">([^<]*)<', html).group(1)


def test_callouts_render_window_means_never_the_latest_point():
    """A row whose snapshot entry has `agg` shows agg.mean with its window and n, and no point-to-point change chip;
    with mean null it shows NO-RESULT and the reason, never `.v`. Rows without `agg` are unchanged."""
    r = _node_callouts(AGG_JS)
    m = r["mean"]
    assert _val(m) == "42.0" and "57" not in m                       # v (57) != agg.mean (42): the mean is shown
    assert '<span class="agg">1 h mean · n 12</span>' in m
    assert 'class="chip up"' not in m and 'class="chip down"' not in m   # no "+7 prev" next to a window mean
    nr = r["noresult"]
    assert _val(nr) == "--" and ">31<" not in nr                    # never the latest point dressed as a mean
    assert 'class="chip noresult" title="n 3 &lt; 9 (75 % of the 12 points 5-min data has in 1h)">NO-RESULT' in nr
    assert 'class="row stale"' in nr                                # dimmed like a stale value
    p = r["plain"]
    assert _val(p) == "88.0" and 'class="agg"' not in p and 'class="chip up">+8.0 prev' in p
    assert _val(r["rowAgg"]) == "6.00" and "1 h mean · n 12" in r["rowAgg"]   # a row's own `agg` reads <series>@1h
    assert _val(r["feature"]) == "575" and "24 h mean · n 280" in r["feature"]   # a feature's `agg` (#151)
    assert r["keys"] == ["q.raw@1h", "p.est@1h", "p.hi", "p.est@1h", None]
    assert r["base"] == ["p.est", "p.hi"]
    s_mean, s_nr, s_plain, s_none = r["shown"]
    assert s_mean == {"v": 42, "tag": "1 h mean · n 12", "reason": None}
    assert s_nr["v"] is None and s_nr["reason"].startswith("n 3 < 9")
    assert s_plain == {"v": 88, "tag": None, "reason": None} and s_none["v"] is None


def test_agg_rows_are_requested_and_read_by_the_same_key():
    """map.js asks the snapshot for exactly the key the box reads (rowKey: `<series>@<window>`), so every agg row of
    Power's #153 resolves; the panel charts the bare series (the history API takes ids, not window tokens)."""
    js = (WEB / "map.js").read_text(encoding="utf-8")
    extra = js[js.index("function extraIds(t)"):js.index("async function loadSnapshot()")]
    assert "ids.add(r.series)" not in extra and extra.count("rowKey(") == 3
    assert "S.snap?.series?.[rowKey(row)]" in js and "series: [baseSeries(row.series), row.ref]" in js
    assert "agg: x.agg" in js
    panel = (WEB / "panel.js").read_text(encoding="utf-8")
    assert "spec.series.map(baseSeries)" in panel and 'headlineHtml(sh.v, sh.v === 0 && spec.plantState ? 0 : row.decimals' in panel and '["Window",' in panel
    rows = []
    for fn in sorted((paths.REPO_ROOT / "network").glob("*/*.geojson")):
        for f in json.loads(fn.read_text(encoding="utf-8"))["features"]:
            rows += [x for x in f["properties"].get("callouts_extra", []) if "@" in x["series"] or x.get("agg")]
    assert rows, "expected Power's #153 @1h burn rows"
    for x in rows:
        sid, _, win = x["series"].partition("@")
        assert (win or x.get("agg")) in ("1h", "24h"), x


BURN_JS = """
const rows = __ROWS__, now = 1793559600, snap = { values: {}, series: {} };
// every burn token: latest point v = 999 (never to be shown); est/hi have a mean, lo is thin
for (const r of rows) snap.series[C.rowKey(r)] = { v: 999, prev: 990, compare: "prev", unit: "MMcf/d", stale: false,
  agg: /\\.lo@/.test(C.rowKey(r)) ? { window: "1h", start: now - 3600, end: now, mean: null, n: 6, n_min: 9, reason: "thin: n=6 < 9" }
                                   : { window: "1h", start: now - 3600, end: now, mean: 41.5, n: 12, n_min: 9, reason: null } };
console.log(JSON.stringify(rows.map((r, i) => ({ key: C.rowKey(r), label: r.label, html: C.rowHtml(r, i, snap, r.label) }))));
"""


SHARE_JS = """
const S = (e, t) => ({ values: { f: e }, series: { tot: t } });
const r = { share: { feature: "f", of: "tot" } };
const mw = (v, stale = false) => ({ v, unit: "MW", stale });
console.log(JSON.stringify({
  ok: C.sharePct(r, S(mw(120), mw(12000))),
  units: C.sharePct(r, S(mw(120), { v: 12000, unit: "MMcf/d" })),
  stale: C.sharePct(r, S(mw(120), mw(12000, true))),
  zero: C.sharePct(r, S(mw(120), mw(0))),
  missing: C.sharePct(r, S(mw(120), undefined)),
  html: C.shareHtml({ feature: "f", of: "tot", label: "% of AESO generation" }, S(mw(120), mw(12000))),
  big: C.shareHtml({ feature: "f", of: "tot", label: "% of AB production" }, S(mw(2803), mw(12740))),
  row: C.rowHtml({ feature: "f", label: "Genesee", shareOf: { feature: "f", of: "tot", label: "% of AESO generation" } }, 0,
                 S(mw(1375), mw(10400)), "Genesee"),
  colors: [C.shareColor(0), C.shareColor(-25), C.shareColor(60)],
  cues: ["% of AESO generation", "% of total export", "% of AB demand", "% of BC production", "% of AESO load", "% of LNG"].map(C.shareCue),
  signs: [C.flowSign(undefined, "ngtl.csr.intraprovincial"), C.flowSign(undefined, "ngtl.city.calgary.demand_est"),
          C.flowSign(undefined, "aeso.csd.ail"), C.flowSign(undefined, "power.plant.x.gas_burn.est@1h"),
          C.flowSign(undefined, "ngtl.dop.osda.flow"), C.flowSign(undefined, "aeso.unit.big.tng"),
          C.flowSign(undefined, "ngtl.csr.field_receipts"), C.flowSign(undefined, "aeso.intertie.montana"),
          C.flowSign("supply", "ngtl.csr.intraprovincial"), C.flowSign("demand", "lng.export.total"),
          C.flowSign(undefined, "ngtl.city.calgary.monthly")],
  demandShare: C.sharePct(r, { values: { f: { ...mw(420), series: "ngtl.city.calgary.demand_est" } }, series: { tot: mw(10000) } }),
  exportShare: C.sharePct({ share: { series: "imp", of: "tot" } }, { values: {}, series: { imp: mw(-500), tot: mw(10000) } }),
  rowShare: C.shareHtml({ series: "gas", of: "tot", label: "% of AB load" }, { values: {}, series: { gas: mw(6200), tot: mw(10487) } }),
  subRow: C.rowHtml({ series: "gas", label: "· oil sands (OSDA)" }, 0, { values: {}, series: { gas: mw(3320) } }, "· oil sands (OSDA)"),
  level: C.rowHtml({ feature: "f", label: "Mica", value_kind: "level" }, 0, { values: { f: { v: 754.2, unit: "m", series: "wsc.realtime.08nb017.level" } }, series: {} }, "Mica"),
  demandRow: C.rowHtml({ feature: "f", label: "Calgary" }, 0, { values: { f: { v: 420, prev: 400, compare: "prev", unit: "MMcf/d",
    series: "ngtl.city.calgary.demand_est" } }, series: {} }, "Calgary"),
  city: C.shareHtml({ feature: "f", of: "tot", band: 0.2, of_band: 0.03, label: "% of AB demand" }, S(mw(420), mw(10000))),
  none: C.shareHtml({ feature: "f", of: "tot", label: "% of x" }, S(mw(1), mw(0))),
  basisOnly: C.shareBand({ share: { feature: "f", of: "tot", of_band: 0.09 } }, S(mw(1), mw(10))),
  estRange: C.shareBand({ share: { feature: "f", of: "tot", est: { lo: "lo", hi: "hi" } } },
                        { values: { f: mw(1000) }, series: { tot: mw(5000), lo: mw(900), hi: mw(1100) } }),
}));
"""


def test_share_row_is_a_percent_of_its_named_basis():
    """Shares (PLAN 4b, 4c): value / basis in %, on the value line ("· 22 %"), coloured by size, the basis named in the
    tooltip; no number when the units differ, either side is stale or missing, or the basis is zero."""
    r = _node_callouts(SHARE_JS)
    assert r["ok"] == 1.0 and r["units"] is None and r["stale"] is None and r["zero"] is None and r["missing"] is None
    assert '#6e8d7f' == r["colors"][0] and r["colors"][1] == r["colors"][2] == "#e8f5ee"     # muted → strong by |size|
    assert '>· 1.0 % gen</span>' in r["html"] and 'title="1.0 % of AESO generation"' in r["html"]
    assert '>· 22 % prod</span>' in r["big"] and 'style="color:#' in r["big"]
    assert '<span class="v">1,375</span>' in r["row"] and '>· 13 % gen</span>' in r["row"] and 'class="row share"' not in r["row"]
    assert r["cues"] == ["gen", "exp", "dem", "prod", "load", "lng"]   # the basis cue: a share can't read as utilisation
    assert r["none"] == ""
    # PLAN 4c signs: demand (and gas burn, deliveries, OSDA, AESO load) reads negative, production and generation positive,
    # interties as stored; `flow` overrides. A demand's change chip carries the sign; its share stays a size.
    assert r["signs"] == [-1, -1, -1, -1, -1, 1, 1, 1, 1, -1, -1]
    assert abs(r["demandShare"] - 4.2) < 1e-9                                   # a share is a size: +4.2 % of AB demand
    # step 5: a row's own share (a fuel's % of AB load); a net export or storage charging (stored negative) reads negative
    assert abs(r["exportShare"] + 5.0) < 1e-9 and '>· 59 % load</span>' in r["rowShare"]
    assert 'title="a reservoir level, not plant output">level</span>' in r["level"]   # Power #241: a level, never output
    assert 'class="row sub"' in r["subRow"] and '<div class="k" title="oil sands (OSDA)">oil sands (OSDA)</div>' in r["subRow"]   # "· " = a part, indented
    assert '<span class="v">-420</span>' in r["demandRow"] and 'class="chip down">' in r["demandRow"], r["demandRow"]
    assert "≈" not in r["html"] and "±" not in r["html"]                       # measured ÷ measured: plain
    # a ±20 % city over a ±3 % total: 4.2 % ±(4.2 × √(0.2² + 0.03²) = 0.85) → "≈ 4.2 ±0.8"; never a bare 4.2 %
    assert '>· ≈4.2 ±0.8 % dem</span>' in r["city"] and "4.2 % of AB demand (estimate ±20%" in r["city"]
    assert abs(r["basisOnly"] - 0.09) < 1e-9 and abs(r["estRange"] - 0.1) < 1e-9   # "1.0 % of AESO generation"


def test_burn_rows_show_the_window_mean_never_the_latest_point():
    """Power's burn rows (network/power, plain labels, @1h series) render through the real callouts.js: the shown value
    is agg.mean with the payload's "1 h mean · n 12" tag (the one place the window appears), or NO-RESULT when the hour
    is thin, never the latest 5-min point `v`."""
    rows = []
    for fn in sorted((paths.REPO_ROOT / "network/power").glob("*.geojson")):
        for f in json.loads(fn.read_text(encoding="utf-8"))["features"]:
            rows += [{"series": x["series"], "agg": x.get("agg"), "label": x["label"], "unit": x["unit"], "priority": 3}
                     for x in f["properties"].get("callouts_extra", []) if ".gas_burn." in x["series"]]
    assert len(rows) >= 15
    out = _node_callouts(BURN_JS.replace("__ROWS__", json.dumps(rows)))
    for r in out:
        h = r["html"]
        assert "mean" not in r["label"] and r["key"].endswith("@1h"), r["label"]
        assert "999" not in h, r["label"]                                   # never the latest point
        if ".lo@" in r["key"]:
            assert _val(h) == "--" and 'title="thin: n=6 &lt; 9">NO-RESULT' in h
        else:
            # gas burn is gas demand, so it reads negative (PLAN 4c signs); the window still comes from the payload
            assert _val(h) == "-41.5" and '<span class="agg">1 h mean · n 12</span>' in h


# ---- river flow particles (PLAN roadmap step 4, P5d; web/rivers.js, web/rivers.geojson)
def test_peace_river_geometry_and_reaches():
    """The Peace centreline runs from W.A.C. Bennett Dam to the Fort Vermilion gauge; its reaches are contiguous; each
    reach's discharge source is a real series (the connector's power.hydro.<plant>.flow) or a mapped WSC gauge."""
    fc = json.loads((WEB / "rivers.geojson").read_text(encoding="utf-8"))
    f = next(x for x in fc["features"] if x["id"] == "river.peace")
    c, props = f["geometry"]["coordinates"], f["properties"]
    assert len(c) >= 100 and "OpenStreetMap" in props["source"] and "ODbL" in props["source"]
    nodes = {}
    for fn in sorted((paths.REPO_ROOT / "network/power").glob("*.geojson")):
        for ft in json.loads(fn.read_text(encoding="utf-8"))["features"]:
            nodes[ft["properties"]["id"]] = ft["geometry"]["coordinates"]
    gauges = {x["properties"]["station"]: x["geometry"]["coordinates"]
              for x in json.loads((WEB / "gauges.geojson").read_text(encoding="utf-8"))["features"]}
    near = lambda a, b, t=0.06: abs(a[0] - b[0]) < t and abs(a[1] - b[1]) < t
    assert near(c[0], nodes["power.plant.hydro_wac_bennett"]) and near(c[-1], gauges["07HF001"])
    rs = props["reaches"]
    assert rs[0]["from"] == 0 and rs[-1]["to"] == len(c) - 1
    assert all(a["to"] == b["from"] and a["from"] < a["to"] for a, b in zip(rs, rs[1:]))
    conn = (paths.REPO_ROOT / "montney/connectors/weather_peace_mw.py").read_text(encoding="utf-8")
    assert 'f"power.hydro.{k}.flow"' in conn
    for r in rs:
        if r["src"]["type"] == "dam":
            assert re.fullmatch(r"power\.hydro\.(wac_bennett|peace_canyon|site_c)\.flow", r["src"]["id"]), r
        else:
            assert r["src"]["id"] in gauges, r
    assert [r["anchor"] for r in rs[:3]] == ["wac_bennett", "peace_canyon", "site_c"]


RIVER_JS = """
const S = { series: { "d.fresh": { v: 1200, stale: false }, "d.stale": { v: 900, stale: true }, "d.zero": { v: 0, stale: false },
                      "d.null": { v: null } },
            gauges: { ok: { value: 1800, state: "ok" }, nr: { value: 950, state: "no-result", reason: "history n=95 < 150" },
                      st: { value: 400, state: "stale", reason: "no real-time discharge in the last 3 h" },
                      se: { value: null, state: "seasonal", reason: "seasonal" },
                      nr0: { value: null, state: "no-result", reason: "no data" } } };
S.series["ab.out"] = { v: 42, stale: false };
const F = (type, id) => P.reachFlow({ type, id }, S);
const sp = (q, moving, km = 60) => P.particleSpec({ q, moving }, km);
console.log(JSON.stringify({
  flows: { fresh: F("dam", "d.fresh"), stale: F("dam", "d.stale"), zero: F("dam", "d.zero"), nul: F("dam", "d.null"),
           missing: F("dam", "nope"), ok: F("gauge", "ok"), nr: F("gauge", "nr"), st: F("gauge", "st"), se: F("gauge", "se"),
           gmissing: F("gauge", "nope"), nr0: F("gauge", "nr0"), series: F("series", "ab.out"),
           none: P.reachFlow({ type: "none", reason: "reservoir level only, no flow" }, S),
           seFallback: P.reachFlow({ type: "gauge", id: "se", fallback: { type: "series", id: "ab.out" } }, S) },
  spec: { q750: sp(750, true), q1500: sp(1500, true), q3000: sp(3000, true), q9000: sp(20000, true), frozen: sp(1500, false),
          unknown: sp(null, false), none: P.particleSpec({ none: true, q: null, moving: false }, 60) },
  dt: { slow: P.frameDt(true, 1000, 1500), fast: P.frameDt(true, 1000, 1033), restart: P.frameDt(false, 1000, 60000),
        pause: P.frameDt(true, 1000, 60000) },
  consts: { Q_REF: P.Q_REF, CAP: P.CAP, SPEED: P.SPEED_PX_S, FRAME: P.FRAME_MS } }));
"""


def test_river_particles_follow_measured_discharge():
    """Motion only from fresh, measured discharge (step 5 adopts Weather's reach-spec rule for every river): a dam or AB
    Rivers series needs a fresh positive value; a gauge moves on state ok, or no-result with a value. Stale, seasonal,
    zero and missing reaches are frozen (never invented motion); a seasonal gauge may fall back to the release above;
    a reservoir level (`none`) draws no particles. Speed and density are ∝ √(Q / Q_REF), capped."""
    r = _node(RIVER_JS, "rivers.js")
    fl, sp, k = r["flows"], r["spec"], r["consts"]
    assert fl["fresh"] == {"q": 1200, "moving": True, "reason": None} and fl["ok"]["moving"] and fl["ok"]["q"] == 1800
    for name in ("stale", "zero", "nul", "missing", "st", "se", "gmissing", "nr0", "none"):
        assert fl[name]["moving"] is False, name
    assert fl["nr"]["moving"] and fl["nr"]["q"] == 950                              # no-result with a fresh value moves
    assert fl["series"]["moving"] and fl["series"]["q"] == 42
    assert fl["se"]["seasonal"] and fl["seFallback"]["moving"] and fl["seFallback"]["q"] == 42 and fl["seFallback"]["seasonal"]
    assert sp["q1500"]["speed"] == k["SPEED"] and abs(sp["q750"]["speed"] - k["SPEED"] * 0.5 ** 0.5) < 1e-9
    assert abs(sp["q3000"]["speed"] - k["SPEED"] * 2 ** 0.5) < 1e-9
    assert sp["q9000"]["speed"] == k["CAP"] * k["SPEED"]                          # capped (20,000 m³/s: √13.3 > 2.5)
    assert sp["none"]["n"] == 0 and sp["none"]["speed"] == 0                       # a reservoir level: no particles
    assert sp["q750"]["n"] < sp["q1500"]["n"] < sp["q3000"]["n"]                    # density grows with discharge
    assert sp["frozen"]["speed"] == 0 and sp["unknown"]["speed"] == 0 and sp["unknown"]["n"] >= 1
    assert k["FRAME"] >= 33                                                         # frame budget: <= ~30 repaints/s
    # true speed at any frame rate: a 2 fps machine advances 0.5 s per frame (not a clamped 0.1 s, which ran the live
    # proof's particles at 10 % of their encoded speed); a restart never jumps; a long stall is capped at 1 s
    assert r["dt"] == {"slow": 0.5, "fast": 0.033, "restart": 0, "pause": 1}
    # G1: the particle layer goes in below every network layer (the first one is areas-fill), so data lines draw over it
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'map.addLayer(rivers, "areas-fill")' in js
    # its id must be new: MapLibre rejects a duplicate id with an error event (no throw), which silently drops the
    # layer, and the toggle would then hide the basemap's own `rivers` waterway line instead
    lid = re.search(r'new RiverLayer\("([^"]+)"', js).group(1)
    assert lid not in re.findall(r'\bid: "([^"]+)"', (WEB / "style.js").read_text(encoding="utf-8"))
    assert lid in [l["id"] for l in json.loads((WEB / "map.json").read_text(encoding="utf-8"))["layers"]]
    assert f'map.getLayer("{lid}")) map.setLayoutProperty("{lid}"' in js
    # L-106 region colours: only the regions' faint tint (not data) sits under the particles; every data layer is above
    assert [l["id"] for l in _network_layers()["layers"]][:2] == ["region-fill", "areas-fill"]


def test_step5_rivers_follow_weathers_reach_specs():
    """Step 5: every river in Weather's two reach specs is drawn (OSM, cited), its reaches are contiguous, and each reach's
    source is a mapped gauge, an AB Rivers series the spec names, or `none` (a reservoir level / a dam without a release)."""
    fc = json.loads((WEB / "rivers.geojson").read_text(encoding="utf-8"))
    by = {f["id"]: f for f in fc["features"]}
    gauges = {x["properties"]["station"] for x in json.loads((WEB / "gauges.geojson").read_text(encoding="utf-8"))["features"]}
    for spec in ("weather-rivers-ab.json", "weather-rivers-columbia.json"):
        d = json.loads((paths.REPO_ROOT / "docs/research/v2" / spec).read_text(encoding="utf-8"))
        for r in d["rivers"]:
            f = by[r["id"]]
            c, rs = f["geometry"]["coordinates"], f["properties"]["reaches"]
            assert "OpenStreetMap" in f["properties"]["source"] and rs, r["id"]
            assert all(a["to"] == b["from"] and a["from"] < a["to"] for a, b in zip(rs, rs[1:])) and rs[-1]["to"] <= len(c) - 1
            series = {a["src"]["id"] for a in r["anchors"] if a.get("src") and a["src"]["type"] == "series"}
            for x in rs:
                for s in (x["src"], x["src"].get("fallback")):
                    if not s:
                        continue
                    assert s["type"] in ("gauge", "series", "none"), x
                    assert s["type"] != "gauge" or s["id"] in gauges, x
                    assert s["type"] != "series" or s["id"] in series, x
    assert "river.peace" in by


def _hex_hl(h):
    import colorsys
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hh, ll, _ = colorsys.rgb_to_hls(r, g, b)
    return hh * 360, ll


def test_gauge_nits_from_149():
    """#149 review: gauges dim off "now" with the network; clicks reach a sprite before the gauge below it; and no band
    colour sits on the alert amber/red (hue within 12 deg AND lightness within 0.2 would read as an alert)."""
    lay = _network_layers()
    gl = {l["id"]: l for l in lay["layers"]}["gauges"]
    p = {"g_fillop": 1, "g_op": 1}
    assert _ev(gl["paint"]["circle-opacity"], p) == 1
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert '"nodes", "gauges"' in js
    style = (WEB / "style.js").read_text(encoding="utf-8")
    assert '["*", ["get", "g_fillop"], ["get", "g_op"], k]' in style            # k = 0.45 off "now"
    bands = re.search(r"BAND_COLOR = \{([^}]*)\}", (WEB / "gauges.js").read_text(encoding="utf-8")).group(1)
    colours = re.findall(r'#[0-9a-fA-F]{6}', bands)
    assert len(colours) == 5
    for alert in ("#ffb547", "#ff4d6d"):
        ah, al = _hex_hl(alert)
        for c in colours:
            h, l = _hex_hl(c)
            dh = min(abs(h - ah), 360 - abs(h - ah))
            assert dh >= 12 or abs(l - al) >= 0.2, (c, alert, round(dh, 1), round(abs(l - al), 2))


API_JS = r"""
globalThis.location = { search: "" };
const calls = [];
globalThis.fetch = (path, opt) => new Promise((res, rej) => {
  const c = { path, inm: opt.headers["If-None-Match"] || null, aborted: false };
  calls.push(c);
  opt.signal?.addEventListener("abort", () => { c.aborted = true; rej(Object.assign(new Error("aborted"), { name: "AbortError" })); });
  setTimeout(() => {
    const h = new Map([["etag", 'W/"v1"'], ["x-next-update", "42"]]);
    const notMod = c.inm === 'W/"v1"';
    res({ status: notMod ? 304 : 200, ok: !notMod, headers: { get: (k) => h.get(k.toLowerCase()) ?? null },
          json: async () => ({ path }), text: async () => "" });
  }, 20);
});
const L = await import(LIB);
const a = L.api("/api/snapshot?extra=a", { key: "snapshot" }), b = L.api("/api/snapshot?extra=a", { key: "snapshot" });
const shared = a === b;
const first = await a;
const c = L.api("/api/snapshot?extra=a", { key: "snapshot" }), d = L.api("/api/snapshot?extra=b", { key: "snapshot" });
let abortedErr = null; try { await c; } catch (e) { abortedErr = L.aborted(e); }
const scoped = await d;
const nm = await L.api("/api/snapshot?extra=b", { etag: first._etag, key: "snapshot" });
console.log(JSON.stringify({ shared, fetches: calls.length, etag: first._etag, next: first._next, abortedErr,
  scoped: scoped.path, nm, inm: calls.at(-1).inm, keys: Object.keys(first) }));
"""


def test_api_requests():
    """L-081 (Database #312): one request in flight per endpoint (a second call for the same path shares it, a new scope
    aborts it, and the abort isn't an error to show), the ETag goes back as If-None-Match and a 304 carries the server's
    X-Next-Update; `_etag`/`_next` ride on the body without becoming JSON keys."""
    lib = "data:text/javascript;base64," + base64.b64encode((WEB / "lib.js").read_text(encoding="utf-8").encode()).decode()
    r = _node(API_JS.replace("LIB", json.dumps(lib)), "power.js")
    assert r["shared"] and r["etag"] == 'W/"v1"' and r["next"] == 42
    assert r["abortedErr"] is True and r["scoped"] == "/api/snapshot?extra=b"
    assert r["nm"] == {"status": 304, "next": 42} and r["inm"] == 'W/"v1"'
    assert r["fetches"] == 4                     # the shared call made one fetch: a, (b shared), c (aborted), d, the 304
    assert r["keys"] == ["path"]
    src = (WEB / "map.js").read_text(encoding="utf-8")
    for ep in ("snapshot", "events", "status", "network"):   # every polled endpoint revalidates and keeps one in flight
        assert re.search(r'api\(["`]/api/' + ep + r'.{0,200}?\{ etag: S\.\w+Tag, key: "' + ep + r'" \}', src, re.S), ep


def test_snapshot_scope_rules():
    """L-081 PR 2 (Database #317): map.json `tiers` is the one tier table (numeric keys only: the server sorts them as
    ints) and matches callouts.js's defaults; a box or dot is in tier t when its minzoom is below the next tier's (the
    server's rule) and no maxzoom ends it at or before tier t's start; the map asks for ?z=<tier of the level zoom>."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    assert all(k.isdigit() for k in cfg["tiers"]), cfg["tiers"]
    r = _node_callouts("""const T = C.TIER_MINZOOM;
console.log(JSON.stringify({ T, cases: [
  C.inTier(0, null, 0, T), C.inTier(6, null, 0, T), C.inTier(6, null, 1, T), C.inTier(6.3, null, 1, T),
  C.inTier(0, 6, 0, T), C.inTier(0, 6, 1, T), C.inTier(0, 6, 3, T), C.inTier(7.5, null, 3, T), C.inTier(null, null, 3, T) ] }));""")
    assert {int(k): v for k, v in r["T"].items()} == {int(k): v for k, v in cfg["tiers"].items()}
    #  tier-0 box in 0 | a zoom-6 box not in 0 | in 1 | minzoom 6.3 lands in tier 1 | maxzoom 6 box in 0, not in 1 or 3 | tier 3
    assert r["cases"] == [True, False, True, True, True, False, False, True, True]
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert "/api/snapshot?z=${t}&extra=" in src and "const t = tierAt(S.ze);" in src   # the level zoom, not map.getZoom()
    assert "/api/snapshot?features=" in src                                           # a lite feature's full entry on click
    # ...for a dot clicked open (its pinned box), a panel, and pinned boxes again after each new scope
    assert src.count("ensureFull([id])") == 2 and "ensureFull([...S.pinned])" in src
    assert "e = entryOf(S.snap, id);" in src                                          # paintNetwork reads lite too


def test_tieout_preliminary_day():
    """Gas #328 (orchestrator): TC revises a GDSR gas day for up to ~47 h, so the tie-out marks a gas day younger than
    map.json `preliminary_hours` (48) since its start (map.json gas_day.start_hour_utc on its date) as preliminary.
    Rule-based on the day's age: no ids."""
    lib = "data:text/javascript;base64," + base64.b64encode((WEB / "lib.js").read_text(encoding="utf-8").encode()).decode()
    src = (WEB / "tieout.js").read_text(encoding="utf-8").replace('from "./lib.js"', f"from {json.dumps(lib)}")
    url = "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    h, start = cfg["tieouts"]["ngtl.csr.net_storage"]["preliminary_hours"], cfg["gas_day"]["start_hour_utc"]
    assert h == 48 and start == 15
    d = 1793491200                                   # gas day 2026-11-01 as stored (00:00 UTC); it starts 15:00 UTC
    nows = [d + 3600 * x for x in (15, 15 + 47.99, 15 + 48, 15 + 72)]
    r = _node('globalThis.location = { search: "" };\nconst T = await import(' + json.dumps(url) + ");\nconsole.log(JSON.stringify("
              + json.dumps(nows) + f".map((n) => T.isPreliminary({d}, n, {start}, {h})).concat([T.isPreliminary({d}, {d}, {start}, 0)])));", "placement.js")
    assert r == [True, True, False, False, False]    # just started, 47.99 h, 48 h, 3 days; and no rule (0 h) means never
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert "gas_day_start_h: config.gas_day?.start_hour_utc" in js   # the tie-out gets the gas day's start from config



def test_columbia_hydro_cue():
    """L-085 (Weather #334): a row's own % of normal (`normal: <series>`, or `"self"` when the value is one) gets an
    above/near/below cue from its thresholds (>= 110 above, <= 90 below), with no bullish/bearish word."""
    r = _node_callouts("""const cue = { above: 110, below: 90 }, snap = { series: { n: { v: 85.2 }, s: { v: 120, stale: true } } };
console.log(JSON.stringify({ cues: [C.cueOf(110, cue), C.cueOf(109.9, cue), C.cueOf(90, cue), C.cueOf(90.1, cue), C.cueOf(null, cue)],
  own: C.rowNormal({ normal: "n" }, 3200, snap), self: C.rowNormal({ normal: "self" }, 80.1, snap), stale: C.rowNormal({ normal: "s" }, 1, snap),
  staleRow: C.rowNormal({ normal: "s" }, 1, snap, true),
  html: C.normalHtml("x", snap, { normal: "n", cue }, 3200), selfHtml: C.normalHtml("x", snap, { normal: "self", cue }, 80.1) }));""")
    assert r["cues"] == ["above", "near", "below", "near", None]
    assert r["own"] == 85.2 and r["self"] == 80.1 and r["stale"] is None     # a stale % of normal: nothing next to a fresh value
    assert r["staleRow"] == 120                                              # ...but a stale row keeps it (its STALE chip covers both)
    assert "· 85 % of normal" in r["html"] and "below normal" in r["html"]
    assert "% of normal" not in r["selfHtml"] and "below normal" in r["selfHtml"]   # the value is the %; just the cue
    assert not re.search(r"bull|bear", r["html"] + r["selfHtml"], re.I)
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    box = next(c for c in cfg["callouts"] if c["id"] == "c.columbia_hydro")
    assert box["tier"] == 1 and all(r["cue"] == {"above": 110, "below": 90} for r in box["rows"] if "normal" in r)   # L-106 8a: tier-1 detail (the region boxes are the overview)
    # folded, it keeps its three headline numbers (lead rows); the storage total waits for a click
    lead = _node_callouts("console.log(JSON.stringify(C.leadRows(" + json.dumps(box["rows"]) + ")));")
    assert lead == [True, True, False, True]
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'if (r.normal && r.normal !== "self") ids.add(r.normal)' in src   # the % of normal series is requested


def test_pacnw_tiers():
    """L-085 (Gas #336): the PacNW boxes (Lower Mainland demand, Sumas, Jackson Prairie, Stanfield, Malin) show from
    zoom 6; fully zoomed out, Huntingdon's tier-0 box carries Sumas and the Lower Mainland under it. maxBounds reaches
    Malin (42.0° N). Every override is an object: the server's scope reads each one (Database #317)."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov = cfg["overrides"]
    for fid in ("gas.bc.lower_mainland", "gas.nwp.sumas", "gas.nwp.jackson_prairie", "gas.stanfield", "gas.malin"):
        assert ov[fid]["tier"] == 1, fid
    assert ov["gas.huntingdon"]["tier"] == 1   # L-106 8a: tier-1 detail (the region boxes are the overview)
    assert all(isinstance(o, dict) for o in ov.values())
    (w, s), (e, n) = cfg["view"]["maxBounds"]
    assert s <= 42.0 - 0.5 and w <= -123.2 and e >= -116.3   # Malin, Vancouver, Kingsgate inside, with margin


def test_outer_level():
    """L-087 (Erik: "when max zoomed [out] ... combine Alberta demand, production, export, and storage"): the floor zoom
    is where maxBounds (or minZoom) stops zooming out on this screen, so a phone reaches the outermost level too; below
    floor + view.outer_span, Alberta's demand, production, exports and storage merge into one box (in that order) and
    SK / MB get their own; BC folds Huntingdon under Kitimat; the rest of today's overview boxes (not their dots) wait."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    r = _node("""const map = (w, h, min) => ({ getMinZoom: () => min, getContainer: () => ({ clientWidth: w, clientHeight: h }) });
const B = """ + json.dumps(cfg["view"]["maxBounds"]) + """;
console.log(JSON.stringify([P.floorZoom(map(1536, 730, 4), B), P.floorZoom(map(412, 915, 2.36), B), P.floorZoom(map(400, 300, 6), B), P.floorZoom(map(1536, 730, 4), null)]));""",
              "placement.js")
    desk, phone, minwins, nob = r
    # PLAN 10a: the bounds are generous, so minZoom stops a desktop (4 here, the test's own); a phone's lower minZoom
    # (NARROW_DZ) meets the bounds' height first, still with the whole continent's regions on its screen
    assert desk == 4 and 2.0 < phone < 3.0
    assert minwins == 6 and nob == 4
    assert cfg["view"]["zoom"] > cfg["view"]["minZoom"] + cfg["view"]["outer_span"]   # the opening view stays on today's overview
    # L-106 8a: the outermost level's own boxes (Alberta / BC merged stacks, SK, MB, the TC overview, Birchbank) are
    # retired or tier-1 detail: the region boxes are the overview at every zoom below 6. The "outer" machinery stays.
    ov, co = cfg["overrides"], {c["id"]: c for c in cfg["callouts"]}
    assert not [k for k, o in [*ov.items(), *co.items()] if "outer" in (o.get("merge_until"), o.get("minzoom"), o.get("maxzoom"), o.get("box_minzoom"))]
    assert not {"c.sk_outer", "c.mb_outer", "c.bc_demand_outer"} & set(co) and cfg["gauge_boxes"]["stations"]["08NE049"]["tier"] == 1
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert "minzoom: o.box_minzoom ?? o.minzoom" in src and "floorZoom(map, config.view.maxBounds)" in src



def test_pacnw_summary():
    """L-086 (Erik: one PacNW box, visible fully zoomed out; Gas #341): imports is the box (title "PacNW"), demand and
    storage merge into it as sections at every zoom; it's tier 0 with no box_minzoom, so it shows at the outermost level
    too. Both boxes near the Columbia (Yakima, The Dalles) prefer opposite sides so they don't crowd each other."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov, co = cfg["overrides"], {c["id"]: c for c in cfg["callouts"]}
    box = ov["gas.pnw.imports"]
    assert box["tier"] == 1 and box["title"] == "PacNW" and "box_minzoom" not in box and "maxzoom" not in box   # L-106 8a: tier-1 detail (the region boxes are the overview)
    for k, sec in (("gas.pnw.demand", "Demand · MMcf/d"), ("gas.pnw.storage", "Storage · Bcf")):
        assert ov[k]["merge_into"] == "gas.pnw.imports" and ov[k]["section"] == sec and "merge_until" not in ov[k], k
    # Columbia goes south-west, PacNW north-east into the open interior, so neither crowds the other; PacNW places
    # before the outermost level's province boxes (L-089: with The Dalles it's a row taller and needs its room first)
    assert box["prefer"][0] in ("N", "NE", "E") and co["c.columbia_hydro"]["prefer"][0] in ("S", "SW", "W")
    # L-089 (Erik): The Dalles flow, with its % of normal and cue, is a section of the PacNW box at every zoom
    d = co["c.pnw_dalles"]
    assert d["merge_into"] == "gas.pnw.imports" and "merge_until" not in d and d["always_show"]
    hl = co["c.columbia_hydro"]["rows"][0]
    assert {k: d["rows"][0][k] for k in ("series", "normal", "cue")} == {k: hl[k] for k in ("series", "normal", "cue")}


def test_weather_region_seams():
    """L-090 (Weather #347) and Weather #370 (east grid to 41 N): each edge's stretch shared with another region (its
    opposite edge on the same line, overlapping along it) skips the edge fade, and only that stretch: the south grid's east
    edge is shared with the east grid for 41..44 N and still fades below 41 N (Reviewer on #370). The east grid's west
    edge joins two neighbours (west grid 44..65, south grid 41..44) into one stretch."""
    g = lambda lon0, lat0, nx, ny, d=0.25: {"t2m": {"geom": {"lon0": lon0, "lat0": lat0, "dx": d, "dy": d, "nx": nx, "ny": ny}}}
    west, east, south = g(-140, 44, 181, 85), g(-95, 41, 55, 49, 0.5), g(-140, 32.5, 91, 24, 0.5)   # -140..-95 x 44..65 etc.
    r = _node("const W = " + json.dumps(west) + ", E = " + json.dumps(east) + ", S = " + json.dumps(south) + """;
const all = [W, E, S];
console.log(JSON.stringify({ w: P.seamsOf(W, all), e: P.seamsOf(E, all), s: P.seamsOf(S, all), alone: P.seamsOf(W, [W]) }));""", "weather.js")
    #               west          east          south        north
    assert r["w"] == [None, [44, 65], [-140, -95], None]   # meets east at -95 (44..65) and south at 44 N
    assert r["e"] == [[41, 65], None, None, None]          # west edge: south grid 41..44 + west grid 44..65; its south edge fades
    assert r["s"] == [None, [41, 44], None, [-140, -95]]   # east edge shared only 41..44 N: below that it fades
    assert r["alone"] == [None, None, None, None]
    src = (WEB / "weather.js").read_text(encoding="utf-8")
    assert "uniform vec4 u_seamWE, u_seamSN;" in src and "span(gi.y, u_seamWE.xy)" in src

def test_california_box():
    """L-090 (Gas #351, Power #350): one California box at tier 0 and at the outermost level. Gas demand is the box
    (title "California"), then power (CAISO), gas storage and gas imports merge in as sections, in that order; the gas
    heads say PG&E + SoCalGas (Gas: direct-connects and SW Gas aren't in). The import points show from zoom 6."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov = cfg["overrides"]
    box = ov["gas.cal.demand"]
    assert box["tier"] == 1 and box["title"] == "California" and "box_minzoom" not in box and "PG&E + SoCalGas" in box["section"]   # L-106 8a: tier-1 detail (the region boxes are the overview)
    merged = sorted((o["merge_order"], k) for k, o in ov.items() if o.get("merge_into") == "gas.cal.demand")
    assert [k for _, k in merged] == ["power.region.ca", "gas.cal.storage", "gas.cal.imports"]
    assert all("merge_until" not in ov[k] for _, k in merged)   # one box at every zoom
    assert all("PG&E + SoCalGas" in ov[k]["section"] for k in ("gas.cal.storage", "gas.cal.imports")) and "CAISO" in ov["power.region.ca"]["section"]
    for k in ("topock", "ehrenberg", "kramer", "wheeler_ridge"):
        assert ov[f"gas.cal.{k}"]["tier"] == 1, k
    assert cfg["view"]["maxBounds"][0][1] <= 32.5



def test_daily_fallback_reads_own_value():
    """L-090 (CAISO): a feature's own value lives in the snapshot's `values`, so the daily fallback checks it there; a
    live hourly value must not fall back to its daily sum (it did: California power showed −537,999 MWh/d, not MW)."""
    src = (WEB / "map.js").read_text(encoding="utf-8")
    df = src[src.index("const dailyOf = (s) =>"):src.index("const ownDaily")]
    assert "s === p.value ? rowLive({ feature: id })" in df and "!live" in df



def test_ontario_quebec():
    """L-091 (Gas #358, Art #359): an Ontario box (gas demand, imports/exports, IESO power as sections) and a Québec box,
    at tier 0 and the outermost level; the TC points from zoom 6; bounds east past Québec City and the Saguenay; nuclear
    plants get Art's static sprite."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov, co = cfg["overrides"], {c["id"]: c for c in cfg["callouts"]}
    assert ov["gas.ont.demand"]["tier"] == 1 and ov["gas.ont.demand"]["title"] == "Ontario" and ov["gas.qc.demand"]["tier"] == 1   # L-106 8a: tier-1 detail (the region boxes are the overview)
    assert all("box_minzoom" not in ov[k] for k in ("gas.ont.demand", "gas.qc.demand"))   # fully zoomed out too
    into = sorted((o.get("merge_order", 0), k) for k, o in [*ov.items(), *co.items()] if o.get("merge_into") == "gas.ont.demand")
    assert [k for _, k in into] == ["c.ont_flows", "power.region.on"]
    assert {r["series"] for r in co["c.ont_flows"]["rows"]} == {"tcm.niagara.imports", "tcm.iroquois.exports"}
    # Power #361: Hydro-Québec's demand and mix merge into the Québec box as its power section
    assert ov["power.region.qc"]["merge_into"] == "gas.qc.demand" and ov["gas.qc.demand"]["title"] == "Québec"
    for k in ("parkway", "niagara", "waddington"):
        assert ov[f"gas.ont.{k}"]["tier"] == 1, k
    assert cfg["view"]["maxBounds"][1][0] >= -71.0      # Québec City (−71.2) and the Saguenay (−71.1) inside
    k = _node("console.log(JSON.stringify([P.spriteKind({ type: 'plant', fuel: 'nuclear' }), P.spriteState('plant-nuclear', 3000, 0.9)]));", "sprites.js")
    assert k == ["plant-nuclear", "static"]
    manifest = json.loads((WEB / "sprites" / "nodes-manifest.json").read_text(encoding="utf-8"))
    assert "plant-nuclear" in json.dumps(manifest)       # the atlas has it (Art #359)
def test_depth_zero_row():
    """Gas #354 (orchestrator): a row with `depth: 0` is a headline of its own, not a part of the one above: a root in
    the row tree (its own parts nest under it) and kept when the box is folded, on every box (Malin's PG&E receipts)."""
    rows = [{"label": "Malin → California"}, {"label": "Tuscarora", "depth": 1}, {"label": "PG&E receipts", "depth": 0},
            {"label": "via Redwood", "depth": 1}, {"label": "GTN local", "depth": 1}]
    r = _node_callouts("const rows = " + json.dumps(rows) + ";\nconsole.log(JSON.stringify({ tree: C.rowTree(rows), lead: C.leadRows(rows) }));")
    assert r["tree"]["depth"] == [0, 1, 0, 1, 1]
    assert r["tree"]["parent"] == [-1, 0, -1, 2, 2]          # the depth-0 row's parts nest under it, not the headline
    assert r["lead"] == [True, False, True, False, False]     # folded: both headlines show
    src = (WEB / "callouts.js").read_text(encoding="utf-8")
    assert "const zoomOk = d === 0 ||" in src                 # a headline isn't held back by its row priority's zoom



def test_hydro_east_boxes():
    """L-091 (Weather #362): the Manitoba and Québec hydro boxes at tier 0 (not at the outermost level, like Columbia
    hydro); an override's `cue` words each "% of normal" row above / near / below normal (Weather: >= 110 / <= 90)."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov = cfg["overrides"]
    for k in ("power.hydro_summary.mb", "power.hydro_summary.qc"):
        assert ov[k]["tier"] == 1 and "box_minzoom" not in ov[k] and ov[k]["cue"] == {"above": 110, "below": 90}, k   # L-106 8a: tier-1 detail (the region boxes are the overview)
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'if (o.cue) for (const r of rows) if (/% of normal/.test(' in src and 'normal: "self", cue: o.cue' in src



def test_temperature_ramp_contrast():
    """Erik (2026-11-03): "I am looking at 3 degrees to 16 degrees, and its a marginal change in colour". Every 3 °C step
    across the common range (−30..+35 °C) is a visible colour change (CIE76 ΔE ≥ 9; the old ramp went down to 6), and
    3 → 16 °C spans a large one (ΔE ≥ 40; was 31). Still one smooth ramp: stops increase and the legend uses the same."""
    src = (WEB / "weather.js").read_text(encoding="utf-8")
    t = src[src.index("t2m: {"):src.index("wind: {")]
    st = [(float(v), c) for v, c in re.findall(r'\[(-?\d+(?:\.\d+)?), "(#[0-9a-f]{6})"\]', t)]
    assert [v for v, _ in st] == sorted(v for v, _ in st) and len(st) >= 15
    def at(v):
        for (v0, c0), (v1, c1) in zip(st, st[1:]):
            if v0 <= v <= v1:
                f = (v - v0) / (v1 - v0); a, b = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (c0, c1))
                return [a[k] + (b[k] - a[k]) * f for k in range(3)]
    def lab(rgb):
        lin = [(c / 255) / 12.92 if c / 255 <= 0.04045 else (((c / 255) + 0.055) / 1.055) ** 2.4 for c in rgb]
        r, g, b = lin
        xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
        f = [q ** (1 / 3) if q > 0.008856 else 7.787 * q + 16 / 116 for q in xyz]
        return (116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2]))
    de = lambda u, v: sum((p - q) ** 2 for p, q in zip(lab(at(u)), lab(at(v)))) ** 0.5
    steps = [de(v, v + 3) for v in range(-30, 33)]
    assert min(steps) >= 9, min(steps)
    assert de(3, 16) >= 40
    # no dark ribbon along an isotherm (Reviewer on #371: a 9 °C stop at L* 53 between 81 and 78 read like a front):
    # no interior stop is more than 10 L* darker than both its neighbours
    L = [lab([int(c[i:i + 2], 16) for i in (1, 3, 5)])[0] for _, c in st]
    dips = [st[i][0] for i in range(1, len(st) - 1) if L[i] < L[i - 1] - 10 and L[i] < L[i + 1] - 10]
    assert not dips, dips



def test_bc_exports_and_alberta_order():
    """L-092 (Erik): D) at moderate zoom (the overview, above the outermost level and below the facility boxes) Huntingdon,
    Sumas → NWP and the Lower Mainland are one box, splitting into their own boxes from zoom 6 (Huntingdon's own box waits,
    its dot doesn't); E) the outermost Alberta box leads with production (a negative merge_order puts a section above the
    target's own rows: config, not a special case)."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov, co = cfg["overrides"], {c["id"]: c for c in cfg["callouts"]}
    # L-106 8a: the overview's BC exports box and the outermost Alberta box are retired (the region box says it); the
    # facility boxes show from zoom 6, and a negative merge_order still puts a section above its target's rows
    assert "c.bc_exports" not in co and "merge_into" not in ov["gas.production.total"]
    assert ov["gas.huntingdon"]["tier"] == 1 and ov["gas.nwp.sumas"]["tier"] == 1 and ov["gas.bc.lower_mainland"]["tier"] == 1
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert "if ((o.merge_order ?? 0) < 0) { dst.rows.splice(dst.front, 0, ...sec)" in src



def _node_sysmenu(script: str):
    """Run `script` with `M` bound to web/sysmenu.js, its imports (lib.js, status.js) inlined as data URLs."""
    def data_url(src):
        return "data:text/javascript;base64," + base64.b64encode(src.encode()).decode()
    lib, status = (data_url((WEB / f).read_text(encoding="utf-8")) for f in ("lib.js", "status.js"))
    src = (WEB / "sysmenu.js").read_text(encoding="utf-8")
    src = src.replace('from "./lib.js"', f"from {json.dumps(lib)}").replace('from "./status.js"', f"from {json.dumps(status)}")
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "sysmenu.js gained an import the test doesn't inline"
    return _node('globalThis.location = { search: "" };\nconst M = await import(' + json.dumps(data_url(src)) + ");\n" + script,
                 "placement.js")


def test_sysmenu_sources_and_system():
    """L-097 (Erik: "remove these dots for the sources and instead make that a tab in the hanburger button"; "I just would
    like to monitor it"): the Sources tab keeps sourceDot's status and title, worst first; the System tab's model reads
    /api/system (#384) with bars against budgets/caps (red when over), and hides storage/requests on DuckDB."""
    st = {"sources": [
        {"id": "a", "name": "Alpha", "enabled": True, "last": {"ok": True, "finished": "x"}},
        {"id": "b", "name": "Bravo", "enabled": True, "last": {"ok": False, "finished": "x"}},
        {"id": "c", "name": "Charlie", "enabled": True, "last": {"ok": True, "finished": "x"}},
        {"id": "d", "name": "Delta", "enabled": False},
        {"id": "e", "name": "Echo", "enabled": True}], "budget": {"c": "warn"}}
    sys_pg = {"now": "t", "machine": {"cpu_pct": 23.5, "cores": 16, "ram_used_gb": 20.1, "ram_total_gb": 34.0, "uptime_s": 90061},
              "processes": {"map": {"n": 1, "rss_mb": 300.0, "cpu_pct": 1.2}, "runner": None,
                            "postgres": {"n": 9, "rss_mb": 700.0, "cpu_pct": 0.8}},
              "runner": None,
              "storage": {"taken_at": "t", "system_bytes": 5e8, "regions": [
                  {"code": "ab", "bytes": 9e9, "budget_bytes": 1e10}, {"code": "bc", "bytes": 1e9, "budget_bytes": 1e10}]},
              "requests": {"hours": 24, "judged": True, "regions": [
                  {"code": "ab", "req_per_h": 12, "cap": 10, "mb_per_h": 1, "over": True, "top": [["s1", 8]]},
                  {"code": "bc", "req_per_h": 2, "cap": 10, "mb_per_h": 0.1, "over": False, "top": []}]}}
    sys_duck = {k: v for k, v in sys_pg.items() if k not in ("storage", "requests")}
    r = _node_sysmenu("const st = " + json.dumps(st) + ", pg = " + json.dumps(sys_pg) + ", duck = " + json.dumps(sys_duck) + """;
const rows = M.sourceRows(st, () => "5m"), v = M.systemModel(pg), d = M.systemModel(duck);
console.log(JSON.stringify({ rows: rows.map((x) => [x.id, x.cls, x.ago]), title: rows[0].title, sum: M.sourceSummary(rows),
  procs: v.procs.map((p) => [p.key, !!p.missing]), total: v.total, cpu: v.machine.cpu,
  storage: v.storage.regions.map((x) => x.level), req: v.requests.regions.map((x) => x.level), duck: [d.storage, d.requests],
  poll: M.SYSTEM_MS }));""")
    assert r["rows"] == [["b", "err", "5m"], ["c", "warn", "5m"], ["e", "", "never"], ["a", "ok", "5m"]]   # worst first; disabled gone
    assert r["title"].startswith("Bravo: error")                       # sourceDot's title, unchanged
    assert r["sum"] == "4 sources · 2 ok · 1 near budget · 1 failing"
    assert r["procs"] == [["map", False], ["runner", True], ["postgres", False]]   # a missing process says so
    assert r["total"]["rss_mb"] == 1000 and abs(r["total"]["cpu_pct"] - 2.0) < 1e-9
    assert 0.02 < r["total"]["ram_share"] < 0.04                        # 1000 MB of 34 GB
    assert r["cpu"]["level"] == "ok"
    assert r["storage"] == ["warn", "ok"] and r["req"] == ["over", "ok"]   # 90 % of budget; 12 / 10 req/h
    assert r["duck"] == [None, None] and r["poll"] == 10000              # DuckDB: no storage/requests; 10 s poll while open
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="menubtn"' in html and 'id="sysmenu"' in html and 'id="dots"' not in html   # the header lost its dot row
    # one ☰ only (orchestrator on #387): the phone's layers toggle wears a stacked-layers icon
    toggle = re.search(r'<button[^>]*id="hudtoggle"[^>]*>(.*?)</button>', html, re.S).group(1)
    assert "&#9776;" not in toggle and "☰" not in toggle and "<svg" in toggle
    src = (WEB / "sysmenu.js").read_text(encoding="utf-8")
    assert "/api/refresh/" in src and 'method: "POST"' in src          # ↻ queues a fetch, as the dots meant to
    # only the row's ↻ fetches, and a queued row ignores more taps (Reviewer / orchestrator on #387: limit requests)
    assert 'e.target.closest(".rf")' in src and '!row.classList.contains("queued")' in src and 'class="rf"' in src
    assert 'this.open && this.tab === "system" && !document.hidden' in src   # the System poll stops when closed or hidden



def test_time_bar_tick_step():
    """Reviewer on #387: on a phone's narrow time bar the day labels piled up ("NoNoNo…"). Labels are thinned by the
    bar's measured width (each label gets 1.5× its width), none when not even two fit; one rule on every screen."""
    r = _node("""console.log(JSON.stringify([P.tickStep(16, 1300, 36), P.tickStep(16, 400, 36), P.tickStep(16, 120, 36),
  P.tickStep(16, 90, 36), P.tickStep(0, 500, 36), P.tickStep(16, 0, 36)]));""")
    wide, mid, narrow, none, empty, unmeasured = r
    assert wide == 1                      # a desktop bar labels every day
    assert mid == 3 and narrow == 8       # thinner as the bar narrows: 400 px fits 7 labels, 120 px fits 2
    assert none == 0 and empty == 0 and unmeasured == 0   # too narrow for two labels, no days, or not laid out yet: none
    src = (WEB / "map.js").read_text(encoding="utf-8")
    assert "tickStep(k.days.length, el.clientWidth, labelPx)" in src and 'addEventListener("resize", () => renderTicks())' in src
    assert "i % 2 ?" not in src           # the fixed every-other-day labels are gone



def test_midwest_zooms():
    """L-101 (Erik: "Make the Midwest boxes visible in more zooms. When I'm mid zoom I don't see anything"): one tier-0
    "Midwest" box (Chicago drop-off, then Bakken supply and MISO Midwest load as sections) that splits at zoom 6 into its
    own boxes; Dawn, Farwell, Wisconsin and Michigan show from zoom 6; Ontario's section names its new headline (#379)."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    ov = cfg["overrides"]
    box = ov["gas.midwest.chicago"]
    # L-106 8a: the merged tier-0 "Midwest" box is retired (it split at zoom 6, where its parts now start as their own
    # tier-1 boxes); the Upper Midwest and Bakken region boxes are the overview
    assert box["tier"] == 1 and "title" not in box and "box_minzoom" not in box
    assert not [k for k, o in ov.items() if o.get("merge_into") == "gas.midwest.chicago"]
    assert all(ov[k]["tier"] == 1 for k in ("gas.midwest.bakken", "power.region.miso_midwest"))
    for k in ("gas.ont.dawn", "gas.glgt.farwell", "gas.midwest.wisconsin", "gas.midwest.michigan"):
        assert ov[k]["tier"] == 1, k
    assert "StatCan" not in ov["gas.ont.demand"]["section"] and "Dawn" in ov["gas.ont.demand"]["section"]


def test_river_frame_pacing():
    """L-102 (Erik: "see if you can find any gains"): river particles sit on whole device pixels, so a frame in which the
    fastest particle in view hasn't crossed one device pixel draws the same image. The wait is the power state's frame
    time, or the time to cross one device pixel when that is longer; never shorter (motion keeps real-time speed)."""
    r = _node("console.log(JSON.stringify([P.riverFrameMs(33, 3.8, 1.25), P.riverFrameMs(33, 14.5, 1.25), P.riverFrameMs(66, 3.8, 2.625), P.riverFrameMs(33, 0, 1.25), P.riverFrameMs(100, 40, 1)]));", "rivers.js")
    slow, fast, phone, none, saver = r
    assert round(slow) == 211          # zoom 7.5's fastest (3.8 px/s at 1.25): one frame per device pixel, ~5 fps
    assert fast == 1000 / (14.5 * 1.25) and fast > 33    # the Peace at the overview: 55 ms, already near the frame time
    assert phone == 1000 / (3.8 * 2.625)                 # a denser screen has smaller pixels, so it draws more often
    assert none == 33 and saver == 100                   # nothing moving: unchanged; a fast river never beats the power state
    src = (WEB / "rivers.js").read_text(encoding="utf-8")
    assert "riverFrameMs(this.frameMs(), vmax, pr)" in src and "requestAnimationFrame(" not in src


def test_region_boxes_are_one_rule():
    """L-106 8a: the region boxes come from one code path with no region id in the JS: map.json `systems` defaults (the
    zoom-6 hand-off), `sections` heads, the title opening the region panel, the share bar on `share: "<series>"` rows,
    and the outlines drawn faint under every network line with one name per boxless region."""
    js = {f: (WEB / f).read_text(encoding="utf-8") for f in ("map.js", "callouts.js", "style.js", "regionpage.js", "panel.js")}
    for f, s in js.items():
        assert not re.search(r'"region\.(wcan|cal|pnw|qc|sont|mbnwo|umw|bakken|mtnw|nyc|sunbelt|tx|upne)\b', s), f
    m = js["map.js"]
    assert "config.systems?.[" in m and "function withSections(" in m and "titleOpens: id" in m and 'p.system === "region"' in m
    assert "typeof x.share === \"string\"" in m and "bar: true" in m
    assert "so.bar" in js["callouts.js"] and "it?.c.titleOpens" in js["callouts.js"] and "!!r.title || !!r.lead" in js["callouts.js"]
    st = js["style.js"]
    order = [st.index(f'id: "{i}"') for i in ("areas-fill", "power-joints", "region-outline", "areas-line", "gas-casing", "power", "region-names", "nodes")]
    assert order == sorted(order)
    assert '["!", REGION_OUTLINE]' in st and '["!", ["has", "callout"]]' in st
    assert "regionPage.open(p.region)" in js["map.js"] and not (WEB / "regionpanel.js").exists()   # the page replaced the panel view


def test_late_rows_say_their_age():
    """L-106 (#428/#433): a row that's late by design but not stale (a region line waiting on BC Hydro's weekday load)
    must not look live: past 6 h for a sub-daily series, 2 days for a daily one (10 for weekly), its value is muted and
    says its age ("· 3 d"). Fresh, stale (its STALE chip says it), monthly (it names its month) and windowed rows are
    unchanged. One rule for every callout row; the region panel's fuel mix uses the same one."""
    r = _node_callouts("""
const now = Date.now() / 1000, row = { series: "x", label: "X", unit: "MW" };
const html = (e) => C.rowHtml(row, 0, { series: { x: { v: 100, series: "x", ...e } } }, "X");
console.log(JSON.stringify({
  hourLate: html({ ts: now - 3 * 86400, freq: "1h" }), hourFresh: html({ ts: now - 2 * 3600, freq: "1h" }),
  dayLate: html({ ts: now - 2.5 * 86400, freq: "D" }), dayOk: html({ ts: now - 1.5 * 86400, freq: "D" }),
  stale: html({ ts: now - 3 * 86400, freq: "1h", stale: true }), monthly: html({ ts: now - 60 * 86400, freq: "M" }),
  fiveMin: html({ ts: now - 7 * 3600, freq: "5min" }),
}));
""")
    assert 'class="age"' in r["hourLate"] and "· 3 d" in r["hourLate"] and " late" in r["hourLate"].split(">")[0]
    assert "· 7 h" in r["fiveMin"] and "· 2 d" not in r["dayOk"]
    assert "· 3 d" in r["dayLate"] or "· 2 d" in r["dayLate"]
    for k in ("hourFresh", "dayOk", "stale", "monthly"):
        assert 'class="age"' not in r[k], k


def test_plant_motion_rule_every_kind():
    """PLAN 10b / L-108 (Erik: "animations for all powerplant sprites when they're turned on"; Art #538): one rule,
    sprites.js plantMotion(kind, v, util, stale, estimated, off, tracked) -> {state, set, u, badge}, for every kind:
    measured, fresh and above zero moves at the wind rate (0.25 + 0.75 util: 1-4 frame changes/s); zero is still;
    missing or stale is frozen with the `?`; an estimated solar / wind / hydro moves at the estimate's rate; an estimated
    gas / coal / oil / biomass / geothermal / other glows steadily with no frames; battery and pumped storage take a
    direction only from a measured signed value; capability 0 is still with no badge. Every set it can pick is in the atlas."""
    thermal = ["plant-combined-cycle", "plant-simple-cycle", "plant-cogen", "plant-gas-fired-steam", "plant-coal-to-gas", "plant-gas", "plant-coal", "plant-oil"]
    frames = ["plant-wind", "plant-hydro", "plant-solar", "plant-nuclear", "plant-biomass", "plant-geothermal", "plant-unknown"]
    kinds = thermal + frames + ["plant-battery", "plant-pumped-storage"]
    r = _node("""const K = """ + json.dumps(kinds) + """;
const cases = { half: [500, 0.5], full: [1000, 1], zero: [0, 0], missing: [null, null], stale: [500, 0.5, true], est: [500, 0.5, false, true],
  estZero: [0, 0, false, true], off: [0, null, false, false, true], neg: [-300, 0.3], untracked: [null, null, false, false, false, false] };
const out = {};
for (const k of K) out[k] = Object.fromEntries(Object.entries(cases).map(([c, a]) => [c, P.plantMotion(k, a[0], a[1], a[2] ?? false, a[3] ?? false, a[4] ?? false, a[5] ?? true)]));
console.log(JSON.stringify({ out, comp: [P.plantMotion("compressor", -50, 0.5).u, P.plantMotion("compressor", 50, 0.5).set],
  lvl: P.plantMotion("plant-hydro", 571.3, null, false, false, false, false) }));""", "sprites.js")
    o = r["out"]
    rate = lambda u: round(0.25 + 0.75 * u, 6)
    for k in kinds:
        m = o[k]
        assert m["zero"]["u"] == 0 and m["zero"]["set"] is None, k                                   # zero is still
        assert m["stale"]["u"] == 0 and m["stale"]["badge"] is True, k                                # stale: frozen, `?`
        assert m["missing"]["u"] == 0 and m["missing"]["badge"] is True, k                            # missing: frozen, `?`
        assert m["off"] == {"state": "glow0" if k in thermal else "static", "set": None, "u": 0, "badge": False}, k   # capability 0
        assert m["estZero"]["u"] == 0, k
    for k in thermal:                       # glow (+ smoke when measured, smokeSpec), never frames; an estimate glows steadily
        assert o[k]["half"]["set"] is None and o[k]["half"]["state"] == "glow2" and o[k]["full"]["state"] == "glow3", k
        assert o[k]["est"]["set"] is None and o[k]["est"]["state"] == "glow2", k
    for k in frames:                        # measured on: frames at the wind rate
        assert o[k]["half"]["set"] == "frame" and round(o[k]["half"]["u"], 6) == rate(0.5) and o[k]["full"]["u"] == 1, k
        assert o[k]["half"]["badge"] is False and o[k]["half"]["state"] == "static", k
    for k in ["plant-wind", "plant-solar", "plant-hydro"]:          # estimated weather / water kinds move at the estimate
        assert o[k]["est"]["set"] == "frame" and round(o[k]["est"]["u"], 6) == rate(0.5), k
    for k in ["plant-biomass", "plant-geothermal", "plant-unknown"]:  # estimated dispatched kinds: a steady glow, no frames
        assert o[k]["est"] == {"state": "glow2", "set": None, "u": 0, "badge": False}, k
    assert o["plant-nuclear"]["est"]["u"] == 0                       # nuclear moves only on its (NRC) measured value
    b, ps = o["plant-battery"], o["plant-pumped-storage"]
    assert (b["half"]["set"], b["neg"]["set"], ps["half"]["set"], ps["neg"]["set"]) == ("discharge", "charge", "generate", "pump")
    assert b["neg"]["u"] > 0 and ps["neg"]["u"] > 0                   # the set runs the right way: never a negative rate
    assert b["est"]["u"] == 0 and b["est"]["state"] == "static" and ps["est"]["u"] == 0   # no direction from an estimate
    for k in frames[1:] + ["plant-battery", "plant-pumped-storage"]:  # a plant with no value source (OSM) is still, no `?`
        assert o[k]["untracked"]["badge"] is False and o[k]["untracked"]["u"] == 0, k
    assert r["comp"] == [-0.5, "frame"]                                  # compressors unchanged (signed ticks)
    # every set the rule can pick exists in Art's atlas at both sizes, frames 0..3
    atlas = json.loads((WEB / "sprites" / "nodes.json").read_text(encoding="utf-8"))
    for k in kinds:
        for c in o[k].values():
            if c["set"]:
                for n in (16, 32):
                    for f in range(4):
                        assert f"{k}-{n}-{c['set']}{f}" in atlas, (k, c["set"], n, f)
            assert f"{k}-16-{c['state']}" in atlas, (k, c["state"])
    st = (WEB / "style.js").read_text(encoding="utf-8")
    assert '["coalesce", ["get", "spr_set"], "frame"], `${k}`]' in st and 'filter: animFilter("spr_u", 0)' in st and 'filter: animFilter("smk_u", 0)' in st
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert "const m = plantMotion(kind, v, util, stale, est, off, tracked);" in js
    # Reviewer on #540: a reservoir level or published capacity isn't output, so those plants are untracked (still, no
    # `?` with a fresh reading), as on main: Mica, Revelstoke and Arrow Lakes (hydro_columbia.geojson)
    assert 'const tracked = !!(p.value || p.util_est) && !["level", "capacity"].includes(p.value_kind);' in js
    lvl = [f["properties"]["id"] for fn in (paths.REPO_ROOT / "network" / "power").glob("*.geojson")
           for f in json.loads(fn.read_text(encoding="utf-8"))["features"] if f["properties"].get("value_kind") in ("level", "capacity")]
    assert {"power.plant.hydro_mica", "power.plant.hydro_revelstoke"} <= set(lvl), lvl
    assert r["lvl"] == {"state": "static", "set": None, "u": 0, "badge": False}
    assert "spr_u: m.u, spr_set: m.set" in js
    # frame budget: the clock and the frame layers count only movers drawn at this level (zmin), one shared 4 Hz clock
    assert "const shown = (S.animPts || []).filter((p) => p.z <= S.ze);" in js and "map.setFilter(id, animFilter(\"spr_u\", ze))" in js
    assert js.count("setInterval(") == js.count("ANIM_TICK_MS);") or "}, ANIM_TICK_MS);" in js


def test_inventory_plants_read_util_est():
    """L-106 (Power #422): an inventory plant with no live MW reads its BA + fuel's shared est. utilisation (`util_est`,
    in the snapshot's `series`, montney/scope.py shared_series): util = the fraction (clamped 0..1), v = util ×
    nameplate. It's `quality: "estimate"`, so the glow is steady and labelled "est.", with no smoke and a static battery
    (sprites.js isEstimate). A wind farm turns with its BA's wind, labelled est. (REGIONS.md §8)."""
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert "f.properties.util_est ? S.snap?.series?.[f.properties.util_est] : null" in js
    assert "util = Math.max(0, Math.min(ue.v, 1)); v = util * (f.properties.nameplate_mw ?? 1)" in js
    assert 'spr_est: est && (GLOW_KINDS.has(kind) || EST_GLOW_KINDS.has(kind) || EST_MOVE_KINDS.has(kind)) && v != null' in js   # L-108
    r = _node("""console.log(JSON.stringify([P.isEstimate({ quality: "estimate" }), P.spriteState("plant-coal", 600, 0.5, false, true),
      P.smokeSpec("plant-coal", 600, 0.5, false, true), P.spriteState("plant-battery", -20, 0.2, false, true),
      P.animSpeed("plant-wind", 50, 0.4, false) > 0]));""", "sprites.js")
    assert r == [True, "glow2", None, "static", True]


def test_util_plant_panel_names_its_hour():
    """Reviewer on #447: EIA-930's utilisation is its latest published hour (11-38 h old), so an inventory plant's panel
    says it's an estimate, from which BA and fuel, and for which hour; never "live", and its headline has no "[band n/a]"
    (estimate `range: false`). The history is the utilisation series."""
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert "const ue = e?.v == null && p.util_est ? S.snap?.series?.[p.util_est] : null;" in js
    assert "for the hour of ${fmtTime(ue.ts)}" in js and 'status = "estimate from EIA-930' in js and "series.unshift(p.util_est)" in js
    assert "spec.status ? esc(spec.status)" in (WEB / "panel.js").read_text(encoding="utf-8")
    r = _node('globalThis.location = { search: "" };\nconst PA = await import(' + json.dumps(_panel_module()) + ");\n"
              'console.log(JSON.stringify([PA.headlineHtml(372, undefined, "MW", { estimate: { range: false, basis: "x" } }),'
              ' PA.headlineHtml(372, undefined, "MW", { estimate: { lo: null, hi: null } })]));')
    assert "band n/a" not in r[0] and ">est<" in r[0] and "372" in r[0]
    assert "[band n/a]" in r[1]          # an estimate that should have a range and doesn't still says so


def test_gas_facilities_layer():
    """L-106 8b (Gas #410, montney/wells.py): every AB + BC reporting gas facility from GET /api/wells is drawn with the
    network's nodes, lazily: fetched the first time the map reaches tier 2, then revalidated by ETag (monthly data).
    Each feature's own `tier` (by rank) sets its zoom; its latest month rides on the feature (the snapshot's zoom scope
    never lists them); it wears the gas-plant sprite; a click opens its panel straight away (no box among ~700 dots);
    the layers list has a "Gas facilities" toggle. Personal-use data: nothing about the facilities is committed."""
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'api("/api/wells", { etag: S.wellsTag, key: "wells" })' in js
    assert "if (!S.wells && tierAt(S.ze) >= WELLS_TIER) loadWells();" in js and "WELLS_TIER = 2" in js and "poll(loadWells, WELLS_MS)" in js
    assert "if (!S.wells && tierAt(S.ze) < WELLS_TIER) return;" in js        # never fetched before zooming in
    assert "return o.tier ?? p?.tier ?? co?.priority ?? 2;" in js
    assert "f.properties.v !== undefined ? { v: f.properties.v, ts: f.properties.ts, stale: !!f.properties.stale }" in js
    assert 'p.type === "facility" ||' in js and "S.layers.wells !== false" in js
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    assert {"id": "wells", "label": "Gas facilities", "on": True} in cfg["layers"]
    k = _node('console.log(JSON.stringify(P.spriteKind({ type: "facility" })));', "sprites.js")
    manifest = json.loads((WEB / "sprites" / "nodes-manifest.json").read_text(encoding="utf-8"))
    assert k == "gas-processing" and k in manifest
    assert not list((paths.REPO_ROOT / "network").glob("**/wells*.geojson"))   # never committed


def test_region_section_without_a_lead_line_folds_whole():
    """Live 8a check (kraken, AB · BC · SK): when a section's lead lines have no data yet (generation waits on every
    part), its fuel rows must not stand in as the headline ("Battery (AB) 0.00 MW"). That section folds whole, head
    included; a box with no lead line anywhere leads with its first row. The region panel leaves a line with no points
    in the range out of its chart and legend."""
    r = _node_callouts("""
const rows = [{ head: "AB · BC · SK", title: true }, { head: "Gas · MMcf/d", section: true, lead: true }, { series: "a", depth: 0 },
  { series: "b", depth: 0, fold: true }, { head: "Power · MW", section: true, lead: false }, { series: "c", depth: 1, fold: true },
  { series: "d", depth: 1, fold: true }];
console.log(JSON.stringify(C.leadRows(rows)));
""")
    assert r == [True, True, True, False, False, False, False]
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert "lead: leadSecs.has(sec)" in js and "out.push(isLead(r) || r === fallback ? (head ? { ...r, compact: true } : r) : { ...r, fold: true });" in js
    assert "(next && !r.fold)" in (WEB / "callouts.js").read_text(encoding="utf-8")


def test_region_colours_from_the_data():
    """L-106 (Erik: "regional borders around the regions ... give each region a colour"): every region outline is drawn
    as a solid border in its own `color` (montney/regions.py, #452) and filled with a faint tint of it, under every data
    line; the tint drops while a weather field is drawn (it would shift the field's colours). The region box's left edge
    and title take the same colour. Colours come from the features only: no region hue in the JS or CSS."""
    r = _network_layers()
    ids = [l["id"] for l in r["layers"]]
    lay = {l["id"]: l for l in r["layers"]}
    fill, line = lay["region-fill"], lay["region-outline"]
    assert fill["type"] == "fill" and line["type"] == "line" and "line-dasharray" not in line["paint"]
    assert fill["paint"]["fill-color"][1] == ["get", "color"] and line["paint"]["line-color"][1] == ["get", "color"]
    assert 0.08 <= fill["paint"]["fill-opacity"] <= 0.12
    widths = line["paint"]["line-width"][3:]                       # [zoom, width, ...]: ~1.5-2 px, never wider than a gas line
    assert all(1.4 <= w <= 2.0 for w in widths[1::2])
    assert ids.index("region-fill") < ids.index("areas-fill") and ids.index("region-outline") < ids.index("gas-casing")
    assert ids.index("region-outline-casing") == ids.index("region-outline") - 1   # its dark casing, right under it
    outline = {"kind": "area", "type": "region_outline", "color": "#f2c94c"}
    assert _ev(fill["filter"], outline) and not _ev(fill["filter"], {"kind": "area", "type": "area"})
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'map.setPaintProperty("region-fill", "fill-opacity", fieldOn ? 0 : REGION_TINT)' in js
    assert "titleOpens: id, color: p.color" in js
    co = (WEB / "callouts.js").read_text(encoding="utf-8")
    assert 'el.style.setProperty("--region", c.color)' in co and "/^#[0-9a-f]{6}$/i.test(c.color" in co
    assert ".callout[data-region] { box-shadow: inset 0 0 0 2px var(--region); }" in (WEB / "map.css").read_text(encoding="utf-8")   # PLAN 9 rule 4
    hues = {ft["properties"]["color"].lower() for ft in json.loads((paths.REPO_ROOT / "network" / "region" / "outlines.geojson")
            .read_text(encoding="utf-8"))["features"]}
    for f in ("map.js", "style.js", "callouts.js", "map.css"):
        src = (WEB / f).read_text(encoding="utf-8").lower()
        assert not [h for h in hues if h in src], f                  # no region hue hard-coded


def test_zoom_range_shows_every_region_at_the_floor():
    """PLAN 10a (Erik, 2026-11-14: "increase the zoom" and "zoom over all of America and northern Mexico ... right now I
    cannot look at Texas"): on 1920x1080 the floor is view.minZoom (the generous bounds don't stop it first), and there
    all 13 regions' territory fits the screen clear of the side panels (layers and legend on the left, each under 320 px;
    header and time bar under 140 px together). The deep end goes to 13 (the DEM's maxzoom 10 overzooms; nothing breaks)."""
    cfg = json.loads((WEB / "map.json").read_text(encoding="utf-8"))
    v = cfg["view"]
    r = _node("""const map = (w, h, min) => ({ getMinZoom: () => min, getContainer: () => ({ clientWidth: w, clientHeight: h }) });
console.log(JSON.stringify([P.floorZoom(map(1920, 1080, """ + str(v["minZoom"]) + """), """ + json.dumps(v["maxBounds"]) + """)]));""", "placement.js")
    assert r[0] == v["minZoom"] and v["maxZoom"] == 13
    outl = json.loads((paths.REPO_ROOT / "network" / "region" / "outlines.geojson").read_text(encoding="utf-8"))

    def pts(c):
        return [c] if isinstance(c[0], (int, float)) else [p for x in c for p in pts(x)]
    allp = [p for f in outl["features"] for p in pts(f["geometry"]["coordinates"])]
    assert len(outl["features"]) == 13
    w, e = min(p[0] for p in allp), max(p[0] for p in allp)
    s, n = min(p[1] for p in allp), max(p[1] for p in allp)
    y = lambda lat: 0.5 - math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / (2 * math.pi)
    scale = 512 * 2 ** v["minZoom"]
    assert (e - w) / 360 * scale <= 1920 - 2 * 320 and (y(s) - y(n)) * scale <= 1080 - 140
    (bw, bs), (be, bn) = v["maxBounds"]
    assert bw < w and be > e and bs < s and bn > n                         # every region inside the bounds, with room


REGION_LAYOUT_JS = r"""
const box = (w, h) => ({ w, h }), sizes = (fw, fh) => ({ full: box(fw, fh), compact: box(150, 60), chip: box(80, 20) });
const R = (id, x, y, ew, eh, dir) => ({ id, p: { x, y }, ext: { w: ew, h: eh }, size: sizes(170, 200), dir });
const big = [R("a", 300, 300, 400, 400), R("b", 900, 300, 400, 400)];          // far apart, roomy: both full, centred
const tight = [R("big", 300, 300, 400, 400), R("small", 400, 300, 150, 120)];   // the smaller region steps down
const tiny = [R("big", 300, 300, 400, 400), R("tiny", 300, 520, 40, 30, "S")]; // too small for a chip: outside, south
// PLAN 10a rule 4: a big region's full box covers a small region's anchor, so no leader can start there: the big box steps down
const hid = [R("big", 300, 300, 300, 330), R("ne", 340, 380, 30, 30, "E"), R("wall", 300, 470, 600, 40)];
const all = (L) => Object.fromEntries(Object.entries(L).map(([k, v]) => [k, [v.form, v.out, v.leader, Math.round(v.x ?? -1), Math.round(v.y ?? -1)]]));
const shift = (rs, dx, dy) => rs.map((r) => ({ ...r, p: { x: r.p.x + dx, y: r.p.y + dy } }));
// hysteresis: a box that was compact needs room to spare before it grows back to full
const edge = [R("e", 300, 300, 175, 205)];
console.log(JSON.stringify({
  big: all(R_.layoutRegions(big)), tight: all(R_.layoutRegions(tight)), tiny: all(R_.layoutRegions(tiny)), hid: all(R_.layoutRegions(hid)),
  hidPan: JSON.stringify(all(R_.layoutRegions(shift(hid, -500, 250))).big.slice(0, 3)) === JSON.stringify(all(R_.layoutRegions(hid)).big.slice(0, 3)),
  pan: JSON.stringify(Object.values(R_.layoutRegions(shift(tight, 777, -333))).map((v) => v.form)) === JSON.stringify(Object.values(R_.layoutRegions(tight)).map((v) => v.form)),
  order: JSON.stringify(R_.layoutRegions([...tight].reverse())) === JSON.stringify(R_.layoutRegions(tight)),
  growFresh: R_.layoutRegions(edge).e.form, growFromCompact: R_.layoutRegions(edge, { e: "compact" }).e.form,
  keepFull: R_.layoutRegions(edge, { e: "full" }).e.form,
  seg: [R_.segHitsRect(0, 0, 10, 10, { x: 4, y: 4, w: 2, h: 2 }), R_.segHitsRect(0, 0, 10, 0, { x: 4, y: 4, w: 2, h: 2 })],
  dirs: R_.dirsFrom("S").slice(0, 3),
}));
"""


def test_region_box_layout_rules():
    """PLAN 9 part 1 (Erik: "at any zoom level it should keep the boxes over their designated geographic region"):
    web/regionlayout.js puts a region's box centred on its anchor in the largest form that fits the region (full, compact,
    chip), never overlapping another; when two would overlap, the smaller region's box steps down instead of drifting; a
    region too small for a chip gets its box outside, in its `box.dir`, with a leader. It's a pure function of relative
    positions, so a pan or the input order never changes it, and growing back a form needs room to spare."""
    src = (WEB / "regionlayout.js").read_text(encoding="utf-8")
    assert not re.search(r"^import ", src, re.M)
    r = _node("const R_ = P;\n" + REGION_LAYOUT_JS, "regionlayout.js")
    assert r["big"]["a"][:3] == ["full", False, False] and r["big"]["a"][3:] == [215, 200]   # centred: x = 300 - 170/2
    assert r["big"]["b"][0] == "full"
    assert r["tight"]["big"][0] == "full" and (r["tight"]["small"][0] != "full" or r["tight"]["small"][1])   # the smaller one gave way
    t = r["tiny"]["tiny"]
    assert t[1] is True and t[2] is True and t[4] > 520            # outside, south of its anchor, with a leader
    # PLAN 10a rule 4: Québec's full box covered New England's anchor at the new floor; the covering box steps down to
    # compact and the small region's box goes outside with its leader (still pan-invariant)
    assert r["hid"]["big"][:2] == ["compact", False] and r["hid"]["ne"][:3] == ["compact", True, True] and r["hidPan"]
    assert r["pan"] and r["order"]
    assert r["growFresh"] == "full" and r["growFromCompact"] == "compact" and r["keepFull"] == "full"
    assert r["seg"] == [True, False] and r["dirs"] == [[0, 1], [-1, 1], [1, 1]]
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'p.system === "region" ? { region: { ext: regionExtent(p.region), dir: p.box?.dir } }' in js
    co = (WEB / "callouts.js").read_text(encoding="utf-8")
    assert "layoutRegions(regionIt.map(" in co and "this.measureForms(it, compact)" in co
    css = (WEB / "map.css").read_text(encoding="utf-8")
    assert '.callout[data-form="chip"] .row:not(.title)' in css and '.callout[data-form="compact"] .row:not(.title):not([data-compact="1"])' in css
    for f in ("regionlayout.js", "callouts.js", "map.js"):          # no region special-cased in JS
        assert not re.search(r'"(?:region\.)?(wcan|cal|pnw|qc|sont|mbnwo|umw|bakken|mtnw|nyc|sunbelt|tx|upne)"', (WEB / f).read_text(encoding="utf-8")), f


def test_region_box_v2_rows():
    """PLAN 9 part 2: every region box has the same six rows (montney/regions.py `box_rows`): Gas · MMcf/d production,
    demand, imports, exports, storage implied (display sign: sources +, sinks −) and Power · MW generation. A row without
    a value shows "—", dimmed, with its reason on hover (never a blank or a zero); a known zero shows 0; tags stay quiet."""
    r = _node_callouts("""
const row = (o) => ({ series: "s", label: "Demand", unit: "MMcf/d", depth: 0, lead: true, ...o });
const snap = { series: { s: { v: 8704, series: "s", ts: Date.now() / 1000 - 3600, freq: "D" } } };
console.log(JSON.stringify({
  live: C.rowHtml(row({ flow: "demand", tags: ["est."] }), 1, snap, "Demand"),
  none: C.rowHtml(row({ series: "x", have: false, why: "no import meters yet" }), 2, snap, "Imports"),
  zero: C.rowHtml(row({ series: "x", zero: true }), 3, snap, "Exports"),
  plain: C.rowHtml(row({ series: "x" }), 4, snap, "Other"),
  v2empty: C.rowHtml(row({ series: "x", v2: true }), 5, snap, "Production"),
  v2stale: C.rowHtml(row({ series: "st", v2: true }), 6, { series: { st: { v: null, series: "st", stale: true, ts: 1 } } }, "Production"),
  zeroStale: C.rowHtml(row({ series: "zs", v2: true, zero: true }), 7, { series: { zs: { v: 0, series: "zs", stale: true, ts: 1 } } }, "Production"),
}));
""")
    assert "-8,704" in r["live"] and 'class="qt">est.<' in r["live"] and " nov" not in r["live"].split(">")[0]
    assert ">—<" in r["none"] and " nov" in r["none"].split(">")[0] and 'title="no import meters yet"' in r["none"]
    assert '<span class="v">0</span>' in r["zero"] and " nov" not in r["zero"].split(">")[0]
    assert '<span class="v">--</span>' in r["plain"]          # an ordinary row's missing value is unchanged
    assert ">—<" in r["v2empty"] and 'title="no value yet"' in r["v2empty"]   # a v2 row is never "--" or blank
    assert "STALE" not in r["v2stale"] and ">—<" in r["v2stale"]     # PLAN 9g: no value, so no STALE chip beside the dash
    assert "STALE" not in r["zeroStale"] and '<span class="v">0</span>' in r["zeroStale"]   # a known zero is a fact, not a stale reading
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'p.system === "region" && Array.isArray(p.box_rows) && p.box_rows.length' in js and "function boxRowsV2(" in js
    assert 'flow: b.sign < 0 ? "demand" : "supply"' in js and "...(first ? { compact: true } : {})" in js
    assert "for (const b of f.properties.box_rows || []) if (b.series) ids.add(b.series);" in js


def _node_regionpage(script: str):
    """Run `script` with `G` bound to web/regionpage.js, its lib.js import inlined (location stubbed)."""
    du = lambda s: "data:text/javascript;base64," + base64.b64encode(s.encode()).decode()
    lib = du((WEB / "lib.js").read_text(encoding="utf-8"))
    src = (WEB / "regionpage.js").read_text(encoding="utf-8").replace('from "./lib.js"', f"from {json.dumps(lib)}")
    assert not re.search(r'^import [^\n]*from "\./', src, re.M), "regionpage.js gained an import the test doesn't inline"
    return _node('globalThis.location = { search: "" };\nconst G = await import(' + json.dumps(du(src)) + ");\n" + script, "placement.js")


def test_region_page_rules():
    """PLAN 9 part 3: the region page (web/regionpage.js, GET /api/region/<id>). Tabs with nothing are disabled; a value
    without one is "—" (dimmed, its reason on hover), a known zero 0; balance lines take the box's display sign (sources
    +, sinks −); a group's bars are each row's part of the line it names (`of`), else of the rows' sum and `rest`; static
    facts get no bar; the weather table matches its degree-day series by name."""
    r = _node_regionpage("""
const d = { box: [{ line: "gas.production", sign: 1 }, { line: "gas.demand", sign: -1 }],
  tabs: { gas: { lines: [{ line: "production", v: 100 }, { line: "demand", v: 80 }], groups: [] }, power: { lines: [], groups: [] }, oil: {} } };
const g = { of: "demand", rest: 10, rows: [{ label: "a", series: "s.a", v: 40 }, { label: "b", series: "s.b", v: 20 },
  { label: "cap", value: 550, unit: "Bcf", as_of: "2026-06", source: "EIA-191" }] };
const w = { hdd: "wx.r.hdd", cdd: "wx.r.cdd", normals: ["wx.r.hdd_normal", "wx.r.cdd_normal"], realized: ["wx.r.hdd.realized"],
  outlook: ["wx.r.hdd_1_7", "wx.r.hdd_8_15", "wx.r.cdd_1_7"] };
const lines = G.signed(d.tabs.gas.lines, d.box, "gas");
const pw = G.signed([{ line: "generation", v: 1 }, { line: "load", v: 1 }, { line: "interchange", v: 1 }], d.box, "power");
const tsg = { of: "demand", rows: [{ label: "same", series: "s.1", v: 40, ts: 100 }, { label: "older", series: "s.2", v: 20, ts: 50 }] };
const tsl = [{ line: "demand", v: 80, ts: 100 }];
console.log(JSON.stringify({
  tabs: G.tabsWith(d), signs: lines.map((l) => l.sign),
  vt: [G.valueText({ v: 1234, sign: -1 }), G.valueText({ v: null, have: false, why: "no source yet" }), G.valueText({ zero: true }), G.valueText(null)],
  bars: G.groupBars(g, lines).map((x) => [x.fact, x.share == null ? null : Math.round(x.share * 100)]),
  barsNoOf: G.groupBars({ rows: g.rows.slice(0, 2), rest: 40 }).map((x) => [x.share, Math.round(x.bar * 100), x.rel]),
  dd: G.ddRows(w), city: G.cityName("wx.fort_st_john.tmean"), pw: pw.map((l) => l.sign),
  tsShare: G.groupBars(tsg, tsl).map((x) => x.share == null ? null : Math.round(x.share * 100)),
}));
""")
    assert r["tabs"] == ["gas"] and r["signs"] == [1, -1]
    assert r["vt"][0] == {"text": "-1,234", "dim": False, "title": ""}
    assert r["vt"][1] == {"text": "—", "dim": True, "title": "no source yet"} and r["vt"][2]["text"] == "0" and r["vt"][3]["text"] == "—"
    assert r["bars"] == [[False, 50], [False, 25], [True, None]]       # of demand (80): 40 -> 50 %, 20 -> 25 %
    assert r["barsNoOf"] == [[None, 100, True], [None, 50, True]]       # no `of`: no share; bars against the largest row
    assert r["dd"][0] == {"label": "HDD", "today": "wx.r.hdd", "normal": "wx.r.hdd_normal", "realized": "wx.r.hdd.realized",
                          "d7": "wx.r.hdd_1_7", "d15": "wx.r.hdd_8_15"} and r["dd"][1]["label"] == "CDD"
    assert r["city"] == "Fort St John"
    assert r["pw"] == [1, -1, 1]                 # power load is a sink, like gas demand (Reviewer on #464)
    assert r["tsShare"] == [None, None]          # a row from another period: no shares in that group at all (PLAN 9g)
    js = (WEB / "map.js").read_text(encoding="utf-8")
    assert 'if (p.system === "region") { panel.close(); regionPage.open(p.region); return; }' in js
    src = (WEB / "regionpage.js").read_text(encoding="utf-8")
    assert "api(`/api/region/${encodeURIComponent(id)}`)" in src and not re.search(r'"(wcan|cal|pnw|qc|sont|mbnwo|umw|bakken|mtnw|nyc|sunbelt|tx|upne)"', src)
    css = (WEB / "map.css").read_text(encoding="utf-8")
    assert "repeat(auto-fill, minmax(300px, 1fr))" in css and ".rpage { width: 100%; height: 100%; border-radius: 0; }" in css


def test_region_group_shares_only_of_a_line():
    """Reviewer on #467/#468: §5b groups are free tables, not partitions. Gas's real groups overlap (AB / BC beside the
    NGTL-area and Montney cuts), nest ("Alberta on NGTL" plus its sectors) and mix units (Bcf inventory beside MMcf/d
    flows). A share is only a row's part of the group's `of` line, same period and unit; with no `of` there is no share
    and no "%" (the bar is against the largest same-unit row, drawn lighter), and "Other / not itemised" needs `of`."""
    r = _node_regionpage("""
const rows = [{ label: "Alberta", series: "a", v: 12000, ts: 100, unit: "MMcf/d" }, { label: "BC", series: "b", v: 7800, ts: 100, unit: "MMcf/d" },
  { label: "NGTL area", series: "n", v: 11000, ts: 100, unit: "MMcf/d" }, { label: "Montney", series: "m", v: 9800, ts: 100, unit: "MMcf/d" },
  { label: "Alberta on NGTL", series: "t", v: 6200, ts: 90, unit: "MMcf/d" }, { label: "Storage inventory", series: "i", v: 400, ts: 100, unit: "Bcf" }];
const lines = [{ line: "production", v: 19800, ts: 100, unit: "MMcf/d" }];
const same = rows.filter((x) => x.label !== "Alberta on NGTL");   // every MMcf/d row on the line's day
const free = G.groupBars({ unit: "MMcf/d", rest: 500, rows }, lines), part = G.groupBars({ of: "production", rest: 500, rows: same }, lines);
const offDay = G.groupBars({ of: "production", rest: 500, rows }, lines);
const html = (g) => G.RegionPage.prototype.groupHtml.call({}, g, lines);   // no constructor: it listens on window
console.log(JSON.stringify({
  free: free.map((x) => [x.share, Math.round(x.bar * 100), x.rel]),
  part: part.map((x) => [x.share == null ? null : Math.round(x.share * 100), x.rel]),
  freeHtml: html({ unit: "MMcf/d", rest: 500, rows }), partHtml: html({ of: "production", rest: 500, rows: same }),
  offDay: offDay.map((x) => [x.share, x.rel]),
}));
""")
    # no `of`: never a share (Alberta isn't 31 % of a double-counted sum); bars against the largest MMcf/d row; Bcf none
    assert [s for s, _, _ in r["free"]] == [None] * 6
    assert [b for _, b, _ in r["free"]] == [100, 65, 92, 82, 52, 0] and all(rel for _, b, rel in r["free"] if b)
    assert "%</span>" not in r["freeHtml"] and "Other / not itemised" not in r["freeHtml"] and 'class="rel"' in r["freeHtml"]
    # `of` production (19,800): Alberta 61 %; a row in another unit gets no share
    assert r["part"] == [[61, False], [39, False], [56, False], [49, False], [None, False]]
    # one row for another day than the line (PLAN 9g): the whole group is a free table, never one share among bare ticks
    assert [s for s, _ in r["offDay"]] == [None] * 6 and r["offDay"][0][1] is True
    assert "Other / not itemised" in r["partHtml"] and "61 %" in r["partHtml"]


def test_region_page_names_a_monthly_period():
    """Reviewer before Gas #469: a monthly (or quarterly / annual) row on the region page names its period ("Aug 2026"),
    from the entry's ts, as the first tag. lateAge leaves monthly entries out by design, so without it a three-month-old
    storage injection read as November's (#434/#435's wrong-sign trap). Daily and hourly rows are unchanged."""
    aug = 1785542400        # 2026-08-01T00:00:00Z
    r = _node_regionpage(f"""
const row = (o) => ({{ label: "Storage net withdrawal", series: "s", unit: "MMcf/d", v: -120, ts: {aug}, ...o }});
const html = (g) => G.RegionPage.prototype.groupHtml.call({{}}, g, []);
console.log(JSON.stringify({{
  labels: [G.periodLabel({{ ts: {aug}, freq: "M" }}), G.periodLabel({{ ts: {aug}, freq: "Q" }}), G.periodLabel({{ ts: {aug}, freq: "A" }}),
           G.periodLabel({{ ts: {aug}, freq: "D" }}), G.periodLabel({{ freq: "M" }})],
  monthly: html({{ rows: [row({{ freq: "M", tags: ["est."] }})] }}), daily: html({{ rows: [row({{ freq: "D" }})] }}),
}}));
""")
    assert r["labels"] == ["Aug 2026", "Q3 2026", "2026", None, None]
    assert 'class="rtags">Aug 2026 · est.' in r["monthly"]          # the month first, before the other tags
    assert "Aug 2026" not in r["daily"]


GROUP_SIGNS_JS = """
const lines = [{ line: "interchange", v: 1000, ts: 1, unit: "MW" }, { line: "generation", v: 20000, ts: 1, unit: "MW" }];
const html = (g) => G.RegionPage.prototype.groupHtml.call({}, g, lines);
const ties = { of: "interchange", unit: "MW", rest: -2500, rows: [{ label: "HQ", series: "a", v: 1800, ts: 1, unit: "MW" },
  { label: "PJM", series: "b", v: 1700, ts: 1, unit: "MW" }, { label: "ISO-NE", series: "c", v: -2000, ts: 1, unit: "MW" }] };
const fuels = (rest) => ({ of: "generation", unit: "MW", rest, rows: [{ label: "Gas", series: "g", v: 12000, ts: 1, unit: "MW" },
  { label: "Nuclear", series: "n", v: 7900, ts: 1, unit: "MW" }] });
const free = { unit: "MW", rows: [{ label: "HQ", series: "a", v: 1800, unit: "MW" }, { label: "ISO-NE", series: "c", v: -900, unit: "MW" }] };
console.log(JSON.stringify({
  ties: G.groupBars(ties, lines).map((x) => [x.share, Math.round(x.bar * 100), x.rel]), tiesHtml: html(ties),
  restTiny: G.showRest(fuels(80), lines), restReal: G.showRest(fuels(400), lines), restHtml: html(fuels(80)),
  free: G.groupBars(free, lines).map((x) => [x.split, x.neg, Math.round(x.bar * 100)]), freeHtml: html(free),
  plain: G.groupBars(fuels(80), lines).map((x) => x.split),
}));
"""


def test_region_group_mixed_signs_are_not_shares():
    """Reviewer on #479/#480 (NYISO / ISO-NE interchange by tie): an `of` group whose rows have mixed signs can't be
    parts of the line (shares past 100 %, a big negative "Other"), so it's drawn as a free table: no %, no "Other",
    relative bars."""
    r = _node_regionpage(GROUP_SIGNS_JS)
    assert [s for s, _, _ in r["ties"]] == [None, None, None] and all(rel for _, _, rel in r["ties"])
    assert "%</span>" not in r["tiesHtml"] and "Other / not itemised" not in r["tiesHtml"]


def test_region_group_rest_under_rounding_is_hidden():
    """Reviewer on #479/#480: "Other / not itemised" under 0.5 % of the line's absolute value is rounding, not a remainder
    (80 MW of 20,000 is 0.4 %: hidden; 400 MW is 2 %: shown)."""
    r = _node_regionpage(GROUP_SIGNS_JS)
    assert r["restTiny"] is False and r["restReal"] is True and "Other / not itemised" not in r["restHtml"]


def test_region_free_table_with_both_signs_draws_from_zero():
    """A free table whose rows have both signs (imports and exports by tie) draws its bars from a zero line in the middle:
    negatives to the left, positives to the right; single-sign groups keep their plain bars."""
    r = _node_regionpage(GROUP_SIGNS_JS)
    assert r["free"] == [[True, False, 100], [True, True, 50]] and r["plain"] == [False, False]
    assert 'class="bar split"' in r["freeHtml"] and 'class="rel neg"' in r["freeHtml"] and 'class="rel pos"' in r["freeHtml"]
    assert 'width:25.0%' in r["freeHtml"]                       # half a cell for the larger side: 900 / 1800 × 50 %
    css = (WEB / "map.css").read_text(encoding="utf-8")
    assert ".rpage .rgroup td.bar.split i.neg { right: 50%; }" in css


def test_region_page_9g_display_rules():
    """PLAN 9g (visual pass on all 13 regions with live data): weekly rows name their week; a tab with no group and no line
    that has a value says it's empty (sont / qc oil) rather than showing a dash and a promise; the gas balance says it's
    built from finished gas days (`through: "yesterday"`) and a late-publishing tab says how late (`lag_s`); a group's note
    shows under its title; "of which" rows are indented; a static fact keeps its value on the row with its source under the
    label (so a long source can't push the card wider than its column)."""
    nov6 = 1793923200       # 2026-11-06T00:00:00Z
    r = _node_regionpage(f"""
const html = (g) => G.RegionPage.prototype.groupHtml.call({{}}, g, []);
const g = {{ title: "Demand by sector", note: "Alberta's split is NGTL's intraprovincial deliveries cut by sector.", unit: "MMcf/d", rows: [
  {{ label: "Industrial", series: "a", v: 5894, ts: 1 }}, {{ label: "of which power burn", series: "b", v: 784, ts: 1 }},
  {{ label: "Large plant", value: 1200, unit: "MW", as_of: "2026-09", source: "EIA-860M Sep 2026 (operating, nameplate, the largest units first)" }}] }};
console.log(JSON.stringify({{
  week: G.periodLabel({{ ts: {nov6}, freq: "W" }}),
  empty: [G.tabEmpty({{ lines: [{{ line: "production", have: false, missing: true }}], groups: [] }}),
          G.tabEmpty({{ lines: [{{ line: "production", have: true, v: null }}], groups: [] }}), G.tabEmpty({{ lines: [], groups: [{{ rows: [] }}] }})],
  bal: [G.balNote({{ through: "yesterday" }}, "gas"), G.balNote({{ lag_s: 172800 }}, "power"), G.balNote({{ lag_s: 7200 }}, "power"), G.balNote({{}}, "gas")],
  html: html(g),
}}));
""")
    assert r["week"] == "wk to Nov 6"          # the EIA weeklies stamp the week-ending Friday (Reviewer on #530)
    assert r["empty"] == [True, False, False]
    assert r["bal"] == ["Finished gas days, through yesterday", "published ~2 d behind", "", ""]
    h = r["html"]
    assert 'class="rgnote"' in h and "intraprovincial deliveries" in h
    assert 'cl sub"' in h and "of which power burn" in h
    assert '<span class="rsrc">2026-09 · EIA-860M Sep 2026' in h.split('<td class="n"')[-2] or 'class="k">Large plant<span class="rsrc">' in h
    css = (WEB / "map.css").read_text(encoding="utf-8")
    assert ".rpage .rgroup td.k { white-space: normal;" in css and "text-overflow: ellipsis; white-space: nowrap; }" not in css.split(".rpage .rgroup td.k")[1][:80]


GROSS_JS = """
const lines = [{ line: "generation", v: 17850, ts: 1, unit: "MW" }];
const fuels = { id: "generation_by_fuel", of: "generation", gross: "fuels' total", unit: "MW", rest: 12, note: "CAISO's fuel mix, 5-minute; batteries charge at midday.",
  rows: [{ label: "Solar", series: "s", v: 14000, ts: 1, unit: "MW" }, { label: "Gas", series: "g", v: 5000, ts: 1, unit: "MW" },
         { label: "Wind", series: "w", v: 2000, ts: 1, unit: "MW" }, { label: "Batteries", series: "b", v: -4847, ts: 1, unit: "MW" },
         { label: "Other", series: "o", v: -300, ts: 1, unit: "MW" }] };
const html = (g) => G.RegionPage.prototype.groupHtml.call({}, g, lines);
const bars = G.groupBars(fuels, lines);
console.log(JSON.stringify({
  mode: G.groupBasis(fuels, lines).mode, basis: G.groupBasis(fuels, lines).basis,
  rows: bars.map((x) => [x.label, x.share == null ? null : Math.round(x.share * 100), x.split, !!x.neg, (x.tags || []).includes("charging")]),
  rest: G.showRest(fuels, lines), html: html(fuels),
  plain: G.groupBasis({ ...fuels, gross: undefined }, lines).mode,
}));
"""


def test_region_gross_shares_when_a_battery_charges():
    """Orchestrator on #530: California's batteries charge at midday (−4,847 MW in generation by fuel). With the same-sign
    rule that switched every fuel's share off; now, when rows of the line's sign carry ≥ 80 % of the absolute total, those
    rows keep shares of their own sum (gross generation), the opposite rows show their value with no share, a bar the other
    way from the zero line, and a "charging" tag when the row is battery or pumped storage (no tag otherwise); no "Other".
    Mixed signs with no dominant side (ties) stay a free table (test_region_group_mixed_signs_are_not_shares)."""
    r = _node_regionpage(GROSS_JS)
    assert r["mode"] == "gross" and r["basis"] == 21000                       # 14,000 + 5,000 + 2,000: gross generation
    assert r["rows"] == [["Solar", 67, True, False, False], ["Gas", 24, True, False, False], ["Wind", 10, True, False, False],
                         ["Batteries", None, True, True, True], ["Other", None, True, True, False]]
    assert r["rest"] is False and "Other / not itemised" not in r["html"]
    assert "charging" in r["html"] and 'class="rel neg"' in r["html"] and "67 %" in r["html"]
    assert r["plain"] == "free"                  # only a group that opts in (the fuel mix) goes gross (Reviewer on #532)


def test_region_gross_card_names_its_basis():
    """Reviewer on #532: gross shares aren't of the line (Bakken gas 262 MW reads 14 % under a 1,480 MW line), so a gross
    card says what they're of in its header; a share card doesn't."""
    r = _node_regionpage(GROSS_JS)
    assert "% of fuels&#39; total" in r["html"] or "% of fuels' total" in r["html"]
    share = _node_regionpage(GROSS_JS.split("const html")[0] + """
console.log(JSON.stringify({ mode: G.groupBasis({ ...fuels, rows: fuels.rows.slice(0, 3) }, lines).mode,
  html: G.RegionPage.prototype.groupHtml.call({}, { ...fuels, rows: fuels.rows.slice(0, 3) }, lines) }));
""")
    assert share["mode"] == "share" and "rgbasis" not in share["html"]


def test_region_interchange_by_tie_is_never_gross():
    """Reviewer on #532, the #479 case on live tx: interchange by tie, North −6 / East −4 MW under a −704 MW interchange
    line on another hour. Without the fuel mix's opt-in that's a free table, not North 60 % / East 40 % of a 10 MW sum."""
    r = _node_regionpage("""
const lines = [{ line: "interchange", v: -704, ts: 100, unit: "MW" }];
const tie = { id: "interchange_by_tie", of: "interchange", unit: "MW",
  rows: [{ label: "North", series: "n", v: -6, ts: 172, unit: "MW" }, { label: "East", series: "e", v: -4, ts: 172, unit: "MW" }] };
console.log(JSON.stringify({ mode: G.groupBasis(tie, lines).mode, shares: G.groupBars(tie, lines).map((x) => x.share ?? null) }));
""")
    assert r["mode"] == "free" and r["shares"] == [None, None]


def test_region_fuels_note_renders():
    """Orchestrator on #530: the "fuels_note" is the `note` on the generation_by_fuel group (12 regions); a group's note
    shows under its title (PLAN 9g)."""
    r = _node_regionpage(GROSS_JS)
    assert 'class="rgnote"' in r["html"] and "batteries charge at midday" in r["html"]


def test_region_gross_shares_on_the_rows_own_hour():
    """An hourly line is stamped an hour or two before its fuel rows (the region engine waits for every part), so the
    rows can't be shares of the line itself; when they share one timestamp they're shares of their own sum (gross mode),
    with "of which" rows kept out of that sum. Rows on different timestamps among themselves stay a free table."""
    r = _node_regionpage("""
const lines = [{ line: "generation", v: 26673, ts: 100, unit: "MW" }];
const rows = (t2) => [{ label: "Gas", series: "g", v: 6000, ts: 172, unit: "MW" }, { label: "of which cogeneration", series: "c", v: 1500, ts: 172, unit: "MW" },
  { label: "Solar", series: "s", v: 4000, ts: t2, unit: "MW" }];
const one = { of: "generation", gross: "fuels' total", rows: rows(172) }, two = { of: "generation", gross: "fuels' total", rows: rows(160) };
console.log(JSON.stringify({ one: [G.groupBasis(one, lines).mode, G.groupBasis(one, lines).basis, G.groupBars(one, lines).map((x) => Math.round(x.share * 100))],
  two: G.groupBasis(two, lines).mode }));
""")
    assert r["one"] == ["gross", 10000, [60, 15, 40]]      # 6,000 + 4,000 (cogeneration is part of gas): gas 60 %, of which 15 %
    assert r["two"] == "free"
