// Weather overlay: grid decoding, DEM mosaic, the WebGL2 field layer (with lapse-rate downscale),
// Canvas2D wind particles, and CPU sampling for the hover readout. Design: docs/research/frontend.md §2-3.

// ------------------------------------------------------------------ colour ramps (ours, frontend.md §2)
export const RAMPS = {
  // Erik (2026-11-03): "I am looking at 3 degrees to 16 degrees, and its a marginal change in colour". Every 3-5 °C
  // across the common range is now its own hue (still interpolated smoothly), with a pale ice tone at 0 °C:
  // purple < -27, blue/cyan to -8, ice at 0, greens 3-9, yellow 12-15, amber/orange 18-24, red/crimson > 27.
  t2m: { unit: "°C", ticks: [-40, -30, -20, -10, 0, 10, 20, 30, 40], label: "TEMPERATURE 2 M", stops: [
    [-45, "#3b0a45"], [-38, "#651c86"], [-32, "#6a3fb4"], [-27, "#4a5ee0"], [-22, "#3b82e6"], [-17, "#2fa6e8"],
    [-12, "#2cc4da"], [-8, "#3fd39a"], [-4, "#a6e8e2"], [0, "#e8f4f6"], [3, "#b8e39a"], [6, "#c3e57c"],
    [9, "#45c66a"], [12, "#a6cf3c"], [15, "#e8d83a"], [18, "#f6bc3a"], [21, "#f59b35"], [24, "#ef7a2f"],
    [27, "#e5562b"], [30, "#d33a32"], [34, "#b0203e"], [39, "#86134a"], [45, "#5a0c40"]] },
  wind: { unit: "km/h", ticks: [0, 20, 40, 60, 80, 100, 120], label: "WIND SPEED 10 M", stops: [
    [0, "#1c2a3e"], [10, "#2a4a6e"], [20, "#2f6f86"], [30, "#3d8f84"], [40, "#7aa86a"],
    [55, "#c9a94e"], [70, "#d57a43"], [90, "#c2414f"], [120, "#b07ad0"]] },
};
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
function rampPixels(stops, n = 256) {
  const lo = stops[0][0], hi = stops.at(-1)[0], px = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) {
    const v = lo + (hi - lo) * (i / (n - 1));
    let j = 0;
    while (j < stops.length - 2 && v > stops[j + 1][0]) j++;
    const [v0, c0] = stops[j], [v1, c1] = stops[j + 1], f = Math.min(1, Math.max(0, (v - v0) / (v1 - v0)));
    const a = hex(c0), b = hex(c1);
    for (let k = 0; k < 3; k++) px[i * 4 + k] = Math.round(a[k] + (b[k] - a[k]) * f);
    px[i * 4 + 3] = 255;
  }
  return px;
}
export const rampCss = (s) => `linear-gradient(90deg,${s.map(([v, c]) => `${c} ${((v - s[0][0]) / (s.at(-1)[0] - s[0][0])) * 100}%`).join(",")})`;

// ------------------------------------------------------------------ geometry
const R2D = 180 / Math.PI;
export const mercX = (lon) => (lon + 180) / 360;
export const mercY = (lat) => { const s = Math.sin(lat / R2D); return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI); };

// ------------------------------------------------------------------ grid payloads (docs/ARCHITECTURE.md §5)
const isoSec = (iso) => Date.parse(/[zZ]$/.test(iso) ? iso : iso + "Z") / 1000;
/** Decode a grid payload into {geom, times (epoch s), data: Float32Array [t][row][col]}. */
export function decodeGrid(p) {
  const bin = atob(p.data), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const dv = new DataView(bytes.buffer), n = bytes.length >> 1, out = new Float32Array(n);
  for (let i = 0; i < n; i++) { const r = dv.getInt16(i * 2, true); out[i] = r === p.nodata ? NaN : r * p.scale + p.offset; }
  fillNaN(out);
  return { geom: { lon0: p.lon0, lat0: p.lat0, dx: p.dx, dy: p.dy, nx: p.nx, ny: p.ny },
    times: (p.times || []).map(isoSec), run: p.run, model: p.model, unit: p.unit, data: out };
}
// nodata cells take the previous valid value in flat [t][row][col] order, so a fill can carry across row and
// time boundaries. Harmless for GFS (no nodata in practice); textures can't hold NaN usefully.
function fillNaN(a) {
  let last = 0;
  for (let i = 0; i < a.length; i++) { if (Number.isNaN(a[i])) a[i] = last; else last = a[i]; }
}
export const sameGeom = (a, b) => a && b && ["lon0", "lat0", "dx", "dy", "nx", "ny"].every((k) => Math.abs(a[k] - b[k]) < 1e-9);

