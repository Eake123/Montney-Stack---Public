// DEV ONLY (?dev=1): answers /api/* from local fixtures and generated synthetic data so the UI can
// be built before the real network and grids land. Never imported in real mode (see lib.js).
import { loadDem, demAt } from "../weather.js";

const BASE = new URL("./fixtures/", import.meta.url);
const json = async (name) => (await fetch(new URL(name, BASE))).json();
const now = () => Date.now() / 1000;
const iso = (sec) => new Date(sec * 1000).toISOString().replace(/\.\d+Z$/, "");
const hash = (s) => { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return (h >>> 0) / 2 ** 32; };

// Plausible magnitudes by series id, so dev screenshots look like the real thing (values are synthetic).
const MAG = [[/linepack_target/, 26300], [/linepack/, 25980], [/field_receipts/, 11240], [/total_export/, 7810],
  [/intraprovincial/, 6020], [/gas_total/, 7480], [/price\.pool/, 61.4], [/empress/, 3480], [/mcneill/, 1210],
  [/alberta_bc|abbc/, 2420], [/egat/, 3600], [/\.cap$/, 0], [/power/, 1600], [/storage/, -310], [/hub/, 7100], [/./, 2900]];
const CAPS = { "dev.gas.empress.cap": 3900, "dev.gas.mcneill.cap": 1550, "dev.gas.abbc.cap": 2700, "dev.gas.ne_bc.cap": 3400,
  "dev.gas.peace.cap": 5200, "dev.gas.l1.cap": 3300, "dev.gas.l2.cap": 7400, "dev.gas.l3.cap": 2700, "dev.gas.l4.cap": 5200,
  "dev.gas.l5.cap": 1550, "dev.gas.l6.cap": 2600, "dev.power.west.cap": 2400, "dev.power.l1.cap": 1500, "dev.gas.egat.cap": 3900 };
const FLOWS = { "dev.gas.l1.flow": 2950, "dev.gas.l2.flow": 6900, "dev.gas.l3.flow": 2420, "dev.gas.l4.flow": 3480,
  "dev.gas.l5.flow": 1210, "dev.gas.l6.flow": 900, "dev.power.l1.mw": 1150, "dev.gas.egat.flow": 3420 };
const base = (id) => CAPS[id] ?? FLOWS[id] ?? MAG.find(([re]) => re.test(id))[1];
const freqOf = (id) => (/gdsr/.test(id) ? "D" : /aeso/.test(id) ? "5min" : "30min");

function history(id, start, end) {
  const step = freqOf(id) === "D" ? 86400 : 1800, b = base(id), seed = hash(id), ts = [], v = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) {
    ts.push(t);
    const k = t / 86400;
    v.push(CAPS[id] ? b : b * (1 + 0.03 * Math.sin(k * 1.3 + seed * 9) + 0.015 * Math.sin(k * 7.1 + seed * 3)));
  }
  return { ts, v, as_of: null, meta: { label: id, unit: /power|aeso\.gen/.test(id) ? "MW" : /price/.test(id) ? "$/MWh" : /linepack/.test(id) ? "MMcf" : "MMcf/d", freq: freqOf(id) } };
}
function summarize(id) {
  const h = history(id, now() - 2 * 86400, now()), n = h.ts.length, daily = h.meta.freq === "D";
  const pi = daily ? n - 2 : h.ts.findIndex((t) => t >= h.ts[n - 1] - 86400);
  return { series: id, v: h.v[n - 1], ts: h.ts[n - 1], prev: h.v[pi], prev_ts: h.ts[pi], unit: h.meta.unit,
    freq: h.meta.freq, compare: daily ? "prev" : "24h", stale: /storage/.test(id) };
}

