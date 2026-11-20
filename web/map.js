// Montney Stack v1: one live map (docs/ARCHITECTURE.md §6, design in docs/research/frontend.md).
import * as ml from "./vendor/maplibre/maplibre-gl.mjs";
import { api, aborted, DEV, esc, fmt, fmtTime, isoSec, setTz, toast, tzAbbr, ago, unitLabel } from "./lib.js";
import { buildStyle, networkLayers, REGION_TINT, dotPaint, nodeIcon, animIcon, animFilter, ANIM_LAYERS, SMOKE_LAYERS, smokeIcon, smokeOffset, FIRE_LAYERS, fireIcon,
  badgeOffset, pinShown, DATA_CREDITS } from "./style.js";
import { RAMPS, rampCss, decodeGrid, loadDem, sameGeom, stepAt, sampleGrid, demAt, FieldLayer, Wind, seamsOf } from "./weather.js";
import { Callouts, TIER_MINZOOM, baseSeries, inTier, flowSign, normalPct, pctNormalId, rowKey, shown, shownAt, tierOf, zoomBounds } from "./callouts.js";
import { floorZoom, levelZoom, tickStep } from "./placement.js";
import { loadSprites, spriteKind, spriteState, needsBadge, animSpeed, smokeSpec, fireFeatures, spriteCss, CENTER_KINDS, SPRITE_32_Z,
  GLOW_KINDS, isEstimate, ANIM_TICK_MS, plantFuel, outOfService, notProducing, capacityInfo, zeroText,
  plantMotion, EST_GLOW_KINDS, EST_MOVE_KINDS } from "./sprites.js";
import { Panel, eventHtml, eventWhen } from "./panel.js";
import { Producers, PROVINCES } from "./producers.js";
import { RegionPage } from "./regionpage.js";
import { Search } from "./search.js";
import { runnerBadge } from "./status.js";
import { SysMenu } from "./sysmenu.js";
import { gaugeStyle, gaugeLines, BAND_COLOR } from "./gauges.js";
import { RiverLayer } from "./rivers.js";
import { Power } from "./power.js";

const $ = (id) => document.getElementById(id);
const SNAPSHOT_MS = 60e3, EVENTS_MS = 5 * 60e3, NETWORK_MS = 5 * 60e3, GRIDS_MS = 10 * 60e3, STATUS_MS = 5 * 60e3;
const WELLS_MS = 30 * 60e3, WELLS_TIER = 2;   // L-106 8b: /api/wells is monthly data; loaded once the map reaches tier 2
// The snapshot polls when the server says new data is due (L-081 X-Next-Update, 15-300 s), never sooner than this:
// a phone's radio waking every 15 s costs more battery than a value up to 30 s late
const SNAPSHOT_MIN_MS = 30e3;
const SAVER_MAX_MS = 5 * 60e3;
const OFF_NOW_S = 1.5 * 3600;
const LAPSE = 0.0065; // degC per metre (standard atmosphere; see frontend.md §3 caveat)

const nowSec = () => Date.now() / 1000;
const S = { pinned: new Set(),
  config: null, net: null, netTag: null, byId: new Map(), snap: null, snapTag: null, events: [], evTag: null, stTag: null,
  wx: null, dem: null, timeSec: null, layers: {}, selected: null,
  ze: null,   // level zoom: the zoom with hysteresis at each zoom-level boundary (placement.js levelZoom)
};

// ------------------------------------------------------------------ boot
const config = S.config = await (await fetch("/static/map.json", { cache: "no-cache" })).json();
if (DEV) document.body.classList.add("dev");
// L-081 PR 2: the tier → minzoom table lives in map.json, so the server scopes the snapshot by the same numbers
if (config.tiers) for (const [t, z] of Object.entries(config.tiers)) if (/^\d+$/.test(t)) TIER_MINZOOM[t] = +z;
// the ☰ menu (L-097), made before the first status: Sources (the header's old dots) and System (/api/system); the Sources tab asks for a fresh status
const agoText = (s) => ago(isoSec(s.last?.finished || s.last?.started));
const sysMenu = new SysMenu({ button: $("menubtn"), panel: $("sysmenu"), agoText, onSources: () => loadStatus().catch(() => {}) });
try { await loadStatus(); } catch (e) { toast(`status: ${e.message}`, true); }

// narrower than 1280 px (a phone): the opening zoom and the zoom-out limit drop so the opening view keeps the extent a
// 1280 px screen sees (L-069); 1280 px and wider unchanged
const NARROW_DZ = Math.max(0, Math.log2(1280 / Math.max(1, innerWidth)));
const map = new ml.Map({
  container: "map", style: buildStyle(), ...config.view,
  zoom: config.view.zoom - NARROW_DZ, minZoom: (config.view.minZoom ?? 0) - NARROW_DZ,
  dragRotate: false, pitchWithRotate: false, touchPitch: false,
  // no symbol cross-fade (L-080): each sprite-clock tick re-places the symbols, and the 300 ms fades overlapped at 4
  // ticks/s, so the map kept rendering ~25 extra frames a second; labels and icons now appear without a fade
  fadeDuration: 0,
  attributionControl: { compact: true, customAttribution: DATA_CREDITS },
});
map.touchZoomRotate.disableRotation();
// Every show/hide decision (callout boxes and rows, node dots, alert pins) uses the level zoom S.ze, not the raw zoom,
// so wobbling across a boundary doesn't flicker anything (PLAN P4a). Updated before callouts re-place on "move".
// The outermost level (L-087, Erik: "when max zoomed [out] ... combine Alberta demand, production, export, and storage"):
// the last `view.outer_span` zoom levels before the map can't zoom out further (maxBounds or minZoom, whichever stops it
// first on this screen), so every screen reaches it. Boxes merge into province summaries there. Config says "outer"
// where it means that zoom; the spots are resolved now and again on resize (a rotated phone stops elsewhere).
const OUTER_SPOTS = [...(config.callouts || []), ...Object.values(config.overrides || {}), ...Object.values(config.gauge_boxes?.stations || {})]
  .flatMap((o) => ["minzoom", "maxzoom", "box_minzoom", "merge_until"].filter((k) => o[k] === "outer").map((k) => [o, k]));
let OUTER_Z = -Infinity, BOUNDS = [];
function setOuter() {
  OUTER_Z = floorZoom(map, config.view.maxBounds) + (config.view.outer_span ?? 0);
  for (const [o, k] of OUTER_SPOTS) o[k] = OUTER_Z;
  BOUNDS = [...new Set([...zoomBounds(config.callouts, { ...config.overrides, ...Object.fromEntries(Object.entries(config.systems || {})
    .map(([k, v]) => [`system:${k}`, v])) }), SPRITE_32_Z])].sort((a, b) => a - b);
}
setOuter();
const levelOf = (ze) => BOUNDS.filter((b) => ze >= b).length;
S.ze = map.getZoom();
map.on("resize", () => { setOuter(); applyDots(); callouts?.update(S.snap); });   // a new pixel ratio changes the sprites' CSS size
map.on("move", () => {
  const prev = S.ze;
  S.ze = levelZoom(map.getZoom(), prev, BOUNDS);
  if (levelOf(S.ze) !== levelOf(prev)) { applyDots(); callouts?.update(S.snap); }   // a level change can merge/split boxes
  // a new tier asks for its scope at once (L-081); what's drawn stays until it arrives
  if (S.snap && tierAt(S.ze) !== tierAt(prev)) loadSnapshot();
  if (!S.wells && tierAt(S.ze) >= WELLS_TIER) loadWells();   // L-106 8b: the facilities, first time zoomed in
});
map.on("error", (e) => { if (/webgl/i.test(e?.error?.message || "")) $("nogl").hidden = false; });
window.__map = map;

const field = new FieldLayer("field");
// Battery (L-080): one power state (active / idle / saver / hidden) paces the wind, river particles and sprite clock.
const power = (window.__power = new Power(map));
// River flow particles (web/rivers.js): frozen unless motion is allowed (same rules as the sprite clock).
const rivers = new RiverLayer("river-flow", { shouldRun: () => !globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches
  && !document.hidden && !document.body.classList.contains("off-now") && power.rate("river") > 0,
  frameMs: () => 1000 / Math.max(1, power.rate("river")) });
power.onChange(() => map.triggerRepaint());   // a river loop that stopped (hidden) picks up again
window.__rivers = rivers;   // diagnostics: reach flows and particle state
const wind = (window.__wind = new Wind(map, $("wind"), power));   // diagnostics handle
const panel = new Panel($("panel"), { onClose: () => { select(null); callouts?.queue(); } });
let callouts;