/** Fractional step index for epoch seconds (steps may be unevenly spaced). */
export function stepAt(times, sec) {
  if (times.length < 2) return 0;
  if (sec <= times[0]) return 0;
  let i = 0; while (i < times.length - 2 && sec > times[i + 1]) i++;
  return Math.min(times.length - 1, i + Math.min(1, Math.max(0, (sec - times[i]) / (times[i + 1] - times[i]))));
}

// ------------------------------------------------------------------ DEM mosaic (Terrarium z5)
// The same tiles the hillshade layer uses (browser-cached). Decoded on the CPU: byte-encoded heights
// can't be filtered before decoding. Covers the grid extent.
export const TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
export async function loadDem(geom, z = 5) {
  const n = 2 ** z, lon1 = geom.lon0 + (geom.nx - 1) * geom.dx, lat1 = geom.lat0 + (geom.ny - 1) * geom.dy;
  const x0 = Math.floor(mercX(geom.lon0) * n), x1 = Math.floor(mercX(lon1) * n);
  const y0 = Math.floor(mercY(lat1) * n), y1 = Math.floor(mercY(geom.lat0) * n);
  const nx = x1 - x0 + 1, ny = y1 - y0 + 1, W = nx * 256, H = ny * 256;
  const cv = new OffscreenCanvas(W, H), cx = cv.getContext("2d");
  await Promise.all(Array.from({ length: nx * ny }, async (_, k) => {
    const i = k % nx, j = (k / nx) | 0;
    const r = await fetch(TERRARIUM.replace("{z}", z).replace("{x}", x0 + i).replace("{y}", y0 + j));
    if (!r.ok) throw new Error(`DEM tile ${r.status}`);
    cx.drawImage(await createImageBitmap(await r.blob()), i * 256, j * 256);
  }));
  const px = cx.getImageData(0, 0, W, H).data, elev = new Float32Array(W * H);
  for (let q = 0; q < W * H; q++) elev[q] = Math.max(0, px[q * 4] * 256 + px[q * 4 + 1] + px[q * 4 + 2] / 256 - 32768);
  return { zoom: z, x0, y0, nx, ny, W, H, elev };
}
export function demAt(dem, lon, lat) {
  if (!dem) return null;
  const n = 2 ** dem.zoom, x = (mercX(lon) * n - dem.x0) * 256, y = (mercY(lat) * n - dem.y0) * 256;
  if (x < 0 || y < 0 || x >= dem.W || y >= dem.H) return null;
  return dem.elev[(y | 0) * dem.W + (x | 0)];
}

// ------------------------------------------------------------------ CPU sampling (hover, particles)
export function sampleGrid(geom, data, lon, lat, t = 0) {
  const gx = (lon - geom.lon0) / geom.dx, gy = (lat - geom.lat0) / geom.dy;
  if (gx < 0 || gy < 0 || gx > geom.nx - 1 || gy > geom.ny - 1) return null;
  const i = Math.min(geom.nx - 2, gx | 0), j = Math.min(geom.ny - 2, gy | 0), fx = gx - i, fy = gy - j, n = geom.nx * geom.ny;
  const t0 = Math.floor(t), t1 = Math.min(Math.max(0, data.length / n - 1), t0 + 1), ft = t - t0;
  const at = (tt) => { const b = tt * n + j * geom.nx + i;
    return (data[b] * (1 - fx) + data[b + 1] * fx) * (1 - fy) + (data[b + geom.nx] * (1 - fx) + data[b + geom.nx + 1] * fx) * fy; };
  return t1 === t0 ? at(t0) : at(t0) * (1 - ft) + at(t1) * ft;
}

