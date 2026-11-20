// Drill-down panel: value, change, history chart (uPlot), details and current events for a feature,
// a callout row, or an event. Opens top-right; Esc closes.
import { api, ago, esc, fmt, fmtDay, fmtTime, isoSec, tzAbbr, unitLabel, wallDate } from "./lib.js";
import { baseSeries, chipHtml, flowSign, isMonthly, monthLabel, shown, signed } from "./callouts.js";
import { renderTieout } from "./tieout.js";

const RANGES = [["7D", "-7d"], ["30D", "-30d"], ["90D", "-90d"], ["1Y", "-365d"]];
const COLORS = ["#00c98b", "#ffb547", "#4cc9f0", "#c9a2ff", "#ff6b8b"];
const SEV = { critical: "crit", warn: "warn", info: "info" };

export class Panel {
  constructor(el, { onClose } = {}) {
    Object.assign(this, { el, onClose, range: "-30d", plot: null });
    addEventListener("keydown", (e) => { if (e.key === "Escape" && !this.el.hidden) this.close(); });
  }
  close() { this.el.hidden = true; this.plot?.destroy(); this.plot = null; this.onClose?.(); }

  /** spec: {title, kicker, entry, row, refEntry, series:[ids], labels, feature (props) | null, eventsRef} */
  async open(spec) {
    this.spec = spec;
    const { entry, row = {}, feature: f } = spec;
    const e = signed(entry, spec.sign ?? 1);   // demand reads negative (PLAN 4c); capacity stays a size
    const unit = unitLabel(row.unit || e?.unit || f?.unit);
    const daily = (e?.freq || "").toUpperCase() === "D";
    // Gas-day vs calendar-day comes from the snapshot's per-series `day` (gas|calendar|""). Until every server
    // sends it, fall back to the known gas-day families (NGTL GDSR/DOP, Westcoast capacities).
    const sid = e?.series || spec.series?.[0] || "";
    const gasDaily = e?.day ? e.day === "gas" : /^(ngtl\.(gdsr|dop)|westcoast)\./.test(sid);
    const asOf = e?.ts == null ? "--" : isMonthly(e) ? `${monthLabel(e.ts)} (monthly mean)`
      : daily ? `${gasDaily ? "gas day" : "day"} ${fmtDay(e.ts)}` : `${fmtTime(e.ts)} ${tzAbbr()}`;
    const capNow = e?.cap_current ?? e?.cap;
    const sh = shown(e), a = e?.agg;
    // a live reading exists (a link with only CER key points has an entry for its capacity but no value or time)
    const live = !!e && (e.v != null || e.ts != null);
    const util = capNow && f?.value_kind !== "capacity" ? Math.abs(e.v) / capNow : null;
    const off = !!spec.plantState?.off;   // PLAN 10c: out of service: no capacity, no utilisation (no "0.00 MW of 0.00 MW")
    const fu = spec.fuel;   // PLAN 10d: a plant's fuel (sprites.js plantFuel), the first fact
    const rows = [
      fu && ["Fuel", fu.key ? `${esc(fu.word)}${fu.tech ? ` <span class="dim">· ${esc(fu.tech)}</span>` : ""}` : `<span class="dim">fuel unknown</span>`],
      f?.value_kind === "capacity" && ["Value is", "published capacity, not flow"],
      f?.value_kind === "level" && ["Value is", "a reservoir level, not plant output (MW not published)"],
      !off && e?.cap_current != null && ["Capability today", `${fmt(e.cap_current)} ${esc(unit)}`],
      !off && e?.cap != null && ["Capacity", `${fmt(e.cap)} ${esc(unit)}${e.cap_next != null ? ` <span class="chip">next ${fmt(e.cap_next)}</span>` : ""}`],
      e?.target != null && ["Target", `${fmt(e.target)} ${esc(unit)}`],
      e?.next != null && ["Next (forward)", `${fmt(e.next)} ${esc(unit)} · ${daily ? fmtDay(e.next_ts) : fmtTime(e.next_ts)}`],
      !off && util != null && ["Utilization", `${(util * 100).toFixed(0)}%`],
      a && ["Window", sh.reason ? `<span class="chip noresult">NO-RESULT</span> ${esc(sh.reason)}`
        : `${esc(sh.tag)} (min ${a.n_min ?? "?"})${a.start != null ? ` · ${fmtTime(a.start, { date: false })}–${fmtTime(a.end, { date: false })} ${tzAbbr()}` : ""}`],
      a && ["Latest point", `${e.v == null ? "--" : fmt(e.v, row.decimals)} ${esc(unit)}`],
      live && ["As of", `${asOf}${e?.ts != null ? ` · ${ago(e.ts)} ago` : ""}`],
      live && ["Status", e?.stale ? '<span class="chip stale">STALE</span>' : spec.status ? esc(spec.status)   // L-106: e.g. an estimate's own wording
        : isMonthly(e) ? "monthly mean, published with a lag (not live)" : "live (not forecast)"],
      f?.operator && ["Operator", esc(f.operator)],
      f && ["Geometry", f.schematic ? "schematic (not a surveyed route)"
        : f.kind === "node" ? (String(f.source || "").startsWith("schematic") ? "approximate point" : "point") : esc(f.kind)],
      f?.source && ["Source", `<span title="${esc(f.source)}">${esc(String(f.source).slice(0, 48))}</span>`],
    ].filter(Boolean);
    this.el.hidden = false;
    this.el.innerHTML = `<button class="x" title="Close (Esc)">✕</button>
      <div class="h">${esc(spec.kicker || "")}</div><div class="name">${esc(spec.title)}${fu ? fuelChip(fu) : ""}</div>
      ${spec.action ? `<button class="act">${esc(spec.action.label)}</button>` : ""}
      ${spec.tieout ? `<div class="tieout dim">loading the tie-out…</div>` : ""}
      ${e && (live || !f || f.value) ? `<div>${headlineHtml(sh.v, sh.v === 0 && spec.plantState ? 0 : row.decimals, unit, spec)}
        ${chipHtml(e, row, spec.refEntry)}</div>${spec.plantState ? `<div class="pstate${spec.plantState.off ? " off" : ""}">${esc(spec.plantState.text)}</div>` : ""}
        ${spec.estimate?.basis ? `<div class="basis">${esc(spec.estimate.basis)}</div>` : ""}` : ""}
      ${spec.series?.length ? `<div class="ranges">${RANGES.map(([l, r]) => `<button data-r="${r}" class="${r === this.range ? "on" : ""}">${l}</button>`).join("")}</div>
        <div class="chart"></div>` : ""}
      <table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>
      ${spec.eventsRef ? `<div class="evh">EVENTS</div><div class="evs">loading…</div>` : ""}
      ${spec.body ? `<div class="evbody">${spec.body}</div>` : ""}`;
    this.el.querySelector(".x").onclick = () => this.close();
    const act = this.el.querySelector(".act");
    if (act) act.onclick = () => spec.action.onClick();   // e.g. the top producers popup (L-075)
    this.el.querySelectorAll(".ranges button").forEach((b) => (b.onclick = () => { this.range = b.dataset.r; this.open(this.spec); }));
    if (spec.tieout) renderTieout(this.el.querySelector(".tieout"), spec.tieout)
      .catch((err) => { const b = this.el.querySelector(".tieout"); if (b) b.textContent = `tie-out unavailable (${err.message})`; });
    if (spec.series?.length) this.chart(spec).catch((err) => { this.el.querySelector(".chart").textContent = `history unavailable (${err.message})`; });
    if (spec.eventsRef) this.events(spec.eventsRef).catch(() => { this.el.querySelector(".evs").textContent = "events unavailable"; });
  }

