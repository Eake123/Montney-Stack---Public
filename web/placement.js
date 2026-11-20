// Callout placement logic with no DOM or map dependency (PLAN P4a: callouts must not jump between zoom levels).
// callouts.js does the DOM work. tests/test_web.py runs these functions under node.

export const DIRS = { N: [0, -1], NE: [1, -1], E: [1, 0], SE: [1, 1], S: [0, 1], SW: [-1, 1], W: [-1, 0], NW: [-1, -1] };
export const LEADERS = [34, 64, 100];       // leader lengths in px, shortest first
// Width of the dead band around each zoom-level boundary. Going in, a level switches at b + HYST/2.
// Coming back out, it switches at b - HYST/2. So a pinch that wobbles across b never flips boxes or dots.
export const HYST = 0.15;

/**
 * Hysteretic "level zoom": the zoom used for every discrete show/hide decision (box tiers, rows, dots, pins).
 * `prev` is the previous level zoom (null on first call) and `bounds` the zoom-level boundaries. Near a boundary
 * it holds the previous side until `z` is more than h/2 past it. Away from boundaries it is just `z`.
 */
export function levelZoom(z, prev, bounds, h = HYST) {
  if (prev == null || !Number.isFinite(prev)) return z;
  let ze = z;
  for (const b of bounds) {
    if (prev < b && z >= b && z < b + h / 2) ze = Math.min(ze, b - 1e-6);       // was below: stay below
    else if (prev >= b && z < b && z >= b - h / 2) ze = Math.max(ze, b);        // was above: stay above
  }
  return ze;
}

/** The box rect for compass `dir` at leader length `L` from anchor pixel `p` (vertical offsets × 0.8). */
export function slotRect(p, dir, L, w, h) {
  const [dx, dy] = DIRS[dir];
  return { x: p.x + dx * L - (dx < 0 ? w : dx === 0 ? w / 2 : 0), y: p.y + dy * L * 0.8 - (dy < 0 ? h : dy === 0 ? h / 2 : 0), w, h };
}

/**
 * Pick a slot {dir, L, x, y, w, h, kept} for one box, or null when nothing fits.
 * - `last` ({dir, L}): the slot used last time. It is kept unless a hard collision forces a move. Hard means
 *   `hit(rect, strict=!lenient)`: boxes that may cover a minor dot as a last resort (`lenient`) aren't moved off one.
 * - `near` ({x, y}): a handoff. A box replacing its overview/breakdown partner takes the free slot nearest the
 *   partner's centre, so it glides as little as possible.
 * - otherwise: the old side at other leader lengths first, then `prefer` order, shortest leader first.
 * `hit(rect, strict)` is true when the rect is off screen or overlaps something. strict=false ignores soft obstacles.
 */
export function chooseSlot({ p, w, h, last = null, prefer = [], near = null, hit, lenient = false }) {
  if (last && DIRS[last.dir]) {
    const r = slotRect(p, last.dir, last.L, w, h);
    if (!hit(r, !lenient)) return { ...r, dir: last.dir, L: last.L, kept: true };
  }
  const dirs = [...new Set([...(last ? [last.dir] : []), ...prefer, ...Object.keys(DIRS)].filter((d) => DIRS[d]))];
  let cands = [];
  for (const L of LEADERS) for (const dir of dirs) cands.push({ dir, L, r: slotRect(p, dir, L, w, h) });
  if (near) {
    const d = (c) => Math.hypot(c.r.x + w / 2 - near.x, c.r.y + h / 2 - near.y);
    cands = cands.map((c, i) => ({ ...c, i, d: d(c) })).sort((a, b) => a.d - b.d || a.i - b.i);
  } else if (last) {
    cands.sort((a, b) => (b.dir === last.dir) - (a.dir === last.dir));   // stable: same side first
  }
  for (const strict of lenient ? [true, false] : [true])
    for (const c of cands) if (!hit(c.r, strict)) return { ...c.r, dir: c.dir, L: c.L, kept: false };
  return null;
}

// ---- glide and fade
export const GLIDE_MS = 200;
export const ease = (t) => (t >= 1 ? 1 : t <= 0 ? 0 : 1 - (1 - t) ** 3);   // ease-out cubic

/** A value that eases to new targets over `ms`. `at(now)` is pure, so rendering any frame is deterministic. */
export class Tween {
  /** `eps`: target changes smaller than this snap instead of starting a new glide (sub-pixel jitter). */
  constructor(v, ms = GLIDE_MS, eps = 0.5) { Object.assign(this, { from: v, to: v, t0: -Infinity, ms, eps }); }
  at(now) { return this.from + (this.to - this.from) * (this.ms > 0 ? ease((now - this.t0) / this.ms) : 1); }
  set(v, now) {
    if (v === this.to) return;
    if (Math.abs(v - this.to) < this.eps && !this.busy(now)) { this.jump(v); return; }
    this.from = this.at(now); this.to = v; this.t0 = now;
  }
  jump(v) { this.from = this.to = v; this.t0 = -Infinity; }
  busy(now) { return now - this.t0 < this.ms; }
}

/**
 * The lowest zoom this map can reach (L-087): its minZoom, or the zoom where the screen is as wide or as tall as
 * maxBounds (MapLibre keeps the view inside the bounds), whichever is higher. 512 px tiles, Web Mercator.
 */
export function floorZoom(map, maxBounds) {
  const min = map.getMinZoom();
  if (!maxBounds) return min;
  const [[w, s], [e, n]] = maxBounds, c = map.getContainer();
  const y = (lat) => { const r = Math.sin((lat * Math.PI) / 180); return 0.5 - Math.log((1 + r) / (1 - r)) / (4 * Math.PI); };
  const zw = Math.log2(c.clientWidth / (512 * ((e - w) / 360))), zh = Math.log2(c.clientHeight / (512 * (y(s) - y(n))));
  return Math.max(min, zw, zh);
}

/**
 * Time-bar day labels (Reviewer on #387: a phone's narrow bar piled them up, "NoNoNo…"): label every `step`-th of `n`
 * days so each label gets `gap` × its width, from the measured track and label widths; 0 = too narrow for even two
 * labels, so none. One rule on every screen (no breakpoint).
 */
export function tickStep(n, trackPx, labelPx, gap = 1.5) {
  if (!(n > 0) || !(trackPx > 0) || !(labelPx > 0)) return 0;
  const fit = Math.floor(trackPx / (labelPx * gap));
  return fit < 2 ? 0 : Math.max(1, Math.ceil(n / fit));
}