// ------------------------------------------------------------------ field layer (WebGL2 custom layer)
const VS = `#version 300 es
in vec2 a_pos; uniform mat4 u_matrix; out vec2 v_merc;
void main() { v_merc = a_pos; gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0); }`;
const FS = `#version 300 es
precision highp float; precision highp sampler2DArray;
uniform sampler2DArray u_field; uniform sampler2D u_ramp, u_zgrid, u_dem;
uniform vec4 u_grid; uniform vec2 u_size; uniform vec3 u_time; uniform vec2 u_range; uniform float u_opacity;
uniform vec4 u_demBox; uniform float u_lapse; uniform vec4 u_seamWE, u_seamSN;
in vec2 v_merc; out vec4 o;
const float PI = 3.141592653589793;
vec4 cubic(float v) { vec4 n = vec4(1.0, 2.0, 3.0, 4.0) - v; vec4 s = n * n * n;
  float x = s.x, y = s.y - 4.0 * s.x, z = s.z - 4.0 * s.y + 6.0 * s.x; return vec4(x, y, z, 6.0 - x - y - z) / 6.0; }
#define BICUBIC(NAME, SAMPLE) float NAME(vec2 tc, float layer) { \\
  tc -= 0.5; vec2 f = fract(tc); tc -= f; vec4 xc = cubic(f.x), yc = cubic(f.y); \\
  vec4 c = tc.xxyy + vec2(-0.5, 1.5).xyxy; vec4 s = vec4(xc.xz + xc.yw, yc.xz + yc.yw); \\
  vec4 off = (c + vec4(xc.yw, yc.yw) / s) / u_size.xxyy; \\
  float s0 = SAMPLE(off.xz), s1 = SAMPLE(off.yz), s2 = SAMPLE(off.xw), s3 = SAMPLE(off.yw); \\
  float sx = s.x / (s.x + s.y), sy = s.z / (s.z + s.w); return mix(mix(s3, s2, sx), mix(s1, s0, sx), sy); }
#define SF(uv) texture(u_field, vec3(uv, layer)).r
#define SZ(uv) texture(u_zgrid, uv).r
BICUBIC(fieldAt, SF)
BICUBIC(zgridAt, SZ)
// 1 inside [r.x, r.y] (grid index along an edge), easing to 0 over one cell outside it
float span(float t, vec2 r) { return smoothstep(r.x - 1.0, r.x, t) * (1.0 - smoothstep(r.y, r.y + 1.0, t)); }
void main() {
  float lon = v_merc.x * 360.0 - 180.0, lat = degrees(atan(sinh(PI * (1.0 - 2.0 * v_merc.y))));
  vec2 gi = vec2((lon - u_grid.x) / u_grid.z, (lat - u_grid.y) / u_grid.w), tc = gi + 0.5;
  float val = mix(fieldAt(tc, u_time.x), fieldAt(tc, u_time.y), u_time.z);
  // Lapse-rate downscale: T_fine = T_grid + lapse * (z_grid - z_dem). Off (0) for wind speed.
  vec2 duv = (v_merc - u_demBox.xy) / u_demBox.zw;
  if (u_lapse != 0.0 && all(greaterThanEqual(duv, vec2(0.0))) && all(lessThanEqual(duv, vec2(1.0))))
    val += u_lapse * (zgridAt(tc, 0.0) - texture(u_dem, duv).r);
  float k = clamp((val - u_range.x) / (u_range.y - u_range.x), 0.0, 1.0);
  vec3 col = texture(u_ramp, vec2(k, 0.5)).rgb;
  // the edge fade, except along the stretch of an edge that another weather region shares (u_seamWE: the west and east
  // edges' shared row range; u_seamSN: the south and north edges' shared column range; empty = lo > hi)
  vec2 lo = gi + vec2(span(gi.y, u_seamWE.xy), span(gi.x, u_seamSN.xy)) * 4.0;
  vec2 hi = u_size - 1.0 - gi + vec2(span(gi.y, u_seamWE.zw), span(gi.x, u_seamSN.zw)) * 4.0;
  vec2 edge = min(lo, hi);
  float a = u_opacity * smoothstep(0.0, 4.0, min(edge.x, edge.y));
  o = vec4(col * a, a);
}`;

/**
 * Where each edge of a weather region meets another region: [west, east, south, north], each the shared stretch along
 * that edge in degrees ([lo, hi]: latitudes for west/east, longitudes for south/north) or null. Another region counts
 * when its opposite edge lies on the same line and the two overlap along it; several neighbours on one edge (the east
 * grid's west edge: the west grid above 44 N, the south grid below) join into one stretch. Only that stretch skips the
 * edge fade, so an edge that is shared for part of its length still fades beyond it (L-090, Reviewer on #370: the south
 * grid's east edge below 41 N). Pure (grid geometries only), for the tests. Rows run south to north (dy > 0).
 */
