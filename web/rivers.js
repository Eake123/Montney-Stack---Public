// River flow particles (PLAN roadmap step 4, P5d; L-028: particles snapped to the pixel grid, every animation encodes
// data). The Peace first: web/rivers.geojson (built by docs/research/frontend-proto/build_rivers.py) gives the
// centreline and its reaches; each reach's discharge comes from its upstream dam (power.hydro.<plant>.flow) or WSC
// gauge (/api/snapshot `gauges`). The pure functions at the top have no DOM use, so tests/test_web.py runs them in node.

/**
 * The wait before the next particle frame (L-102). Particles sit on whole device pixels, so a frame in which the fastest
 * moving particle in view has not crossed one device pixel draws the same image: wait at least as long as it takes to
 * cross one (motion is advanced by real time, so speed is unchanged), and never less than the power state's frame time.
 * `maxPxS`: the fastest in-view speed in CSS px/s; `pr`: device pixels per CSS pixel.
 */
export function riverFrameMs(frameMs, maxPxS, pr) {
  return maxPxS > 0 && pr > 0 ? Math.max(frameMs, 1000 / (maxPxS * pr)) : frameMs;
}

/** Reference discharge: particle speed and density are ∝ √(Q / Q_REF), capped at CAP (Peace ~1-2.5k m³/s, AB rivers 10s-100s). */
export const Q_REF = 1500;
export const CAP = 2.5;
export const SPEED_PX_S = 14;    // screen px/s at Q = Q_REF (the same at every zoom)
export const PER_KM = 1 / 6;     // particles per km of reach at Q = Q_REF
export const FRAME_MS = 33;      // the layer asks for a repaint at most ~30 times a second, only while something moves

/**
 * Seconds to advance particles this frame. While the loop runs, the real time since the last frame (capped at 1 s), so
 * speed stays true even on a slow machine that renders a few frames a second. When the loop restarts (it was frozen,
 * off-screen or hidden), 0: particles resume from where they stopped instead of jumping.
 */
export function frameDt(wasLooping, last, now) {
  return wasLooping ? Math.min(1, Math.max(0, now - last) / 1000) : 0;
}

/**
 * A reach's discharge and whether it may move. Dam reach: the flow series' snapshot entry must exist, be fresh (not
 * stale) and positive. Gauge reach: the gauge's state must be "ok" (stale / no-result / seasonal never move: no invented
 * motion). Returns { q, moving, reason }; q may be known while frozen (it then sets a static density).
 */
export function reachFlow(src, snap) {
  // a reservoir level, or a dam with no live release: nothing to draw (no particles at all, not frozen ones)
  if (src.type === "none") return { q: null, moving: false, none: true, reason: src.reason || "no live flow" };
  if (src.type === "dam" || src.type === "series") {   // a dam's flow series (Peace) or an AB Rivers outflow (step 5)
    const e = snap?.series?.[src.id];
    if (!e || e.v == null) return { q: null, moving: false, reason: "no outflow reported" };
    if (e.stale) return { q: e.v, moving: false, reason: "outflow stale" };
    return e.v > 0 ? { q: e.v, moving: true, reason: null } : { q: e.v, moving: false, reason: "no outflow" };
  }
  const g = snap?.gauges?.[src.id];
  if (!g) return { q: null, moving: false, reason: "gauge not reported" };
  // seasonal (open water only): Weather's spec may name the release above as the winter source; else frozen and flagged
  if (g.state === "seasonal") {
    if (src.fallback) { const f = reachFlow(src.fallback, snap); return { ...f, seasonal: true, reason: f.reason || "seasonal gauge: the release above" }; }
    return { q: null, moving: false, seasonal: true, reason: g.reason || "seasonal gauge, no data" };
  }
  // a fresh measured value moves: state ok, or no-result (too little history for a percentile) with a value; never stale
  if (g.state === "ok" || (g.state === "no-result" && g.value != null)) {
    return g.value > 0 ? { q: g.value, moving: true, reason: null } : { q: g.value, moving: false, reason: "no flow" };
  }
  return { q: g.value ?? null, moving: false, reason: g.reason || g.state };
}

