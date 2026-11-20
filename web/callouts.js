// Map callouts: compact value boxes with leader lines (frontend.md §4). Config: web/map.json "callouts".
// Placement: zoom levels by tier (0 = overview), 8 compass candidates with per-callout preference, greedy by tier,
// keep-out for HUD panels / nodes / region labels, drop when nothing fits. Stability (PLAN P4a, placement.js):
// sticky slots, ~200 ms glide and fade, zoom-level hysteresis, and in-place overview <-> breakdown handoff.
import { ageLabel, esc, fmt, fmtSigned, fmtTime, lateAge, unitLabel } from "./lib.js";
import { chooseSlot, GLIDE_MS, slotRect, Tween } from "./placement.js";
import { FORMS, layoutRegions } from "./regionlayout.js";

// Zoom levels (docs/WIDGETS.md): tier 0 is the overview (production, demand, storage, major exports) and shows
// from the furthest zoom-out; zooming in past BREAKOUT_Z swaps it for the per-area / per-point breakdown.
export const BREAKOUT_Z = 6;
export const TIER_MINZOOM = { 0: 0, 1: BREAKOUT_Z, 2: 6.75, 3: 7.5 };
export const ROW_MINZOOM = { 1: 0, 2: 6.75, 3: 7.5 };   // secondary rows inside a box appear later than boxes do
export const tierOf = (c) => c.tier ?? 2;
/**
 * Compact boxes (L-069, Erik: "a mobile friendly style ... dynamic, so dont hardcode boxes"): one code path, chosen by
 * the map's size, never by device or feature. Narrower than `maxW` or shorter than `maxH` (CSS px), boxes show only their
 * lead rows (see leadRows), and at most one box per `perBox` px² of map shows (min 3; always_show boxes don't count).
 */
export const COMPACT = { maxW: 600, maxH: 500, perBox: 50000, min: 3 };
export const isCompact = (W, H) => W < COMPACT.maxW || H < COMPACT.maxH;
export const boxBudget = (W, H) => Math.max(COMPACT.min, Math.round((W * H) / COMPACT.perBox));
/**
 * Which rows a folded box keeps (L-069; on every screen since L-070), from the rows themselves: a title head, the first
 * data row, and the first data row after each `section` head (a merged box keeps each section's total, e.g. gas and
 * power demand). A plain merged part's head (a facility under a total) starts no new lead. A row marked `lead: true`
 * also stays (L-085: a summary box's few headline numbers, e.g. the Columbia's flow, storage and runoff forecast), and
 * so does a row with `depth: 0`, a headline of its own and not a part of the one above (Gas #354: Malin's PG&E
 * receipts aren't a part of GTN's Malin value). The rest wait for a click. L-106 8a: a head marked `lead: true` (a region
 * box's "Gas · MMcf/d") stays too, and a `fold: true` row folds even first in its section (a region's secondary lines).
 */
export function leadRows(rows) {
  let next = true;
  return rows.map((r) => {
    if (r.head) { if (r.section) next = true; return !!r.title || !!r.lead; }
    // L-106 8a: `fold: true` (a region box's secondary line, map.json sections[].lead) folds even at depth 0
    const lead = (next && !r.fold) || !!r.lead || (r.depth === 0 && !r.fold); next = false; return lead;
  });
}
/**
 * A box's row tree (L-073, Erik: nested split-out boxes). Rows stay one flat list in pre-order (a parent, then its
 * children, then its next sibling). The first row after the start, a section head or a merged part's head is a root
 * (depth 0, the headline), and so is a row with `depth: 0` (another headline, e.g. Gas #354's Malin PG&E receipts; its
 * own parts nest under it). Any other row's depth is its `depth` (1 = a part of the headline, 2 = a part of the nearest
 * depth-1 row above, ...), else, for "·" marks, that many levels below the nearest row above without marks, else 1; and
 * at most one deeper than the row before. Its parent is the nearest row above with one less depth. Heads get depth null.
 */
export function rowTree(rows) {
  const depth = [], parent = [];
  let prev = -1, plain = 0;   // the previous row's depth; the depth of the nearest row above without "·" marks
  rows.forEach((r, i) => {
    if (r.head) { depth.push(null); parent.push(-1); if (!r.title) prev = -1; return; }
    if (prev < 0 || r.depth === 0) { depth.push(0); parent.push(-1); prev = plain = 0; return; }
    const dots = (String(r.label || "").match(/^\s*((?:·\s*)+)/)?.[1].match(/·/g) || []).length;
    // "·" marks count from the nearest plain row above (siblings stay siblings); a plain row without `depth` is depth 1
    const want = Number.isInteger(r.depth) && r.depth > 0 ? r.depth : dots ? plain + dots : 1;
    const d = Math.max(1, Math.min(want, prev + 1));
    if (!dots) plain = d;
    let p = i - 1;
    while (p >= 0 && (depth[p] == null || depth[p] >= d)) p--;
    depth.push(d); parent.push(p); prev = d;
  });
  return { depth, parent };
}
/** Is a box (or a feature's dot) of this tier shown at zoom z? `minzoom`/`maxzoom` override the tier's range. */
export const shownAt = (c, z) => z >= (c.minzoom ?? TIER_MINZOOM[tierOf(c)] ?? 0) && !(c.maxzoom != null && z >= c.maxzoom);
/** Every zoom-level boundary (tiers, rows, per-box minzoom/maxzoom): where hysteresis applies. */
/** Shown somewhere in tier t: its minzoom is below the next tier's (the server's snapshot scope, L-081 Database #317), and no maxzoom ends it before t. */
export function inTier(minzoom, maxzoom, t, tiers) {
  const hi = tiers[t + 1] ?? Infinity;
  return (minzoom ?? 0) < hi && !(maxzoom != null && maxzoom <= tiers[t]);
}
export function zoomBounds(callouts = [], overrides = {}) {
  const b = new Set([...Object.values(TIER_MINZOOM), ...Object.values(ROW_MINZOOM)]);
  for (const c of [...callouts, ...Object.values(overrides)])
    for (const z of [c.minzoom, c.maxzoom, c.box_minzoom, c.merge_until]) if (typeof z === "number") b.add(z);
  return [...b].filter((z) => z > 0).sort((x, y) => x - y);
}