export function seamsOf(g, grids) {
  const box = (h) => { const q = h.t2m.geom; return { w: q.lon0, e: q.lon0 + (q.nx - 1) * q.dx, s: q.lat0, n: q.lat0 + (q.ny - 1) * q.dy }; };
  const a = box(g), on = (x, y) => Math.abs(x - y) < 1e-6, others = grids.filter((h) => h !== g).map(box);
  const shared = (touch, lo, hi, blo, bhi) => {
    const segs = others.filter(touch).map((b) => [Math.max(lo, blo(b)), Math.min(hi, bhi(b))]).filter(([x, y]) => y - x > 1e-6);
    return segs.length ? [Math.min(...segs.map((s) => s[0])), Math.max(...segs.map((s) => s[1]))] : null;
  };
  return [
    shared((b) => on(b.e, a.w), a.s, a.n, (b) => b.s, (b) => b.n),
    shared((b) => on(b.w, a.e), a.s, a.n, (b) => b.s, (b) => b.n),
    shared((b) => on(b.n, a.s), a.w, a.e, (b) => b.w, (b) => b.e),
    shared((b) => on(b.s, a.n), a.w, a.e, (b) => b.w, (b) => b.e),
  ];
}

function setFilter(gl, target) {
  for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
    [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(target, k, v);
}
function tex2D(gl, w, h, ifmt, fmt, type, data) {
  const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texImage2D(gl.TEXTURE_2D, 0, ifmt, w, h, 0, fmt, type, data); setFilter(gl, gl.TEXTURE_2D); return t;
}

export class FieldLayer {
  constructor(id) { Object.assign(this, { id, type: "custom", renderingMode: "2d", t: 0, opacity: 0.6, lapse: 0, ready: false }); }
  onAdd(map, gl) {
    this.map = map; this.gl = gl;
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
    const p = this.prog = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(p);
    this.loc = Object.fromEntries(["u_matrix", "u_field", "u_ramp", "u_zgrid", "u_dem", "u_grid", "u_size", "u_time",
      "u_range", "u_opacity", "u_demBox", "u_lapse", "u_seamWE", "u_seamSN"].map((n) => [n, gl.getUniformLocation(p, n)]));
    this.buf = gl.createBuffer();
    // 1x1 placeholders so the sampler bindings are always valid
    this.zgTex = tex2D(gl, 1, 1, gl.R16F, gl.RED, gl.FLOAT, new Float32Array([0]));
    this.demTex = tex2D(gl, 1, 1, gl.R16F, gl.RED, gl.FLOAT, new Float32Array([0]));
    this.demBox = [0, 0, 0, 0];
    if (this.pending) this.setField(...this.pending);
    if (this.pendingTerrain) this.setTerrain(...this.pendingTerrain);
  }
  /** frames: Float32Array [t][row][col]; nt layers. */
  setField(geom, frames, nt, ramp) {
    if (!this.gl) { this.pending = [geom, frames, nt, ramp]; return; }
    const gl = this.gl;
    this.geom = geom; this.nt = nt; this.ramp = ramp;
    const a = [mercX(geom.lon0), mercY(geom.lat0)];
    const b = [mercX(geom.lon0 + (geom.nx - 1) * geom.dx), mercY(geom.lat0 + (geom.ny - 1) * geom.dy)];
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([a[0], a[1], b[0], a[1], a[0], b[1], b[0], b[1]]), gl.STATIC_DRAW);
    if (this.tex) gl.deleteTexture(this.tex);
    this.tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D_ARRAY, this.tex);
    gl.texImage3D(gl.TEXTURE_2D_ARRAY, 0, gl.R16F, geom.nx, geom.ny, nt, 0, gl.RED, gl.FLOAT, frames);
    setFilter(gl, gl.TEXTURE_2D_ARRAY);
    if (this.rampTex) gl.deleteTexture(this.rampTex);
    this.rampTex = tex2D(gl, 256, 1, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, rampPixels(ramp.stops));
    this.ready = true; this.map.triggerRepaint();
  }
  /** z grid (same geometry as the field) + DEM mosaic enable the lapse-rate downscale. */
  setTerrain(zgrid, dem) {
    if (!this.gl) { this.pendingTerrain = [zgrid, dem]; return; }
    const gl = this.gl;
    this.zgTex = tex2D(gl, zgrid.geom.nx, zgrid.geom.ny, gl.R16F, gl.RED, gl.FLOAT, zgrid.data);
    this.demTex = tex2D(gl, dem.W, dem.H, gl.R16F, gl.RED, gl.FLOAT, dem.elev);
    const n = 2 ** dem.zoom; this.demBox = [dem.x0 / n, dem.y0 / n, dem.nx / n, dem.ny / n];
    this.hasTerrain = true; this.map.triggerRepaint();
  }
  render(gl, args) {
    if (!this.ready) return;
    const L = this.loc, g = this.geom, t0 = Math.floor(this.t), t1 = Math.min(this.nt - 1, t0 + 1);
    gl.useProgram(this.prog);
    gl.uniformMatrix4fv(L.u_matrix, false, args.defaultProjectionData.mainMatrix);
    gl.uniform4f(L.u_grid, g.lon0, g.lat0, g.dx, g.dy); gl.uniform2f(L.u_size, g.nx, g.ny);
    // L-080 / L-090: the stretch of each edge shared with another region (seamsOf, degrees) as grid indices; none = empty
    const ix = (i, lo0, d) => (this.seam?.[i] ? this.seam[i].map((x) => (x - lo0) / d) : [1e9, -1e9]);
    gl.uniform4f(L.u_seamWE, ...ix(0, g.lat0, g.dy), ...ix(1, g.lat0, g.dy));
    gl.uniform4f(L.u_seamSN, ...ix(2, g.lon0, g.dx), ...ix(3, g.lon0, g.dx));
    gl.uniform3f(L.u_time, t0, t1, this.t - t0);
    gl.uniform2f(L.u_range, this.ramp.stops[0][0], this.ramp.stops.at(-1)[0]);
    gl.uniform1f(L.u_opacity, this.opacity); gl.uniform1f(L.u_lapse, this.hasTerrain ? this.lapse : 0);
    gl.uniform4f(L.u_demBox, ...this.demBox);
    [[this.tex, gl.TEXTURE_2D_ARRAY, "u_field"], [this.rampTex, gl.TEXTURE_2D, "u_ramp"], [this.zgTex, gl.TEXTURE_2D, "u_zgrid"],
      [this.demTex, gl.TEXTURE_2D, "u_dem"]].forEach(([t, target, name], i) => {
      gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(target, t); gl.uniform1i(L[name], i); });
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    const pos = gl.getAttribLocation(this.prog, "a_pos");
    gl.enableVertexAttribArray(pos); gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
}

