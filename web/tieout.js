// AB + BC tie-out (L-079, Erik: "AB + BC prod − exports − demand = NGTL net storage + Aitken Creek net storage ... this
// is essentially just saying AB + BC should tie out. And that formula is the check"). Shown in the panel when the clicked
// row's series is a map.json `tieouts` trigger. Everything comes from that config (Gas #296's series); nothing here
// knows an id:
//   bars:     the supply-side waterfall, in order, each {series | [series...], label, sign}: + adds, − subtracts
//   storage:  the actual storage terms (+ = withdrawal), each {series, label}
//   residual: {series, band, label}: actual − implied after the carried terms; it ties out when |residual| ≤ band
//   raw:      {series, band, label}: the same before the carried terms (context, not a tie-out)
//   chart:    [{series, label, history?}]: lines for the history; `history` (Gas #299: monthly, freq M) extends a daily
//             line back by months, over `history_days`; where both have a point the daily one wins
//   day_of:   the series whose latest point is "the latest day" (the balance writes a day only when every input exists)
//   preliminary_hours: TC revises a GDSR gas day for up to ~47 h after it starts (Gas #328), so a day younger than this
//             is marked "preliminary" in the headers and shaded on the chart. The gas day's start hour (UTC) comes from
//             map.json `gas_day` (map.js passes it as `gas_day_start_h`).
//   raw.breakdown (L-080 item 4, Erik: "Include the estimation when I click the unaccounted for amount and what it assumes
//             it is"): {title, parts: [{series, label, assume, confidence?, band | band_rel | band_series, measured?, parts?: [...]}], history?,
//             report?: {url, label, summary}, note?}.
//             Clicking the raw error line opens it: each part at that day with its band and assumption, a part's own parts
//             folded under it, and the sum (Gas #313: the parts sum exactly to the raw error). Fetched on the first open.
//             history: {title, months, series: [{series, label}]}, monthly lines over history_days.
// Values are read at that day from /api/series (a term's own latest point may be a newer day), so the bars add up.
import { api, esc, fmt, fmtDay } from "./lib.js";

const ids = (s) => (Array.isArray(s) ? s : [s]);
/** The value of a term (one series, or the sum of several) at time t, or null if any part has no point there. */
export function termAt(data, series, t) {
  let sum = 0;
  for (const id of ids(series)) {
    const d = data[id], i = d?.ts?.indexOf(t) ?? -1;
    if (i < 0 || d.v[i] == null) return null;
    sum += d.v[i];
  }
  return sum;
}

/** Is gas day t (its date's 00:00 UTC, as stored) younger than `hours` since it started (`startH` UTC that date)? */
export const isPreliminary = (t, nowS, startH, hours) => hours > 0 && t != null && nowS - (t + startH * 3600) < hours * 3600;
const prelimOf = (cfg) => (t) => isPreliminary(t, Date.now() / 1000, cfg.gas_day_start_h ?? 0, cfg.preliminary_hours ?? 0);
const PRELIM_TIP = "TC revises a gas day's GDSR for up to ~47 h after it starts: this day's numbers, and its error, may still move";
const prelimChip = (on) => (on ? ` <span class="chip stale prelim" title="${esc(PRELIM_TIP)}">preliminary</span>` : "");

/** The waterfall for day t: each bar's signed contribution, the running total, and what is left at the end. */
export function waterfall(cfg, data, t) {
  let run = 0;
  const bars = cfg.bars.map((b) => {
    const v = termAt(data, b.series, t), c = v == null ? null : (b.sign ?? 1) * v;
    if (c != null) run += c;
    return { label: b.label, v, c, run: c == null ? null : run };
  });
  return { bars, left: bars.every((b) => b.c != null) ? run : null };
}