/** Text for a value's change chip from the snapshot compare rule (docs/ARCHITECTURE.md §3), or a ref series. */
export function change(entry, row = {}, refEntry = null) {
  if (!entry || entry.v == null) return null;
  // Feature-level target from the snapshot (docs/ARCHITECTURE.md §3), else a ref series fetched via ?extra=.
  if (row.vs_target && entry.target != null) return { d: entry.v - entry.target, label: row.ref_label || "vs tgt" };
  if (row.ref) return refEntry?.v == null ? null : { d: entry.v - refEntry.v, label: row.ref_label || "vs ref" };
  if (entry.prev == null) return null;
  const f = (entry.freq || "").toUpperCase();   // a monthly series' change is month over month (L-078), whatever its compare
  const label = f === "M" ? "m/m" : entry.compare === "prev" ? (f === "D" ? "d/d" : "prev") : entry.compare;
  return { d: entry.v - entry.prev, label };
}
/** A row's snapshot key: `<series>@<window>` when the row asks for a window mean (`agg`, #151), else its series. */
export function rowKey(r) {
  if (!r?.series || !r.agg || r.series.includes("@")) return r?.series;
  return `${r.series}@${r.agg}`;
}
/** The bare series id (history queries and the panel chart take ids, not `@window` tokens). */
export const baseSeries = (t) => (typeof t === "string" ? t.split("@")[0] : t);
const winLabel = (w) => String(w ?? "").replace(/^(\d+)\s*([a-z]+)$/i, "$1 $2");   // "1h" -> "1 h"
/**
 * What a row shows (docs/WIDGETS.md "Window means", #151). An entry with `agg` shows `agg.mean`, tagged with its
 * window and n ("1 h mean · n 12"). When the mean is null it shows `agg.reason` as NO-RESULT, never the latest point
 * `.v` passed off as a window mean. Without `agg`: the latest value `.v`.
 */
export function shown(e) {
  const a = e?.agg;
  if (!a) return { v: e?.v ?? null, tag: null, reason: null };
  if (a.mean == null) return { v: null, tag: null, reason: a.reason || `no ${winLabel(a.window)} mean` };
  return { v: a.mean, tag: `${winLabel(a.window)} mean · n ${a.n}`, reason: null };
}
export function chipHtml(entry, row, refEntry) {
  if (entry?.agg) {   // a window mean: no point-to-point change chip; NO-RESULT carries the reason
    const s = shown(entry);
    if (s.reason) return `<span class="chip noresult" title="${esc(s.reason)}">NO-RESULT</span>`;
    return entry.stale ? `<span class="chip stale">STALE</span>` : "";
  }
  if (entry?.stale) return `<span class="chip stale">STALE</span>`;
  const c = change(entry, row, refEntry);
  if (!c) return "";
  const dec = row.decimals ?? (Math.abs(c.d) < 100 ? 1 : 0);
  return `<span class="chip ${c.d >= 0 ? "up" : "down"}">${fmtSigned(c.d, dec)} ${esc(c.label)}</span>`;
}

/**
 * A share row (PLAN 4b, L-058): the feature's shown value as a % of its basis series (`share: {of, label}`, e.g. "% of
 * AESO generation"). null, and so no row, when either is missing or stale, the basis is zero, or their units differ:
 * never a % of the wrong thing.
 */
/**
 * Display sign (PLAN 4c, L-060): production reads positive, demand negative, for gas and power. `flow: "demand"` or
 * `"supply"` (on the feature, or on a row) decides; otherwise a demand-like series id reads negative (…demand…,
 * intraprovincial, deliver…, OSDA, `.city.`, AESO's `.ail`, load, gas_burn) and everything else reads as stored.
 */
const DEMAND_RE = /(demand|intraprovincial|deliver|osda|\.city\.|\.ail\b|(^|[._])load([._]|$)|gas_burn)/;
export function flowSign(flow, series) {
  if (flow === "demand") return -1;
  if (flow === "supply") return 1;
  return DEMAND_RE.test(String(series || "")) ? -1 : 1;
}
/** A snapshot entry with its displayed sign applied: value, compare/target/next points and the window mean. */
export function signed(e, k) {
  if (!e || k === 1) return e;
  const n = (x) => (x == null ? x : -x);
  return { ...e, v: n(e.v), prev: n(e.prev), target: n(e.target), next: n(e.next), agg: e.agg && { ...e.agg, mean: n(e.agg.mean) } };
}

/** A share's numerator entry: its feature's value, or a row's own series (an extra row's share, step 5). */
const sharePart = (s, snap) => (s.feature ? snap?.values?.[s.feature] : snap?.series?.[s.series]);
export function sharePct(r, snap) {
  const e = sharePart(r.share, snap), t = snap?.series?.[r.share.of];
  if (!e || !t || e.stale || t.stale || (e.unit && t.unit && e.unit !== t.unit)) return null;
  const a = shown(e).v, b = shown(t).v;
  // the part as stored over the size of the whole: a city is +5.2 % of AB demand (both stored positive, only displayed
  // negative), while a net export or storage charging (stored negative) reads as a negative share, never a positive one
  return a == null || b == null || b === 0 ? null : (a / Math.abs(b)) * 100;
}
/**
 * A share's relative uncertainty, or 0 for measured ÷ measured. Numerator: the feature's `band`, else half its
 * `estimate` lo–hi range over its value. Basis: a `band` on its snapshot entry, else the share's `of_band`. The two
 * relative bands combine in quadrature, sqrt(a² + b²) (independent errors), so ±4.2 % on 20 % of a ±3 % total is ±0.9 pp.
 */