// ------------------------------------------------------------------ synthetic GFS-like grid (same shape as /api/grid)
const G = { lon0: -140, lat0: 44, dx: 0.25, dy: 0.25, nx: 181, ny: 85 };
const HOURS = [...Array.from({ length: 41 }, (_, i) => i * 3), ...Array.from({ length: 44 }, (_, i) => 126 + i * 6)];
let gridCache;
async function grids() {
  if (gridCache) return gridCache;
  const run = Math.floor((now() - 4 * 3600) / 21600) * 21600, n = G.nx * G.ny, nt = HOURS.length;
  const dem = await loadDem({ ...G }).catch(() => null);
  const zg = new Float32Array(n);
  for (let j = 0; j < G.ny; j++) for (let i = 0; i < G.nx; i++) {
    let s = 0, c = 0;
    for (let a = -2; a <= 2; a++) for (let b = -2; b <= 2; b++) { s += demAt(dem, G.lon0 + i * G.dx + a * 0.05, G.lat0 + j * G.dy + b * 0.05) ?? 0; c++; }
    zg[j * G.nx + i] = s / c;
  }
  const t2m = new Float32Array(n * nt), u = new Float32Array(n * nt), v = new Float32Array(n * nt);
  for (let t = 0; t < nt; t++) {
    const days = HOURS[t] / 24, front = 58.5 - days * 0.55, lowLon = -126 + days * 1.1, lowLat = 55 - days * 0.25;
    const localHour = ((run / 3600 + HOURS[t] - 6) % 24 + 24) % 24, diurnal = 4 * Math.sin(((localHour - 9) / 24) * 2 * Math.PI);
    for (let j = 0; j < G.ny; j++) for (let i = 0; i < G.nx; i++) {
      const lon = G.lon0 + i * G.dx, lat = G.lat0 + j * G.dy, c = j * G.nx + i, k = t * n + c;
      const cold = 16 / (1 + Math.exp((front - lat) / 0.9));
      t2m[k] = 12 - (lat - 49) - cold + diurnal + 2.5 * Math.sin(lon * 0.7 + days) * Math.cos(lat * 0.9) - 0.0065 * zg[c];
      const dx = (lon - lowLon) * Math.cos(lat * Math.PI / 180), dy = lat - lowLat, r2 = dx * dx + dy * dy, sw = 9 * Math.exp(-r2 / 18);
      u[k] = 6 + (sw * -dy) / Math.sqrt(r2 + 0.5) + 3 * Math.sin(lat * 0.8 + days);
      v[k] = (sw * dx) / Math.sqrt(r2 + 0.5) + (lat > front ? -4 : 1);
    }
  }
  const times = HOURS.map((h) => iso(run + h * 3600) + "Z");
  const pack = (name, unit, data, ts, scale) => {
    const i16 = new Int16Array(data.length);
    for (let q = 0; q < data.length; q++) i16[q] = Math.round(data[q] / scale);
    let bin = ""; const bytes = new Uint8Array(i16.buffer);
    for (let q = 0; q < bytes.length; q += 0x8000) bin += String.fromCharCode(...bytes.subarray(q, q + 0x8000));
    return { var: name, unit, model: "dev_synthetic", run: iso(run) + "Z", fetched: iso(now()) + "Z", ...G, times: ts,
      scale, offset: 0, nodata: -32768, data: btoa(bin) };
  };
  gridCache = { t2m: pack("t2m", "degC", t2m, times, 0.01), u10: pack("u10", "m/s", u, times, 0.01),
    v10: pack("v10", "m/s", v, times, 0.01), z: pack("z", "m", zg, [], 1) };
  return gridCache;
}

// ------------------------------------------------------------------ router
export async function handle(path) {
  const url = new URL(path, location.origin), q = url.searchParams, p = url.pathname;
  if (p === "/api/status") return { version: "dev", now: iso(now()), tz: { name: "America/Edmonton", abbr: "MT", offset_minutes: -360 },
    sources: [{ id: "dev", name: "dev fixtures", enabled: true, last: { finished: iso(now()), ok: true } }] };
  if (p === "/api/network") return json("network.json");
  if (p === "/api/snapshot") {
    const net = await json("network.json"), values = {};
    for (const f of net.features) {
      const pr = f.properties; if (!pr.value) continue;
      const e = summarize(pr.value); if (pr.unit) e.unit = pr.unit;
      if (pr.capacity) { e.cap = base(pr.capacity); e.cap_ts = e.ts; }
      values[f.id] = e;
    }
    const series = Object.fromEntries((q.get("extra") || "").split(",").filter(Boolean).map((id) => [id, summarize(id)]));
    return { as_of: iso(now()) + "Z", values, series };
  }
  if (p === "/api/series") {
    const days = Number((q.get("start") || "-30d").replace(/[^\d]/g, "")) || 30;
    return Object.fromEntries(q.get("ids").split(",").map((id) => [id, history(id, now() - days * 86400, now())]));
  }
  if (p === "/api/events") {
    const ref = q.get("ref");
    return (await json("events.json")).filter((e) => !ref || e.refs.includes(ref)).map((e) => ({ ...e,
      starts: e.starts_h == null ? null : iso(now() + e.starts_h * 3600), ends: e.ends_h == null ? null : iso(now() + e.ends_h * 3600),
      published: iso(now() - 3600), first_seen: iso(now() - 3600), updated: iso(now() - 3600), status: "active", extra: {} }));
  }
  if (p === "/api/grids") { const g = await grids();
    return Object.values(g).map((x) => ({ var: x.var, unit: x.unit, model: x.model, run: x.run, fetched: x.fetched, prev: false, n_times: x.times.length })); }
  const m = p.match(/^\/api\/grid\/(\w+)$/);
  if (m) return (await grids())[m[1]];
  throw new Error(`dev api: no fixture for ${p}`);
}