map.on("load", async () => {
  map.addSource("net", { type: "geojson", data: { type: "FeatureCollection", features: [] }, promoteId: "id" });
  map.addSource("events", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("fires", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("gauges", { type: "geojson", data: { type: "FeatureCollection", features: [] }, promoteId: "id" });
  try { S.gaugeFc = await (await fetch("/static/gauges.geojson", { cache: "no-cache" })).json(); } catch (e) { toast(`gauges: ${e.message}`, true); }
  try { S.riverFc = await (await fetch("/static/rivers.geojson", { cache: "no-cache" })).json(); rivers.setRivers(S.riverFc); }
  catch (e) { toast(`rivers: ${e.message}`, true); }
  // Sprites first, so no symbol ever refers to a missing image (web/sprites.js: pixel-exact at any display scaling).
  // Node sprites: pixel-exact in device px. Flanges: exact in CSS px, so collars line up with the CSS-px pipe widths.
  for (const [atlas, fit] of [["nodes", "device"], ["smoke", "device"], ["wildfire", "device"], ["flanges", "css"], ["power-lines", "css"]])
    try { const index = await loadSprites(map, `/static/sprites/${atlas}`, { fit }); if (atlas === "nodes") S.nodeAtlas = index; }
    catch (e) { toast(`${atlas} sprites: ${e.message}`, true); }
  for (const l of networkLayers()) map.addLayer(l, "places");
  map.addLayer(rivers, "areas-fill");   // below every network layer: data lines always draw over the particles (G1)
  map.addLayer(field, "hillshade");
  callouts = new Callouts({ map, config, anchorPoint, list: calloutList, featureName: (id) => S.byId.get(id)?.properties.name || id,
    isRegion: (id) => { const p = S.byId.get(id)?.properties; if (!p) return null;
      return p.type === "region" ? p.name.toUpperCase() : p.region_value ? (p.callout?.label || p.name).toUpperCase() : null; },
    nodeRects,
    levelZoom: () => S.ze, onOpen: openCallout,
    onClose: (c) => { S.pinned.delete(c.anchor); select(null); callouts.update(S.snap); },
    systemOf: (c) => c.system || S.byId.get(c.anchor)?.properties.system || null }, $("callouts"), $("leaders"));
  window.__callouts = callouts;   // diagnostics handle (region box forms and sizes), like window.__map
  buildHud();
  wireClicks();
  $("hudtoggle").onclick = () => {   // small screens (L-068): show or fold the layers list and legend; boxes re-place around them
    const on = document.body.classList.toggle("hud-open");
    $("hudtoggle").setAttribute("aria-expanded", String(on)); callouts?.queue();
  };
  // The required credits are long: show them expanded on first load, then collapse to the (i) button after 12 s; at
  // once on a small screen, where they would cover a third of it (L-068). The (i) still opens them.
  const small = matchMedia("(max-width: 900px), (max-height: 500px)").matches;
  setTimeout(() => { document.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show"); callouts?.queue(); }, small ? 0 : 12000);
  await loadNetwork();
  await Promise.allSettled([loadSnapshot(), loadEvents(), loadGrids()]);
  // Polling (L-080, L-081): nothing while the tab is hidden (a missed refresh runs as soon as it's visible again), one
  // request at a time, and the next one when the server says new data is due (`fn` returns its X-Next-Update seconds,
  // else the fixed interval). Battery saver multiplies the snapshot's wait by `saverMul`.
  const poll = (fn, ms, { saverMul = 1, minMs = 0 } = {}) => {
    let missed = false, timer = 0;
    // saver stretches the wait, up to 5 minutes (Reviewer on #318: ×2 on a 300 s hint would be 10 min on a phone)
    const wait = (next) => { const w = Math.max(minMs, next > 0 ? next * 1000 : ms);
      return power.saver ? Math.min(w * saverMul, Math.max(w, SAVER_MAX_MS)) : w; };
    const run = async () => {
      if (document.hidden) { missed = true; return; }   // no timer while hidden: visibilitychange restarts it
      let next = null;
      try { next = await fn(); } catch { /* fn reports its own errors */ }
      clearTimeout(timer); timer = setTimeout(run, wait(next));
    };
    document.addEventListener("visibilitychange", () => { if (!document.hidden && missed) { missed = false; run(); } });
    timer = setTimeout(run, wait(fn.first?.()));
  };
  loadSnapshot.first = () => S.snap?._next;
  poll(loadSnapshot, SNAPSHOT_MS, { saverMul: 2, minMs: SNAPSHOT_MIN_MS }); poll(loadEvents, EVENTS_MS);
  poll(loadNetwork, NETWORK_MS); poll(loadGrids, GRIDS_MS); poll(loadWells, WELLS_MS);
  // the runner badge comes with the snapshot (L-081), so the sources (☰ menu) refresh every 5 min, or on opening it
  poll(() => loadStatus().catch(() => { /* keep last */ }), STATUS_MS);
  poll(() => { renderClock(); if (Math.abs((S.timeSec ?? 0) - nowSec()) < OFF_NOW_S) setTime(nowSec()); else setTime(S.timeSec); }, 60e3);
  window.__ready = true;
});

// ------------------------------------------------------------------ network
async function loadNetwork() {
  try {
    const fc = await api("/api/network", { etag: S.netTag, key: "network" });
    if (fc.status === 304) return;
    S.net = fc; S.netTag = fc._etag || null;
    indexFeatures();
    if (fc.errors?.length) toast(`network: ${fc.errors.length} validation error(s), see /api/network`, true);
    $("empty").hidden = fc.features.length > 0;
    paintNetwork();
  } catch (e) { toast(`network: ${e.message}`, true); }
}
/** S.byId: the network's features and, once loaded, Gas's facilities (/api/wells), so a click, a search or ?features= finds both. */
function indexFeatures() {
  S.byId = new Map([...(S.net?.features || []), ...(S.wells?.features || [])].map((f) => [f.id ?? f.properties.id, f]));
}
/**
 * Every AB + BC gas facility (L-106 8b, Gas #410, montney/wells.py): node features in the network's schema with their
 * latest month inline (`v`, `ts`, `stale`; the snapshot's zoom scope never lists them) and `tier` from rank (top 100 per
 * province at tier 2, the rest at tier 3). Loaded the first time the map reaches tier 2, then revalidated by ETag every
 * WELLS_MS. Personal-use data (`licence: "personal"`): drawn here, never committed (no proof image shows a name).
 */
async function loadWells() {
  if (!S.wells && tierAt(S.ze) < WELLS_TIER) return;   // not zoomed in yet: nothing to fetch
  if (S.wellsBusy) return;
  S.wellsBusy = true;
  try {
    const fc = await api("/api/wells", { etag: S.wellsTag, key: "wells" });
    if (fc.status === 304) return;
    S.wells = fc; S.wellsTag = fc._etag || null;
    indexFeatures(); paintNetwork();
  } catch (e) { if (!aborted(e)) toast(`facilities: ${e.message}`, true); }
  finally { S.wellsBusy = false; }
}
function anchorPoint(id) {
  const f = S.byId.get(id);
  if (!f) return null;
  const g = f.geometry, p = f.properties;
  if (p.label_point) return p.label_point;
  if (g.type === "Point") return g.coordinates;
  const pts = g.type === "LineString" ? g.coordinates : g.type === "MultiLineString" ? g.coordinates.flat()
    : g.type === "Polygon" ? g.coordinates[0] : g.coordinates.flat(2);
  if (p.kind === "link") return pts[Math.floor(pts.length / 2)];
  const xs = pts.map((c) => c[0]), ys = pts.map((c) => c[1]);
  return [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
}
function paintNetwork() {
  if (!S.net || !map.getSource("net")) return;
  const wells = S.layers.wells !== false ? S.wells?.features || [] : [];
  const features = [...S.net.features, ...wells].map((f) => {
    const id = f.id ?? f.properties.id;
    // a facility's latest month rides on the feature (montney/wells.py); a click's ?features= entry wins once fetched
    const e = entryOf(S.snap, id) ?? (f.properties.v !== undefined ? { v: f.properties.v, ts: f.properties.ts, stale: !!f.properties.stale } : undefined);
    const cap = e?.cap_current ?? e?.cap; // today's capability when flow lags a day
    let util = e?.v != null && cap && f.properties.value_kind !== "capacity" ? Math.abs(e.v) / cap : null;
    let v = e?.v ?? null, stale = !!e?.stale;
    // L-106 (Power #422): an inventory plant with no live MW reads its BA + fuel's est. utilisation (`util_est`, a shared
    // series the snapshot carries once, montney/scope.py shared_series): util = that fraction, v = util × nameplate.
    // The plant is `quality: "estimate"`, so sprites.js keeps it a labelled steady glow: no smoke, a battery stays static.
    const ue = v == null && f.properties.util_est ? S.snap?.series?.[f.properties.util_est] : null;
    if (ue?.v != null) { util = Math.max(0, Math.min(ue.v, 1)); v = util * (f.properties.nameplate_mw ?? 1); stale = !!ue.stale; }
    const o = ovOf(id);
    return { ...f, id, properties: { ...f.properties, id, v, util, stale, context: !!f.properties.context,
      zmin: o.minzoom ?? TIER_MINZOOM[featureTier(id)] ?? 0, ...spriteProps(f.properties, o, v, util, stale, outOfService(f.properties, e)) } };
  });
  map.getSource("net").setData({ type: "FeatureCollection", features });
  // where the moving sprites are, so the animation clock only runs while one is on screen
  // with each one's zmin: the clock runs only for movers shown at this level (PLAN 10b frame budget)
  S.animPts = features.filter((f) => f.properties.spr_u || f.properties.smk_u).map((f) => ({ c: f.geometry.coordinates, z: f.properties.zmin ?? 0 }))
    .concat((S.firePts || []).map((c) => ({ c, z: 0 })));
  S.animZ = [...new Set(S.animPts.map((p) => p.z))].sort((a, b) => a - b);
  S.animKey = null; applyAnimFilter();
  callouts?.update(S.snap);
}

/** Sprite properties for a node (web/sprites.js). Regions keep their dashed ring (the callouts draw region rings). */
function spriteProps(p, o, v, util, stale, off = false) {
  if (p.kind !== "node" || p.type === "region") return {};
  const kind = spriteKind(p, o.sprite);
  if (!kind) return {};
  const anchor = CENTER_KINDS.has(kind) ? "center" : "bottom";
  const est = isEstimate(p);   // L-106: a BA-rate inventory plant is quality "estimate"
  // L-108 (sprites.js plantMotion): still image, timed set and rate, `?` badge. `off` = capability 0 (PLAN 10c): still,
  // no badge. `tracked`: the plant has an output source (551 OSM plants have none; a reservoir level or published
  // capacity isn't output: Mica, Revelstoke, Arrow Lakes, Reviewer on #540): still, no badge
  const tracked = !!(p.value || p.util_est) && !["level", "capacity"].includes(p.value_kind);
  const m = plantMotion(kind, v, util, stale, est, off, tracked);
  return { spr_kind: kind, spr_state: m.state, spr_anchor: anchor, spr_badge: m.badge,
    spr_u: m.u, spr_set: m.set || "frame",   // cycles/s (0 = still) and the timed set the frame layers draw
    // plant smoke: fresh, measured, positive output only (an estimate gets none); 0 = no plume
    ...((sm) => ({ smk_u: sm?.u ?? 0, smk_var: sm?.variant ?? "normal" }))(smokeSpec(kind, v, util, stale, est)),
    // L-106: an estimated glow is labelled "est." under the sprite (style.js node-est), so it never reads as measured MW
    spr_est: est && (GLOW_KINDS.has(kind) || EST_GLOW_KINDS.has(kind) || EST_MOVE_KINDS.has(kind)) && v != null,   // L-108: an estimate that glows or moves says "est."
    spr_sort: p.context ? 0 : p.type === "plant" || p.type === "facility" ? 1 : 2 };   // gas nodes draw over plants and facilities, context nodes underneath
}
/** Screen boxes the callouts keep clear of: each shown node's sprite (or a region's ring), "soft" in callouts.js. */
function nodeRects() {
  const s = spriteCss(S.ze >= SPRITE_32_Z ? 32 : 16, map.getPixelRatio());   // the sprite's real CSS size
  return [...S.byId.values()].filter((f) => f.properties.kind === "node" && featureShown(f.properties.id)).map((f) => {
    const q = map.project(f.geometry.coordinates), p = f.properties;
    const kind = p.type === "region" ? null : spriteKind(p, config.overrides?.[p.id]?.sprite);
    if (!kind) return { x: q.x - 9, y: q.y - 9, w: 18, h: 18 };
    return CENTER_KINDS.has(kind) ? { x: q.x - s / 2 - 1, y: q.y - s / 2 - 1, w: s + 2, h: s + 2 }
      : { x: q.x - s / 2 - 1, y: q.y - s - 1, w: s + 2, h: s + 2 };
  });
}

// ------------------------------------------------------------------ snapshot (every 60 s)
// ------------------------------------------------------------------ callout list
// Features carry `callout: {label, priority}` (+ optional `callouts_extra` rows), so new network features get a box
// without code changes. web/map.json adds system-wide callouts and per-feature `overrides` (prefer, tier, hide, label).
const DEFAULT_PREFER = ["E", "NE", "SE", "W", "NW", "SW", "N", "S"];
// A feature's zoom tier: map.json override, else its callout priority. Uncalled-out features (compressors etc.)
// show with tier 2. Node dots use the same tier, so the overview isn't covered in minor dots.
/** A feature's override: map.json `systems[<its system>]` (a default for every feature of that system, e.g. L-106: region
 * boxes hand off at zoom 6) under its own `overrides[id]`. */
function ovOf(id) {
  const sys = config.systems?.[S.byId.get(id)?.properties.system], own = config.overrides?.[id];
  return sys ? { ...sys, ...own } : own || {};
}
function featureTier(id) {
  const p = S.byId.get(id)?.properties, o = ovOf(id), co = p?.callout;
  return o.tier ?? p?.tier ?? co?.priority ?? 2;   // a feature's own `tier`: Gas's facilities, by rank (L-106 8b)
}
const featureShown = (id) => { const o = ovOf(id);
  return shownAt({ tier: featureTier(id), minzoom: o.minzoom }, S.ze); };
// Display rules (PLAN 4b, L-058): a stale, missing or zero value gets no box (solar at night, an idle unit, a dead feed),
// except a box marked `always_show: true` (on the feature, or on a map.json callout), which keeps showing with its STALE
// chip. A feature box goes when its own
// value does; other rows go one by one, and a box with no rows left goes. A NO-RESULT window mean (a thin hour) stays:
// it says why there's no number. "Zero" = shows as 0 at the row's precision.
/** The row's series has a reading in the snapshot (fresh or stale), unlike rowLive (fresh and non-zero). */
const rowHas = (r) => { const e = r.feature ? S.snap?.values?.[r.feature] : S.snap?.series?.[rowKey(r)]; return !!e && (e.v != null || e.ts != null); };
function rowLive(r) {
  const e = r.feature ? S.snap?.values?.[r.feature] : S.snap?.series?.[rowKey(r)];
  if (!e || e.stale) return false;
  const s = shown(e);
  if (s.reason) return true;
  return s.v != null && Number(fmt(s.v, r.decimals).replace(/,/g, "")) !== 0;
}
/**
 * River gauge boxes (L-067, Erik: "these river gauges for the columbia should have boxes"): map.json `gauge_boxes`
 * {basins, tier, rank, stations: {<STN>: {tier}}} gives each gauge of those basins a box at its point, its discharge
 * (with "· N % of normal", #267) on one row. Low rank and tier 3 by default, so they place after the dam boxes.
 */
function gaugeBoxes() {
  const gb = config.gauge_boxes, basins = new Set(gb?.basins || []);
  if (!gb || !S.gaugeFc) return [];
  return S.gaugeFc.features.filter((f) => basins.has(f.properties.basin)).flatMap((f) => {
    const p = f.properties, o = gb.stations?.[p.station] || {};
    const row = { series: gaugeSeries(p.station), label: p.name, unit: "m3/s", priority: 1 };
    if (!rowLive(row)) return [];   // stale or missing: no box (the display rule)
    return [{ id: `g:${p.station}`, tier: o.tier ?? gb.tier ?? 3, minzoom: o.minzoom, rank: o.rank ?? gb.rank ?? 3, gauge: p,
      anchor: { at: f.geometry.coordinates, point: true }, prefer: o.prefer || gb.prefer || DEFAULT_PREFER, rows: [row] }];
  });
}
const gaugeSeries = (station) => `wsc.realtime.${station.toLowerCase()}.discharge`;
function calloutList() {
  const have = (id) => S.byId.has(id);
  const out = [];
  for (const c of config.callouts || []) {
    if (c.unless_feature && have(c.unless_feature)) continue;
    const rows = c.rows.filter((r) => !(r.unless_feature && have(r.unless_feature)) && (c.always_show || rowLive(r)));
    // the first row is the box's own value (e.g. the Montney total): without it the box goes, so a part row never
    // stands in for the whole
    if (!c.always_show && rows[0] !== c.rows.find((r) => !(r.unless_feature && have(r.unless_feature)))) continue;
    if (rows.length) out.push({ ...c, rows });
  }
  out.push(...gaugeBoxes());
  const ov = config.overrides || {};
  for (const [id, f] of S.byId) {
    const p = f.properties, co = p.callout, o = ovOf(id);
    if (!co || o.hide || p.context) continue;
    // PLAN 9 part 2: the v2 region box, the same six rows for every region (montney/regions.py `box_rows`): Gas · MMcf/d
    // (production, demand, imports, exports, storage implied; sources +, sinks −, so the four add up to the fifth) and
    // Power · MW (generation). Every row always shows: "—" (dimmed, its `why` on hover) without a value, 0 for a known zero.
    if (p.system === "region" && Array.isArray(p.box_rows) && p.box_rows.length) {
      out.push({ id: `f:${id}`, tier: featureTier(id), minzoom: o.box_minzoom ?? o.minzoom, maxzoom: o.maxzoom, rank: o.rank, anchor: id,
        prefer: o.prefer || co.prefer || DEFAULT_PREFER, always_show: true, rows: boxRowsV2(p.box_rows, o.label || co.label || p.name),
        system: "mixed", titleOpens: id, color: p.color, region: { ext: regionExtent(p.region), dir: p.box?.dir } });
      continue;
    }
    // L-106 8a: a box whose extras carry its own value line (montney/regions.py region boxes) draws that line there,
    // with its label, section and depth, under a title row; no separate headline row
    const extras = p.callouts_extra || [], selfRow = !!p.value && extras.some((x) => x.series === p.value);
    const keep = p.always_show ? () => true : rowLive;   // the feature's own always_show (Gas/Power set it)
    // A live-only series with no value falls back to its daily sum when the feature's own panel lists `<series>.daily`
    // (L-078: IESO's hourly MW isn't live yet, its MWh/d daily sums are); data-driven, the label says "daily".
    // the daily sibling: `<series>.daily`, or `<x>.daily` for a `<x>.hourly` series (Power #292: ieso.gen.total.hourly)
    const dailyOf = (s) => { const d = [`${s}.daily`, String(s).replace(/\.hourly$/, ".daily")].find((x) => x !== s && (p.panel?.series || []).includes(x));
      // the feature's own value is in the snapshot's values, not its series (L-090: CAISO's live hourly showed its daily)
      const live = s === p.value ? rowLive({ feature: id }) : rowLive({ series: s });
      return d && !live && rowLive({ series: d }) ? d : null; };
    const ownDaily = !selfRow && p.value && dailyOf(p.value);
    const own = !selfRow && p.value && (ownDaily ? { series: ownDaily, flow: p.flow, label: `${o.label || co.label || p.name} · daily total`, priority: 1, opens: id }
      : { feature: id, flow: p.flow, label: o.label || co.label || p.name, unit: p.unit, value_kind: p.value_kind, band: p.band,
        vs_target: !!p.target, ref_label: p.target ? "vs tgt" : undefined, priority: 1 }); // a box's own value always shows
    if (own && !keep(own)) continue;                                                  // its value is stale/missing/zero: no box
    const rows = [...(o.rows_before || [])].filter(keep);
    // the share rides on the value line (PLAN 4c): "2,803 MMcf/d · 22 %", basis named in its tooltip (callouts.js rowHtml)
    if (own && p.share?.of) own.shareOf = { ...(ownDaily ? { series: ownDaily } : { feature: id }), of: dailyOf(p.share.of) || p.share.of,
      flow: p.flow, band: p.band, est: p.estimate, of_band: p.share.of_band, label: p.share.label || `% of ${p.share.of}` };
    if (own) rows.push(own);
    for (const x of extras) {
      const xd = !x.agg && dailyOf(x.series);   // the same daily fallback for a part (and its share's basis)
      const r = { series: xd || x.series, agg: x.agg, band: x.band, flow: x.flow, label: x.label, unit: xd ? undefined : x.unit, depth: x.depth,   // depth: L-073
        section: x.section, priority: x.priority ?? co.priority ?? 2 };
      if (x.share?.of) r.shareOf = { series: xd || rowKey(x), of: dailyOf(x.share.of) || x.share.of, band: x.band, of_band: x.share.of_band,
        label: x.share.label || `% of ${x.share.of}` };   // an extra row's own share (e.g. a fuel's % of AB load)
      // L-106: `share: "<series>"` (region fuel rows: % of generation) draws a share bar too
      else if (typeof x.share === "string") r.shareOf = { series: xd || rowKey(x), of: x.share, label: shareLabel(x.share, extras), bar: true };
      // a row's own always_show keeps it while it has any reading (stale ones carry their chip); a line with no data
      // at all is left out, so a thin region's box stays short (REGIONS.md §5)
      if ((x.always_show && rowHas(r)) || keep(r)) rows.push(r);
    }
    // (a box drawing its value line among its rows, i.e. a region box, goes only when none of its lines is left)
    // an override's `cue` (L-091, Weather #362) words every "% of normal" row of the box above / near / below normal,
    // as map.json rows do with `normal: "self"` (the value is the % of normal)
    if (o.cue) for (const r of rows) if (/% of normal/.test(r.unit || (r.feature === id ? p.unit : "") || "")) Object.assign(r, { normal: "self", cue: o.cue });
    const sectioned = extras.some((x) => x.section);
    if (rows.length) out.push({ id: `f:${id}`, tier: featureTier(id), minzoom: o.box_minzoom ?? o.minzoom, maxzoom: o.maxzoom, rank: o.rank, anchor: id,
      prefer: o.prefer || co.prefer || DEFAULT_PREFER, always_show: !!p.always_show, rows: sectioned ? withSections(rows, o.label || co.label || p.name) : rows,
      ...(sectioned ? { system: "mixed", titleOpens: id, color: p.color } : {}),
      // PLAN 9: a generated region's box is laid out by regionlayout.js: its region's extent and outside direction
      ...(p.system === "region" ? { region: { ext: regionExtent(p.region), dir: p.box?.dir } } : {}) });   // color: the region's (its border and tint)
  }
  // Click to open (PLAN 4c): a running asset clicked while it has no box gets the box it would have when shown, pinned at
  // its point (tier 0, placed first) until closed. It shows whatever the snapshot has, the display rules aside.
  for (const id of S.pinned) {
    const f = S.byId.get(id); if (!f) { S.pinned.delete(id); continue; }
    const p = f.properties, o = ov[id] || {}, co = p.callout || {};
    const rows = [...(o.rows_before || [])].filter(rowLive);
    const own = { feature: id, flow: p.flow, label: o.label || co.label || p.name, unit: p.unit, value_kind: p.value_kind, band: p.band,
      vs_target: !!p.target, ref_label: p.target ? "vs tgt" : undefined, priority: 1 };
    if (p.share?.of) own.shareOf = { feature: id, of: p.share.of, flow: p.flow, band: p.band, est: p.estimate,
      of_band: p.share.of_band, label: p.share.label || `% of ${p.share.of}` };
    rows.push(own);
    for (const x of p.callouts_extra || []) {
      const r = { series: x.series, agg: x.agg, band: x.band, flow: x.flow, label: x.label, unit: x.unit, priority: 1 };
      if (x.share?.of) r.shareOf = { series: rowKey(x), of: x.share.of, band: x.band, of_band: x.share.of_band,
        label: x.share.label || `% of ${x.share.of}` };
      if (rowLive(r)) rows.push(r);
    }
    const i = out.findIndex((c) => c.id === `f:${id}`);
    const box = { id: `f:${id}`, tier: 0, rank: -1, anchor: id, closable: true, prefer: o.prefer || co.prefer || DEFAULT_PREFER, rows };
    if (i >= 0) out[i] = box; else out.push(box);
  }
  // One box for several features (PLAN step 6, Erik: "the gas and the power should be in the same box"): map.json
  // `merge_into` appends a feature's box to another's as a section, under a head row naming its unit and share basis,
  // below `merge_until` (zoomed out; zoomed in, where the gas parts appear, the two boxes have room on their own). The
  // target gets its `title` and its own `section` head. Rows, units, shares and signs are unchanged; display only.
  // A config box (map.json `callouts`) merges the same way, by its own `merge_into`/`merge_until`/`section` (L-087).
  // Below `merge_until` the target's `outer_title` (if any) names the whole box (e.g. "Alberta").
  const merges = [...Object.entries(ov).map(([id, o]) => [`f:${id}`, o, S.byId.get(id)?.properties.name || id]),
    ...(config.callouts || []).map((c) => [c.id, c, c.anchor?.label || c.id])]
    .sort((x, y) => (x[1].merge_order ?? 0) - (y[1].merge_order ?? 0));   // sections in `merge_order`, then file order
  for (const [boxId, o, name] of merges) {
    if (!o.merge_into || (o.merge_until != null && S.ze >= o.merge_until)) continue;
    const src = out.findIndex((c) => c.id === boxId), dst = out.find((c) => c.id === `f:${o.merge_into}` || c.id === o.merge_into);
    if (src < 0 || !dst) continue;
    const [box] = out.splice(src, 1), to = ov[o.merge_into] || (config.callouts || []).find((c) => c.id === o.merge_into) || {};
    if (!dst.merged) {
      const title = to.outer_title && S.ze < OUTER_Z ? to.outer_title : to.title;
      dst.rows = [...(title ? [{ head: title, title: true, priority: 1 }] : []),
        ...(to.section ? [{ head: to.section, section: true, priority: 1 }] : []), ...dst.rows];
      dst.merged = true; dst.system = "mixed";   // gas + power: no single system tint
      dst.front = title ? 1 : 0;                 // where a section with a negative merge_order goes: under the title
    }
    // a `section` (e.g. "Power · MW") keeps its first row when folded; a plain part (a facility under a total) folds away
    const sec = [{ head: o.section || name, section: !!o.section, priority: 1 }, ...box.rows];
    // a negative `merge_order` puts the section above the target's own rows (L-092, Erik: the outermost Alberta box
    // leads with production), in merge_order; otherwise it follows them
    if ((o.merge_order ?? 0) < 0) { dst.rows.splice(dst.front, 0, ...sec); dst.front += sec.length; }
    else dst.rows.push(...sec);
  }
  // Handoff partners: an overview box that hands off at `maxzoom` and the tier-0/1 box showing the same value swap in
  // place (callouts.js), whichever way you zoom. "Same value" = a row's series, or a feature row's own value series.
  const seriesOf = (r) => r.series || S.byId.get(r.feature)?.properties.value;
  for (const c of out) {
    if (tierOf(c) !== 0 || c.maxzoom == null) continue;
    const want = new Set(c.rows.map(seriesOf).filter(Boolean));
    const d = out.find((x) => x !== c && tierOf(x) <= 1 && !x.partner && x.rows.some((r) => want.has(seriesOf(r))));
    if (d) { c.partner = d.id; d.partner = c.id; }
  }
  return out;
}
/**
 * L-106 8a: rows that carry a `section` (gas | power | oil) get the box's title row and a head row where each section
 * starts: map.json `sections[<section>].head` ("Gas · MMcf/d"), else the section's name and its first row's unit. A
 * folded box keeps the heads and the section's `lead` lines (by standard line, REGIONS.md §4: region.<r>.<section>.<line>);
 * its other depth-0 lines fold until clicked. One rule for every region box; a box with only some sections stays short.
 */
function withSections(rows, title) {
  // a section's lead lines (map.json sections[].lead, by standard line; every depth-0 row when it lists none)
  const isLead = (r) => { const cfg = config.sections?.[r.section] || {}, line = String(r.series || "").split(`.${r.section}.`)[1];
    return r.depth === 0 && (!cfg.lead || (!!line && cfg.lead.includes(line))); };
  // a section with no lead line yet (its parts haven't all reported: e.g. AB · BC · SK generation waits on BC Hydro) folds
  // whole, head included, so a fuel row never stands in as its headline; a box with none at all leads with its first row
  const fallback = rows.some(isLead) ? null : rows.find((r) => r.depth === 0) || rows[0];
  const leadSecs = new Set(rows.filter((r) => isLead(r) || r === fallback).map((r) => r.section));
  const out = [{ head: title, title: true, priority: 1 }];
  let sec = null;
  for (const r of rows) {
    const cfg = config.sections?.[r.section] || {};
    if (r.section && r.section !== sec) {
      sec = r.section;
      out.push({ head: cfg.head || `${sec[0].toUpperCase()}${sec.slice(1)} · ${unitLabel(r.unit || "")}`, section: true,
        lead: leadSecs.has(sec), priority: 1 });
    }
    // PLAN 9: a compact box keeps the title and two headline numbers: the first lead line of each of the first two sections
    const head = (isLead(r) || r === fallback) && !out.some((x) => x.compact && x.section === r.section) && new Set(out.filter((x) => x.compact).map((x) => x.section)).size < 2;
    out.push(isLead(r) || r === fallback ? (head ? { ...r, compact: true } : r) : { ...r, fold: true });
  }
  return out;
}
/**
 * PLAN 9 part 2: the v2 box's rows: the title, a head where each section starts (map.json `sections[].head`), then each
 * row, all leads (nothing folds); the first row of each section is the compact form's headline (production, generation).
 * `sign` is the display sign (montney/regions.py: sources +, sinks −), carried as the row's `flow`.
 */
function boxRowsV2(rows, title) {
  const out = [{ head: title, title: true, priority: 1 }];
  let sec = null;
  for (const b of rows) {
    const first = b.section !== sec;
    if (first) { sec = b.section; out.push({ head: config.sections?.[sec]?.head || `${sec[0].toUpperCase()}${sec.slice(1)} · ${unitLabel(b.unit || "")}`,
      section: true, lead: true, priority: 1 }); }
    out.push({ series: b.series, label: b.label, unit: b.unit, section: b.section, depth: 0, priority: 1, lead: true,
      flow: b.sign < 0 ? "demand" : "supply", tags: b.tags || [], have: b.have !== false, zero: !!b.zero, why: b.why, v2: true,
      ...(first ? { compact: true } : {}) });
  }
  return out;
}
/**
 * A region's extent for its box layout: the bbox [w, s, e, n] of its outline's largest polygon (an island or a clipped
 * sliver doesn't stretch it), from the generated outline (type region_outline, `region` = its id). Cached per network load.
 */
function regionExtent(region) {
  if (S.regionExt?.net !== S.net) S.regionExt = { net: S.net, m: new Map() };
  const m = S.regionExt.m;
  if (!m.has(region)) {
    const f = [...S.byId.values()].find((x) => x.properties.type === "region_outline" && x.properties.region === region);
    let best = null;
    if (f) {
      const g = f.geometry, polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
      for (const poly of polys) {
        const xs = poly[0].map((c) => c[0]), ys = poly[0].map((c) => c[1]);
        const b = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
        if (!best || (b[2] - b[0]) * (b[3] - b[1]) > (best[2] - best[0]) * (best[3] - best[1])) best = b;
      }
    }
    m.set(region, best);
  }
  return m.get(region);
}
/** The basis a region share names: its row's label ("% of generation"), lowercased, without "(AB)"-style notes. */
const shareLabel = (series, extras) => `% of ${String(extras.find((x) => x.series === series)?.label || series).replace(/\s*\(.*\)\s*$/, "").toLowerCase()}`;
// ------------------------------------------------------------------ snapshot scope (L-081 PR 2, Database #317)
/**
 * Full entries for features the scoped snapshot only has as lite (no box at this tier): a dot clicked open, a pinned
 * box, a panel. `?features=` (ETag'd, cached) adds them to the current snapshot's values; then the boxes redraw.
 */
async function ensureFull(ids) {
  const snap = S.snap, need = ids.filter((id) => snap?.lite && !snap.values?.[id] && S.byId.get(id)?.properties.value);
  if (!need.length) return;
  try {
    const r = await api(`/api/snapshot?features=${encodeURIComponent(need.join(","))}`);   // no key: two clicks both land
    Object.assign((snap.values ??= {}), r.values || {});
    if (snap === S.snap) { paintNetwork(); }
  } catch (err) { if (!aborted(err)) toast(`features: ${err.message}`, true); }
}
/** The tier of a level zoom: the highest t with ze ≥ tiers[t]. */
const tierAt = (ze) => Math.max(0, ...Object.keys(TIER_MINZOOM).map(Number).filter((t) => ze >= TIER_MINZOOM[t]));
const boxInTier = (c, t) => inTier(c.minzoom ?? TIER_MINZOOM[tierOf(c)], c.maxzoom, t, TIER_MINZOOM);
const featureInTier = (id, t) => { const o = ovOf(id);
  return inTier(o.minzoom ?? TIER_MINZOOM[featureTier(id)], o.maxzoom, t, TIER_MINZOOM); };
/** A feature's entry: its full one (a box at this tier, or fetched on click), else its lite [v, stale, cap]. */
function entryOf(snap, id) {
  const e = snap?.values?.[id];
  if (e) return e;
  const l = snap?.lite?.[id];
  return l ? { v: l[0], stale: l[1], cap: l[2] } : undefined;
}

/** The extra series the boxes and rows shown at tier t need (rivers and gauges draw at every zoom). */
function extraIds(t) {
  const ids = new Set(), feats = [...S.byId.values()].filter((f) => featureInTier(f.properties.id, t));
  // a row with `agg` (or a `<series>@<window>` series) asks the snapshot for its window mean (#151)
  for (const c of config.callouts || []) if (boxInTier(c, t)) for (const r of c.rows) {
    if (r.series) ids.add(rowKey(r)); if (r.ref) ids.add(r.ref); if (r.normal && r.normal !== "self") ids.add(r.normal);   // L-085
  }
  for (const f of feats) for (const x of f.properties.callouts_extra || []) ids.add(rowKey(x));
  for (const f of feats) for (const b of f.properties.box_rows || []) if (b.series) ids.add(b.series);   // PLAN 9 part 2
  // the daily sums a live-only row can fall back to (L-078): those the feature's panel lists as `<series>.daily`
  for (const f of feats) for (const s of f.properties.panel?.series || []) if (/\.daily$/.test(s)) ids.add(s);
  for (const f of feats) if (f.properties.share?.of) ids.add(f.properties.share.of);   // share rows' basis
  for (const f of feats) for (const x of f.properties.callouts_extra || []) if (x.share?.of) ids.add(x.share.of);
  for (const [id, o] of Object.entries(config.overrides || {})) if (featureInTier(id, t))
    for (const r of o.rows_before || []) { if (r.series) ids.add(rowKey(r)); if (r.ref) ids.add(r.ref); }
  // river flow vs normal (step 7): every dam outflow / WSC discharge shown, and every gauge, asks for its % of normal
  for (const id of [...ids, ...feats.map((f) => f.properties.value)]) if (pctNormalId(id)) ids.add(pctNormalId(id));
  for (const f of S.gaugeFc?.features || []) ids.add(gaugeSeries(f.properties.station)).add(pctNormalId(gaugeSeries(f.properties.station)));
  // river reaches' flow series: the Peace dams and AB Rivers outflows, including a seasonal gauge's winter fallback
  for (const f of S.riverFc?.features || []) for (const r of f.properties.reaches)
    for (const s of [r.src, r.src.fallback]) if (s && (s.type === "dam" || s.type === "series")) ids.add(s.id);
  // estimated features' lo/hi: every tier, since a click on one opens its range (`?features=` carries no series)
  for (const f of S.byId.values()) if (f.properties.estimate) ids.add(f.properties.estimate.lo).add(f.properties.estimate.hi);
  return [...ids];
}
async function loadSnapshot() {
  try {
    const t = tierAt(S.ze);
    const r = await api(`/api/snapshot?z=${t}&extra=${encodeURIComponent(extraIds(t).join(","))}`, { etag: S.snapTag, key: "snapshot" });
    if (r.status === 304) return r.next;   // nothing changed (L-081): keep what's drawn
    S.snap = r; S.snapTag = r._etag || null;
    ensureFull([...S.pinned]);   // a pinned box with only a lite entry at this tier gets its full one
    paintNetwork();
    paintGauges();
    rivers.update(S.snap);
    S.asof = `data ${fmtTime(isoSec(S.snap.as_of), { date: false })} ${tzAbbr()}`;
    $("asof").textContent = S.asof;
    if ("runner" in S.snap) renderRunner(S.snap.runner);   // Postgres: the banner rides on the snapshot (L-081)
    return r._next;
  } catch (e) { if (!aborted(e)) toast(`snapshot: ${e.message}`, true); }
}

// ------------------------------------------------------------------ events: pins + ticker
/** "bottom" when the pinned feature is a node whose sprite stands on its point, else "center" (style.js pin layers). */
function pinAnchor(id) {
  const p = S.byId.get(id)?.properties;
  if (!p || p.kind !== "node" || p.type === "region") return "center";
  const kind = spriteKind(p, config.overrides?.[id]?.sprite);
  return kind && !CENTER_KINDS.has(kind) ? "bottom" : "center";
}
const PIN_LAYERS = ["event-pins", "event-pins-c"];
async function loadEvents() {
  try {
    const r = await api("/api/events?state=current", { etag: S.evTag, key: "events" });
    if (r.status !== 304) { S.events = r; S.evTag = r._etag || null; }   // 304: redraw the same events (fires age by the clock)
    const pins = [];
    for (const ev of S.events) {
      const ref = (ev.refs || []).find((r) => anchorPoint(r)), at = ref && anchorPoint(ref);
      if (at) pins.push({ type: "Feature", geometry: { type: "Point", coordinates: at },
        properties: { id: ev.id, severity: ev.severity, kind: ev.kind, title: ev.title, anchor: pinAnchor(ref) } });
    }
    map.getSource("events")?.setData({ type: "FeatureCollection", features: pins });
    const fires = fireFeatures(S.events, nowSec());
    map.getSource("fires")?.setData({ type: "FeatureCollection", features: fires });
    S.firePts = fires.filter((f) => f.geometry.type === "Point").map((f) => f.geometry.coordinates);
    const tick = S.events.filter((e) => e.kind !== "outage" || !(e.refs || []).length).slice(0, 12);
    $("ticker").innerHTML = tick.length ? tick.map((e, i) => `<button class="tk ${e.severity}" data-i="${S.events.indexOf(e)}">
      <span class="sev"></span>${esc(e.title)} <span class="dim">${esc(e.kind)} · ${eventWhen(e)}</span></button>`).join("")
      : '<span class="dim">no current bulletins or alerts</span>';
    $("ticker").querySelectorAll(".tk").forEach((b) => (b.onclick = () => openEvent(S.events[+b.dataset.i])));
  } catch (e) { $("ticker").innerHTML = `<span class="dim">events unavailable</span>`; }
}
/** River gauges: the WSC stations (web/gauges.geojson) joined with the snapshot's flow-vs-normal state (web/gauges.js). */
function paintGauges() {
  if (!S.gaugeFc || !map.getSource("gauges")) return;
  const G = S.snap?.gauges || {};
  map.getSource("gauges").setData({ ...S.gaugeFc, features: S.gaugeFc.features.map((f) => {
    const g = G[f.properties.station], st = gaugeStyle(g);
    return { ...f, properties: { ...f.properties, g_fill: st.fill, g_fillop: st.fillOpacity, g_op: st.opacity, g_state: g?.state || "none" } };
  }) });
}
function openGauge(p) {
  const g = S.snap?.gauges?.[p.station];
  panel.open({ kicker: `RIVER GAUGE · ${p.station} · ${p.basin}`.toUpperCase(), title: p.name, series: g?.series ? [g.series] : [],
    labels: ["discharge (m³/s)"],
    body: `<p class="gauge-lines">${gaugeLines(g, normalPct(g?.series, S.snap)).map(esc).join("<br>")}</p><p class="dim">flow vs the 1995-2024 normal for this time of
      year (±7 days, trailing 24 h mean). Station: ${esc(p.wsc_name)}, WSC.</p>` });
}
function openEvent(ev) {
  if (!ev) return;
  panel.open({ kicker: `${ev.kind.toUpperCase()} · ${ev.severity} · ${ev.source}`, title: ev.title, series: [],
    body: `${eventHtml(ev)}${ev.body ? `<p>${esc(ev.body)}</p>` : ""}<p class="dim">refs: ${esc((ev.refs || []).join(", ") || "none")}
      · first seen ${ago(isoSec(ev.first_seen))} ago</p>` });
}

// ------------------------------------------------------------------ weather grids
// Weather regions (L-080, Weather #303): the main grid `t2m`/`u10`/`v10`/`z`, plus any `t2m<suffix>` set in the catalogue
// (`_east`: 0.5°, −95..−75). Each region gets its own field layer, terrain lapse and time step; the wind samples them all.
// Grids share their seam column and are drawn between cell centres, so they meet without overlapping.
const VARS = ["t2m", "u10", "v10", "z"];
const fieldFor = (sfx) => {
  if (!sfx) return field;
  S.fields ??= {};
  if (!S.fields[sfx]) {
    S.fields[sfx] = new FieldLayer(`field${sfx}`); S.fields[sfx].opacity = field.opacity;
    if (map.getLayer("hillshade")) map.addLayer(S.fields[sfx], "hillshade");
    applyLayers();
  }
  return S.fields[sfx];
};
const fieldLayers = () => [field, ...Object.values(S.fields || {})];
async function loadGrid(cur, sfx) {
  const [t2m, u, v, z] = await Promise.all(VARS.map((n) => (cur[n + sfx] ? api(`/api/grid/${n}${sfx}`).then(decodeGrid).catch(() => null) : null)));
  if (!t2m) return null;
  const g = { sfx, t2m, u: u && sameGeom(u.geom, t2m.geom) ? u : null, v: v && sameGeom(v.geom, t2m.geom) ? v : null,
    z: z && sameGeom(z.geom, t2m.geom) ? z : null };
  if (g.u && g.v) { g.speed = new Float32Array(g.u.data.length); for (let i = 0; i < g.speed.length; i++) g.speed[i] = Math.hypot(g.u.data[i], g.v.data[i]) * 3.6; }
  return g;
}
/** Is a catalogue grid's box within 5° of the current view? (L-080: load extra weather regions only when looked at) */
function regionNear(c) {
  if (!c) return false;
  const b = map.getBounds(), m = 5, e = c.lon0 + (c.nx - 1) * c.dx, n = c.lat0 + (c.ny - 1) * c.dy;
  return c.lon0 - m <= b.getEast() && e + m >= b.getWest() && c.lat0 - m <= b.getNorth() && n + m >= b.getSouth();
}
// a region that comes into view after the last load is fetched then (once; loadGrids keeps it afterwards)
map.on("moveend", () => {
  const cur = S.gridCat; if (!cur || S.gridLoading) return;
  const missing = Object.keys(cur).some((k) => { const x = k.match(/^t2m(_[a-z0-9]+)$/)?.[1];
    return x && !S.wx?.grids?.some((g) => g.sfx === x) && regionNear(cur[k]); });
  if (missing) { S.gridLoading = true; loadGrids().finally(() => { S.gridLoading = false; }); }
});
async function loadGrids() {
  try {
    const cat = await api("/api/grids");
    const cur = Object.fromEntries(cat.filter((g) => !g.prev).map((g) => [g.var, g]));
    if (!cur.t2m) { setWxStatus("no weather grid yet"); return; }
    S.gridCat = cur;
    // an extra region loads only once the view has come within 5° of it (battery: the east grid isn't fetched for an
    // AB/BC view); once loaded it stays, and refreshes with the main grid
    const sfxs = ["", ...Object.keys(cur).map((k) => k.match(/^t2m(_[a-z0-9]+)$/)?.[1]).filter(Boolean).sort()
      .filter((x) => S.wx?.grids?.some((g) => g.sfx === x) || regionNear(cur[`t2m${x}`]))];
    const key = sfxs.flatMap((x) => VARS.map((v) => `${v}${x}:${cur[v + x]?.run}:${cur[v + x]?.fetched}`)).join("|");
    if (S.wx?.key === key) return;
    const grids = (await Promise.all(sfxs.map((x) => loadGrid(cur, x)))).filter(Boolean);
    const main = grids.find((g) => g.sfx === "");
    if (!main) { setWxStatus("weather grid unreadable"); return; }
    const wx = { key, ...main, grids };   // the main grid's fields at the top level: the slider and legend use its times
    const windGrids = grids.filter((g) => g.u && g.v).map((g) => ({ geom: g.t2m.geom, u: g.u.data, v: g.v.data }));
    if (windGrids.length) wind.setGrids(windGrids);
    S.wx = wx;
    applyField();
    // a region on another model run says so (Reviewer, #309): a lagging run holds its last frame at the slider's end
    const other = grids.filter((g) => g.sfx && g.t2m.run !== main.t2m.run).map((g) => `${g.sfx.slice(1)} ${g.t2m.model || ""} run ${fmtTime(isoSec(g.t2m.run))}`);
    setWxStatus(`${main.t2m.model || "model"} run ${fmtTime(isoSec(main.t2m.run))} ${tzAbbr()}${other.length ? ` · ${other.join(" · ")}` : ""}`);
    setupSlider();
    S.dems ??= {};
    for (const g of grids) {
      if (!g.z) continue;
      const lay = fieldFor(g.sfx), dem = S.dems[g.sfx];
      if (dem) { lay.setTerrain(g.z, dem); continue; }
      loadDem(g.t2m.geom).then((d) => { S.dems[g.sfx] = d; if (!g.sfx) S.dem = d; lay.setTerrain(g.z, d); applyField(); map.triggerRepaint(); })
        .catch((e) => { if (!g.sfx) toast(`terrain detail unavailable: ${e.message}`); });
    }
  } catch (e) { setWxStatus("weather unavailable"); }
}
function applyField() {
  // L-106 region colours: the tint only where no weather field is drawn (it would shift the field's colours); borders stay
  const fieldOn = !!S.wx && !S.layers.nofield && !!(S.layers.temp || S.layers.windspeed);
  if (map.getLayer("region-fill")) map.setPaintProperty("region-fill", "fill-opacity", fieldOn ? 0 : REGION_TINT);
  const wx = S.wx; if (!wx) return;
  const useWind = !!(S.layers.windspeed && wx.speed);
  // a region's edge that meets another region's opposite edge doesn't fade (they share that row/column; weather.js seamsOf)
  for (const g of wx.grids) {
    const lay = fieldFor(g.sfx), speed = useWind && g.speed;
    lay.seam = seamsOf(g, wx.grids);
    if (useWind && !speed) continue;
    lay.setField(g.t2m.geom, speed || g.t2m.data, g.t2m.times.length || 1, useWind ? RAMPS.wind : RAMPS.t2m);
    lay.lapse = !useWind && S.layers.terrain && S.dems?.[g.sfx] ? LAPSE : 0;
  }
  renderLegend();
  setTime(S.timeSec ?? nowSec());
}
function setWxStatus(s) { $("wxstatus").textContent = s; }

// ------------------------------------------------------------------ time slider (moves WEATHER only)
function setupSlider() {
  const t = S.wx.t2m.times, sl = $("slider");
  sl.min = t[0]; sl.max = t.at(-1); sl.step = 1800;
  const days = [];
  for (let s = Math.ceil(t[0] / 86400) * 86400; s <= t.at(-1); s += 86400) days.push(s);
  S.tickDays = { days, t0: t[0], t1: t.at(-1) };
  renderTicks();
  $("nowmark").style.left = `${Math.min(100, Math.max(0, ((nowSec() - t[0]) / (t.at(-1) - t[0])) * 100))}%`;
}
/** Day labels on the time bar, thinned to the bar's measured width (placement.js tickStep); again on every resize. */
function renderTicks() {
  const el = $("ticks"), k = S.tickDays;
  if (!k) return;
  const labels = k.days.map((s) => fmtTime(s, { time: false }));
  const ctx = (renderTicks.ctx ??= document.createElement("canvas").getContext("2d"));
  ctx.font = getComputedStyle(el).font;
  const labelPx = Math.max(...labels.map((l) => ctx.measureText(l).width), 1);
  const step = tickStep(k.days.length, el.clientWidth, labelPx);
  el.innerHTML = k.days.map((s, i) => `<span style="left:${((s - k.t0) / (k.t1 - k.t0)) * 100}%">${step && i % step === 0 ? labels[i] : ""}</span>`).join("");
}
addEventListener("resize", () => renderTicks());
function setTime(sec) {
  S.timeSec = sec;
  const wx = S.wx;
  if (wx) {
    const ts = wx.t2m.times, clamped = Math.min(ts.at(-1), Math.max(ts[0], sec)), step = stepAt(ts, clamped);
    $("slider").value = clamped;
    // each region at its own step for this time (the grids' time steps differ); the wind's grids in the same order
    (wx.grids || [wx]).forEach((g) => {
      const t = g.t2m.times, st = stepAt(t, Math.min(t.at(-1), Math.max(t[0], clamped)));
      fieldFor(g.sfx || "").t = st;
      const wg = wind.grids?.find((w) => w.geom === g.t2m.geom); if (wg) wg.t = st;
    });
    wind.t = step;
    $("when").textContent = `WX ${fmtTime(clamped, { dow: true })} ${tzAbbr()}`;
  }
  const off = Math.abs(sec - nowSec()) > OFF_NOW_S;
  document.body.classList.toggle("off-now", off);
  $("netlive").textContent = `NETWORK: LIVE ${fmtTime(nowSec(), { date: false })} ${tzAbbr()}`;
  if (map.getLayer("gas")) {
    const o = off ? 0.45 : 1;
    for (const [id, prop] of [["gas", "line-opacity"], ["gas-casing", "line-opacity"], ["power", "line-opacity"],
      ["power-casing", "line-opacity"]]) map.setPaintProperty(id, prop, o);
  }
  applyDots();
  map.triggerRepaint();
}
/** Node, region and alert-pin visibility for the current level zoom, dimmed off "now". */
/** PLAN 10b frame budget: the frame and smoke layers hold only the movers shown at this level (style.js animFilter),
 * set again only when that set changes (a level change across a mover's zmin, or new data). */
function applyAnimFilter() {
  if (!map.getLayer("nodes")) return;
  const key = (S.animZ || []).filter((z) => z <= S.ze).length;
  if (key === S.animKey) return;
  S.animKey = key;
  const ze = (S.animZ || [])[key - 1] ?? -1;   // the highest shown zmin: the filter's threshold until the next change
  for (const id of ANIM_LAYERS) map.setFilter(id, animFilter("spr_u", ze));
  for (const id of SMOKE_LAYERS) map.setFilter(id, animFilter("smk_u", ze));
}
function applyDots() {
  if (!map.getLayer("nodes")) return;
  applyAnimFilter();
  const k = document.body.classList.contains("off-now") ? 0.45 : 1;
  rivers.opacity = k; map.triggerRepaint();
  const pr = map.getPixelRatio();
  for (const [id, paint] of Object.entries(dotPaint(k, S.ze, pr, { on: !!S.animOn, t: S.animT || 0 })))
    for (const [prop, v] of Object.entries(paint)) map.setPaintProperty(id, prop, v);
  // 16 px sprites below SPRITE_32_Z, 32 px from it (same level zoom, so the swap can't flicker either). The badge
  // offset follows the sprite's real CSS size, which also changes with the display's pixel ratio.
  const key = `${S.ze >= SPRITE_32_Z}|${pr}`;
  if (S.spriteKey !== key) { S.spriteKey = key; map.setLayoutProperty("nodes", "icon-image", nodeIcon(S.ze));
    ANIM_LAYERS.forEach((id, i) => map.setLayoutProperty(id, "icon-image", animIcon(i, S.ze)));
    FIRE_LAYERS.forEach((id, i) => map.setLayoutProperty(id, "icon-image", fireIcon(i, S.ze)));
    SMOKE_LAYERS.forEach((id, i) => { map.setLayoutProperty(id, "icon-image", smokeIcon(i, S.ze));
      map.setLayoutProperty(id, "icon-offset", smokeOffset(S.ze, pr)); });
    map.setLayoutProperty("node-badges", "icon-offset", badgeOffset(S.ze, pr)); }
}

// ------------------------------------------------------------------ sprite motion (STYLE.md: one shared clock)
// Wind rotors and compressor ticks advance on this single 4 Hz clock (at most 4 frame updates/s per feature, no
// per-feature timers). Each tick changes only the frame layers' paint (style.js animPaint), never the layout. Motion
// runs only while a moving sprite is on screen, the view is live (not off "now"), the tab is visible and the user
// hasn't asked for reduced motion; otherwise the static image is drawn once and nothing repaints.
const reduceMotion = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)");
/** Why motion is off right now, or "on". The first failing condition wins, so a stalled clock names its cause. */
function animReason() {
  if (reduceMotion?.matches) return "reduced-motion";
  if (document.hidden) return "tab-hidden";
  if (power.rate("clock") === 0) return "paused";   // L-080: only when hidden (saver keeps a 1 Hz clock)
  if (document.body.classList.contains("off-now")) return "off-now";
  const shown = (S.animPts || []).filter((p) => p.z <= S.ze);   // PLAN 10b: only movers drawn at this level
  if (!shown.length) return "no-movers";
  const b = map.getBounds();
  return shown.some((p) => b.contains(p.c)) ? "on" : "movers-off-screen";
}
// Diagnostics (Reviewer, #111): the current reason and tick count; reason changes are logged in ?dev=1 mode.
window.__motion = { reason: "init", ticks: 0 };
setInterval(() => {
  if (!map.getLayer("nodes")) return;
  const reason = animReason(), on = reason === "on";
  if (reason !== window.__motion.reason) { if (DEV) console.debug(`[motion] ${window.__motion.reason} -> ${reason}`); window.__motion.reason = reason; }
  if (!on && !S.animOn) return;            // already static: no work, no repaint
  // idle (L-080): 1 Hz instead of 4 (every 4th tick), so a map left open repaints a quarter as often
  if (on && S.animOn && (S.clockN = (S.clockN || 0) + 1) % Math.max(1, Math.round(4 / power.rate("clock")))) return;
  window.__motion.ticks++;
  S.animOn = on; S.animT = performance.now() / 1000;
  const k = document.body.classList.contains("off-now") ? 0.45 : 1;
  const paint = dotPaint(k, S.ze, map.getPixelRatio(), { on, t: S.animT });
  for (const id of ["nodes", ...ANIM_LAYERS, ...SMOKE_LAYERS, ...FIRE_LAYERS]) map.setPaintProperty(id, "icon-opacity", paint[id]["icon-opacity"]);
}, ANIM_TICK_MS);

// ------------------------------------------------------------------ HUD
function buildHud() {
  if (DEV) $("devbanner").hidden = false;
  const box = $("layers");
  for (const l of config.layers) S.layers[l.id] = !!l.on;
  box.innerHTML = '<div class="h">LAYERS</div>' + config.layers.map((l) =>
    `<label><input type="${l.group ? "radio" : "checkbox"}" ${l.group ? `name="${l.group}"` : ""} data-l="${l.id}" ${l.on ? "checked" : ""}> ${esc(l.label)}</label>`).join("") +
    (config.layers.some((l) => l.group === "field") ? '<label><input type="radio" name="field" data-l="nofield"> No field</label>' : "") +
    '<label class="op">opacity <input type="range" id="opacity" min="0.2" max="0.9" step="0.05" value="0.6"></label>' +
    // L-080: slower animations, frozen sprites, a lighter wind field, half the snapshot polling
    '<label class="bs" title="Slower animations and less polling. Automatic on phones and on battery; tick to choose."><input type="checkbox" id="saver"> Battery saver <span class="dim" id="saverauto"></span></label>';
  box.addEventListener("change", (e) => {
    const l = e.target.dataset.l; if (!l) return;
    if (e.target.type === "radio") { for (const x of config.layers.filter((y) => y.group === "field")) S.layers[x.id] = x.id === l; S.layers.nofield = l === "nofield"; }
    else S.layers[l] = e.target.checked;
    applyLayers();
  });
  $("opacity").oninput = (e) => { for (const f of fieldLayers()) f.opacity = +e.target.value; map.triggerRepaint(); };
  const saverUi = () => { $("saver").checked = power.saver; $("saverauto").textContent = power.auto ? "(auto)" : ""; };
  $("saver").onchange = (e) => power.setSaver(e.target.checked);
  power.onChange(saverUi); saverUi();
  applyLayers();
  renderLegend();
  setTime(nowSec());
  $("slider").addEventListener("input", (e) => setTime(+e.target.value));
  $("now").onclick = () => setTime(nowSec());
  // Forecast playback: a frame loop only while playing (L-080: it used to spin at 60 Hz all the time)
  let playing = false, looping = false;   // one loop at most: pause then play inside a frame must not start a second
  const tick = () => {
    if (window.__freeze || !playing) { looping = false; return; }
    if (S.wx) { const t = S.wx.t2m.times; let s = S.timeSec + 1800; if (s > t.at(-1)) s = t[0]; setTime(s); }
    requestAnimationFrame(tick);
  };
  $("play").onclick = (e) => { playing = !playing; e.currentTarget.innerHTML = playing ? "&#10074;&#10074;" : "&#9654;";
    if (playing && !looping) { looping = true; tick(); } };
  // Hover readout: field value under the cursor (terrain-adjusted when the downscale is on)
  map.on("mousemove", (e) => {
    const wx = S.wx, hv = $("hover");
    if (!wx || S.layers.nofield) { hv.hidden = true; return; }
    const { lng, lat } = e.lngLat;
    // the region covering this point (L-080: the east grid past −95°)
    const g = (wx.grids || [wx]).find((x) => sampleGrid(x.t2m.geom, x.t2m.data, lng, lat, 0) != null);
    if (!g) { hv.hidden = true; return; }
    const lay = fieldFor(g.sfx || ""), step = lay.t;
    let t = sampleGrid(g.t2m.geom, g.t2m.data, lng, lat, step);
    if (t == null) { hv.hidden = true; return; }
    let adj = "";
    if (lay.lapse && g.z) { const zg = sampleGrid(g.z.geom, g.z.data, lng, lat, 0), zd = demAt(S.dems?.[g.sfx || ""] || S.dem, lng, lat);
      if (zg != null && zd != null) { t += lay.lapse * (zg - zd); adj = " (terrain-adj.)"; } }
    let w = "";
    if (g.u && g.v) { const u = sampleGrid(g.t2m.geom, g.u.data, lng, lat, step), v = sampleGrid(g.t2m.geom, g.v.data, lng, lat, step);
      if (u != null) { const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"], from = (Math.atan2(-u, -v) * 180 / Math.PI + 360) % 360;
        w = ` · ${fmt(Math.hypot(u, v) * 3.6, 0)} km/h ${dirs[Math.round(from / 45) % 8]}`; } }
    hv.hidden = false; hv.textContent = `${fmt(t, 1)} °C${adj}${w}`;
    hv.style.transform = `translate(${e.point.x + 14}px,${e.point.y + 14}px)`;
  });
  map.getCanvas().addEventListener("mouseleave", () => ($("hover").hidden = true));
}
function applyLayers() {
  const L = S.layers, vis = (on) => (on ? "visible" : "none");
  if (!map.getLayer("gas")) return;
  for (const f of fieldLayers()) if (map.getLayer(f.id)) map.setLayoutProperty(f.id, "visibility", vis(!L.nofield && (L.temp || L.windspeed)));
  wind.on = !!L.wind; wind.wake();
  ["gas-casing", "gas", "gas-flanges"].forEach((id) => map.setLayoutProperty(id, "visibility", vis(L.gas)));
  ["power-casing", "power", "power-joints"].forEach((id) => map.setLayoutProperty(id, "visibility", vis(L.power)));
  ["areas-fill", "areas-line"].forEach((id) => map.setLayoutProperty(id, "visibility", vis(L.areas)));
  if (map.getLayer("gauges")) map.setLayoutProperty("gauges", "visibility", vis(L.gauges));
  if (map.getLayer("river-flow")) map.setLayoutProperty("river-flow", "visibility", vis(L["river-flow"]));
  PIN_LAYERS.forEach((id) => map.setLayoutProperty(id, "visibility", vis(L.events)));
  map.setLayoutProperty("hillshade", "visibility", vis(L.terrain));
  $("overlay").hidden = !L.callouts;
  if (S.wellsShown !== (L.wells !== false)) { S.wellsShown = L.wells !== false; if (S.wells) paintNetwork(); }   // L-106 8b
  applyField();
  callouts?.queue();
}
function renderLegend() {
  const r = S.layers.windspeed && S.wx?.speed ? RAMPS.wind : RAMPS.t2m, lo = r.stops[0][0], hi = r.stops.at(-1)[0];
  $("legend").innerHTML = `<div class="lab"><span>${r.label}${S.wx ? "" : " (no grid yet)"}</span><span>${r.unit}</span></div>
    <div class="bar" style="background:${rampCss(r.stops)}"></div>
    <div class="ticksl">${r.ticks.map((v) => `<span style="left:${((v - lo) / (hi - lo)) * 100}%">${v}</span>`).join("")}</div>
    <div class="row"><span class="sw gas"></span>GAS: width = flow, colour = % of capacity</div>
    <div class="row"><span class="sw pow"></span>POWER: colour = % of rating</div>
    <div class="row">RIVERS: flow vs normal ${["very low", "low", "normal", "high", "very high"].map((b) =>
      `<span class="gsw" title="${b}" style="background:${BAND_COLOR[b]}"></span>`).join("")} (dark = no result, hollow = seasonal)</div>
    <div class="row schem">Lines are SCHEMATIC (straight between cited points), not true routes</div>`;
}
// NGTL gas day D starts 08:00 MST = fixed 15:00 UTC (TC bulletin 3374725064): gas day = date(now_utc - 15 h).
function gasDay(sec) {
  const h = config.gas_day?.start_hour_utc;
  if (h == null) return null;
  return new Date((sec - h * 3600) * 1000).toISOString().slice(0, 10);
}
/** /api/status: the time zone, the ☰ menu's sources and the runner badge; a 304 keeps them (L-081 ETag). */
async function loadStatus() {
  const st = await api("/api/status", { etag: S.stTag, key: "status" });
  if (st.status === 304) return;
  S.stTag = st._etag || null;
  setTz(st.tz); renderStatus(st);
}
function renderClock() {
  const gd = gasDay(nowSec());
  $("clock").innerHTML = `${gd ? `<b>GAS DAY ${gd}</b> · ` : ""}<b>${fmtTime(nowSec(), { dow: true })} ${tzAbbr()}</b> <span id="asof">${S.asof || ""}</span>`;
}
function renderRunner(runner) {
  const rb = runnerBadge(runner);   // only on Postgres; nothing while the runner is running
  $("runner").hidden = !rb;
  if (rb) { $("runner").className = `runner ${rb.cls}`; $("runner").textContent = rb.text; $("runner").title = rb.title; }
}
function renderStatus(st) {
  renderClock();
  sysMenu.setStatus(st);   // the sources' health lives in the ☰ menu's Sources tab now (L-097), not as header dots
  renderRunner(st.runner);
}

// ------------------------------------------------------------------ selection + clicks
function select(id) {
  if (S.selected) map.setFeatureState({ source: "net", id: S.selected }, { sel: false });
  S.selected = id && S.byId.has(id) ? id : null;
  if (S.selected) map.setFeatureState({ source: "net", id: S.selected }, { sel: true });
}
async function openFeature(id) {
  const f = S.byId.get(id); if (!f) return;
  await ensureFull([id]);   // a feature without a box at this tier has only a lite entry (L-081 ?features=)
  const p = f.properties, e = entryOf(S.snap, id);
  select(id);
  // PLAN 9 part 3: a generated region (system region) opens its region page (web/regionpage.js, GET /api/region/<id>)
  if (p.system === "region") { panel.close(); regionPage.open(p.region); return; }
  const series = [p.value, p.capacity, ...(p.panel?.series || [])].filter((s, i, a) => typeof s === "string" && a.indexOf(s) === i);
  // An estimated feature's headline carries its band or its lo–hi range (panel.js headlineHtml): never a bare number.
  let est = p.estimate ? { lo: S.snap?.series?.[p.estimate.lo]?.v ?? null, hi: S.snap?.series?.[p.estimate.hi]?.v ?? null,
    basis: p.estimate.basis } : null;
  // L-106 (Power #422; Reviewer on #447): an inventory plant drawn from its BA + fuel's est. utilisation says so, and says
  // which hour: EIA-930 posts 11-38 h behind, so the glow is that hour's, not now. Its history is the utilisation's.
  let entry = e, status;
  const ue = e?.v == null && p.util_est ? S.snap?.series?.[p.util_est] : null;
  if (ue?.v != null) {
    const u = Math.max(0, Math.min(ue.v, 1)), np = p.nameplate_mw;
    entry = { ...ue, series: p.util_est, v: np != null ? u * np : null, unit: "MW", cap: np ?? null };
    est = { range: false, basis: `Estimate, not this plant's measured output: its balancing authority's ${String(p.fuel_group || p.fuel || "").replace(/_/g, " ")} ` +
      `utilisation (${p.ba ?? "BA"}, EIA-930) ${Math.round(u * 100)} %${np != null ? ` × ${fmt(np)} MW nameplate` : ""}, for the hour of ${fmtTime(ue.ts)} ${tzAbbr()} (${ago(ue.ts)} ago).` };
    status = "estimate from EIA-930's latest published hour (not live)";
    series.unshift(p.util_est);
  }
  // PLAN 10d (Erik: "make the fuel type obvious"): a plant's panel names its fuel in the header ("OIL PLANT"), beside
  // the name (a chip with its map sprite) and as the first fact; the region page's fuel words, "fuel unknown" if none
  const fuel = p.type === "plant" ? plantFuel(p) : null;
  const kind = fuel && spriteKind(p, config.overrides?.[id]?.sprite);
  const spr = kind && S.nodeAtlas && Object.entries(S.nodeAtlas).find(([k]) => k.startsWith(`${kind}-16-`))?.[1];
  const kicker = fuel ? `${fuel.key ? `${fuel.word} plant` : "power plant · fuel unknown"} · ${id}` : `${p.system} ${p.type} · ${id}`;
  // PLAN 10c: out of service (capability 0) or idle but available, in the source's own word for its capacity series;
  // a feature `basis` (Power #537: the US nuclear plants' NRC daily level) says what the number is, where an estimate's would
  const ci = capacityInfo(p.capacity, config.capacity_words);
  const plantState = outOfService(p, entry) ? { off: true, text: `Out of service (${ci.word} 0)` }
    : notProducing(p, entry) ? { off: false, text: zeroText(ci, entry.cap_current ?? entry.cap, entry.unit || "MW", fmt) } : null;
  if (!est && p.basis) est = { range: false, basis: p.basis, reading: true };
  if (est?.reading && !status) status = `${String(entry?.freq).toUpperCase() === "D" ? "a daily reading" : "a reading"}, not metered MW`;
  panel.open({ kicker: kicker.toUpperCase(), title: p.name, entry, feature: { ...p, kind: p.kind },
    fuel: fuel && { ...fuel, sprite: spr ? { x: spr.x, y: spr.y, w: spr.width, h: spr.height } : null }, plantState,
    band: p.band, estimate: est, status, sign: flowSign(p.flow, p.value),
    series, labels: series.map((sid) => (sid === p.value ? p.name : sid === p.capacity ? "capacity"
      : sid === p.util_est ? `est. utilisation (${p.ba ?? "BA"} ${p.fuel_group ?? ""}, fraction)` : undefined)),   // others: the series' own label
    eventsRef: p.panel?.events_ref || id, action: producerAction(p.value), tieout: tieoutFor(p.value) });
  callouts?.queue();
}
// Top producers (L-075): a panel showing a series that map.json `producers` maps to a province offers the popup
const producers = (window.__producers = new Producers());   // diagnostics handle, like window.__map
const regionPage = (window.__regionPage = new RegionPage());   // PLAN 9 part 3: the region page (diagnostics handle too)
// AB + BC tie-out (L-079): a panel showing a map.json `tieouts` trigger series adds that tie-out view (web/tieout.js)
// the gas day's start hour rides along, so tieout.js can tell a preliminary (still revised) day from its age
const tieoutFor = (series) => { const c = config.tieouts?.[baseSeries(series)];
  return c && { ...c, gas_day_start_h: config.gas_day?.start_hour_utc ?? 0 }; };
function producerAction(series) {
  const prov = config.producers?.[baseSeries(series)];
  return prov ? { label: `Top producers in ${PROVINCES[prov] || prov} ›`, onClick: () => producers.open(prov) } : undefined;
}
// Search (L-076): every named thing on the map, plus the top producers (fetched once, on first use). Built from what is
// loaded, so new features are found without a list. Picking flies there and opens what a click would.
const humanType = (p) => `${p.system ? `${p.system[0].toUpperCase()}${p.system.slice(1)} ` : ""}${String(p.type || p.kind || "feature").replace(/_/g, " ")}`;
const dotIcon = (cls) => `<i class="si ${cls}"></i>`;
const bboxOf = (g) => { const pts = g.type === "Point" ? [g.coordinates] : g.type === "LineString" ? g.coordinates
  : g.type === "MultiLineString" || g.type === "Polygon" ? g.coordinates.flat() : g.coordinates.flat(2);
  const xs = pts.map((c) => c[0]), ys = pts.map((c) => c[1]); return [[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]]; };
function flyThen(target, then) {
  const go = () => { map.off("moveend", go); then?.(); };
  map.on("moveend", go);
  if (Array.isArray(target[0])) map.fitBounds(target, { padding: 80, maxZoom: 8, duration: 900 });
  else map.flyTo({ center: target, zoom: Math.max(map.getZoom(), 8), duration: 900 });
}
let topProducers = null;
async function searchItems() {
  const items = [];
  for (const [id, f] of S.byId) {
    const p = f.properties;
    if (!p.name) continue;
    const at = f.geometry.type === "Point" ? anchorPoint(id) : bboxOf(f.geometry);
    items.push({ label: p.name, group: humanType(p), sub: p.operator || id, keys: [id, p.operator, p.fuel, p.station].filter(Boolean),
      icon: dotIcon(p.system || "other"), pick: () => flyThen(at, () => { openBox(id); openFeature(id); }) });
  }
  for (const f of S.gaugeFc?.features || []) {
    const p = f.properties;
    items.push({ label: p.name, group: "River gauge", sub: `${p.station} · ${p.basin}`, keys: [p.station, p.basin, p.wsc_name],
      icon: dotIcon("gauge"), pick: () => flyThen(f.geometry.coordinates, () => openGauge(p)) });
  }
  for (const f of S.riverFc?.features || [])
    if (f.properties.name) items.push({ label: f.properties.name, group: "River", sub: "", keys: [f.properties.id], icon: dotIcon("river"),
      pick: () => flyThen(bboxOf(f.geometry)) });
  for (const c of config.callouts || []) {
    if (!c.anchor?.at || !c.anchor.label) continue;
    const z = Math.min(map.getZoom(), (c.maxzoom ?? 99) - 0.5);
    items.push({ label: c.anchor.label, group: "Box", sub: c.rows?.[0]?.label || "", keys: c.rows.map((r) => r.label), icon: dotIcon(c.system || "other"),
      pick: () => { map.flyTo({ center: c.anchor.at, zoom: z, duration: 900 }); callouts?.expanded.add(c.id); callouts?.queue(); } });
  }
  // the top producers (Gas's /api/top-producers): a well or facility opens in the top-producers popup, selected
  topProducers ??= Promise.all(Object.keys(PROVINCES).map((prov) => api(`/api/top-producers?province=${prov}`)
    .then((fc) => ({ prov, fc })).catch(() => ({ prov, fc: null }))));
  for (const { prov, fc } of await topProducers)
    for (const f of fc?.features || []) {
      const p = f.properties;
      items.push({ label: p.name || p.id, group: `Top producers · ${PROVINCES[prov]}`, keys: [p.id, p.operator, p.location].filter(Boolean),
        sub: `#${p.rank} · ${p.operator || ""} · ${p.latest == null ? "--" : fmt(p.latest, p.latest < 10 ? 1 : 0)} MMcf/d`,
        icon: `<i class="si ${p.kind === "well" ? "well" : "pad"}"></i>`, pick: () => producers.open(prov, p.id) });
    }
  return items;
}
const search = new Search($("search"), { items: searchItems });
function openCallout(c, row) {
  if (row.feature) return openFeature(row.feature);
  if (row.opens) return openFeature(row.opens);   // a daily fallback headline opens its feature's panel (L-078)
  if (c.gauge) return openGauge(c.gauge);   // a gauge box opens the gauge's own panel
  const anchorId = typeof c.anchor === "string" ? c.anchor : null;
  // A row showing its anchor feature's own value opens the feature (capacity, events, its history).
  if (anchorId && !row.ref && S.byId.get(anchorId)?.properties.value === row.series) return openFeature(anchorId);
  select(anchorId);
  const e = S.snap?.series?.[rowKey(row)], ref = row.ref ? S.snap?.series?.[row.ref] : null;
  panel.open({ kicker: anchorId ? `NODE · ${anchorId}` : `REGION · ${c.anchor.label}`, title: row.label || row.series,
    entry: e, row, refEntry: ref, sign: flowSign(row.flow, row.series),
    series: [baseSeries(row.series), row.ref].filter(Boolean), labels: [row.label, row.ref_label],
    feature: anchorId ? S.byId.get(anchorId)?.properties : null, eventsRef: anchorId, action: producerAction(row.series),
    tieout: tieoutFor(row.series) });
  callouts?.queue();
}
/** Click to open (PLAN 4c): a node with a value whose box isn't on screen gets its box pinned at its point. */
function openBox(id) {
  const p = S.byId.get(id)?.properties;
  // L-106 8b: a gas facility (/api/wells) has no box by design (no callouts among ~700 dots): its click opens its panel
  if (!p?.value || p.type === "facility" || S.pinned.has(id) || callouts?.st.get(`f:${id}`)?.shown) return false;
  S.pinned.add(id); select(id); callouts?.update(S.snap);
  ensureFull([id]);   // its box shows once its full entry is in (a lite feature, L-081)
  return true;
}
function wireClicks() {
  // One handler, highest-priority layer wins (a node inside an area opens the node, not the area).
  const order = [...PIN_LAYERS, "nodes", "gauges", ...FIRE_LAYERS, "regions", "gas", "power", "areas-fill"];   // gauges draw below the sprites
  // a finger is wider than a cursor (L-068): on touch screens a tap takes features within 12 px
  const slop = matchMedia("(pointer: coarse)").matches ? 12 : 0;
  map.on("click", (e) => {
    const at = slop ? [[e.point.x - slop, e.point.y - slop], [e.point.x + slop, e.point.y + slop]] : e.point;
    // Dots hidden at this zoom (opacity 0) are still "rendered": skip them so they can't be clicked.
    const hits = map.queryRenderedFeatures(at, { layers: order.filter((l) => map.getLayer(l)) })
      .filter((h) => PIN_LAYERS.includes(h.layer.id) ? pinShown(h.properties.severity, S.ze)
        : !["nodes", "regions"].includes(h.layer.id) || featureShown(h.properties.id));
    if (!hits.length) return;
    hits.sort((a, b) => order.indexOf(a.layer.id) - order.indexOf(b.layer.id));
    const h = hits[0];
    if (PIN_LAYERS.includes(h.layer.id) || FIRE_LAYERS.includes(h.layer.id)) openEvent(S.events.find((x) => x.id === h.properties.id));
    else if (h.layer.id === "gauges") openGauge(h.properties);
    else if (h.layer.id === "nodes" && openBox(h.properties.id)) return;
    else openFeature(h.properties.id);
  });
  for (const id of order) {
    const shown = PIN_LAYERS.includes(id) ? (f) => pinShown(f.properties.severity, S.ze)
      : id === "nodes" || id === "regions" ? (f) => featureShown(f.properties.id) : () => true;
    map.on("mousemove", id, (e) => { map.getCanvas().style.cursor = e.features.some(shown) ? "pointer" : ""; });
    map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
  }
}
