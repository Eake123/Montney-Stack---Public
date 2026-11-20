// Pixel-art node sprites (PLAN P4c, LOG L-028; atlas and rules: web/sprites/, docs/art/NODES.md).
// No imports, so tests/test_web.py can run the kind/state mapping under node.

/** Network `type` → atlas kind (NODES.md "Kind selection"). Plants go by `fuel` instead. */
export const TYPE_KIND = {
  receipt: "receipt", hub: "hub", storage: "storage", border: "border", compressor: "compressor",
  interconnect: "interconnect", intertie: "intertie", lng: "lng-terminal", region: "region", system: "system",
  gas_processing: "gas-processing", meter: "meter",
  hydro_summary: "plant-hydro",   // Weather #362: a basin's water summary (Manitoba, Québec) wears the static hydro sprite
  facility: "gas-processing",     // L-106 8b (Gas #410): an AB / BC reporting gas facility (/api/wells) wears the gas-plant sprite
};
/** Single-fuel plant aliases. Compound strings (e.g. "cogeneration + simple_cycle") are never guessed. Every Regions
 * `FUELS` key (docs/REGIONS.md §4) has one, except `other`, which keeps the unknown-fuel plant (Art #404, NODES.md). */
export const FUEL_KIND = {
  combined_cycle: "plant-combined-cycle", simple_cycle: "plant-simple-cycle", cogeneration: "plant-cogen",
  cogen: "plant-cogen", gas_fired_steam: "plant-gas-fired-steam", coal_to_gas: "plant-coal-to-gas",
  hydro: "plant-hydro", wind: "plant-wind", solar: "plant-solar", nuclear: "plant-nuclear",   // L-091 (Art #359): static
  // L-106 (Art #404): an inventory "gas" with no documented subtype is generic gas (never a guessed CC/SC)
  gas: "plant-gas", coal: "plant-coal", oil: "plant-oil", battery: "plant-battery", biomass: "plant-biomass",
  geothermal: "plant-geothermal", pumped_storage: "plant-pumped-storage",
};
/** Atlas kinds whose image reports output through a glow level (others are `static`). */
export const GLOW_KINDS = new Set(["plant-combined-cycle", "plant-simple-cycle", "plant-cogen", "plant-gas-fired-steam",
  "plant-coal-to-gas", "plant-gas", "plant-coal", "plant-oil"]);
/** Battery: a signed state from net MW (NODES.md "Battery proposal"), never a glow and never a timer. */
export const BATTERY_KIND = "plant-battery";
/**
 * A value that is an estimate, not a measured plant output: an `estimate` lo/hi or `band` on the feature, or
 * `quality: "estimate"` (REGIONS.md §3; e.g. a BA fuel-level utilisation in Power's inventory, §8). It may set a steady
 * glow (labelled "est." on the map), never smoke and never a battery direction.
 */
export const isEstimate = (p) => !!(p?.estimate || p?.band || p?.quality === "estimate");
/** Kinds drawn centred on the point; everything else stands on it (`bottom`), per the manifest. */
export const CENTER_KINDS = new Set(["hub", "region", "system", "warn", "critical", "unknown"]);
/** Native sprite sizes: 16 px up to this level zoom, 32 px from it (the switch uses the hysteretic level zoom). */
export const SPRITE_32_Z = 8;
/** On-screen CSS size of a device-exact sprite (loadSprites' default): `base` art px drawn at round(ratio) device px
 * each, so 16 px art is 16 CSS px at 100 % and 200 %, 12.8 at 125 % and 21.33 at 150 %. Layout (badge, ring, callout
 * keep-out) must use this, not the nominal 16/32. */
export const spriteCss = (base, pr = 1) => (base * Math.max(1, Math.round(pr))) / pr;

/**
 * Atlas kind for a node's properties. Plants use Power's `fuel_primary` when the network gives one (a compound plant's
 * main fuel, network/power/plants.geojson), else `fuel`. `override` = map.json `overrides[id].sprite`, only for
 * exceptions the fuel can't express (e.g. coal-to-gas conversions).
 */
