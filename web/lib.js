// Shared helpers: API access, number and time formatting.

// ?dev=1 swaps every /api call for local fixtures (web/dev/). Real mode never loads them.
export const DEV = new URLSearchParams(location.search).get("dev") === "1";
let devApi = null;

async function devGet(path) {
  if (DEV) {
    devApi ??= await import("./dev/devapi.js");
    return devApi.handle(path);
  }
}
/** Requests in flight by `key` (L-081): one per endpoint. */
const inflight = new Map();
/**
 * GET an API path. Returns parsed JSON, or {status: 304} when the ETag matched. The body carries the response's ETag as
 * `_etag` and the server's `X-Next-Update` (seconds until new data is due) as `_next`; a 304 carries `next`.
 * With `key` (L-081, Database: one request in flight per endpoint): a call for the same path while one is in flight
 * shares it, and a call for another path (the scope changed) aborts it.
 */
export function api(path, { etag, key } = {}) {   // not async: a shared call returns the very same promise
  if (DEV) return devGet(path);
  const cur = key && inflight.get(key);
  if (cur) { if (cur.path === path) return cur.p; cur.ctrl.abort(); }
  const ctrl = key ? new AbortController() : null, e = { path, ctrl, p: null };
  e.p = (async () => {
    try {
      const r = await fetch(path, { headers: etag ? { "If-None-Match": etag } : {}, signal: ctrl?.signal });
      const next = Number(r.headers.get("X-Next-Update")) || null;
      if (r.status === 304) return { status: 304, next };
      if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
      const body = await r.json();
      const tag = r.headers.get("ETag");
      if (body && typeof body === "object") {
        if (tag) Object.defineProperty(body, "_etag", { value: tag });
        Object.defineProperty(body, "_next", { value: next });
      }
      return body;
    } finally {
      if (key && inflight.get(key) === e) inflight.delete(key);   // before the caller sees the result
    }
  })();
  if (key) inflight.set(key, e);
  return e.p;
}
/** A request `api` aborted because its scope changed: not an error to show. */
export const aborted = (e) => e?.name === "AbortError";

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export function fmt(v, decimals) {
  if (v == null || Number.isNaN(v)) return "--";
  const a = Math.abs(v);
  const d = decimals ?? (a >= 100 ? 0 : a >= 10 ? 1 : 2);
  if (Number(v.toFixed(d)) === 0) v = 0; // no "-0"
  return v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
export function fmtSigned(v, decimals) {
  if (v == null || Number.isNaN(v)) return "--";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + fmt(Math.abs(v), decimals);
}
const UNIT_LABEL = { degC: "°C", "degC-d": "HDD" };
export const unitLabel = (u) => UNIT_LABEL[u] ?? u ?? "";

// ------------------------------------------------------------------ Alberta time
// Browsers may carry stale zone data (Alberta is permanent UTC-6 from 2026-11-01), so all Alberta
// times are formatted with the offset the server reports from its bundled tzdata (/api/status tz).
// Display label is always "MT": IANA labels Alberta's permanent UTC-6 (from 2026-11-01) "CST", which nobody here uses.
const TZ = { offset: -360, abbr: "MT" };
export function setTz(tz) {
  if (tz && Number.isFinite(tz.offset_minutes)) TZ.offset = tz.offset_minutes;
}
export const tzAbbr = () => TZ.abbr;
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const p2 = (n) => String(n).padStart(2, "0");

/** Wall-clock fields in Alberta for epoch seconds. */
export function ab(sec) {
  const d = new Date((sec + TZ.offset * 60) * 1000);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(),
    mi: d.getUTCMinutes(), dow: d.getUTCDay() };
}
export function fmtTime(sec, { date = true, dow = false, time = true } = {}) {
  if (sec == null) return "--";
  const t = ab(sec), out = [];
  if (dow) out.push(DOW[t.dow]);
  if (date) out.push(`${MON[t.mo]} ${t.d}`);
  if (time) out.push(`${p2(t.h)}:${p2(t.mi)}`);
  return out.join(" ");
}
export const fmtDate = (sec) => { const t = ab(sec); return `${t.y}-${p2(t.mo + 1)}-${p2(t.d)}`; };
/** Daily series are calendar dates at 00:00 UTC: format the date itself, no zone shift. */
export function fmtDay(sec) { const d = new Date(sec * 1000); return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`; }

/** A Date whose *local* fields equal Alberta wall time (what uPlot's tzDate expects). */
export function wallDate(sec) {
  const d = new Date((sec + TZ.offset * 60) * 1000);
  return new Date(d.getTime() + d.getTimezoneOffset() * 60000);
}

/** ISO strings from the API are naive UTC unless they carry a zone. */
export const isoSec = (iso) => (iso ? Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z") / 1000 : null);

/**
 * A reading that's late but not stale (L-106, #428/#433: a region line that waits by design on a slow part, e.g. BC Hydro's
 * weekday load; the snapshot's stale flag honours the region's `lag:`): its age in seconds when the newest point is older
 * than 6 h for a sub-daily series, 2 days for a daily one or 10 days for a weekly one, else null. Stale, monthly (it
 * names its month) and windowed (`agg`) entries are left to their own labels. One rule for every row.
 */
export function lateAge(e, nowS = Date.now() / 1000) {
  if (!e || e.ts == null || e.stale || e.agg) return null;
  const f = String(e.freq || "").toUpperCase(), age = nowS - e.ts;
  const lim = f === "D" ? 2 * 86400 : f === "W" ? 10 * 86400 : /(MIN|H)$/.test(f) ? 6 * 3600 : null;
  return lim != null && age > lim ? age : null;
}
/** "7 h" / "3 d": a late reading's age for a row (lateAge). */
export const ageLabel = (s) => (s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`);
export function ago(sec) {
  if (sec == null) return "never";
  const s = Date.now() / 1000 - sec;
  if (s < 90) return `${Math.max(0, Math.round(s))}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export function toast(msg, err = false, ms = 6000) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}