export function shareBand(r, snap) {
  const e = sharePart(r.share, snap), t = snap?.series?.[r.share.of];
  let a = Math.abs(r.share.band ?? 0);
  const lo = snap?.series?.[r.share.est?.lo]?.v, hi = snap?.series?.[r.share.est?.hi]?.v, v = e ? shown(e).v : null;
  if (!a && r.share.est && lo != null && hi != null && v) a = Math.abs(hi - lo) / 2 / Math.abs(v);
  const b = Math.abs(t?.band ?? r.share.of_band ?? 0);
  return Math.sqrt(a * a + b * b);
}

/** Share colour by size (PLAN 4c): a neutral ramp, small = muted #6e8d7f, from 25 % = strong #e8f5ee (sqrt, so small shares
 * still separate). Never an identity or alert colour. */
export function shareColor(pct) {
  const t = Math.sqrt(Math.min(1, Math.abs(pct) / 25)), a = [0x6e, 0x8d, 0x7f], b = [0xe8, 0xf5, 0xee];
  return "#" + a.map((x, i) => Math.round(x + (b[i] - x) * t).toString(16).padStart(2, "0")).join("");
}
/**
 * The share on the value line (PLAN 4c): " · 22 %", or " · ≈22 ±4 %" when either side is an estimate (bands in
 * quadrature, shareBand), signed like the value, coloured by size. The basis is named in the tooltip ("22 % of AESO
 * generation"). Nothing when there's no number (missing, stale, zero basis, units differ).
 */
/**
 * A tiny basis cue after an inline share, so "· 13 %" after MW can't read as utilisation (Reviewer on #229): "gen", "exp",
 * "dem", "prod", "load", from the share's label ("% of AESO generation" → "gen"); else the label's last word, cut to 4.
 */
const CUES = [["generation", "gen"], ["export", "exp"], ["demand", "dem"], ["production", "prod"], ["load", "load"],
  ["supply", "sup"], ["consumption", "use"]];
export function shareCue(label) {
  const l = String(label || "").toLowerCase();
  const hit = CUES.find(([w]) => l.includes(w));
  if (hit) return hit[1];
  const w = l.replace(/^\s*%\s*(of\s+)?/, "").trim().split(/\s+/).pop() || "";
  return w.slice(0, 4);
}
export function shareHtml(so, snap) {
  if (!so) return "";
  const q = { share: so }, pct = sharePct(q, snap);
  if (pct == null) return "";
  const a = Math.abs(pct), dec = a < 10 ? 1 : 0, rel = shareBand(q, snap);
  const basis = /^\s*%/.test(so.label) ? so.label : `% of ${so.label}`;
  const pm = rel > 0 ? ` ±${fmt(Math.max(dec ? 0.1 : 1, a * rel), dec)}` : "";
  const tip = `${fmt(pct, 1)} ${basis}${rel > 0 ? ` (estimate ±${Math.round(rel * 100)}%: value and basis bands in quadrature)` : ""}`;
  // L-106 8a: a region fuel row (`bar`) also draws its share as a short bar, so the mix reads at a glance
  const bar = so.bar ? `<span class="sbar" aria-hidden="true"><i style="width:${Math.min(100, a).toFixed(0)}%;background:${shareColor(pct)}"></i></span>` : "";
  return `${bar}<span class="share" style="color:${shareColor(pct)}" title="${esc(tip)}">· ${rel > 0 ? "≈" : ""}${fmt(pct, dec)}${pm} % ${esc(shareCue(so.label))}</span>`;
}

/**
 * River flow vs normal (step 7, Erik; Weather #266): a dam outflow or WSC discharge series has a sibling
 * `<series>_pct_normal`, today's 24 h mean as a % of the median for the same dates (±7 d) in all years. Absent or stale
 * (too little history) → nothing, never 0. `pctNormalId` names the sibling, or null for any other series.
 */
const FLOW_RE = /^(abrivers\.[a-z0-9_]+\.outflow|wsc\.realtime\.[a-z0-9_]+\.discharge)$/;
export function pctNormalId(series) { return series && FLOW_RE.test(series) ? `${series}_pct_normal` : null; }
export function normalPct(series, snap) {
  const e = snap?.series?.[pctNormalId(series)];
  return e && e.v != null && !e.stale ? e.v : null;
}
/**
 * A row's own % of normal (L-085, Weather #334): `normal: "<series>"` names it, `normal: "self"` means the row's value
 * is one (a forecast's % of average). `cue: {above, below}` words it: ≥ above "above normal", ≤ below "below normal",
 * else "near normal". No bullish/bearish reading (Weather: Erik makes that call). A stale % of normal shows only on a
 * row that is stale itself (its STALE chip covers both, e.g. reservoir storage ~3 days behind by design); never next to
 * a fresh value.
 */
