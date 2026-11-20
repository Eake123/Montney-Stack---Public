// The ☰ menu (L-097, Erik: "a small setting with a hamburger button in the UI that would open it up ... I just would
// like to monitor it"; "remove these dots for the sources and instead make that a tab in the hanburger button").
// Two tabs: Sources (the header's old dots: fetch health + budget, click to queue a refresh) and System (GET /api/system:
// what Montney costs this machine). The view models at the top are pure, so tests/test_web.py runs them under node.
import { api, esc, fmt } from "./lib.js";
import { sourceDot } from "./status.js";

/** Sources tab rows: every enabled source, worst first (blocked, err, warn, never run, ok), then by name. */
const ORDER = { blocked: 0, err: 1, warn: 2, "": 3, ok: 4 };
export function sourceRows(st, agoText = () => "") {
  return (st?.sources || []).filter((s) => s.enabled).map((s) => {
    const d = sourceDot(s, st.budget, agoText);
    return { id: s.id, name: s.name || s.id, cls: d.cls, title: d.title, ago: s.last ? agoText(s) : "never" };
  }).sort((a, b) => (ORDER[a.cls] ?? 3) - (ORDER[b.cls] ?? 3) || a.name.localeCompare(b.name));
}
/** The count line for the Sources tab: how many are ok / need a look. */
export function sourceSummary(rows) {
  const bad = rows.filter((r) => r.cls === "err" || r.cls === "blocked").length, warn = rows.filter((r) => r.cls === "warn").length;
  return `${rows.length} sources · ${rows.length - bad - warn} ok` + (warn ? ` · ${warn} near budget` : "") + (bad ? ` · ${bad} failing` : "");
}

const GB = 1e9;
/** A bar: a share (0..1, can exceed 1) and its level: ok below 80 %, warn to 100 %, over above (or when told). */
const bar = (used, cap, over) => {
  const f = cap > 0 ? used / cap : 0;
  return { f, level: over || f > 1 ? "over" : f >= 0.8 ? "warn" : "ok" };
};
/**
 * The System tab from /api/system (orchestrator #384). CPU % is of the whole machine for every row. Storage and
 * requests exist on Postgres only (absent on DuckDB): their sections are null then.
 */
export function systemModel(sys) {
  const m = sys?.machine || {}, P = sys?.processes || {};
  const procs = [["map", "Map server"], ["runner", "Runner"], ["postgres", "Postgres"]].map(([k, label]) => {
    const p = P[k];
    return p ? { key: k, label, n: p.n, rss_mb: p.rss_mb, cpu_pct: p.cpu_pct } : { key: k, label, missing: true };
  });
  const live = procs.filter((p) => !p.missing);
  const rss = live.reduce((a, p) => a + (p.rss_mb || 0), 0), cpu = live.reduce((a, p) => a + (p.cpu_pct || 0), 0);
  const ramGb = m.ram_total_gb || 0;
  const total = { rss_mb: rss, cpu_pct: cpu, ram_share: ramGb ? rss / 1024 / (ramGb * GB / 2 ** 30) : null };
  const storage = sys?.storage ? {
    taken_at: sys.storage.taken_at, system_bytes: sys.storage.system_bytes,
    regions: sys.storage.regions.map((r) => ({ code: r.code, bytes: r.bytes, cap: r.budget_bytes, ...bar(r.bytes, r.budget_bytes) })),
  } : null;
  const requests = sys?.requests ? {
    hours: sys.requests.hours, judged: sys.requests.judged,
    regions: sys.requests.regions.map((r) => ({ code: r.code, rate: r.req_per_h, cap: r.cap, mb_per_h: r.mb_per_h,
      top: r.top || [], ...bar(r.req_per_h, r.cap, r.over) })),
  } : null;
  return { machine: { cpu_pct: m.cpu_pct, cores: m.cores, ram_used_gb: m.ram_used_gb, ram_total_gb: ramGb, uptime_s: m.uptime_s,
    ram: bar(m.ram_used_gb || 0, ramGb), cpu: bar(m.cpu_pct || 0, 100) }, procs, total, storage, requests };
}

export const SYSTEM_MS = 10000;   // the System tab's poll while open (CPU % is since the previous call)

const size = (b) => (b >= GB ? `${fmt(b / GB, 1)} GB` : `${fmt(b / 1e6, 0)} MB`);
const uptime = (s) => (s >= 86400 ? `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`);
const barHtml = (b) => `<span class="sbar ${b.level}"><i style="width:${Math.min(100, Math.round(b.f * 100))}%"></i></span>`;