export function spriteKind(p, override) {
  if (override) return override;
  if (p.type === "plant") {
    // A compound string ("cogeneration + simple_cycle") matches no single-fuel key, so it falls to plant-unknown.
    const fuel = String(p.fuel_primary ?? p.fuel ?? "").trim().toLowerCase().replace(/[\s-]+/g, "_");
    return FUEL_KIND[fuel] ?? "plant-unknown";
  }
  return TYPE_KIND[p.type] ?? null;   // null: no sprite (the node keeps no icon)
}

/** The region page's fuel words (montney/regions.py FUELS, "Generation by fuel"): a plant's fuel reads the same there. */
export const FUEL_WORDS = ["gas", "coal", "nuclear", "hydro", "pumped_storage", "wind", "solar", "battery", "oil", "biomass",
  "geothermal", "other"];
/** Gas-fired technologies (AESO's and Ontario's `fuel`, EIA-860M's prime mover): the fuel is gas, the technology rides along. */
const GAS_TECH = new Set(["combined_cycle", "simple_cycle", "cogeneration", "cogen", "gas_fired_steam", "coal_to_gas"]);
const word = (k) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
/**
 * A plant's fuel for its panel (PLAN 10d, Erik: "make the fuel type obvious"): `{ key, word, tech }`, `word` being the
 * region page's fuel word ("Gas", "Pumped storage") and `tech` the gas technology when the source names one
 * ("combined cycle", or a compound plant's whole string "cogeneration + simple cycle"). The inventories' `fuel_group`
 * wins (EIA-860M, OpenStreetMap, Ontario); an AESO plant's `fuel_primary`, then `fuel`, maps to it. Nothing known → key
 * null, word "fuel unknown" (never a guess: a compound string with no `fuel_primary` whose parts aren't all gas stays unknown).
 */