/** Speed (screen px/s) and particle count for a reach of `km` length: both ∝ √(Q / Q_REF), capped. Frozen: speed 0; none: no particles. */
export function particleSpec(flow, km) {
  if (flow.none) return { speed: 0, n: 0, rel: 0 };
  // ∝ √(Q / Q_REF), capped: one rule for the Peace (~1,200 m³/s) and a 30 m³/s Oldman, the bigger river still faster
  const rel = flow.q != null && flow.q > 0 ? Math.min(Math.sqrt(flow.q / Q_REF), CAP) : 0;
  const n = rel > 0 ? Math.max(1, Math.round(km * PER_KM * rel)) : Math.max(1, Math.round(km * PER_KM * 0.25));
  return { speed: flow.moving ? SPEED_PX_S * rel : 0, n, rel };
}

const mercX = (lon) => (lon + 180) / 360;
const mercY = (lat) => { const s = Math.sin((lat * Math.PI) / 180); return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI); };
const kmBetween = (a, b) => {
  const R = 6371, t = Math.PI / 180, dLat = (b[1] - a[1]) * t, dLon = (b[0] - a[0]) * t;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * t) * Math.cos(b[1] * t) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
};

/**
 * MapLibre custom layer, inserted BELOW the network layers so every data line draws over it (L-028 G1 by construction).
 * Particles are square 2-device-px points whose centres the vertex shader snaps to the device pixel grid (pixel art).
 * Moving particles: #d3ece0; frozen reaches: dim #7f948a dots that never move.
 */
