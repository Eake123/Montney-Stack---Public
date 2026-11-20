// Region box layout (PLAN 9 part 1, Erik 2026-11-13: "at any zoom level it should keep the boxes over their designated
// geographic region"). A pure function of the boxes' anchor points, region extents and form sizes, all in screen px at the
// current (hysteretic) level zoom. Panning only translates those, and nothing here looks at the screen edges, so a pan can
// never change a box's form or place. No imports, so tests/test_web.py runs it under node.
//
// Rules (docs/WIDGETS.md "Region box placement"):
//  1. A box's home is its region: when it fits (FIT), it is centred on the region's anchor, with no leader.
//  2. Boxes never overlap. When two would, the box of the smaller region steps down a form instead of drifting:
//     full -> compact (title + two headline numbers) -> chip (title only) -> outside.
//  3. A region too small on screen for even a chip gets its box just outside it, in the direction its region file names
//     (`box.dir`; if that side is taken, the nearest free one), with a short leader in the region's colour, never over
//     another box or across one.
//  4. A centred box never hides a smaller region's box (PLAN 10a: at the floor zoom Québec's full box covered New
//     England's anchor, so its leader had nowhere to start): when a region gets no spot and a centred box covers its
//     anchor, that box steps down a form and the layout runs again.
//  Upgrades need room to spare (hysteresis), so nothing flips back and forth while pinching.

export const FORMS = ["full", "compact", "chip"];
// a form fits its region when it's at most this many times the region's on-screen extent: the full box must sit inside its
// region's bbox, a compact one may spill a little, a chip (one line) a little more
export const FIT = { full: 1.0, compact: 1.2, chip: 1.6 };
export const UPGRADE = 0.85;  // ...and a form bigger than the box's last one must fit with this much to spare (hysteresis)
export const PAD = 6;         // px kept between boxes
export const UPGRADE_PAD = 14; // px a box growing past its last form must keep
export const OUT_GAP = 10;    // px between a region's edge and its outside box
export const OUT_STEP = 14;   // px an outside box moves out along its direction when its first spot is taken
export const OUT_TRIES = 6;
export const DIRS = { N: [0, -1], NE: [1, -1], E: [1, 0], SE: [1, 1], S: [0, 1], SW: [-1, 1], W: [-1, 0], NW: [-1, -1] };

const rank = (f) => FORMS.indexOf(f);
const RING = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
/** The eight directions, `dir` first (E when unset), then by angular closeness to it, clockwise first on ties. */
export function dirsFrom(dir) {
  const i = Math.max(0, RING.indexOf(dir || "E")), out = [RING[i]];
  for (let k = 1; k <= 4; k++) { out.push(RING[(i + k) % 8]); if (k < 4) out.push(RING[(i - k + 8) % 8]); }
  return out.map((d) => DIRS[d]);
}
const overlap = (a, b, pad) => a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
const centred = (r, f) => ({ x: r.p.x - r.size[f].w / 2, y: r.p.y - r.size[f].h / 2, w: r.size[f].w, h: r.size[f].h });

/** Does the segment (x1,y1)-(x2,y2) cross the rectangle? (Liang-Barsky clip.) */
export function segHitsRect(x1, y1, x2, y2, b) {
  let t0 = 0, t1 = 1;
  const dx = x2 - x1, dy = y2 - y1;
  for (const [p, q] of [[-dx, x1 - b.x], [dx, b.x + b.w - x1], [-dy, y1 - b.y], [dy, b.y + b.h - y1]]) {
    if (p === 0) { if (q < 0) return false; continue; }
    const t = q / p;
    if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
  }
  return true;
}

/** The largest form that fits the region's extent; a form above `last` must fit with UPGRADE to spare. null: none fits. */
export function fitForm(r, last) {
  for (const f of FORMS) {
    // `last` outside or hidden counts as below every form, so coming back in needs the margin too
    const lastRank = !last ? -1 : FORMS.includes(last) ? rank(last) : FORMS.length;
    const k = rank(f) >= lastRank || lastRank < 0 ? FIT[f] : FIT[f] * UPGRADE, s = r.size[f];   // only a bigger form needs the margin
    if (s && s.w <= r.ext.w * k && s.h <= r.ext.h * k) return f;
  }
  return null;
}

