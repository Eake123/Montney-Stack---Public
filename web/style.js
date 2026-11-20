// Our own basemap style (frontend.md §1): dark ground, field under hillshade, water, borders and labels.
// Data layers (network, events) are added by map.js after the network loads.
import { TERRARIUM } from "./weather.js";
import { BREAKOUT_Z } from "./callouts.js";
import { SPRITE_32_Z, spriteCss, ANIM_FRAMES, SMOKE_OFFSET } from "./sprites.js";
import { GAUGE_RING } from "./gauges.js";

// Required attribution (docs/ARCHITECTURE.md §6: every basemap/terrain/data source).
const OFM = '<a href="https://openfreemap.org">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/">OpenMapTiles</a> ' +
  'Data from <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
// tilezen/joerd docs/attribution.md "Required attribution", the lines that apply to our bounds and DEM zooms.
const TERRAIN = 'Terrain: <a href="https://registry.opendata.aws/terrain-tiles/">Terrain Tiles</a> ' +
  '(<a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">sources</a>): ' +
  "Canada terrain data contains information licensed under the Open Government Licence – Canada; " +
  "United States 3DEP (formerly NED) and global GMTED2010 and SRTM terrain data courtesy of the U.S. Geological Survey; " +
  "Global ETOPO1 terrain data U.S. National Oceanic and Atmospheric Administration; " +
  "ArcticDEM terrain data DEM(s) were created from DigitalGlobe, Inc., imagery and funded under National Science Foundation awards 1043681, 1559691, and 1542736";
export const DATA_CREDITS = [
  'Weather overlay: <a href="https://www.nco.ncep.noaa.gov/pmb/products/gfs/">NOAA GFS</a> (public domain; not endorsed by NOAA)',
  'Point forecasts: <a href="https://open-meteo.com/">Weather data by Open-Meteo.com</a> (CC BY 4.0)',
  'Gas: <a href="https://www.tcenergy.com/">TC Energy</a> (NGTL)',
  'Power: <a href="https://www.aeso.ca/">AESO</a>',
];