export function rowNormal(r, v, snap, rowStale = false) {
  if (r?.normal === "self") return v ?? null;
  const e = r?.normal ? snap?.series?.[r.normal] : null;
  return e && e.v != null && (!e.stale || rowStale) ? e.v : null;
}
export const cueOf = (p, cue) => (p == null || !cue ? null : p >= cue.above ? "above" : p <= cue.below ? "below" : "near");
const CUE_COLOR = { below: "#a9a46c", above: "#5aa0dc", near: "var(--muted)" };   // gauges.js BAND_COLOR low / high
/** " · 84 % of normal" on a river row's value line; low (< 75 %) and high (> 125 %) tinted with the gauge colours. */
export function normalHtml(series, snap, r, v, rowStale = false) {
  const own = r?.normal != null, p = own ? rowNormal(r, v, snap, rowStale) : normalPct(series, snap);
  if (p == null) return "";
  if (own && r.cue) {   // the row's own basis is in its label; the cue word carries the colour
    const c = cueOf(p, r.cue);
    return `${r.normal === "self" ? "" : `<span class="share">· ${fmt(p, 0)} % of normal</span>`}<span class="share cue" style="color:${CUE_COLOR[c]}" title="≥ ${r.cue.above} % above normal, ≤ ${r.cue.below} % below">· ${c} normal</span>`;
  }
  const col = p < 75 ? "#a9a46c" : p > 125 ? "#5aa0dc" : "var(--muted)";   // gauges.js BAND_COLOR low / high
  const tip = "today's 24 h mean ÷ the median for the same dates (±7 days), all years (WSC approved daily means)";
  return `<span class="share" style="color:${col}" title="${esc(tip)}">· ${fmt(p, 0)} % of normal</span>`;
}

/** A slot for an expanded compact box: the first compass slot clear of the screen edges and HUD panels, else the
 * preferred slot clamped into the free band between the HUD panels at the top and bottom. */
function expandedSlot(p, w, h, W, H, huds, prefer = []) {
  const clear = (r) => r.x >= 4 && r.y >= 4 && r.x + r.w <= W - 4 && r.y + r.h <= H - 4 &&
    !huds.some((b) => r.x < b.x + b.w && b.x < r.x + r.w && r.y < b.y + b.h && b.y < r.y + r.h);
  const dirs = [...new Set([...prefer, "N", "S", "E", "W", "NE", "NW", "SE", "SW"])];
  for (const L of [34, 64]) for (const dir of dirs) { const r = slotRect(p, dir, L, w, h); if (clear(r)) return { ...r, dir, L }; }
  const r = slotRect(p, prefer[0] || "N", 34, w, h), x = Math.min(Math.max(4, r.x), W - 4 - w);
  // the free band is taken over the box's own columns (not the anchor's), so it never slides under a top panel
  const over = (b) => b.x < x + w && b.x + b.w > x;
  const top = Math.max(4, ...huds.filter((b) => b.y < H / 3 && over(b)).map((b) => b.y + b.h));
  const bottom = Math.min(H - 4, ...huds.filter((b) => b.y > H / 2 && over(b)).map((b) => b.y));
  return { x, y: Math.min(Math.max(top, r.y), Math.max(top, bottom - h)), w, h, dir: prefer[0] || "N", L: 34 };
}

/** Region names under a shared anchor stack one line apart (e.g. BC production and Westcoast T-North, both at Fort
 * St John): `stack(p)` returns how many names were already drawn at that point. */
const LABEL_STEP = 11;
function labelStack() {
  const seen = new Map();
  return (p) => { const k = `${Math.round(p.x / 4)},${Math.round(p.y / 4)}`, n = seen.get(k) || 0; seen.set(k, n + 1); return n; };
}

/** A monthly series (freq M) and its month, "Aug 2026" (UTC: monthly points sit on the 1st). */
export const isMonthly = (e) => (e?.freq || "").toUpperCase() === "M";
export const monthLabel = (ts) => new Date(ts * 1000).toLocaleString("en-CA", { month: "short", year: "numeric", timeZone: "UTC" });

/** One callout row's HTML from the snapshot: the shown value (a window mean when the entry has `agg`), unit, chips. */
export function rowHtml(r, i, snap, label) {
  // a section head inside a merged box (PLAN step 6): the box's title, or a section's name with its unit and share basis
  if (r.head) return `<div class="row head${r.title ? " title" : ""}" data-i="${i}" data-p="${r.priority || 1}"><div class="hd">${esc(r.head)}</div></div>`;
  const raw = r.feature ? snap?.values?.[r.feature] : snap?.series?.[rowKey(r)];
  const e = signed(raw, flowSign(r.flow, r.feature ? raw?.series : r.series));   // demand reads negative (PLAN 4c)
  const ref = r.ref ? snap?.series?.[r.ref] : null;
  const s = shown(e);
  const est = r.band && s.v != null ? `<span class="est" title="Estimate ±${Math.round(r.band * 100)}%">≈</span>` : "";   // no "≈ --"
  const band = r.band && s.v != null ? `<span class="u bd">±${fmt(Math.abs(s.v) * r.band, 0)}</span>` : "";
  const capTag = r.value_kind === "capacity" && !/capacity/i.test(label) ? '<span class="chip">cap</span>'
    : r.value_kind === "level" ? '<span class="chip" title="a reservoir level, not plant output">level</span>' : "";
  // the window and n come from the payload (agg), never from the label: one place, and it can't contradict the data
  // a monthly series (L-078: StatCan generation, ~2-3 months behind by design) names its month, so it never reads as live
  const tag = s.tag ? `<span class="agg">${esc(s.tag)}</span>`
    : isMonthly(e) && e.ts != null ? `<span class="agg" title="monthly mean">${esc(monthLabel(e.ts))} · monthly</span>` : "";
  // a label starting "· " is a part of the row above (Gas, step 5): indented, without the marker
  const part = /^\s*·\s/.test(label), name = part ? label.replace(/^\s*(·\s*)+/, "") : label;   // "·" / "··": depth (rowTree)
  // L-106 (#428/#433): late by design but not stale: the value is muted and says its age, so it never reads as live
  const late = s.v != null ? lateAge(e) : null;
  const age = late != null ? `<span class="age" title="newest point ${esc(fmtTime(e.ts))} (late by design, not stale)">· ${ageLabel(late)}</span>` : "";
  // PLAN 9 part 2: a v2 region row without a value says so ("—", dimmed, its reason on hover); a known zero shows 0;
  // its tags (est., scope) stay quiet after the label
  const nov = s.v == null && !r.zero && (r.v2 || r.have === false || !!r.why);
  const known0 = !!r.zero && (s.v == null || s.v === 0);   // a known zero (montney/regions.py `zero`): a fact, not a reading
  const vtext = known0 ? "0" : s.v == null ? (nov ? "—" : "--") : fmt(s.v, r.decimals);
  const qt = r.tags?.length ? `<span class="qt">${esc(r.tags.join(" · "))}</span>` : "";
  return `<div class="row${part ? " sub" : ""}${(e?.stale || s.reason) && !known0 ? " stale" : ""}${late != null ? " late" : ""}${nov ? " nov" : ""}" data-i="${i}" data-p="${r.priority || 1}"${nov ? ` title="${esc(r.why || "no value yet")}"` : ""}><div class="k" title="${esc([name, ...(r.tags || [])].join(" · "))}">${esc(name)}${qt}${capTag}</div>
    <div class="vv">${est}<span class="v">${vtext}</span>${band}
    <span class="u">${esc(unitLabel(r.unit || e?.unit))}</span>${tag}${age}${shareHtml(r.shareOf, snap)}${normalHtml(r.feature ? raw?.series : r.series, snap, r, s.v, !!e?.stale)}${nov || known0 ? "" : chipHtml(e, r, ref)}<span class="live">LIVE</span></div></div>`;   // PLAN 9g: "—" and a known zero need no STALE chip
}