/**
 * regions: [{ id, p: {x, y} anchor, ext: {w, h} region extent, size: {full, compact, chip}: {w, h}, dir: "E" }]
 * prev: { id: form } from the last layout (hysteresis). Returns { id: { form, x, y, w, h, leader, out } }, where form is
 * "full" | "compact" | "chip" (centred, or outside when `out`) or "none" (no room anywhere: the outline and name stay).
 */
export function layoutRegions(regions, prev = {}) {
  const area = (r) => r.ext.w * r.ext.h;
  // bigger regions keep their form first; ties by id, so the result never depends on input order
  const order = [...regions].sort((a, b) => area(b) - area(a) || (a.id < b.id ? -1 : 1));
  const cap = new Map();   // rule 4: the largest form a box may take (rank), after it hid a smaller region's box
  for (let pass = 0; pass <= regions.length * FORMS.length; pass++) {
    const res = layoutPass(order, prev, cap);
    const hidden = order.filter((r) => res[r.id].form === "none");
    // the biggest centred box over a hidden region's anchor steps down (bigger regions first, so the result is stable)
    const cover = hidden.map((h) => order.find((o) => o !== h && res[o.id].form !== "none" && !res[o.id].out
      && h.p.x >= res[o.id].x - PAD && h.p.x <= res[o.id].x + res[o.id].w + PAD && h.p.y >= res[o.id].y - PAD && h.p.y <= res[o.id].y + res[o.id].h + PAD)).find(Boolean);
    if (!cover) return res;
    cap.set(cover.id, rank(res[cover.id].form) + 1);
  }
  return layoutPass(order, prev, cap);
}

function layoutPass(order, prev, cap) {
  const capped = (r, f) => (f && cap.has(r.id) && rank(f) < cap.get(r.id) ? FORMS[cap.get(r.id)] ?? null : f);
  const form = new Map(order.map((r) => [r.id, capped(r, fitForm(r, prev[r.id]))]));
  const regions = order;
  const growing = (r) => { const p = prev[r.id]; return !p || p === "out" || p === "none" || rank(form.get(r.id)) < rank(p); };
  // rule 2: while two centred boxes overlap, the smaller region's box steps down (chip -> outside)
  for (let guard = 0; guard < regions.length * FORMS.length + 1; guard++) {
    let clash = null;
    for (let i = 0; i < order.length && !clash; i++) {
      const a = order[i], fa = form.get(a.id); if (!fa) continue;
      for (let j = i + 1; j < order.length; j++) {
        const b = order[j], fb = form.get(b.id); if (!fb) continue;
        const pad = growing(a) || growing(b) ? UPGRADE_PAD : PAD;
        if (overlap(centred(a, fa), centred(b, fb), pad)) { clash = b; break; }   // b: the smaller region (order)
      }
    }
    if (!clash) break;
    const f = form.get(clash.id);
    form.set(clash.id, rank(f) < FORMS.length - 1 ? FORMS[rank(f) + 1] : null);
  }
  const out = {}, boxes = [];
  for (const r of order) {
    const f = form.get(r.id);
    if (f) { const b = centred(r, f); out[r.id] = { form: f, ...b, leader: false, out: false }; boxes.push(b); }
  }
  // rule 3: outside, in the region's direction, beyond its edge; never over a box, and its leader never across one
  for (const r of order) {
    if (form.get(r.id)) continue;
    let done = null;
    // its own direction first, then the others nearest to it (deterministic), so a taken side never hides the box
    for (const [dx, dy] of dirsFrom(r.dir)) for (const f of ["compact", "chip"]) {
      if (done) break;
      const s = r.size[f]; if (!s) continue;
      for (let k = 0; k < OUT_TRIES && !done; k++) {
        const d = OUT_GAP + k * OUT_STEP;
        // the box's near edge sits d px beyond the region's extent along the direction
        const cx = r.p.x + dx * (r.ext.w / 2 + d + s.w / 2), cy = r.p.y + dy * (r.ext.h / 2 + d + s.h / 2);
        const b = { x: cx - s.w / 2, y: cy - s.h / 2, w: s.w, h: s.h };
        const ex = Math.max(b.x, Math.min(r.p.x, b.x + b.w)), ey = Math.max(b.y, Math.min(r.p.y, b.y + b.h));
        if (boxes.some((o) => overlap(b, o, PAD) || segHitsRect(r.p.x, r.p.y, ex, ey, o))) continue;
        done = { form: f, ...b, leader: true, out: true };
      }
    }
    out[r.id] = done || { form: "none", leader: false, out: false };
    if (done) boxes.push(done);
  }
  return out;
}