export async function renderTieout(box, cfg) {
  const all = [...new Set([cfg.day_of, ...cfg.bars.flatMap((b) => ids(b.series)), ...cfg.storage.map((s) => s.series),
    cfg.residual.series, cfg.residual.band, cfg.raw?.series, cfg.raw?.band, ...cfg.chart.map((c) => c.series)].filter(Boolean))];
  const hist = cfg.chart.map((c) => c.history).filter(Boolean);
  const [data, long] = await Promise.all([api(`/api/series?ids=${encodeURIComponent(all.join(","))}&start=-${cfg.days || 30}d`),
    hist.length ? api(`/api/series?ids=${encodeURIComponent(hist.join(","))}&start=-${cfg.history_days || 1100}d`).catch(() => ({})) : {}]);
  Object.assign(data, long);
  const t = data[cfg.day_of]?.ts?.at(-1);
  if (t == null) { box.innerHTML = `<div class="dim">${esc(cfg.title)}: no balance day yet.</div>`; return; }
  const unit = cfg.unit || "MMcf/d", f = (v) => (v == null ? "--" : fmt(v, 0));
  const sgn = (v) => (v == null ? "--" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmt(Math.abs(v), 0)}`);
  const w = waterfall(cfg, data, t), max = Math.max(...w.bars.map((b) => Math.abs(b.c ?? 0)), 1);
  const st = cfg.storage.map((s) => ({ ...s, v: termAt(data, s.series, t) }));
  const stSum = st.every((s) => s.v != null) ? st.reduce((a, s) => a + s.v, 0) : null;
  const res = termAt(data, cfg.residual.series, t), band = termAt(data, cfg.residual.band, t);
  const raw = cfg.raw && termAt(data, cfg.raw.series, t), rawBand = cfg.raw && termAt(data, cfg.raw.band, t);
  const ties = res != null && band != null && Math.abs(res) <= band;
  const bar = (c) => `<span class="wb ${c < 0 ? "neg" : "pos"}" style="width:${Math.round((Math.abs(c ?? 0) / max) * 100)}%"></span>`;
  const prelim = prelimOf(cfg);
  box.innerHTML = `<div class="th">${esc(cfg.title)} · gas day ${esc(fmtDay(t))} · ${esc(unit)}${prelimChip(prelim(t))}</div>
    <table class="wf">${w.bars.map((b) => `<tr><td>${esc(b.label)}</td><td class="n">${sgn(b.c)}</td><td class="b">${bar(b.c)}</td></tr>`).join("")}
      <tr class="tot"><td>${esc(cfg.left_label || "= left over")}</td><td class="n">${sgn(w.left)}</td><td></td></tr></table>
    <table class="wf st">${st.map((s) => `<tr><td>${esc(s.label)}</td><td class="n">${sgn(s.v)}</td><td></td></tr>`).join("")}
      <tr class="tot"><td>${esc(cfg.storage_label || "= actual storage")}</td><td class="n">${sgn(stSum)}</td><td></td></tr></table>
    <div class="res ${ties ? "ok" : "off"}"><span>${esc(cfg.residual.label)}</span>
      <b>${sgn(res)} ±${f(band)}</b> <span class="chip ${ties ? "up" : "stale"}">${ties ? "ties out ✓" : "doesn't tie out ✗"}</span></div>
    ${cfg.raw ? (cfg.raw.breakdown
      ? `<button type="button" class="rawres brk" aria-expanded="false"><span class="caret">▸</span> ${esc(cfg.raw.label)}: <b>${sgn(raw)}</b> ±${f(rawBand)} <span class="dim">· what it is</span></button><div class="bd" hidden></div>`
      : `<div class="dim rawres">${esc(cfg.raw.label)}: ${sgn(raw)} ±${f(rawBand)}</div>`) : ""}
    ${cfg.note ? `<div class="dim tnote">${esc(cfg.note)}</div>` : ""}
    <div class="tchart"></div>`;
  chart(box.querySelector(".tchart"), cfg, data, unit);
  const btn = box.querySelector("button.brk"), bd = box.querySelector(".bd");
  if (btn) btn.onclick = () => {
    const open = bd.hidden; bd.hidden = !open; btn.setAttribute("aria-expanded", String(open));
    btn.querySelector(".caret").textContent = open ? "▾" : "▸";
    if (open && !bd.dataset.done) { bd.dataset.done = "1"; bd.innerHTML = `<div class="dim">loading the breakdown…</div>`;
      openBreakdown(bd, cfg, t, raw).catch((err) => { delete bd.dataset.done; bd.textContent = `breakdown unavailable (${err.message})`; }); }
  };
}

/** A part's band at value v: ±band (absolute), ±band_rel × |v|, or a band series' value that day; null when measured or unbanded. */
export const bandOf = (p, v, data, t) => (v == null || p.measured ? null : p.band_series ? termAt(data, p.band_series, t)
  : p.band_rel != null ? p.band_rel * Math.abs(v) : p.band ?? null);
/** The breakdown rows at day t (depth 0 parts, each followed by its depth 1 parts) and the parts' sum (null if any is missing). */
export function breakdownRows(bd, data, t) {
  const rows = [], row = (p, depth, parent) => { const v = termAt(data, p.series, t);
    rows.push({ series: ids(p.series).join(","), label: p.label, assume: p.assume || "", v, band: bandOf(p, v, data, t), measured: !!p.measured, confidence: p.confidence || "",
      depth, parent, kids: (p.parts || []).length }); return v; };
  let sum = 0;
  for (const p of bd.parts) {
    const v = row(p, 0, null);
    sum = v == null || sum == null ? null : sum + v;
    for (const k of p.parts || []) row(k, 1, ids(p.series).join(","));
  }
  return { rows, sum };
}
const bdIds = (parts) => parts.flatMap((p) => [...ids(p.series), ...(p.band_series ? [p.band_series] : []), ...bdIds(p.parts || [])]);

async function openBreakdown(box, cfg, t, raw) {
  const bd = cfg.raw.breakdown, unit = cfg.unit || "MMcf/d", hist = (bd.history?.series || []).map((h) => h.series);
  const [data, long] = await Promise.all([api(`/api/series?ids=${encodeURIComponent(bdIds(bd.parts).join(","))}&start=-${cfg.days || 30}d`),
    hist.length ? api(`/api/series?ids=${encodeURIComponent(hist.join(","))}&start=-${cfg.history_days || 1100}d`).catch(() => ({})) : {}]);
  const { rows, sum } = breakdownRows(bd, data, t);
  const sgn = (v) => (v == null ? "--" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmt(Math.abs(v), 0)}`);
  const off = sum != null && raw != null && Math.abs(sum - raw) > 1;   // Gas: they sum exactly; say so if a day doesn't
  box.innerHTML = `<div class="th">${esc(bd.title || "Where the error goes")} · gas day ${esc(fmtDay(t))} · ${esc(unit)}${prelimChip(prelimOf(cfg)(t))}</div>
    <table class="wf bdt">${rows.map((r) => `<tr class="bp d${r.depth}${r.kids ? " haskids" : ""}" data-s="${esc(r.series)}"${r.parent ? ` data-p="${esc(r.parent)}" hidden` : ""}>
        <td>${r.kids ? `<span class="caret">▸</span> ` : ""}${esc(r.label)}</td><td class="n">${sgn(r.v)}</td>
        <td class="n dim">${r.measured ? "measured" : r.band != null ? `±${fmt(r.band, 0)}` : ""}</td></tr>
      ${r.assume || r.confidence ? `<tr class="ba d${r.depth}"${r.parent ? ` data-p="${esc(r.parent)}" hidden` : ""}><td colspan="3" class="dim">${r.confidence ? `<span class="conf">confidence: ${esc(r.confidence)}</span>${r.assume ? " · " : ""}` : ""}${esc(r.assume)}</td></tr>` : ""}`).join("")}
      <tr class="tot"><td>${esc(bd.sum_label || "= tie-out error")}</td><td class="n">${sgn(sum)}</td><td class="n dim">${off ? `≠ ${sgn(raw)}` : ""}</td></tr></table>
    ${bd.note ? `<div class="dim tnote">${esc(bd.note)}</div>` : ""}
    ${bd.report ? `<div class="bdr">${bd.report.summary ? `<span class="dim">${esc(bd.report.summary)}</span> ` : ""}<a href="${esc(bd.report.url)}" target="_blank" rel="noopener">${esc(bd.report.label || "Full report")} ↗</a></div>` : ""}
    ${hist.length ? `<div class="th bdh">${esc(bd.history.title || "History")}</div><div class="bdchart"></div>` : ""}`;
  // a part with parts folds them under it (closed first)
  for (const tr of box.querySelectorAll("tr.haskids")) {
    tr.onclick = () => { const open = tr.classList.toggle("open"); tr.querySelector(".caret").textContent = open ? "▾" : "▸";
      for (const k of box.querySelectorAll(`tr[data-p="${CSS.escape(tr.dataset.s)}"]`)) k.hidden = !open; };
  }
  if (hist.length) chart(box.querySelector(".bdchart"), { chart: bd.history.series, months: bd.history.months }, { ...data, ...long }, unit);   // monthly: never preliminary
}