/** The ☰ button and its panel: one code path on every screen (the panel's size comes from CSS). */
export class SysMenu {
  constructor({ button, panel, agoText, onSources }) {
    Object.assign(this, { button, panel, agoText, onSources, tab: "sources", st: null, sys: null, timer: 0, busy: false });
    panel.innerHTML = `<div class="smhead"><div class="smtabs" role="tablist">
        <button type="button" role="tab" data-tab="sources">Sources</button><button type="button" role="tab" data-tab="system">System</button>
      </div><button type="button" class="smx" title="Close" aria-label="Close">✕</button></div><div class="smbody"></div>`;
    this.body = panel.querySelector(".smbody");
    button.onclick = () => this.toggle();
    panel.querySelector(".smx").onclick = () => this.close();
    for (const t of panel.querySelectorAll("[data-tab]")) t.onclick = () => this.show(t.dataset.tab);
    addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) this.close(); });
    document.addEventListener("visibilitychange", () => this.schedule());
    // only the row's ↻ fetches (each refresh is an upstream request; Erik: "really limit the requests"), and only once
    // until the list redraws: a stray tap while scanning the list, or a second tap, costs nothing
    this.body.addEventListener("click", (e) => {
      const btn = e.target.closest(".rf"), row = btn?.closest("[data-src]");
      if (row && !row.classList.contains("queued")) this.refresh(row.dataset.src, row);
    });
  }
  get open() { return !this.panel.hidden; }
  toggle() { if (this.open) this.close(); else this.show(this.tab); }
  show(tab) {
    this.tab = tab; this.panel.hidden = false; this.button.setAttribute("aria-expanded", "true");
    for (const t of this.panel.querySelectorAll("[data-tab]")) t.setAttribute("aria-selected", String(t.dataset.tab === tab));
    if (tab === "sources") { this.renderSources(); this.onSources?.(); }   // a fresh /api/status (ETag: a 304 is free)
    else { this.renderSystem(); this.poll(); }
    this.schedule();
  }
  close() { this.panel.hidden = true; this.button.setAttribute("aria-expanded", "false"); this.schedule(); }
  /** New /api/status (map.js): the Sources tab redraws if it's showing. */
  setStatus(st) { this.st = st; if (this.open && this.tab === "sources") this.renderSources(); }
  renderSources() {
    const rows = sourceRows(this.st, this.agoText);
    this.body.innerHTML = !this.st ? `<div class="dim">loading…</div>` : `<div class="smsum dim">${esc(sourceSummary(rows))} · ↻ fetches one now</div>
      <ul class="srclist">${rows.map((r) => `<li data-src="${esc(r.id)}" title="${esc(r.title)}">
        <span class="dot ${r.cls}"></span><span class="sn">${esc(r.name)}</span><span class="sa dim">${esc(r.ago)}</span>
        <button type="button" class="rf" title="Fetch ${esc(r.name)} now" aria-label="Fetch ${esc(r.name)} now">↻</button></li>`).join("")}</ul>`;
  }
  async refresh(id, row) {
    row.classList.add("queued"); row.querySelector(".rf").disabled = true; row.querySelector(".sa").textContent = "queued…";
    try {
      const r = await fetch(`/api/refresh/${encodeURIComponent(id)}`, { method: "POST" });
      row.querySelector(".sa").textContent = r.ok ? "queued ✓" : `failed (${r.status})`;
    } catch { row.querySelector(".sa").textContent = "failed"; }
  }
  /** The System poll runs only while the System tab is open and the page is visible (CPU % is since the last call). */
  schedule() {
    clearTimeout(this.timer); this.timer = 0;
    if (this.open && this.tab === "system" && !document.hidden) this.timer = setTimeout(() => this.poll(), SYSTEM_MS);
  }
  async poll() {
    if (this.busy) return;
    this.busy = true;
    try { this.sys = await api("/api/system"); } catch (e) { this.err = e.message; } finally { this.busy = false; }
    if (this.open && this.tab === "system") { this.renderSystem(); this.schedule(); }
  }
  renderSystem() {
    if (!this.sys) { this.body.innerHTML = `<div class="dim">${this.err ? `system unavailable (${esc(this.err)})` : "loading…"}</div>`; return; }
    const v = systemModel(this.sys), m = v.machine;
    const pct = (x) => (x == null ? "--" : `${fmt(x, 1)} %`);
    const proc = (p) => p.missing ? `<tr><td>${esc(p.label)}</td><td class="n dim" colspan="2">not running</td></tr>`
      : `<tr><td>${esc(p.label)}${p.n > 1 ? ` <span class="dim">×${p.n}</span>` : ""}</td><td class="n">${fmt(p.rss_mb, 0)} MB</td><td class="n">${pct(p.cpu_pct)}</td></tr>`;
    this.body.innerHTML = `<div class="smsec">This machine</div>
      <div class="smrow"><span>CPU</span>${barHtml(m.cpu)}<span class="n">${pct(m.cpu_pct)} <span class="dim">of ${m.cores} cores</span></span></div>
      <div class="smrow"><span>RAM</span>${barHtml(m.ram)}<span class="n">${fmt(m.ram_used_gb, 1)} / ${fmt(m.ram_total_gb, 1)} GB</span></div>
      <div class="smnote dim">up ${uptime(m.uptime_s || 0)}</div>
      <div class="smsec">Montney <span class="dim">(memory · CPU of the whole machine)</span></div>
      <table class="smt">${v.procs.map(proc).join("")}
        <tr class="tot"><td>Total</td><td class="n">${fmt(v.total.rss_mb, 0)} MB${v.total.ram_share != null ? ` <span class="dim">${pct(v.total.ram_share * 100)} of RAM</span>` : ""}</td><td class="n">${pct(v.total.cpu_pct)}</td></tr></table>
      ${v.storage ? `<div class="smsec">Storage <span class="dim">vs budget${v.storage.taken_at ? ` · measured ${esc(this.agoText({ last: { finished: v.storage.taken_at } }))} ago` : ""}</span></div>
        ${v.storage.regions.map((r) => `<div class="smrow"><span>${esc(r.code)}</span>${barHtml(r)}<span class="n">${size(r.bytes)} / ${size(r.cap)}</span></div>`).join("")}
        <div class="smnote dim">system tables ${size(v.storage.system_bytes || 0)}</div>` : ""}
      ${v.requests ? `<div class="smsec">Requests <span class="dim">per hour vs cap · last ${fmt(v.requests.hours, 1)} h${v.requests.judged ? "" : " (too little history to judge)"}</span></div>
        ${v.requests.regions.map((r) => `<div class="smrow" title="${esc(r.top.map(([s, q]) => `${s} ${fmt(q, 1)}/h`).join(" · "))}"><span>${esc(r.code)}</span>${barHtml(r)}<span class="n">${fmt(r.rate, 1)} / ${fmt(r.cap, 0)}</span></div>`).join("")}` : ""}`;
  }
}