  async chart(spec) {
    const ids = spec.series.map(baseSeries), get = (r) => api(`/api/series?ids=${encodeURIComponent(ids.join(","))}&start=${r}`);
    let data = await get(this.range), widened = false;
    // nothing in the selected range (e.g. CER key points that end months ago): widen to a year rather than say "no history"
    if (!ids.some((id) => (data[id]?.ts || []).length) && this.range !== "-365d") { data = await get("-365d"); widened = true; }
    const box = this.el.querySelector(".chart");
    if (!box || this.spec !== spec) return;
    // One unit per axis (L-078: IESO's hourly MW next to its daily MWh/d sums). With two units, 7D shows the sub-daily
    // series and 30D+ the daily/monthly ones (each falls back to the other if it has no points), and any second unit
    // left gets its own axis on the right.
    const has = (id) => (data[id]?.ts || []).length > 0, unitOf = (id) => data[id]?.meta?.unit || "";
    let unitNote = "";
    if (new Set(ids.filter(has).map(unitOf)).size > 1) {
      const coarse = (id) => /^(D|M)$/i.test(data[id]?.meta?.freq || ""), short = this.range === "-7d" && !widened;
      const pick = ids.filter((id) => coarse(id) !== short);
      if (pick.some(has)) {
        const others = ids.filter((id) => !pick.includes(id) && has(id));
        if (others.length) unitNote = `Showing ${short ? "sub-daily" : "daily"} ${[...new Set(pick.filter(has).map(unitOf))].join(" / ")}; ` +
          `${[...new Set(others.map(unitOf))].join(" / ")} on ${short ? "30D and longer" : "7D"}.`;
        ids.splice(0, ids.length, ...pick);
      }
    }
    // a series whose newest point is over a week old is dated context (CER key points publish with a lag): say so, so it
    // never reads as today's flow
    const now = Date.now() / 1000, last = (id) => data[id]?.ts?.at(-1);
    const dated = ids.filter((id) => last(id) != null && now - last(id) > 7 * 86400);
    const absent = ids.filter((id) => last(id) == null);
    const note = [unitNote, widened && "No points in the selected range: showing 1Y.",
      dated.length && `Dated: ${dated.map((id) => `${data[id]?.meta?.label || id} ends ${fmtDay(last(id))}`).join("; ")}. Context, not today's flow.`,
      absent.length && !widened && `${absent.length} series ${absent.length === 1 ? "has" : "have"} no points in this range (dated: try 1Y).`].filter(Boolean).join(" ");
    box.parentElement.querySelector(".chartnote")?.remove();
    if (note) box.insertAdjacentHTML("afterend", `<div class="chartnote dim">${esc(note)}</div>`);
    // Merge timestamps across series (union), null where a series has no point.
    const tsSet = new Set();
    ids.forEach((id) => (data[id]?.ts || []).forEach((t) => tsSet.add(t)));
    const xs = [...tsSet].sort((a, b) => a - b);
    if (!xs.length) { box.textContent = "no history yet"; return; }
    // every series reads with its sign (demand negative), so gas-burn and delivery lines match the box; the headline
    // series uses the headline's sign (the feature's `flow` can override its id)
    const at = (i) => spec.series.map(baseSeries).indexOf(ids[i]);   // ids may be a subset (one unit per axis)
    const k = (i) => (at(i) === 0 && spec.sign != null ? spec.sign : flowSign(null, ids[i]));
    const cols = ids.map((id, i) => { const m = new Map((data[id]?.ts || []).map((t, j) => [t, data[id].v[j] == null ? null : k(i) * data[id].v[j]]));
      return xs.map((t) => m.get(t) ?? null); });
    const daily = ids.every((id) => /^(D|M)$/i.test(data[id]?.meta?.freq || ""));
    const u0 = unitOf(ids.find(has) || ids[0]), u2 = [...new Set(ids.filter(has).map(unitOf))].find((u) => u !== u0);
    this.plot?.destroy();
    this.plot = new uPlot({
      width: box.clientWidth || 300, height: 120, padding: [6, 4, 0, 0],
      tzDate: daily ? (ts) => { const d = new Date(ts * 1000); return new Date(d.getTime() + d.getTimezoneOffset() * 60000); } : wallDate,
      legend: { show: ids.length > 1, live: false }, cursor: { points: { size: 5 } },
      scales: { x: { time: true }, ...(u2 ? { y2: {} } : {}) },
      axes: [{ stroke: "#6e8d7f", grid: { stroke: "rgba(29,50,39,.6)", width: 1 }, ticks: { show: false }, font: "10px JetBrains Mono", size: 22,
               values: (u, splits) => { const span = (u.scales.x.max - u.scales.x.min) || 0;
                 return splits.map((s) => (daily ? fmtDay(s) : fmtTime(s, { date: span > 2 * 86400, time: span <= 2 * 86400 }))); } },
             { stroke: "#6e8d7f", grid: { stroke: "rgba(29,50,39,.6)", width: 1 }, ticks: { show: false }, font: "10px JetBrains Mono", size: 44,
               values: (u, v) => v.map((x) => fmt(x)), label: u2 ? u0 : undefined, labelSize: u2 ? 12 : 0, labelFont: "9px JetBrains Mono" },
             ...(u2 ? [{ scale: "y2", side: 1, stroke: "#6e8d7f", grid: { show: false }, ticks: { show: false }, font: "10px JetBrains Mono", size: 44,
               values: (u, v) => v.map((x) => fmt(x)), label: u2, labelSize: 12, labelFont: "9px JetBrains Mono" }] : [])],
      series: [{}, ...ids.map((id, i) => ({ label: (spec.labels?.[at(i)] || data[id]?.meta?.label || id) + (dated.includes(id) ? ` (to ${fmtDay(last(id))})` : ""),
        stroke: COLORS[i % COLORS.length], scale: unitOf(id) === u0 ? "y" : "y2",
        width: 1.5, fill: i === 0 ? "rgba(0,201,139,.10)" : undefined, dash: i > 0 && /cap/.test(id) ? [4, 3] : undefined,
        spanGaps: true, points: { show: false } }))],
    }, [xs, ...cols], box);
  }