// ------------------------------------------------------------------ wind particles (Canvas2D overlay)
// L-080 (battery): the loop runs at `power.rate("wind")` frames/s (30 active, 15 idle, 10 battery saver, 0 hidden),
// and not at all while the layer is off. Motion and trail fade scale with the frame time, so the field looks the
// same at any rate, just less smooth. The canvas is drawn at a capped pixel ratio (2; 1 in saver), and the particle
// count follows the screen area, so a phone draws a fraction of a desktop's.
const REF_FRAME_MS = 1000 / 60, REF_AREA = 1536 * 730, MAX_N = 2600;
export const windCount = (w, h, saver) => Math.round(Math.max(400, Math.min(MAX_N, MAX_N * (w * h) / REF_AREA)) * (saver ? 0.5 : 1));
export const windDpr = (dpr, saver) => Math.min(dpr || 1, saver ? 1 : 2);
export class Wind {
  constructor(map, canvas, power = null) {
    Object.assign(this, { map, canvas, power, on: true, t: 0, u: null, v: null, geom: null, n: 0, running: false, lastT: 0 });
    this.ctx = canvas.getContext("2d");
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    map.on("move", () => this.ctx.clearRect(0, 0, canvas.width, canvas.height));
    map.on("resize", () => this.resize()); this.resize();
    power?.onChange(() => { this.resize(); this.wake(); });
    this.wake();
  }
  setData(geom, u, v) { this.setGrids([{ geom, u, v }]); }
  /** Several wind grids (L-080: the west 0.25° and Weather's east 0.5°), each {geom, u, v, t}: a particle takes its
   * wind from the first grid that covers it, at that grid's own time step (`t`, set per grid by the slider). */
  setGrids(grids) {
    this.grids = grids.map((g, i) => ({ ...g, t: this.grids?.[i]?.t ?? 0 }));
    Object.assign(this, { geom: grids[0]?.geom, u: grids[0]?.u, v: grids[0]?.v });
    for (let i = 0; i < this.n; i++) this.spawn(i, true); this.wake();
  }
  /** [u, v] at a point from the first grid covering it, or null. */
  sample(lon, lat) {
    for (const g of this.grids || []) {
      const uu = sampleGrid(g.geom, g.u, lon, lat, g.t), vv = uu == null ? null : sampleGrid(g.geom, g.v, lon, lat, g.t);
      if (uu != null && vv != null) return [uu, vv];
    }
    return null;
  }
  resize() {
    const r = this.map.getContainer().getBoundingClientRect(), saver = !!this.power?.saver, d = windDpr(devicePixelRatio, saver);
    this.canvas.width = r.width * d; this.canvas.height = r.height * d; this.dpr = d;
    const n = windCount(r.width, r.height, saver);
    if (n !== this.n) { this.n = n; this.p = new Float32Array(n * 3); if (this.u) for (let i = 0; i < n; i++) this.spawn(i, true); }
  }
  spawn(i, randomAge) { const b = this.map.getBounds();
    this.p[i * 3] = b.getWest() + Math.random() * (b.getEast() - b.getWest());
    this.p[i * 3 + 1] = b.getSouth() + Math.random() * (b.getNorth() - b.getSouth());
    this.p[i * 3 + 2] = randomAge ? Math.random() * 50 : 0; }
  /** Start the loop if it may run (the layer is on, data is in, motion allowed); it stops itself otherwise. */
  wake() { if (!this.running && this.mayRun()) { this.running = true; this.lastT = 0; requestAnimationFrame((t) => this.frame(t)); } }
  mayRun() { return this.on && !!this.u && !this.reduced && !document.hidden && (this.power?.rate("wind") ?? 60) > 0; }
  frame(t) {
    if (window.__freeze || !this.mayRun()) {   // stop: clear what's left and wait for wake()
      this.running = false; this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height); return;
    }
    const fps = this.power?.rate("wind") ?? 60, gap = t - this.lastT, sleep = 1000 / fps - 17;
    // below ~60 fps, sleep until the next frame is due: one wake per drawn frame, not one per display frame
    if (sleep > 0) setTimeout(() => requestAnimationFrame((tt) => this.frame(tt)), sleep); else requestAnimationFrame((tt) => this.frame(tt));
    if (this.lastT && gap < 1000 / fps - 2) return;               // not yet: hold the frame rate
    const f = this.lastT ? Math.min(4, gap / REF_FRAME_MS) : 1;   // in 60 Hz frames, so motion and fade keep their speed
    this.lastT = t;
    const { ctx, canvas, map } = this;
    ctx.globalCompositeOperation = "destination-in"; ctx.fillStyle = `rgba(0,0,0,${0.86 ** f})`;
    ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.globalCompositeOperation = "source-over";
    const z = map.getZoom(), W = 512 * 2 ** z, c = map.getCenter(), d = this.dpr;
    const cx = mercX(c.lng) * W - canvas.width / d / 2, cy = mercY(c.lat) * W - canvas.height / d / 2;
    const pxPerDeg = W / 360, k = 0.14 * f, paths = [new Path2D(), new Path2D(), new Path2D()];
    for (let i = 0; i < this.n; i++) {
      const o = i * 3, lon = this.p[o], lat = this.p[o + 1];
      const uv = this.sample(lon, lat);
      if (uv == null || (this.p[o + 2] += f) > 50) { this.spawn(i, false); continue; }
      const [uu, vv] = uv;
      const lon2 = lon + (uu * k) / pxPerDeg, lat2 = lat + (vv * k * Math.cos(lat / R2D)) / pxPerDeg;
      const sp = Math.hypot(uu, vv) * 3.6;
      const path = paths[sp < 20 ? 0 : sp < 40 ? 1 : 2];
      path.moveTo((mercX(lon) * W - cx) * d, (mercY(lat) * W - cy) * d); path.lineTo((mercX(lon2) * W - cx) * d, (mercY(lat2) * W - cy) * d);
      this.p[o] = lon2; this.p[o + 1] = lat2;
    }
    ctx.lineWidth = 0.7 * d; ctx.lineCap = "round";
    ["rgba(235,245,240,0.10)", "rgba(235,245,240,0.20)", "rgba(235,245,240,0.34)"].forEach((s, i) => { ctx.strokeStyle = s; ctx.stroke(paths[i]); });
  }
}