export class RiverLayer {
  /** `frameMs()`: the ms between particle frames (L-080: from the power state; FRAME_MS if not given). */
  constructor(id, { shouldRun = () => true, frameMs = () => FRAME_MS } = {}) {
    Object.assign(this, { id, type: "custom", renderingMode: "2d", reaches: [], snap: null, shouldRun, frameMs, last: 0, looping: false, opacity: 1 });
  }
  /** River FeatureCollection (web/rivers.geojson): precompute per-reach polylines with cumulative km. */
  setRivers(fc) {
    this.reaches = [];
    for (const f of fc.features) {
      const c = f.geometry.coordinates;
      for (const r of f.properties.reaches) {
        const pts = c.slice(r.from, r.to + 1), cum = [0];
        for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + kmBetween(pts[i - 1], pts[i]));
        const lons = pts.map((q) => q[0]), lats = pts.map((q) => q[1]);
        this.reaches.push({ ...r, river: f.id, pts, cum, km: cum.at(-1), parts: [],
          bbox: [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)] });
      }
    }
    this.update(this.snap);
  }
  /** New snapshot: recompute each reach's flow and (re)seed its particles evenly along it. */
  update(snap) {
    this.snap = snap;
    for (const r of this.reaches) {
      r.flow = reachFlow(r.src, snap); r.spec = particleSpec(r.flow, r.km);
      if (r.parts.length !== r.spec.n) r.parts = Array.from({ length: r.spec.n }, (_, i) => ((i + 0.5) / r.spec.n) * r.km);
    }
    this.map?.triggerRepaint();
  }
  moving() { return this.reaches.some((r) => r.flow?.moving); }
  /** The fastest moving reach in view, in screen px/s (0: none; off-screen motion costs repaints and shows nothing). */
  maxSpeedInView() {
    const b = this.map.getBounds(), W = b.getWest(), S = b.getSouth(), E = b.getEast(), N = b.getNorth();
    let v = 0;
    for (const r of this.reaches) if (r.flow?.moving && r.bbox[0] <= E && r.bbox[2] >= W && r.bbox[1] <= N && r.bbox[3] >= S) v = Math.max(v, r.spec.speed);
    return v;
  }
  movingInView() { return this.maxSpeedInView() > 0; }
  onAdd(map, gl) {
    this.map = map;
    const vs = `attribute vec3 a_pos; uniform mat4 u_matrix; uniform vec2 u_view; uniform float u_size; varying float v_moving;
      void main() { vec4 p = u_matrix * vec4(a_pos.xy, 0.0, 1.0); vec2 px = (p.xy / p.w * 0.5 + 0.5) * u_view;
        px = floor(px + 0.5);   /* even point size: centre on a pixel corner, so the square covers whole device pixels */
        gl_Position = vec4((px / u_view * 2.0 - 1.0) * p.w, p.z, p.w); gl_PointSize = u_size; v_moving = a_pos.z; }`;
    const fs = `precision mediump float; varying float v_moving; uniform float u_opacity;
      void main() { vec4 c = v_moving > 0.5 ? vec4(0.827, 0.925, 0.878, 0.9) : vec4(0.498, 0.580, 0.541, 0.55);
        gl_FragColor = vec4(c.rgb * c.a, c.a) * u_opacity; }`;
    const sh = (t, s) => { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o); return o; };
    this.prog = gl.createProgram();
    gl.attachShader(this.prog, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(this.prog, sh(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(this.prog);
    this.loc = { a_pos: gl.getAttribLocation(this.prog, "a_pos"),
      ...Object.fromEntries(["u_matrix", "u_view", "u_size", "u_opacity"].map((u) => [u, gl.getUniformLocation(this.prog, u)])) };
    this.buf = gl.createBuffer();
  }
  /** Advance moving particles by screen speed (converted to km at the current zoom), then draw all of them. */
  render(gl, args) {
    if (!this.reaches.length) return;
    const now = performance.now(), dt = frameDt(this.looping, this.last, now); this.last = now;
    const run = this.shouldRun(), z = this.map.getZoom(), lat = this.map.getCenter().lat;
    const pxPerKm = (512 * 2 ** z) / (40075 * Math.cos((lat * Math.PI) / 180));
    const data = [];
    for (const r of this.reaches) {
      const step = run && r.flow?.moving ? (r.spec.speed * dt) / pxPerKm : 0;
      for (let i = 0; i < r.parts.length; i++) {
        if (step) r.parts[i] = (r.parts[i] + step) % r.km;
        const d = r.parts[i]; let k = 1;
        while (k < r.cum.length - 1 && r.cum[k] < d) k++;
        const a = r.pts[k - 1], b = r.pts[k], f = (d - r.cum[k - 1]) / Math.max(1e-9, r.cum[k] - r.cum[k - 1]);
        const lon = a[0] + (b[0] - a[0]) * f, la = a[1] + (b[1] - a[1]) * f;
        data.push(mercX(lon), mercY(la), r.flow?.moving ? 1 : 0);
      }
    }
    const pr = this.map.getPixelRatio();
    gl.useProgram(this.prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.loc.a_pos); gl.vertexAttribPointer(this.loc.a_pos, 3, gl.FLOAT, false, 0, 0);
    gl.uniformMatrix4fv(this.loc.u_matrix, false, args.defaultProjectionData.mainMatrix);
    gl.uniform2f(this.loc.u_view, gl.drawingBufferWidth, gl.drawingBufferHeight);
    gl.uniform1f(this.loc.u_size, 2 * Math.max(1, Math.round(pr))); gl.uniform1f(this.loc.u_opacity, this.opacity);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(gl.POINTS, 0, data.length / 3);
    // keep animating (≤ ~30 fps) only while a moving reach exists and motion is allowed
    const vmax = run ? this.maxSpeedInView() : 0;
    this.looping = vmax > 0;
    if (this.looping && !this.pending) {
      // paced by a timer (L-080): one wake per drawn frame. triggerRepaint schedules MapLibre's own animation frame,
      // which pauses with the tab by itself (L-102: no second requestAnimationFrame hop). The wait is the power state's
      // frame time, or longer while nothing in view would move a device pixel (riverFrameMs).
      this.pending = true;
      const wait = riverFrameMs(this.frameMs(), vmax, pr) - 17 - (performance.now() - now);
      setTimeout(() => { this.pending = false; this.map.triggerRepaint(); }, Math.max(0, wait));
    }
  }
}