export class Callouts {
  /**
   * @param ctx {map, config, list(), anchorPoint(id) -> [lon,lat]|null, featureName(id), onOpen(callout,row),
   *             isRegion(id), nodeRects() -> screen boxes of shown node sprites, levelZoom() -> the hysteretic zoom
   *             for show/hide (map.js)}
   */
  constructor(ctx, layer, svg) {
    Object.assign(this, ctx, { layer, svg, items: [], els: new Map(), st: new Map(), expanded: new Set(), openRows: new Set(),
      compact: false });
    this.ms = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? 0 : GLIDE_MS;
    let raf = 0, dirty = false;
    // One rAF loop: re-place if the map moved, then render, and keep rendering while a glide or fade runs.
    this.kick = () => {
      if (!raf) raf = requestAnimationFrame(() => {
        raf = 0;
        if (dirty) { dirty = false; this.place(); }
        if (this.render(performance.now())) this.kick();
      });
    };
    this.queue = () => { dirty = true; this.kick(); };
    this.map.on("move", this.queue); this.map.on("resize", this.queue);
  }
  /** Placement state per box: its slot, offset from the anchor (gliding), and opacity (fading). Survives updates. */
  state(id) {
    let s = this.st.get(id);
    if (!s) this.st.set(id, (s = { slot: null, shown: false, ox: new Tween(0, this.ms), oy: new Tween(0, this.ms),
      a: new Tween(0, this.ms, 0.01) }));
    return s;
  }
  /** Rebuild box contents from the latest snapshot. Elements and placement state persist across updates. */
  update(snap) {
    const list = this.list(), keep = new Set(list.map((c) => c.id));
    for (const [id, el] of this.els) if (!keep.has(id)) { el.remove(); this.els.delete(id); this.st.delete(id); }
    this.items = list.map((c) => {
      let el = this.els.get(c.id);
      if (!el) {
        el = document.createElement("div");
        el.dataset.id = c.id; el.style.visibility = "hidden";
        if (c.titleOpens) el.dataset.opens = "1";   // L-106 8a: the title opens the region (map.css cursor + ›)
        el.addEventListener("click", (ev) => {
          const it = this.items.find((x) => x.el === el);
          if (ev.target.closest(".cx")) { ev.stopPropagation(); if (it) this.onClose?.(it.c); return; }   // a pinned box's ✕
          // L-106 8a: a region box's title opens the region (balance, fuel mix, watch list), folded or not
          if (it?.c.titleOpens && ev.target.closest(".row.head.title")) { this.onOpen(it.c, { feature: it.c.titleOpens }); return; }
          // Fold / expand (L-069, every screen since L-070, Erik): a click on a folded box opens it in place. On an open box
          // the box's header folds it: its frame, a head row, the ▴ strip, or a headline row's label (the text a folded box
          // shows on top; a box without a title head has no other header, L-070 fold bug). A row's value, and every
          // breakdown row, open that row's panel.
          // PLAN 9: a region box in its compact or chip form doesn't unfold in place (it would no longer fit its region)
          if (it && +el.dataset.more > 0 && !(it.c.region && el.dataset.form && el.dataset.form !== "full")) {
            const id = it.c.id, onRow = ev.target.closest(".row:not(.head)");
            if (!this.expanded.has(id)) { this.expanded.add(id); this.queue(); return; }
            // a row with parts (L-073): its label (with ▸ / ▾) opens or closes them; its number opens its graph
            if (onRow?.classList.contains("parent") && ev.target.closest(".k")) {
              const k = onRow.dataset.key;
              if (this.openRows.has(k)) this.openRows.delete(k); else this.openRows.add(k);
              this.queue(); return;
            }
            const header = ev.target.closest(".more, .row.head") || !onRow || (onRow.dataset.lead === "1" && ev.target.closest(".k"));
            if (header) { this.expanded.delete(id); this.queue(); return; }
          }
          if (ev.target.closest(".row.head")) return;   // a section head opens nothing
          const row = ev.target.closest(".row");
          if (row && it) this.onOpen(it.c, it.c.rows[+row.dataset.i]);
        });
        this.layer.appendChild(el); this.els.set(c.id, el);
      }
      // the frame's tint (map.css), every update: a box merged below `merge_until` is "mixed", and its own system once split
      const sys = this.systemOf?.(c);   // "gas" | "power" | "mixed" | null
      el.className = sys ? `callout ${sys}` : "callout";
      if (c.region) el.classList.add("regionbox");   // PLAN 9: placed by regionlayout.js, in a form (data-form)
      // L-106 region colours: a region box's left edge and title in its region's `color` (the same as its border and tint)
      if (/^#[0-9a-f]{6}$/i.test(c.color || "")) { el.dataset.region = "1"; el.style.setProperty("--region", c.color); }
      else if (el.dataset.region) { delete el.dataset.region; el.style.removeProperty("--region"); }
      const lead = leadRows(c.rows);
      const rows = c.rows.map((r, i) => rowHtml(r, i, snap, r.label || (r.feature ? this.featureName(r.feature) : r.series))
        .replace('<div class="row', `<div data-lead="${lead[i] ? 1 : 0}"${r.compact ? ' data-compact="1"' : ""} class="row`));
      el.innerHTML = (c.closable ? '<button class="cx" title="Close">✕</button>' : "") + rows.join("") + '<div class="more"></div>';
      // the row tree (L-073): depth, parent and parts per row; a row with parts gets a ▸ / ▾ before its label
      const { depth, parent } = rowTree(c.rows), kids = c.rows.map((_, i) => parent.filter((p) => p === i).length);
      for (const row of el.querySelectorAll?.(".row") || []) {
        const i = +row.dataset.i, d = depth[i], r = c.rows[i];
        if (d == null) continue;
        Object.assign(row.dataset, { depth: d, parent: parent[i], key: `${c.id}|${rowKey(r) || r.feature || r.label}` });
        row.classList.toggle("sub", d >= 2);
        if (d >= 3) row.style.paddingLeft = `${10 * (d - 1)}px`;
        if (d >= 1 && kids[i]) { row.classList.add("parent"); row.querySelector(".k")?.insertAdjacentHTML("afterbegin", '<span class="tw"></span>'); }
      }
      return { c, el, sizes: null };   // a region box's form sizes: measured again on the next place()
    });
    this.place();
    if (this.render(performance.now())) this.kick();
  }
  anchorOf(c) {
    if (typeof c.anchor === "string") {
      const at = this.anchorPoint(c.anchor); if (!at) return null;
      const reg = this.isRegion?.(c.anchor);
      return { at, region: !!reg, label: reg || undefined };
    }
    return { at: c.anchor.at, label: c.anchor.label, region: !c.anchor.point };   // `point`: a plain endcap, no ring
  }
  /** Decide which boxes show and their target slots. render() draws them, gliding and fading to the targets. */
  place() {
    const map = this.map, z = this.levelZoom?.() ?? map.getZoom(), now = performance.now();
    const W = map.getContainer().clientWidth, H = map.getContainer().clientHeight;
    const occupied = [...document.querySelectorAll(".hud:not([hidden]), .maplibregl-ctrl-attrib")]
      .map((e) => e.getBoundingClientRect()).filter((r) => r.width)
      .map((r) => ({ x: r.left - 4, y: r.top - 4, w: r.width + 8, h: r.height + 8, hud: true }));
    const huds = occupied.slice();   // panels and controls: the only thing an expanded box must clear
    // Node clearance is "soft": tier-0/1 boxes may cover a minor node as a last resort, never another box or panel.
    for (const r of this.nodeRects?.() || []) occupied.push({ ...r, soft: true });
    const anchors = new Map(), stack = labelStack();
    for (const { c } of this.items) {
      const a = this.anchorOf(c);
      if (!a) continue;
      const p = map.project(a.at); anchors.set(c.id, { ...a, p });
      if (a.region && shownAt(c, z)) { const w = Math.max(14, (a.label || "").length * 6 + 8), n = stack(p);
        occupied.push({ x: p.x - w / 2, y: p.y - 7 + n * LABEL_STEP, w, h: 30 }); }
    }
    const hit = (a, strict = true) => a.x < 4 || a.y < 4 || a.x + a.w > W - 4 || a.y + a.h > H - 4 ||
      occupied.some((b) => (strict || !b.soft) && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h);
    // Rows carry their own priority (e.g. a region's secondary values): hide them below their zoom tier. Every box
    // (L-070) folds to its lead rows (headline values with their daily change) until clicked, and says how many rows wait.
    const compact = (this.compact = isCompact(W, H));
    this.layer.classList.toggle("compact", compact);
    // Nested parts (L-073): a row two or more levels deep shows only while its parent shows and is opened; then its
    // row priority no longer hides it (the user asked for it).
    for (const { c, el } of this.items) {
      const open = this.expanded.has(c.id), shown = new Map();
      let extra = 0, foldable = 0;
      for (const row of el.querySelectorAll(".row")) {
        const d = row.dataset.depth == null ? null : +row.dataset.depth, par = row.parentElement?.querySelector(`.row[data-i="${row.dataset.parent}"]`);
        // a headline (tree depth 0: a box's or section's first row, or a `depth: 0` row, Gas #354) shows with its box at
        // any zoom; the row-priority zoom gate is for parts
        const zoomOk = d === 0 || z >= (ROW_MINZOOM[+row.dataset.p] ?? 0), lead = row.dataset.lead === "1", head = row.classList.contains("head");
        const show = d != null && d >= 2 ? !!shown.get(+row.dataset.parent) && this.openRows.has(par?.dataset.key) : zoomOk && (open || lead);
        if (!head && !lead) { foldable++; if (zoomOk && (d == null || d <= 1)) extra++; }
        shown.set(+row.dataset.i, show);
        row.style.display = show ? "" : "none";
        const tw = row.querySelector(":scope > .k > .tw");
        if (tw) { const o = this.openRows.has(row.dataset.key); tw.textContent = o ? "▾ " : "▸ "; tw.parentElement.title = o ? "Hide the parts" : "Show the parts"; }
      }
      el.classList.toggle("open", open && foldable > 0);
      const more = el.querySelector(".more"), hint = !foldable ? "" : open ? "▴" : `+${extra || foldable} ▾`;
      el.dataset.more = String(foldable);
      if (more) { more.textContent = hint; more.hidden = !hint; more.title = open ? "Fold" : "Show the breakdown"; }
    }
    // PLAN 9 part 1 (Erik: "keep the boxes over their designated geographic region"): region boxes first, by their own
    // rules (regionlayout.js): centred on their anchor in the largest form that fits, stepping down instead of drifting,
    // outside with a leader only when the region is too small. Screen edges play no part, so a pan never changes them.
    const regionIt = this.items.filter(({ c }) => c.region && anchors.get(c.id) && shownAt(c, z));
    if (regionIt.length) {
      for (const it of regionIt) if (!it.sizes || it.sizesKey !== `${compact}|${this.expanded.has(it.c.id)}`) this.measureForms(it, compact);
      const lay = layoutRegions(regionIt.map(({ c, sizes }) => ({ id: c.id, p: anchors.get(c.id).p, ext: this.extentOf(c.region.ext),
        size: sizes, dir: c.region.dir })), this.regionForms || {});
      this.regionForms = {};
      for (const it of regionIt) {
        const L = (it.lay = lay[it.c.id]);
        this.regionForms[it.c.id] = L.out ? "out" : L.form;
        if (L.form !== "none") occupied.push({ x: L.x, y: L.y, w: L.w, h: L.h });   // every other box keeps clear of it
      }
    }
    let budget = compact ? boxBudget(W, H) : Infinity;
    // Where each shown box's centre is on screen right now, before this placement: the start point of a handoff.
    const was = new Map();
    for (const { c, el } of this.items) {
      const s = this.st.get(c.id), a = anchors.get(c.id);
      if (s?.shown && a) was.set(c.id, { x: a.p.x + s.ox.at(now) + el.offsetWidth / 2,
        y: a.p.y + s.oy.at(now) + el.offsetHeight / 2, alpha: s.a.at(now) });
    }
    const handedOff = [];
    // Placement order: tier, then system/region boxes (map.json, region anchors) before individual features.
    const rank = (c) => c.rank ?? (!c.id.startsWith("f:") ? 0 : this.isRegion?.(c.anchor) ? 1 : c.anchor.startsWith("gas.") ? 2 : 3);
    // a box the user opened (compact: tapped to expand) places first, so growing never pushes it off the map
    const opened = (c) => (this.expanded.has(c.id) ? 0 : 1);
    for (const { c, el } of [...this.items].sort((a, b) => opened(a.c) - opened(b.c) || tierOf(a.c) - tierOf(b.c) || rank(a.c) - rank(b.c))) {
      const a = anchors.get(c.id), s = this.state(c.id);
      if (c.region) {   // laid out above (regionlayout.js): its form, its rect, a leader only when it sits outside its region
        const L = a && shownAt(c, z) && regionIt.length ? this.items.find((x) => x.c === c)?.lay : null;
        if (!L || L.form === "none") { if (s.shown) { s.shown = false; s.a.set(0, now); } continue; }
        el.dataset.form = L.form; s.w = L.w; s.h = L.h; s.leader = L.leader;
        const ox = L.x - a.p.x, oy = L.y - a.p.y;
        if (!s.shown && s.a.at(now) < 0.02) { s.ox.jump(ox); s.oy.jump(oy); }
        s.ox.set(ox, now); s.oy.set(oy, now); s.a.set(1, now); s.shown = true;
        continue;
      }
      s.w = el.offsetWidth; s.h = el.offsetHeight;   // cached for render(), which then never forces a layout
      let placed = null, from = null;
      const anyRow = [...el.querySelectorAll(".row")].some((r) => r.style.display !== "none");
      if (a && anyRow && shownAt(c, z) && a.p.x > 0 && a.p.y > 0 && a.p.x < W && a.p.y < H) {
        // In-place handoff: an overview box and its breakdown box (`partner`, set in map.js) swap at the same spot.
        if (!s.shown && c.partner && was.has(c.partner)) from = was.get(c.partner);
        const open = this.expanded.has(c.id);
        placed = chooseSlot({ p: a.p, w: el.offsetWidth, h: el.offsetHeight, last: from ? null : s.slot, prefer: c.prefer, near: from,
          hit, lenient: tierOf(c) <= 1 || open });
        // A box the user expanded must show: it may cover region labels and dots, and if no slot by its anchor
        // fits, it is shifted into the screen (its leader still points at the anchor).
        if (!placed && open) placed = expandedSlot(a.p, el.offsetWidth, el.offsetHeight, W, H, huds, c.prefer);
      }
      // Fade out where it is. A fading box no longer reserves its space (it's not pushed to `occupied`), so a box
      // placed this frame may briefly overlap it while it fades (200 ms); that's accepted.
      const free = c.always_show || c.closable || this.expanded.has(c.id);   // always_show, pinned, expanded: outside the budget
      if (placed && !free && budget <= 0) placed = null;   // compact: the space budget is spent (L-069)
      if (!placed) { if (s.shown) { s.shown = false; s.a.set(0, now); } continue; }
      if (!free) budget--;
      occupied.push(placed);
      s.slot = { dir: placed.dir, L: placed.L };
      const ox = placed.x - a.p.x, oy = placed.y - a.p.y;
      if (from) {   // start on the partner's box, then glide to our own slot
        s.ox.jump(from.x - el.offsetWidth / 2 - a.p.x); s.oy.jump(from.y - el.offsetHeight / 2 - a.p.y);
        s.a.jump(Math.max(from.alpha, s.a.at(now)));
        handedOff.push(c.partner);
      } else if (!s.shown && s.a.at(now) < 0.02) { s.ox.jump(ox); s.oy.jump(oy); }   // appearing: fade in at its slot
      s.ox.set(ox, now); s.oy.set(oy, now); s.a.set(1, now);
      s.shown = true;
    }
    // The box that handed off disappears at once, so the two never show side by side.
    for (const id of handedOff) { const s = this.st.get(id); if (s && !s.shown) s.a.jump(0); }
  }
  /** A region box's size in each form (FORMS), measured once per content / screen class / open state, not per frame. */
  measureForms(it, compact) {
    const el = it.el, keep = el.dataset.form, sizes = {};
    for (const f of FORMS) { el.dataset.form = f; sizes[f] = { w: el.offsetWidth, h: el.offsetHeight }; }
    if (keep) el.dataset.form = keep; else delete el.dataset.form;
    it.sizes = sizes; it.sizesKey = `${compact}|${this.expanded.has(it.c.id)}`;
  }
  /** A region's on-screen extent: its bbox [w, s, e, n] projected at the current zoom (a pan only translates it). */
  extentOf(ext) {
    if (!ext) return { w: 0, h: 0 };
    const a = this.map.project([ext[0], ext[3]]), b = this.map.project([ext[2], ext[1]]);
    return { w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) };
  }
  /** Draw boxes and leaders at time `now`. Returns true while a glide or fade is still running. */
  render(now) {
    const map = this.map;
    let lines = "", busy = false;
    const stack = labelStack();
    for (const { c, el } of this.items) {
      const s = this.st.get(c.id), alpha = s ? s.a.at(now) : 0;
      if (s) busy ||= s.a.busy(now) || s.ox.busy(now) || s.oy.busy(now);
      const at = alpha > 0.01 ? this.anchorOf(c) : null;
      // a hidden box keeps its last (or 0,0) slot; Chromium still draws its backdrop blur there, smearing whatever box is
      // placed on top of that spot, so the blur goes with the box
      if (!at) { el.style.visibility = "hidden"; el.style.backdropFilter = "none"; continue; }
      const p = map.project(at.at), w = s.w ?? el.offsetWidth, h = s.h ?? el.offsetHeight;
      const x = p.x + s.ox.at(now), y = p.y + s.oy.at(now);
      el.style.visibility = ""; el.style.backdropFilter = ""; el.style.pointerEvents = s.shown ? "" : "none";
      el.style.setProperty("--a", alpha.toFixed(3));
      el.style.transform = `translate(${Math.round(x)}px,${Math.round(y)}px)`;
      const ex = Math.max(x, Math.min(p.x, x + w)), ey = Math.max(y, Math.min(p.y, y + h));
      if (c.region) {   // PLAN 9: centred boxes have no leader; an outside box points at its anchor in its region's colour
        if (s.leader && /^#[0-9a-f]{6}$/i.test(c.color || "")) {
          const sx = (v) => Math.floor(v) + 0.5, seg = `x1="${sx(p.x)}" y1="${sx(p.y)}" x2="${sx(ex)}" y2="${sx(ey)}"`;
          lines += `<g opacity="${alpha.toFixed(3)}"><line ${seg} class="leader-case"/><line ${seg} class="leader" style="stroke:${c.color}"/>` +
            `<circle cx="${p.x}" cy="${p.y}" r="3" class="rdot" style="fill:${c.color}"/></g>`;
        }
        continue;
      }
      // Pixel leader (docs/art/CALLOUT_PROPOSAL.md, L-041 Q10): a 1 px #7f948a line on a 3 px #020504 casing, drawn to the
      // actual endpoints with ends snapped to pixel centres (crisp at odd widths). Boxes and anchors themselves don't move.
      const sx = (v) => Math.floor(v) + 0.5, reg = at.region ? " region" : "", n = at.region ? stack(p) : 0;
      const seg = `x1="${sx(p.x)}" y1="${sx(p.y)}" x2="${sx(ex)}" y2="${sx(ey)}"`;
      lines += `<g opacity="${alpha.toFixed(3)}"><line ${seg} class="leader-case${reg}"/><line ${seg} class="leader${reg}"/>` +
        (at.region ? (n ? "" : `<circle cx="${p.x}" cy="${p.y}" r="5" class="ring"/>`) +
                     `<text x="${p.x}" y="${p.y + 16 + n * LABEL_STEP}" class="rlab">${esc(at.label || "")}</text>`
                   // endcap (Art's handoff): 4×4 px, a 2×2 #d3ece0 centre inside a 1 px #020504 outline; the leader's
                   // pixel-centred end falls inside the light centre
                   : `<rect x="${Math.floor(p.x) - 1}" y="${Math.floor(p.y) - 1}" width="4" height="4" class="cap-case"/>` +
                     `<rect x="${Math.floor(p.x)}" y="${Math.floor(p.y)}" width="2" height="2" class="cap"/>`) + "</g>";
    }
    this.svg.innerHTML = lines;
    return busy;
  }
}