/** One line per chart entry: its monthly history (if any) then its daily points; a daily point wins on a shared ts. */
export function chartColumns(cfg, data) {
  const pts = (id) => (data[id]?.ts || []).map((t, i) => [t, data[id].v[i]]);
  const lines = cfg.chart.map((c) => new Map([...(c.history ? pts(c.history) : []), ...pts(c.series)]));
  const xs = [...new Set(lines.flatMap((m) => [...m.keys()]))].sort((a, b) => a - b);
  return { xs, cols: lines.map((m) => xs.map((t) => m.get(t) ?? null)) };
}

function chart(box, cfg, data, unit) {
  if (!globalThis.uPlot || !box) return;
  const { xs, cols } = chartColumns(cfg, data);
  if (xs.length < 2) { box.innerHTML = `<div class="dim">History fills in daily (${xs.length} day so far).</div>`; return; }
  const months = cfg.months || cfg.chart.some((c) => c.history && data[c.history]?.ts?.length);
  const COL = ["#ff6b1a", "#3dffb8", "#ffb547", "#3d9bff", "#c792ff", "#ff5c8a", "#9fd36b", "#d8d8d8"];
  new globalThis.uPlot({ width: Math.max(240, box.clientWidth || 300), height: 130, legend: { show: true, live: false },
    scales: { x: { time: true } }, cursor: { points: { size: 5 } },
    axes: [{ stroke: "#6e8d7f", grid: { stroke: "rgba(29,50,39,.6)" }, ticks: { show: false }, font: "10px JetBrains Mono", size: 22,
             values: (u, s) => s.map((x) => (months ? new Date(x * 1000).toLocaleString("en-CA", { month: "short", year: "2-digit", timeZone: "UTC" }) : fmtDay(x))) },
           { stroke: "#6e8d7f", grid: { stroke: "rgba(29,50,39,.6)" }, ticks: { show: false }, font: "10px JetBrains Mono", size: 48,
             values: (u, v) => v.map((x) => fmt(x, 0)), label: unit, labelSize: 12, labelFont: "9px JetBrains Mono" }],
    series: [{}, ...cfg.chart.map((c, i) => ({ label: c.label, stroke: COL[i % COL.length], width: 1.5, dash: c.dash, spanGaps: true,
      points: { show: true, size: 4 } }))],
    hooks: { draw: [(u) => shadePreliminary(u, xs.filter(prelimOf(cfg)))] } }, [xs, ...cols], box);
}
/** Shade the chart from the first preliminary day to the right edge, labelled, so its last points don't read as final. */
function shadePreliminary(u, days) {
  if (!days.length) return;
  const ctx = u.ctx, { left, top, width, height } = u.bbox, x0 = Math.max(left, Math.round(u.valToPos(days[0], "x", true) - 4 * devicePixelRatio));
  ctx.save();
  ctx.fillStyle = "rgba(255,181,71,.12)"; ctx.fillRect(x0, top, left + width - x0, height);
  ctx.fillStyle = "#ffb547"; ctx.font = `${9 * devicePixelRatio}px JetBrains Mono`; ctx.textAlign = "right";
  ctx.fillText("preliminary", left + width - 2 * devicePixelRatio, top + 10 * devicePixelRatio);
  ctx.restore();
}