  async events(ref) {
    const evs = await api(`/api/events?ref=${encodeURIComponent(ref)}&state=current`);
    const box = this.el.querySelector(".evs");
    if (!box) return;
    box.innerHTML = evs.length ? evs.slice(0, 8).map(eventHtml).join("") : '<span class="dim">none current</span>';
  }
}

export function eventWhen(ev) {
  const s = isoSec(ev.starts), e = isoSec(ev.ends);
  if (!s && !e) return "undated";
  return `${s ? fmtTime(s, { time: false }) : "…"} → ${e ? fmtTime(e, { time: false }) : "open"}`;
}
/**
 * The panel's big number. An estimated feature never shows a bare number:
 * - `spec.band` (a feature's `band`, e.g. gas.kitimat 0.10): "≈ v ±abs unit", the same format as the callout (#163);
 * - `spec.estimate` ({lo, hi, basis}: the lo/hi values and the basis text, Power's Peace dam nodes): "est v [lo–hi] unit",
 *   with the basis on the line below (open()). A missing lo/hi still reads "est" with "[band n/a]".
 * Otherwise the plain value and unit.
 */
/** A plant's fuel beside its name (PLAN 10d): its 16 px map sprite (nodes atlas) and the fuel word, or "fuel unknown". */
export function fuelChip(fu) {
  const s = fu.sprite, icon = s ? `<i class="fspr" style="width:${s.w}px;height:${s.h}px;background-position:-${s.x}px -${s.y}px"></i>` : "";
  return ` <span class="fuelchip${fu.key ? "" : " unk"}">${icon}${esc(fu.key ? fu.word : "fuel unknown")}</span>`;
}