export function buildStyle() {
  return {
    version: 8,
    // no paint transitions (L-080): the 4 Hz sprite clock changes icon-opacity, and MapLibre's default 300 ms fade made
    // every tick a transition, so the map re-rendered at 60 fps for good. The frame flips were never meant to fade.
    transition: { duration: 0, delay: 0 },
    glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
    sources: {
      omt: { type: "vector", url: "https://tiles.openfreemap.org/planet", attribution: OFM },
      dem: { type: "raster-dem", encoding: "terrarium", tileSize: 256, maxzoom: 10, tiles: [TERRARIUM], attribution: TERRAIN },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#16201b" } },
      // ---- the weather field (custom layer) is inserted here, before "hillshade"
      { id: "hillshade", type: "hillshade", source: "dem", paint: { "hillshade-exaggeration": 0.7,
        "hillshade-shadow-color": "rgba(0,0,0,0.62)", "hillshade-highlight-color": "rgba(255,255,255,0.10)",
        "hillshade-accent-color": "rgba(0,0,0,0.25)", "hillshade-illumination-direction": 315 } },
      { id: "water", type: "fill", source: "omt", "source-layer": "water", paint: { "fill-color": "rgba(10,22,32,0.72)" } },
      { id: "water-edge", type: "line", source: "omt", "source-layer": "water", minzoom: 5,
        paint: { "line-color": "rgba(150,200,215,0.25)", "line-width": 0.6 } },
      { id: "rivers", type: "line", source: "omt", "source-layer": "waterway", filter: ["==", ["get", "class"], "river"],
        paint: { "line-color": "rgba(14,34,48,0.8)", "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.5, 8, 1.4] } },
      { id: "prov", type: "line", source: "omt", "source-layer": "boundary", filter: ["==", ["get", "admin_level"], 4],
        paint: { "line-color": "rgba(10,16,13,0.75)", "line-width": 1.1, "line-dasharray": [3, 2] } },
      { id: "country", type: "line", source: "omt", "source-layer": "boundary", filter: ["==", ["get", "admin_level"], 2],
        paint: { "line-color": "rgba(10,16,13,0.9)", "line-width": 1.5 } },
      // ---- network layers are inserted here, before "places"
      { id: "places", type: "symbol", source: "omt", "source-layer": "place", filter: ["==", ["get", "class"], "city"],
        layout: { "text-field": ["upcase", ["coalesce", ["get", "name_en"], ["get", "name"]]], "text-font": ["Noto Sans Regular"],
          "text-size": 9, "text-letter-spacing": 0.08, "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 0.6 },
        paint: { "text-color": "#e2efe8", "text-halo-color": "rgba(2,5,4,0.85)", "text-halo-width": 1.3 } },
    ],
  };
}

/** Network + event layers (sources "net" and "events" must exist). Inserted below place labels. */
// Dots appear at their feature's zoom tier (`zmin`, set in map.js from the callout tier). The test is against the
// hysteretic level zoom `ze` (placement.js levelZoom), not the raw zoom, so dots and callout boxes switch together
// and don't flicker at a boundary. map.js re-applies dotPaint whenever the level changes.
const atLevel = (x, ze) => ["case", ["<=", ["coalesce", ["get", "zmin"], 0], ze], x, 0];
// Alerts: only critical pins on the overview; the rest appear with the breakdown (callouts.js BREAKOUT_Z).
const pinOpacity = (ze) => (ze >= BREAKOUT_Z ? 1 : ["case", ["==", ["get", "severity"], "critical"], 1, 0]);
export const pinShown = (severity, ze) => severity === "critical" || ze >= BREAKOUT_Z;
/** Node sprite image at level zoom `ze`: `<kind>-<16|32>-<state>` (web/sprites.js sets spr_kind/spr_state). */
export const nodeIcon = (ze = 0) => ["concat", ["get", "spr_kind"], ze >= SPRITE_32_Z ? "-32-" : "-16-", ["get", "spr_state"]];
/** Frame layers for moving sprites (every producing plant kind, L-108, and compressors): one per frame, so the shared
 * clock only changes paint (cheap, no symbol re-layout). Image = `<kind>-<16|32>-<set><k>`, the set from sprites.js
 * plantMotion: "frame", or a direction ("charge"/"discharge", "pump"/"generate") that already runs the right way. */
export const ANIM_LAYERS = Array.from({ length: ANIM_FRAMES }, (_, k) => `nodes-anim-${k}`);
export const animIcon = (k, ze = 0) => ["concat", ["get", "spr_kind"], ze >= SPRITE_32_Z ? "-32-" : "-16-",
  ["coalesce", ["get", "spr_set"], "frame"], `${k}`];
/**
 * The frame and smoke layers' filter at level zoom `ze` (PLAN 10b frame budget): only movers whose sprite is shown at
 * this level (`zmin`), so the shared clock's paint changes never touch the thousands of plants too small to be drawn
 * (at the floor zoom, none of the inventory). map.js sets it again only when the set of shown movers changes.
 */
export const animFilter = (prop, ze = Infinity) => ["all", ["==", ["get", "kind"], "node"],
  ...(prop === "spr_u" ? [["has", "spr_kind"], ["!=", ["coalesce", ["get", "spr_u"], 0], 0]] : [[">", ["coalesce", ["get", "smk_u"], 0], 0]]),
  ["<=", ["coalesce", ["get", "zmin"], 0], ze]];
/** sprites.js frameAt(u, t) as an expression on the feature's signed speed `spr_u` at clock time `t` (a literal). */
export function animFrame(t, prop = "spr_u") {
  const f = ["%", ["floor", ["*", ANIM_FRAMES, ["abs", ["get", prop]], t]], ANIM_FRAMES];
  return ["case", ["<", ["get", prop], 0], ["-", ANIM_FRAMES - 1, f], f];
}
/** Wildfire (Art's docs/art/WILDFIRE.md): one layer per flicker frame; image `wildfire/<hotspot|perimeter>-<16|32>-<k>`. */
export const FIRE_LAYERS = Array.from({ length: ANIM_FRAMES }, (_, k) => `fire-${k}`);
export const FIRE_U = 0.5;   // flicker: 2 frames/s, a fixed rate (Art: never encodes intensity, size or spread)
export const fireIcon = (k, ze = 0) => ["concat", "wildfire/", ["get", "fire"], ze >= SPRITE_32_Z ? "-32-" : "-16-", `${k}`];
/** Gas-plant smoke (PLAN 4b): one layer per frame like the rotors; image `smoke/<normal|low>-<16|32>-<k>`. */
export const SMOKE_LAYERS = Array.from({ length: ANIM_FRAMES }, (_, k) => `smoke-${k}`);
export const smokeIcon = (k, ze = 0) => ["concat", "smoke/", ["get", "smk_var"], ze >= SPRITE_32_Z ? "-32-" : "-16-", `${k}`];
/** The puff's offset on its plant's stack (sprites.js SMOKE_OFFSET), in CSS px at the sprite's real size. */
export const smokeOffset = (ze = 0, pr = 1) => { const n = ze >= SPRITE_32_Z ? 32 : 16, c = spriteCss(n, pr) / n;
  return ["match", ["get", "spr_kind"], ...Object.entries(SMOKE_OFFSET).flatMap(([k, o]) => [k, ["literal", [o[n][0] * c, o[n][1] * c]]]),
    ["literal", [0, 0]]]; };
/** The `unknown` badge is always the 16 px one, centred on the sprite's top-right corner (16 or 32 px sprite). */
export const badgeOffset = (ze = 0, pr = 1) => { const s = spriteCss(ze >= SPRITE_32_Z ? 32 : 16, pr);
  return ["case", ["==", ["get", "spr_anchor"], "bottom"], ["literal", [s / 2 - 1, -s + 1]], ["literal", [s / 2 - 1, -s / 2 + 1]]]; };
const selRing = (k, ze) => ["case", ["boolean", ["feature-state", "sel"], false], atLevel(k, ze), 0];
/** Node sprite, badge, selection ring, region and alert-pin opacity at level zoom `ze`. Scaled by `k` (map.js dims the
 * network to 0.45 off "now"). */
/**
 * Opacities for the moving sprites at clock time `t`. While `on`, a moving feature (spr_u != 0) is drawn only by the
 * frame layer whose frame is current, and the main `nodes` layer hides it. While off (historical view, reduced
 * motion, hidden tab, nothing on screen moving) the main layer draws its static image and the frame layers show nothing.
 */
export function animPaint(nodeA, on, t = 0) {
  const moving = ["!=", ["coalesce", ["get", "spr_u"], 0], 0];
  return {
    nodes: { "icon-opacity": on ? ["case", moving, 0, nodeA] : nodeA },
    ...Object.fromEntries(ANIM_LAYERS.map((id, k) => [id, { "icon-opacity": on ? ["case", ["==", animFrame(t), k], nodeA, 0] : 0 }])),
    // smoke only while motion is on (live, visible, no reduced motion): off, there's no plume at all, never a frozen one
    ...Object.fromEntries(SMOKE_LAYERS.map((id, k) => [id, { "icon-opacity": on ? ["case", ["==", animFrame(t, "smk_u"), k], nodeA, 0] : 0 }])),
  };
}
export function dotPaint(k = 1, ze = 0, pr = 1, anim = { on: false, t: 0 }) {
  const nodeA = atLevel(["case", ["get", "context"], 0.5 * k, k], ze), s = spriteCss(ze >= SPRITE_32_Z ? 32 : 16, pr);
  return {
    ...animPaint(nodeA, anim.on, anim.t),
    // fire: flicker while motion is on (a fixed rate; frame = floor(4·FIRE_U·t) mod 4), a still frame 0 under reduced
    // motion or a hidden tab, and nothing off "now" (k < 1): a past view must not show today's fires
    ...Object.fromEntries(FIRE_LAYERS.map((id, i) => [id, { "icon-opacity": k < 1 ? 0
      : anim.on ? (Math.floor(ANIM_FRAMES * FIRE_U * anim.t) % ANIM_FRAMES === i ? 1 : 0) : i === 0 ? 1 : 0 }])),
    "node-badges": { "icon-opacity": nodeA },
    "node-est": { "text-opacity": nodeA },
    // the selection ring circles the sprite: centred on it (bottom-anchored sprites stand on the point)
    "node-sel": { "circle-stroke-opacity": selRing(k, ze), "circle-radius": s / 2 + 4, "circle-translate": [0, -s / 2] },
    "node-sel-c": { "circle-stroke-opacity": selRing(k, ze), "circle-radius": s / 2 + 4 },
    regions: { "circle-stroke-opacity": atLevel(k, ze) },
    // Alert pins sit off the sprite's top-left corner (the top-right holds the `unknown` badge), by the sprite's real
    // CSS size: above it for sprites standing on the point, beside the top for centred ones (hub, system, links, areas).
    "event-pins": { "circle-opacity": pinOpacity(ze), "circle-stroke-opacity": pinOpacity(ze),
      "circle-translate": [-(s / 2 + 5), -(s - 3)] },
    "event-pins-c": { "circle-opacity": pinOpacity(ze), "circle-stroke-opacity": pinOpacity(ze),
      "circle-translate": [-(s / 2 + 5), -(s / 2 - 3)] },
    "gas-flanges": { "icon-opacity": k },   // dims with the network off "now"
    "power-joints": { "icon-opacity": k },
    // river gauges dim off "now" with the network (Reviewer, #149)
    gauges: { "circle-opacity": ["*", ["get", "g_fillop"], ["get", "g_op"], k], "circle-stroke-opacity": ["*", ["get", "g_op"], k] },
  };
}

// Gas pipe geometry, shared by the casing and the flange overlay (P4c item 3, L-028 G1-G7, docs/art/PIPELINE_FLANGES.md).
export const FLOW_FULL = 8000;            // |MMcf/d| at which line widths stop growing
export const CASING_W = [4, 11];          // gas-casing width (px) at 0 and FLOW_FULL; flowW(4, 11)
export const FLANGE_MIN_STROKE = 3;       // G4: below this rendered casing width the overlay is off (plain line)
export const FLANGE_SPACING = 64;         // screen px between flanges, the same at every zoom
export const POWER_JOINT_SPACING = 96;    // screen px between transmission/intertie joints (power-lines atlas, #129)
const absFlow = ["abs", ["coalesce", ["get", "v"], 0]];
const flowW = (lo, hi) => ["interpolate", ["linear"], absFlow, 0, lo, FLOW_FULL, hi];
/**
 * Flange image from |flow|: height H = casing width rounded up to even, + 2 (casing <= 4 -> 6, 4 < w <= 6 -> 8, ...,
 * 10 < w <= 11 -> 14). `step` switches at >= a stop, so each stop sits just above the flow where the casing reaches an
 * even width: that width itself still gets the lower height.
 */
export function flangeImage([lo, hi] = CASING_W) {
  const flowAt = (w) => ((w - lo) / (hi - lo)) * FLOW_FULL, h = (w) => 2 * Math.ceil(w / 2) + 2, EPS = 1e-6;
  const out = ["step", absFlow, `line/flange-${h(lo)}`];
  for (let w = 2 * Math.ceil(lo / 2); w < hi; w += 2) if (w >= lo) out.push(flowAt(w) + EPS, `line/flange-${w + 4}`);
  return out;
}

/** A region's tint opacity with no weather field drawn (L-106 region colours; map.js applyField drops it under a field). */
export const REGION_TINT = 0.1;
/** The generated region outlines (L-106 8a, network/region/outlines.geojson). */
const REGION_OUTLINE = ["all", ["==", ["get", "kind"], "area"], ["==", ["get", "type"], "region_outline"]];
export function networkLayers() {
  const util = ["coalesce", ["get", "util"], -1];
  const gasColor = ["case", ["<", util, 0], "#9fb3a9",
    ["interpolate", ["linear"], util, 0, "#7f948a", 0.6, "#eef6f2", 0.85, "#ffb547", 1, "#ff4d6d"]];
  const powColor = ["case", ["<", util, 0], "#b9a6db",
    ["interpolate", ["linear"], util, 0, "#7d68a8", 0.6, "#c9a2ff", 0.85, "#ffb547", 1, "#ff4d6d"]];
  const isLink = (sys) => ["all", ["==", ["get", "kind"], "link"], ["==", ["get", "system"], sys]];
  return [
    // Areas stay light so terrain and the weather field read through: a near-transparent fill (it also makes
    // the area clickable) and a dashed outline that turns amber/red as capability fills up.
    // L-106 (Erik: "regional borders around the regions ... give each region a colour"): a faint tint in the region's own
    // colour (`color` on the generated outline, montney/regions.py), the lowest network layer. map.js applyField sets its
    // opacity: REGION_TINT with no weather field drawn, 0 while one is (a tint would shift the temperature colours).
    { id: "region-fill", type: "fill", source: "net", filter: REGION_OUTLINE,
      paint: { "fill-color": ["coalesce", ["get", "color"], "#d3ece0"], "fill-opacity": REGION_TINT, "fill-antialias": false } },
    { id: "areas-fill", type: "fill", source: "net", filter: ["all", ["==", ["get", "kind"], "area"], ["!", REGION_OUTLINE]],
      paint: { "fill-color": ["case", ["<", util, 0.85], "rgba(211,236,224,0.015)",
        ["interpolate", ["linear"], util, 0.85, "rgba(255,181,71,0.05)", 1, "rgba(255,77,109,0.08)"]] } },
    // Pixel joints on transmission and intertie lines (Art #129, roadmap step 3; L-028 G1-G7). An overlay only: the power
    // paints are untouched. Drawn BELOW every line layer (areas-line included), so the wire runs through each joint's transparent 6 px corridor
    // and any crossing line draws over it: a joint can never cover a data-line pixel. 16 px at every zoom (the 32 px
    // corridor is 12 px, which would float the clips 4 px off the fixed 4 px power casing). Pitch `viewport` keeps NEAREST.
    { id: "power-joints", type: "symbol", source: "net", filter: isLink("power"),
      layout: { "symbol-placement": "line", "symbol-spacing": POWER_JOINT_SPACING,
        "icon-image": ["case", ["==", ["get", "type"], "intertie"], "line/intertie-joint-16", "line/power-joint-16"],
        "icon-rotation-alignment": "map", "icon-pitch-alignment": "viewport", "icon-keep-upright": true,
        "icon-size": 1, "icon-padding": 0, "icon-allow-overlap": true, "icon-ignore-placement": true },
      paint: { ...dotPaint()["power-joints"] } },
    // L-106 8a: the region outlines (system region, montney/regions.py), solid at every zoom, under every data line layer
    // (above the power joints' corridors), so they never fight a gas or power line (both draw over them; areas and power
    // lines are dashed). Erik's region borders: each in its region's `color`, ~1.8 px, softening above zoom 6.
    // a thin dark casing under it, as the gas lines have, so the colour reads on the temperature field and on terrain alike
    { id: "region-outline-casing", type: "line", source: "net", filter: REGION_OUTLINE, layout: { "line-join": "round" },
      paint: { "line-color": "#020504", "line-width": ["interpolate", ["linear"], ["zoom"], 4, 3.2, 6, 3.6, 8, 2.8],
        "line-opacity": ["interpolate", ["linear"], ["zoom"], 4, 0.45, 6, 0.45, 8, 0.3] } },
    { id: "region-outline", type: "line", source: "net", filter: REGION_OUTLINE, layout: { "line-join": "round" },
      paint: { "line-color": ["coalesce", ["get", "color"], "#d3ece0"],
        "line-width": ["interpolate", ["linear"], ["zoom"], 4, 1.6, 6, 1.8, 8, 1.4],
        "line-opacity": ["interpolate", ["linear"], ["zoom"], 4, 0.7, 6, 0.7, 8, 0.45] } },
    { id: "areas-line", type: "line", source: "net", filter: ["all", ["==", ["get", "kind"], "area"], ["!", REGION_OUTLINE]],
      paint: { "line-width": ["case", ["<", util, 0.85], 1, 1.6], "line-dasharray": [2, 2],
        "line-color": ["case", ["<", util, 0], "rgba(211,236,224,0.35)",
          ["interpolate", ["linear"], util, 0, "rgba(211,236,224,0.35)", 0.85, "rgba(255,181,71,0.8)", 1, "rgba(255,77,109,0.9)"]] } },
    // Gas-plant smoke (PLAN 4b): on the plant's stack, only for fresh positive output (smk_u > 0). Below every data
    // line (L-028 G1: a puff never covers a gas or power line) and below the plant sprite, so it rises from behind
    // the stack.
    ...SMOKE_LAYERS.map((id, k) => ({ id, type: "symbol", source: "net",
      filter: animFilter("smk_u", 0),
      layout: { "icon-image": smokeIcon(k), "icon-anchor": "bottom", "icon-offset": smokeOffset(), "icon-size": 1,
        "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "spr_sort"] },
      paint: { ...dotPaint()[id] } })),
    // Wildfire perimeters (CWFIS, updated in the last 7 days): the real outline and its source date, below the data lines
    { id: "fire-perim", type: "line", source: "fires", filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "line-color": "#ff4d6d", "line-width": 1.2, "line-opacity": 0.8, "line-dasharray": [3, 2] } },
    { id: "fire-perim-label", type: "symbol", source: "fires", minzoom: 7, filter: ["==", ["geometry-type"], "Polygon"],
      layout: { "text-field": ["concat", "fire · last ", ["get", "lastdate"]], "text-font": ["Noto Sans Regular"],
        "text-size": 10, "symbol-placement": "point" },
      paint: { "text-color": "#ff8aa0", "text-halo-color": "#020504", "text-halo-width": 1.2 } },
    { id: "gas-casing", type: "line", source: "net", filter: isLink("gas"), layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#020504", "line-width": flowW(...CASING_W) } },
    { id: "gas", type: "line", source: "net", filter: isLink("gas"), layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-width": flowW(1.5, 7), "line-color": gasColor } },
    // Pixel flanges on gas pipes (an overlay only: gas/gas-casing paint is untouched, G1). Measured, non-stale, non-zero
    // flow only, so zero, missing and stale lines stay plain and distinguishable (G5). Off where the casing would be
    // narrower than FLANGE_MIN_STROKE (G4). Pitch alignment `viewport` keeps MapLibre on NEAREST sampling (crisp).
    { id: "gas-flanges", type: "symbol", source: "net",
      filter: ["all", isLink("gas"), ["!=", ["get", "v"], null], [">", absFlow, 0], ["!", ["get", "stale"]],
        [">=", flowW(...CASING_W), FLANGE_MIN_STROKE]],
      layout: { "symbol-placement": "line", "symbol-spacing": FLANGE_SPACING, "icon-image": flangeImage(),
        "icon-rotation-alignment": "map", "icon-pitch-alignment": "viewport",
        // keep-upright: the collar's light row stays on top whichever way a segment was digitised (STYLE.md light)
        "icon-keep-upright": true,
        "icon-size": 1, "icon-padding": 0, "icon-allow-overlap": true, "icon-ignore-placement": true },
      paint: { ...dotPaint()["gas-flanges"] } },
    { id: "power-casing", type: "line", source: "net", filter: isLink("power"), paint: { "line-color": "#020504", "line-width": 4 } },
    { id: "power", type: "line", source: "net", filter: isLink("power"),
      paint: { "line-width": 2, "line-dasharray": [2, 1.2], "line-color": powColor } },
    // Region names (L-106 8a) while the region boxes show (below zoom 6, map.json systems.region.maxzoom): small, faint
    // caps at the anchor (one per region: an outline's islands and clipped parts would each get one) of a region with no
    // box yet (no lines: montney/regions.py gives it no `callout`); a boxed region's anchor gets the callouts' ring
    // label. Optional, so a crowded spot drops it.
    { id: "region-names", type: "symbol", source: "net", maxzoom: 6,
      filter: ["all", ["==", ["get", "kind"], "node"], ["==", ["get", "system"], "region"], ["!", ["has", "callout"]]],
      layout: { "text-field": ["upcase", ["get", "name"]], "text-font": ["Noto Sans Regular"], "text-size": 10,
        "text-letter-spacing": 0.12, "text-max-width": 8, "text-anchor": "top", "text-offset": [0, 0.9], "text-optional": true },
      paint: { "text-color": "rgba(211,236,224,0.42)", "text-halo-color": "#020504", "text-halo-width": 1 } },
    // a region-type node's ring (AESO load etc.); the generated region anchors (system region) have their outline instead
    { id: "regions", type: "circle", source: "net", filter: ["all", ["==", ["get", "kind"], "node"], ["==", ["get", "type"], "region"],
      ["!=", ["get", "system"], "region"]],
      paint: { "circle-radius": 9, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "rgba(211,236,224,0.6)", "circle-stroke-width": 1,
        ...dotPaint().regions } },
    // Node sprites (P4c, L-028): pixel-art symbols from web/sprites/ chosen by type and plant fuel (web/sprites.js).
    // icon-size stays 1 and each image's pixelRatio equals the map's, so MapLibre samples them NEAREST (crisp).
    // River gauges (P5d, web/gauges.js): fill = flow-vs-normal band, only when the gauge's state is "ok"; a blue ring marks
    // a gauge. map.js joins /api/snapshot `gauges` onto the station points (g_fill, g_fillop, g_op).
    { id: "gauges", type: "circle", source: "gauges",
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 4.5, 9, 7], "circle-color": ["get", "g_fill"],
        "circle-stroke-color": GAUGE_RING, "circle-stroke-width": 1.5, ...dotPaint().gauges } },
    ...["node-sel", "node-sel-c"].map((id) => ({ id, type: "circle", source: "net",
      filter: ["all", ["==", ["get", "kind"], "node"], ["has", "spr_kind"], ["==", ["get", "spr_anchor"], id === "node-sel" ? "bottom" : "center"]],
      paint: { "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#d3ece0", "circle-stroke-width": 1.5,
        ...dotPaint()[id] } })),
    // Active fires (Art's wildfire glyphs): a hotspot cell (newest detection < 24 h) or a recently updated perimeter (< 48 h),
    // below the node sprites so a network symbol stays readable
    ...FIRE_LAYERS.map((id, k) => ({ id, type: "symbol", source: "fires", filter: ["==", ["geometry-type"], "Point"],
      layout: { "icon-image": fireIcon(k), "icon-anchor": "bottom", "icon-size": 1, "icon-allow-overlap": true,
        "icon-ignore-placement": true },
      paint: { ...dotPaint()[id] } })),
    { id: "nodes", type: "symbol", source: "net", filter: ["all", ["==", ["get", "kind"], "node"], ["has", "spr_kind"]],
      layout: { "icon-image": nodeIcon(), "icon-anchor": ["get", "spr_anchor"], "icon-size": 1,
        "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "spr_sort"] },
      paint: { ...dotPaint().nodes } },
    // Moving sprites (wind rotors, compressor ticks): one layer per frame, only moving features (spr_u != 0).
    ...ANIM_LAYERS.map((id, k) => ({ id, type: "symbol", source: "net",
      filter: animFilter("spr_u", 0),
      layout: { "icon-image": animIcon(k), "icon-anchor": ["get", "spr_anchor"], "icon-size": 1,
        "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "spr_sort"] },
      paint: { ...dotPaint()[id] } })),
    // Missing or stale data: the `unknown` badge at the sprite's top-right (NODES.md "Data states").
    { id: "node-badges", type: "symbol", source: "net", filter: ["all", ["==", ["get", "kind"], "node"], ["get", "spr_badge"]],
      layout: { "icon-image": "unknown-16-static", "icon-anchor": "center", "icon-offset": badgeOffset(),
        "icon-size": 1, "icon-allow-overlap": true, "icon-ignore-placement": true },
      paint: { ...dotPaint()["node-badges"] } },
    // L-106 (NODES.md "Glow", REGIONS.md §8): a glow from an estimate (e.g. a BA fuel-level utilisation) is labelled
    // "est." just under the point, so it never reads as measured plant MW. Steady, no smoke. Hidden when crowded.
    { id: "node-est", type: "symbol", source: "net", filter: ["all", ["==", ["get", "kind"], "node"], ["get", "spr_est"]],
      layout: { "text-field": "est.", "text-font": ["Noto Sans Regular"], "text-size": 10, "text-anchor": "top",
        "text-offset": [0, 0.15], "text-optional": true },
      paint: { "text-color": "#9fb3a9", "text-halo-color": "#020504", "text-halo-width": 1.2, ...dotPaint()["node-est"] } },
    // pins split by the anchor of the feature they point at (circle-translate can't vary per feature)
    ...["event-pins", "event-pins-c"].map((id) => ({ id, type: "circle", source: "events",
      filter: id === "event-pins" ? ["!=", ["get", "anchor"], "center"] : ["==", ["get", "anchor"], "center"],
      paint: { "circle-radius": 5, ...dotPaint()[id],
        "circle-color": ["match", ["get", "severity"], "critical", "#ff4d6d", "warn", "#ffb547", "#9fb3a9"],
        "circle-stroke-color": "#020504", "circle-stroke-width": 1.5 } })),
  ];
}
export const NETWORK_LINE_LAYERS = ["areas-fill", "areas-line", "gas-casing", "gas", "power-casing", "power", "nodes", "node-badges", "node-est"];