export function plantFuel(p) {
  const norm = (s) => String(s ?? "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  const fuel = norm(p?.fuel), prim = norm(p?.fuel_primary), grp = norm(p?.fuel_group);
  const parts = fuel.split(/_?\+_?/).filter(Boolean);
  const of = (f) => (FUEL_WORDS.includes(f) ? f : GAS_TECH.has(f) ? "gas" : null);
  const key = of(grp) ?? of(prim) ?? of(fuel) ?? (parts.length > 1 && parts.every((f) => of(f) === "gas") ? "gas" : null);
  const tech = key === "gas" && (GAS_TECH.has(fuel) || parts.length > 1)
    ? String(p.fuel).trim().replace(/_/g, " ").replace(/\s*\+\s*/g, " + ").replace(/gas fired/i, "gas-fired") : null;
  return { key, word: key ? word(key) : "fuel unknown", tech };
}

/**
 * PLAN 10c: a plant whose source says it can produce nothing right now (its capability series reads 0, e.g. IESO's
 * Portlands and Pickering since 2026-11-07) and that isn't producing: out of service, a known state, not missing data.
 * `e` is the plant's snapshot entry (cap_current / cap from its `capacity` series). Stale data says nothing.
 */
export const outOfService = (p, e) => p?.type === "plant" && !!e && !e.stale && (e.cap_current ?? e.cap) === 0 && !(e.v > 0);
/** Producing nothing with capacity above 0: idle and available only when the capacity is live (see capacityInfo). */
export const notProducing = (p, e) => p?.type === "plant" && !!e && !e.stale && e.v === 0 && (e.cap_current ?? e.cap) > 0;
/**
 * A plant's capacity series in its source's terms (map.json `capacity_words`, longest series-id prefix wins):
 * `{ word, live, zero }`. `live`: availability published hour by hour (IESO capability), so 0 MW against it is "idle,
 * available"; a static rating (AESO max capability, NRC net summer capacity) isn't (Reviewer on #539). Unmatched:
 * "capacity", not live.
 */
export function capacityInfo(series, words = {}) {
  const k = Object.keys(words).filter((w) => !w.startsWith("_") && String(series ?? "").startsWith(w)).sort((a, b) => b.length - a.length)[0];
  const v = k ? words[k] : null, o = typeof v === "string" ? { word: v } : v || {};
  return { word: o.word || "capacity", live: o.live === true, zero: o.zero || null };
}
/** The plant panel's line for 0 MW with capacity above 0 (PLAN 10c): idle against live availability, else not producing. */
export function zeroText(ci, cap, unit = "MW", fmt = String) {
  return ci.live ? `Idle, available (${ci.word} ${fmt(cap)} ${unit})`
    : ci.zero ? `Not producing (${ci.zero})` : `Not producing (0 of ${fmt(cap)} ${unit} ${ci.word})`;
}

/**
 * Image state. Thermal plants: measured zero → glow0, output/reference up to 1/3 → glow1, up to 2/3 → glow2,
 * above → glow3 (clamped at nameplate). No measured value or no positive reference → glow0 plus the `unknown`
 * badge (see `needsBadge`). An estimate picks its glow the same way: the glow is steady, so it never reads as motion.
 * Battery: fresh, measured, signed net MW > 0 → `discharge`, < 0 → `charge`; zero, missing, stale or estimated →
 * `static` (an estimate or a stale value can't say which way one battery runs). Other kinds are `static`; wind and
 * compressor motion uses the frame layers (animSpeed / frameAt below).
 */
export function spriteState(kind, v, util, stale = false, estimated = false, off = false) {
  // PLAN 10c: a plant known to be out of service (capability 0) is still: glow0 for a glowing kind, else static
  if (off && !stale) return GLOW_KINDS.has(kind) ? "glow0" : "static";
  if (kind === BATTERY_KIND) return stale || estimated || v == null || !Number.isFinite(v) || v === 0 ? "static" : v > 0 ? "discharge" : "charge";
  if (!GLOW_KINDS.has(kind)) return "static";
  if (v == null || util == null || !Number.isFinite(util)) return "glow0";
  if (v <= 0 || util <= 0) return "glow0";   // STYLE.md: glow on max(MW, 0); util is |v|/cap, so test v itself
  return util <= 1 / 3 ? "glow1" : util <= 2 / 3 ? "glow2" : "glow3";
}
/** Show the `unknown` badge: stale data, a thermal plant with no measured output or no positive reference, or a
 * battery with no signed value. */
export function needsBadge(kind, v, util, stale, off = false) {
  if (off && !stale) return false;   // PLAN 10c: known to be off (capability 0), not unknown
  return !!stale || ((GLOW_KINDS.has(kind) || ANIM_KINDS.has(kind)) && (v == null || util == null || !Number.isFinite(util)))
    || (kind === BATTERY_KIND && (v == null || !Number.isFinite(v)));
}

// ---- motion (P4c, docs/art/STYLE.md "Silhouettes and data" + "Budget"): every animation encodes data (L-005)
/** Kinds whose atlas has rotation/tick frames (frame0..3). */
export const ANIM_KINDS = new Set(["plant-wind", "compressor"]);
export const ANIM_FRAMES = 4;      // frames per cycle (one cycle = 120° for a three-blade rotor)
export const WIND_MIN_U = 0.25;   // cycles/s for a farm producing anything: 1 frame/s
export const ANIM_TICK_MS = 250;   // the one shared clock: at most 4 frame updates per second per feature
/**
 * Signed speed in cycles per second; 0 means static. Wind: u = clamp(max(MW, 0) / reference, 0, 1), so zero or
 * negative output stops the rotor. Compressor: the same on |flow| / flow reference, signed by flow direction (ticks run
 * backwards for reverse flow). Missing value, missing or non-positive reference, or stale data: 0, frozen (with the
 * `?` badge from needsBadge). `util` is |v| / reference, from map.js.
 */
export function animSpeed(kind, v, util, stale) {
  if (!ANIM_KINDS.has(kind) || stale || v == null || util == null || !Number.isFinite(util) || util <= 0) return 0;
  // Wind (PLAN 4b): any producing farm visibly turns, 1 frame/s at the lowest output up to 4/s at full. Pure utilisation
  // left a typical 2-15 % farm changing frame every 2-12 s, which read as frozen. Still data: no output = still.
  if (kind === "plant-wind") return v > 0 ? WIND_MIN_U + (1 - WIND_MIN_U) * Math.min(util, 1) : 0;
  return v === 0 ? 0 : Math.sign(v) * Math.min(util, 1);
}
// ---- PLAN 10b / L-108 (Erik: "animations for all powerplant sprites when they're turned on"; Art #538, NODES.md)
/** Kinds with full-node `frame0..3` (wind's rotor, plus #538's six): the frames replace the still image while on. */
export const FRAME_KINDS = new Set(["plant-wind", "plant-hydro", "plant-solar", "plant-nuclear", "plant-biomass", "plant-geothermal", "plant-unknown"]);
/** Estimated plants of these kinds move at the estimate's rate (weather and water drive an area's fleet together). */
export const EST_MOVE_KINDS = new Set(["plant-wind", "plant-solar", "plant-hydro"]);
/** Dispatched kinds whose estimate keeps a steady glow, never frames (#538 gave them glow0..3; thermal already glows). */
export const EST_GLOW_KINDS = new Set(["plant-biomass", "plant-geothermal", "plant-unknown"]);
export const PUMPED_KIND = "plant-pumped-storage";
/** The wind rule (NODES.md): cycles/s = 0.25 + 0.75 × clamp(utilisation, 0, 1), i.e. 1 to 4 frame changes a second. */
export const motionRate = (util) => WIND_MIN_U + (1 - WIND_MIN_U) * Math.min(Math.max(util, 0), 1);
const glowOf = (v, util) => (v == null || util == null || !Number.isFinite(util) || v <= 0 || util <= 0 ? "glow0"
  : util <= 1 / 3 ? "glow1" : util <= 2 / 3 ? "glow2" : "glow3");

/**
 * The one motion rule (L-108), callable by tests: a node's still image, its timed set and rate, and its `?` badge.
 * Returns { state, set, u, badge }: `state` = the still image (drawn while motion is off: reduced motion, history,
 * a hidden tab, off-screen), `set` = the timed set ("frame", "charge"/"discharge", "pump"/"generate") or null,
 * `u` = cycles/s (0 = still; negative only for a compressor's reverse flow), `badge` = the `?`.
 *  - Measured, fresh, above zero, with a positive reference: moves at motionRate(util). Zero is still. Missing or stale
 *    (or no reference) is frozen with the `?` (only for a plant that has a value source: `tracked`; 551 OSM plants
 *    have none and stay still without one).
 *  - Estimated solar / wind / hydro: moves at the estimate's rate (the caller adds "est.").
 *  - Estimated biomass / geothermal / other: a steady glow from the estimate, no frames; estimated gas / coal / oil
 *    glow as before (and never smoke: smokeSpec). Estimated nuclear: still (US nuclear is measured since #537).
 *  - Battery / pumped storage: a direction only from a measured, signed value (discharge/generate > 0,
 *    charge/pump < 0; the set encodes the direction, so u stays positive). Estimated: still.
 *  - `off` (capability 0, PLAN 10c): still (glow0 for a glowing kind), no badge: known to be off.
 */
export function plantMotion(kind, v, util, stale = false, estimated = false, off = false, tracked = true) {
  const still = (state, badge) => ({ state, set: null, u: 0, badge: !!badge });
  if (off && !stale) return still(GLOW_KINDS.has(kind) ? "glow0" : "static", false);
  const valid = !stale && v != null && Number.isFinite(v) && util != null && Number.isFinite(util);
  if (kind === "compressor") {
    const u = animSpeed(kind, v, util, stale);
    return { state: "static", set: u ? "frame" : null, u, badge: needsBadge(kind, v, util, stale) };
  }
  if (GLOW_KINDS.has(kind)) return still(spriteState(kind, v, util, stale, estimated), needsBadge(kind, v, util, stale));
  if (kind === BATTERY_KIND || kind === PUMPED_KIND) {
    const state = kind === BATTERY_KIND ? spriteState(kind, v, util, stale, estimated) : "static";
    const badge = tracked && (!!stale || v == null || !Number.isFinite(v));
    if (estimated || !valid || v === 0 || !(util > 0)) return still(state, badge);
    const set = kind === BATTERY_KIND ? (v > 0 ? "discharge" : "charge") : (v > 0 ? "generate" : "pump");
    return { state, set, u: motionRate(util), badge: false };
  }
  if (FRAME_KINDS.has(kind)) {
    // the wind keeps its badge rule (needsBadge: any farm without a reading); the new kinds badge only when tracked
    const badge = kind === "plant-wind" ? needsBadge(kind, v, util, stale) : tracked && !valid;
    if (estimated && EST_GLOW_KINDS.has(kind)) return still(valid ? glowOf(v, util) : "static", badge);
    if (estimated && !EST_MOVE_KINDS.has(kind)) return still("static", badge);
    if (!valid || !(v > 0) || !(util > 0)) return still("static", badge);
    return { state: "static", set: "frame", u: motionRate(util), badge: false };
  }
  return still(spriteState(kind, v, util, stale, estimated), needsBadge(kind, v, util, stale));
}
// ---- plant smoke (PLAN 4b, L-058; Art's docs/art/SMOKE.md, L-106 NODES.md): an activity puff, only for real output
/** Fuel-burning thermal kinds (the same set that glows: gas, coal, oil). Never solar, hydro, wind, battery, geothermal
 * or gas processing. An activity symbol, not measured emissions. */
export const SMOKE_KINDS = GLOW_KINDS;
export const SMOKE_LOW = 1 / 3;   // below a third of the plant's reference output: the thinner "low" puff
/**
 * The plume for a gas-fired plant, or null (no plume, not a frozen one): only fresh, measured, positive output.
 * Stale, missing, zero/negative, or an estimated value (`estimate`/`band` on the feature) gives nothing. The rate is
 * the wind's: 1 frame/s at the lowest output up to 4/s at full.
 */
export function smokeSpec(kind, v, util, stale, estimated) {
  if (!SMOKE_KINDS.has(kind) || stale || estimated || v == null || !(v > 0) || util == null || !Number.isFinite(util) || util <= 0) return null;
  return { u: WIND_MIN_U + (1 - WIND_MIN_U) * Math.min(util, 1), variant: util < SMOKE_LOW ? "low" : "normal" };
}
/**
 * Where the puff sits on a bottom-anchored plant sprite, in native sprite px from the node point: [dx, dy] at 16 and 32.
 * Measured from nodes.png: the puff's base is centred on the (right-hand) stack, and its outlet (2 px above the puff
 * canvas's bottom at 16, 4 px at 32; SMOKE.md) sits on the stack mouth.
 */
export const SMOKE_OFFSET = {
  "plant-combined-cycle": { 16: [3, -13], 32: [7, -24] }, "plant-simple-cycle": { 16: [3, -13], 32: [7, -24] },
  "plant-coal-to-gas": { 16: [3, -13], 32: [7, -24] }, "plant-cogen": { 16: [0, -12], 32: [1, -22] },
  "plant-gas-fired-steam": { 16: [-4, -12], 32: [-7, -22] },
  // L-106: Art's NODES.md table (stack mouths coal (11,2)/(22,4), oil (12,2)/(24,4), generic gas (4,2)/(8,4))
  "plant-coal": { 16: [3, -12], 32: [6, -24] }, "plant-oil": { 16: [4, -12], 32: [8, -24] },
  "plant-gas": { 16: [-4, -12], 32: [-8, -24] },
};

// ---- wildfire (Art's docs/art/WILDFIRE.md)
/**
 * Wildfire features from the CWFIS alert events (fire_cwfis): a perimeter's real outline with its source date (always,
 * while the event is current), and a flame only for an active fire: a hotspot cell whose newest detection is < 24 h old
 * (small glyph), or a perimeter CWFIS updated in the last 48 h (large glyph at its centroid). An older perimeter keeps
 * its outline and date but no flame (WILDFIRE.md: no stale, inactive or historical flames).
 */
export const FIRE_HOT_S = 24 * 3600, FIRE_PERIM_S = 48 * 3600;
const utcSec = (s) => (s ? Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z") / 1000 : null);   // naive = UTC
export function fireFeatures(events, now) {
  const out = [];
  for (const ev of events || []) {
    const x = ev.extra || {};
    if (x.type === "hotspots" && x.lon != null) {
      const last = utcSec(ev.published);
      if (last != null && now - last <= FIRE_HOT_S)
        out.push({ type: "Feature", geometry: { type: "Point", coordinates: [x.lon, x.lat] }, properties: { id: ev.id, fire: "hotspot" } });
    } else if (x.type === "perimeter" && x.geometry) {
      const last = utcSec(x.lastdate ?? ev.published), day = String(x.lastdate || "").slice(0, 10);
      const polys = x.geometry.type === "MultiPolygon" ? x.geometry.coordinates : [x.geometry.coordinates];
      for (const rings of polys) out.push({ type: "Feature", geometry: { type: "Polygon", coordinates: rings },
        properties: { id: ev.id, lastdate: day } });
      if (last != null && now - last <= FIRE_PERIM_S && x.lon != null)
        out.push({ type: "Feature", geometry: { type: "Point", coordinates: [x.lon, x.lat] }, properties: { id: ev.id, fire: "perimeter" } });
    }
  }
  return out;
}

/** Frame index (0..3) at shared-clock time `t` seconds: floor(4·|u|·t) mod 4, counted down for negative u. With
 * |u| <= 1 this changes at most 4 times a second. style.js `animFrame` is the same formula as a MapLibre expression. */
export function frameAt(u, t) {
  const k = Math.floor(ANIM_FRAMES * Math.abs(u) * t) % ANIM_FRAMES;
  return u < 0 ? ANIM_FRAMES - 1 - k : k;
}
export const imageKey = (kind, size, state) => `${kind}-${size}-${state}`;

/**
 * Load the atlas so icons stay pixel-exact at any display scaling. The usual sprite URL makes MapLibre pick @2x
 * on a 125 % or 150 % screen and resample it with LINEAR filtering (blurry, uneven pixels). Instead every image is
 * cut from the @1x atlas, upscaled by an integer n with nearest-neighbour, and added with pixelRatio equal to the
 * map's own, so MapLibre draws it 1:1 in device pixels with NEAREST filtering. n = round(device pixel ratio):
 * a 16 px sprite is 16 device px at 100 % and 125 %, and 32 at 150 % and 200 %.
 *
 * `fit: "css"` is for art that must line up with CSS-sized geometry (the gas flanges sit against line widths given
 * in CSS px). The image is upscaled by n = ceil(ratio) and added at pixelRatio n, so it's exactly its native size in
 * CSS px. That's still pixel-exact at 100 % and 200 %; at 125 % / 150 % MapLibre downsamples it slightly (a little
 * soft) rather than letting the collar fall short of the casing.
 */
export async function loadSprites(map, base = "/static/sprites/nodes", { fit = "device" } = {}) {
  const [index, img] = await Promise.all([
    fetch(`${base}.json`, { cache: "no-cache" }).then((r) => r.json()),
    new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = `${base}.png`; }),
  ]);
  const add = () => {
    const pr = map.getPixelRatio(), css = fit === "css";
    const n = css ? Math.max(1, Math.ceil(pr - 1e-9)) : Math.max(1, Math.round(pr)), ratio = css ? n : pr;
    const c = document.createElement("canvas"), g = c.getContext("2d", { willReadFrequently: true });
    for (const [key, r] of Object.entries(index)) {
      c.width = r.width * n; c.height = r.height * n;
      g.imageSmoothingEnabled = false; g.clearRect(0, 0, c.width, c.height);
      g.drawImage(img, r.x, r.y, r.width, r.height, 0, 0, c.width, c.height);
      if (map.hasImage(key)) map.removeImage(key);
      map.addImage(key, g.getImageData(0, 0, c.width, c.height), { pixelRatio: ratio });
    }
    return pr;
  };
  let pr = add();
  // Moving the window to a screen with another scaling changes the ratio: re-cut the images for it.
  map.on("resize", () => { if (map.getPixelRatio() !== pr) pr = add(); });
  return index;
}