export function headlineHtml(v, decimals, unit, spec = {}) {
  const big = `<span class="big">${v == null ? "--" : fmt(v, decimals)}</span>`, u = `<span class="u">${esc(unit)}</span>`;
  const est = spec.estimate?.reading ? null : spec.estimate;   // PLAN 10c: a `basis` reading (NRC daily) isn't an estimate
  if (est) {
    // `range: false`: an estimate with no lo/hi by design (L-106: a plant from its BA's utilisation); its basis says how
    const range = est.range === false ? "" : est.lo != null && est.hi != null ? `[${fmt(est.lo, decimals)}–${fmt(est.hi, decimals)}]` : "[band n/a]";
    return `<span class="est" title="Estimate">est</span> ${big}${range ? ` <span class="u">${range}</span>` : ""} ${u}`;
  }
  if (spec.band) {
    const pm = v == null ? "" : ` <span class="u">±${fmt(Math.abs(v) * spec.band, 0)}</span>`;
    return `<span class="est" title="Estimate ±${Math.round(spec.band * 100)}%">≈</span> ${big}${pm} ${u}`;
  }
  return `${big} ${u}`;
}

export function eventHtml(ev) {
  return `<div class="ev ${SEV[ev.severity] || "info"}"><span class="sev"></span>
    <span class="t">${ev.url ? `<a href="${esc(ev.url)}" target="_blank" rel="noopener">${esc(ev.title)}</a>` : esc(ev.title)}</span>
    <span class="w">${esc(ev.kind)} · ${eventWhen(ev)}</span></div>`;
}
