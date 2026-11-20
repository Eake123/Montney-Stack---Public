// Region page (PLAN 9 part 3, Erik 2026-11-13): a click on a region box's title opens a large tabbed view of the region,
// built from one call, GET /api/region/<id> (montney/regions.py; docs/REGIONS.md §5b). Tabs Gas / Power / Oil: the tab's
// balance lines beside their history chart, then the region's detail groups as tables with bars (production by basin,
// demand by sector, generation by fuel, ...). A click on any row charts that series (/api/series). Static facts (a value
// with a date and a source) are plain rows. The weather strip and the watch list close the page. Nothing here names a
// region: everything comes from the payload. Desktop: most of the screen; phone: full screen.
import { ageLabel, api, esc, fmt, fmtDay, fmtTime, lateAge, unitLabel, wallDate } from "./lib.js";

export const TABS = [["gas", "Gas"], ["power", "Power"], ["oil", "Oil"]];
const RANGES = [["7D", "-7d"], ["30D", "-30d"], ["90D", "-90d"], ["1Y", "-365d"]];
const COLORS = ["#00c98b", "#ffb547", "#4cc9f0", "#c9a2ff", "#ff6b8b", "#eef6f2", "#9fd36a"];

/** A line with no box row of its own is a sink when it is demand, load or exports (as the map reads demand-like flows). */
const SINK = /(^|\.)(demand|load|exports?)$/;
/**
 * The tab's lines with their display sign: the box row's for the same line (`<tab>.<line>`; sources +, sinks −); a line
 * the box doesn't carry (power load, …) is − when it's a sink (Reviewer on #464: load read + while gas demand read −).
 */
export function signed(lines, box, tab) {
  const s = new Map((box || []).map((b) => [b.line, b.sign]));
  return lines.map((l) => { const key = l.line?.includes(".") ? l.line : `${tab}.${l.line}`;
    return { ...l, sign: l.sign ?? s.get(key) ?? (SINK.test(key) ? -1 : 1) }; });
}

/** A tab with no group and no line that has (or will have) a value: shown as plainly empty (PLAN 9g: sont / qc oil). */
export function tabEmpty(t) {
  return !(t?.groups || []).length && (t?.lines || []).every((l) => l.have === false || l.missing);
}

/** Which tabs have anything to show (a line or a group); the first of them opens. */
export function tabsWith(data) {
  return TABS.filter(([k]) => { const t = data?.tabs?.[k]; return !!t && ((t.lines || []).length || (t.groups || []).length); }).map(([k]) => k);
}

/**
 * A value as the page shows it: { text, dim, title }. No value: "—", dimmed, the reason (`why`) as the tooltip; a known
 * zero (`zero`): "0"; else the number with its display `sign` (sources +, sinks −) when the payload gives one.
 */
export function valueText(r, decimals) {
  if (r == null) return { text: "—", dim: true, title: "" };
  if (r.zero) return { text: "0", dim: false, title: r.note || "" };
  const v = r.v ?? r.value;
  if (v == null || r.have === false) return { text: "—", dim: true, title: r.why || "no value yet" };
  return { text: fmt((r.sign ?? 1) * v, decimals), dim: !!r.stale, title: r.note || "" };
}

/**
 * A group's rows with their bar and share. §5b groups are free tables, not partitions: rows overlap (AB and BC beside the
 * NGTL-area cut), nest ("Alberta on NGTL" and its sectors) and mix units (flows beside Bcf inventory). So a share is only
 * a row's part of the line the group names (`of`, e.g. demand), for the same period and unit (Reviewer on #464/#467).
 * Without `of` there is no share and no "%": the bar is the row against the group's largest row in the group's unit
 * (`rel`, drawn lighter), and a row in another unit gets none. Static facts (`value`, no series) never get a bar.
 */
export const REST_TOL = 0.005;   // "Other / not itemised" under 0.5 % of the line is rounding, not a remainder

export const DOMINANT = 0.8;   // the line's sign is "dominant" when rows of that sign carry this much of the absolute total
const CHARGING = /battery|batteries|pumped[ _-]?storage/i;
const SUB = /^of which\b/i;   // a part of the row above ("of which power burn")

/**
 * How a group's rows relate to its `of` line. Returns { mode, of, basis, unit, mixed }:
 *  - "share": every row with a value (in the line's unit, on the line's day) has the line's sign: each row is its part of
 *    the line (basis = |line|);
 *  - "gross": only in a group that opts in (`gross`, set by the server on the fuel mix alone, names the basis for the
 *    card's header; Reviewer on #532), and only when the rows share one timestamp and rows of the line's sign carry ≥ DOMINANT (80 %) of the
 *    absolute total ("of which" rows aside): those rows are parts of their own sum (basis = gross generation); any that
 *    run the other way (a battery or pumped storage charging at midday, orchestrator on #530) show their value with no
 *    share, drawn the other way from the zero line;
 *  - "free": no `of` line, rows on different days among themselves (PLAN 9g), or signs mixed with no dominant side
 *    (interchange by tie, Reviewer on #479/#480): no shares, bars against the largest row.
 * `mixed`: the rows have both signs (their bars are drawn from a zero line).
 */
export function groupBasis(group, lines = []) {
  const rows = group.rows || [];
  const of = group.of ? lines.find((l) => l.line === group.of || l.line?.endsWith(`.${group.of}`)) : null;
  const ofOk = of?.v != null && Math.abs(of.v) > 0;
  const unit = ofOk ? of.unit : group.unit || rows.find((r) => r.series && r.unit)?.unit;
  const liveRows = rows.filter((r) => r.series != null && r.v != null && r.have !== false && (!r.unit || !unit || r.unit === unit));
  const nz = liveRows.filter((r) => r.v !== 0);
  const mixed = nz.some((r) => r.v > 0) && nz.some((r) => r.v < 0);
  const free = { mode: "free", of: null, basis: null, unit: group.unit || rows.find((r) => r.series && r.unit)?.unit, mixed };
  if (!ofOk) return free;
  const s = Math.sign(of.v);
  // "share": every row on the line's day / hour and of its sign; the line itself is the basis
  if (liveRows.every((r) => r.ts == null || of.ts == null || r.ts === of.ts) && nz.every((r) => Math.sign(r.v) === s))
    return { mode: "share", of, basis: Math.abs(of.v), unit: of.unit, mixed };
  // "gross": the rows' own sum is the basis, so they need one timestamp among themselves (not the line's: an hourly line
  // is stamped an hour or two before its fuel rows, the region engine waiting for every part); "of which" rows are parts
  // of the row above, so they stay out of the sum. Rows on different days among themselves (exports by point) stay free.
  const top = nz.filter((r) => !SUB.test(r.label || "")), ts = new Set(liveRows.map((r) => r.ts).filter((t) => t != null));
  const same = top.filter((r) => Math.sign(r.v) === s);
  const total = top.reduce((a, r) => a + Math.abs(r.v), 0), gross = same.reduce((a, r) => a + Math.abs(r.v), 0);
  if (group.gross && ts.size <= 1 && total > 0 && gross / total >= DOMINANT) return { mode: "gross", of, basis: gross, unit: of.unit, mixed, sign: s };
  return free;
}

export function groupBars(group, lines = []) {
  const rows = group.rows || [];
  const { mode, basis, unit, mixed, sign } = groupBasis(group, lines);
  const sameUnit = (r) => !r.unit || !unit || r.unit === unit;
  const live = (r) => r.series != null && r.v != null && r.have !== false;
  const max = Math.max(0, ...rows.filter((r) => live(r) && sameUnit(r)).map((r) => Math.abs(r.v)));
  return rows.map((r) => {
    const fact = r.series == null && r.value != null;
    if (fact || !live(r) || !sameUnit(r)) return { ...r, fact, share: null, bar: 0, rel: false, split: false };
    if (mode === "share") { const share = Math.abs(r.v) / basis; return { ...r, fact, share, bar: Math.min(1, share), rel: false, split: false }; }
    if (mode === "gross") {
      // the line's side: parts of their own sum; the other side: its value, no share, drawn the other way (and named)
      const own = r.v === 0 || Math.sign(r.v) === sign, share = own ? Math.abs(r.v) / basis : null;
      const tags = !own && CHARGING.test(r.label || "") ? [...(r.tags || []), "charging"] : r.tags;
      return { ...r, tags, fact, share, bar: Math.min(1, Math.abs(r.v) / basis), rel: !own, split: true, neg: r.v < 0 };
    }
    // a free table: against its largest row; with both signs, from a zero line (imports one way, exports the other)
    return { ...r, fact, share: null, bar: max > 0 ? Math.abs(r.v) / max : 0, rel: true, split: mixed, neg: r.v < 0 };
  });
}

/** Show "Other / not itemised": only when the rows are shares of the `of` line itself, and |rest| is past rounding. */
export function showRest(group, lines = []) {
  const { mode, basis } = groupBasis(group, lines);
  return mode === "share" && group.rest != null && Math.abs(group.rest) >= REST_TOL * basis;
}

/**
 * The period a weekly / monthly / quarterly / annual value is for, from its `ts` (UTC): "wk to Nov 6" (the week ending then), "Aug 2026", "Q3 2026", "2026"; null for
 * any other freq. lateAge leaves these out by design (they name their period), so the page must name it, or a months-old
 * monthly row (an August storage injection, say) reads as current in November (Reviewer, #434/#435's wrong-sign trap).
 */
export function periodLabel(r) {
  const f = String(r?.freq || "").toUpperCase();
  if (r?.ts == null || !/^(M|MS|Q|QS|A|AS|Y|YS|W)$/.test(f)) return null;
  const d = new Date(r.ts * 1000), y = d.getUTCFullYear();
  if (f === "W") return `wk to ${d.toLocaleString("en-CA", { month: "short", day: "numeric", timeZone: "UTC" })}`;   // PLAN 9g: weekly rows; the EIA weekly sources stamp the week-ending Friday (Reviewer on #530)
  if (f[0] === "M") return `${d.toLocaleString("en-CA", { month: "short", timeZone: "UTC" })} ${y}`;
  if (f[0] === "Q") return `Q${Math.floor(d.getUTCMonth() / 3) + 1} ${y}`;
  return String(y);
}

function tagsHtml(r) {
  const tags = [...(r.tags || [])];
  const period = periodLabel(r);
  if (period) tags.unshift(period);   // first, so the month is the first thing read after the label
  const late = lateAge(r);
  if (late != null) tags.push(ageLabel(late));
  if (r.stale) tags.push("stale");
  return tags.length ? `<span class="rtags">${tags.map((t) => esc(t)).join(" · ")}</span>` : "";
}

/** "wx.fort_st_john.tmean" -> "Fort St John". */
export const cityName = (sid) => String(sid).split(".")[1]?.split("_").map((w) => w[0]?.toUpperCase() + w.slice(1)).join(" ") || sid;

/**
 * The degree-day table's rows from the payload's weather series (REGIONS.md §5b): HDD and CDD, each with today, its
 * normal, the realized total and the 1-7 / 8-15 day outlooks, matched by name (…hdd_normal, …hdd.realized, …hdd_1_7).
 */
export function ddRows(w) {
  if (!w) return [];
  const all = [...(w.normals || []), ...(w.realized || []), ...(w.outlook || [])];
  return [["hdd", "HDD"], ["cdd", "CDD"]].filter(([k]) => w[k]).map(([k, label]) => ({ label, today: w[k],
    normal: all.find((s) => s.endsWith(`${k}_normal`)), realized: all.find((s) => s.endsWith(`${k}.realized`)),
    d7: all.find((s) => s.endsWith(`${k}_1_7`)), d15: all.find((s) => s.endsWith(`${k}_8_15`)) }));
}

/**
 * The balance's caveat, if any (PLAN 9g): a gas balance built from finished gas days (`through: "yesterday"`), or a tab
 * whose data publishes late (`lag_s`, 6 h or more).
 */
export function balNote(t, tab) {
  const parts = [];
  if (t?.through === "yesterday") parts.push(tab === "gas" ? "Finished gas days, through yesterday" : "Through yesterday");
  if (t?.lag_s >= 6 * 3600) parts.push(`published ~${t.lag_s >= 86400 ? `${Math.round(t.lag_s / 86400)} d` : `${Math.round(t.lag_s / 3600)} h`} behind`);
  return parts.length ? parts.join(" · ") : "";
}

export class RegionPage {
  constructor(root = document.body) {
    this.root = root; this.range = "-30d";
    addEventListener("keydown", (e) => { if (e.key === "Escape" && this.el) this.close(); });
  }
  close() { this.plot?.destroy(); this.plot = null; this.el?.remove(); this.el = null; }

  async open(id, { tab } = {}) {
    this.close();
    const el = (this.el = document.createElement("div"));
    el.className = "modal-back rpage-back";
    el.innerHTML = `<div class="modal rpage" role="dialog" aria-label="Region"><div class="mh"><div><div class="h">REGION</div>
      <div class="sub dim">loading…</div></div><button class="x" title="Close (Esc)">✕</button></div><div class="rbody"></div></div>`;
    el.addEventListener("click", (e) => { if (e.target === el) this.close(); });
    el.querySelector(".x").onclick = () => this.close();
    this.root.appendChild(el);
    let data;
    try { data = await api(`/api/region/${encodeURIComponent(id)}`); }
    catch (err) { if (this.el === el) el.querySelector(".rbody").innerHTML = `<div class="rempty">This region's page isn't available yet (${esc(err.message)}).</div>`; return; }
    if (this.el !== el) return;
    this.data = data;
    const tabs = tabsWith(data);
    this.tab = tab && tabs.includes(tab) ? tab : tabs[0] || "gas";
    this.render();
  }

  render() {
    const d = this.data, el = this.el; if (!d || !el) return;
    if (/^#[0-9a-f]{6}$/i.test(d.color || "")) el.querySelector(".rpage").style.setProperty("--region", d.color);
    const members = (d.members || []).map((m) => String(m).split("-").pop()).join(" · ");
    el.querySelector(".mh .h").innerHTML = `<span class="rname">${esc(d.name || d.id)}</span>`;
    el.querySelector(".mh .sub").textContent = [members, d.as_of ? `as of ${fmtTime(Date.parse(d.as_of) / 1000)}` : ""].filter(Boolean).join(" · ");
    const have = tabsWith(d), t = d.tabs?.[this.tab] || {};
    const lines = signed(t.lines || [], d.box, this.tab), groups = t.groups || [];
    el.querySelector(".rbody").innerHTML = `
      <div class="rtabs" role="tablist">${TABS.map(([k, label]) => `<button role="tab" data-t="${k}" class="${k === this.tab ? "on" : ""}"
        ${have.includes(k) ? "" : "disabled title=\"Nothing for this region yet\""}>${label}</button>`).join("")}</div>
      <div class="rscroll">
        ${d.note ? `<div class="rnote">${esc(d.note)}</div>` : ""}
        ${tabEmpty(t) ? `<div class="rempty">Nothing for ${esc(TABS.find(([k]) => k === this.tab)?.[1].toLowerCase() || this.tab)} in ${esc(d.name || d.id)} yet${
          lines[0]?.why ? ` (${esc(lines[0].why)})` : ""}.</div>` : `<section class="rbal"><div class="rbalk">
          ${balNote(t, this.tab) ? `<div class="rthrough">${esc(balNote(t, this.tab))}</div>` : ""}
          <table class="rlines">${lines.map((l, i) => { const vt = valueText(l);
            return `<tr data-s="${esc(l.series || "")}" data-i="${i}" class="${l.series ? "cl" : ""}${vt.dim ? " dim" : ""}" title="${esc(vt.title)}">
              <td class="k">${esc(l.label || l.line)}${tagsHtml(l)}</td><td class="n">${vt.text}</td><td class="u">${esc(unitLabel(l.unit || ""))}</td></tr>`; }).join("")
            || `<tr><td class="dim">No balance lines for this tab yet.</td></tr>`}</table></div>
          <div class="rchartbox"><div class="rchead"><span class="rctitle"></span><span class="rranges">${RANGES.map(([l, r]) =>
            `<button data-r="${r}" class="${r === this.range ? "on" : ""}">${l}</button>`).join("")}</span></div><div class="rchart"></div></div>
        </section>`}
        ${groups.length ? `<section class="rgroups">${groups.map((g) => this.groupHtml(g, lines)).join("")}</section>`
          : lines.length && !tabEmpty(t) ? `<div class="rnogroups dim">The detail tables for this tab (by basin, sector, fuel …) are still being filled in.</div>` : ""}
        ${this.weatherHtml(d.weather)}
        <section class="rwatch"><div class="rh">What to watch</div>${(d.watch || []).filter((w) => String(w).trim()).length
          ? `<ul>${d.watch.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : `<div class="dim">Nothing listed yet.</div>`}</section>
      </div>`;
    el.querySelectorAll(".rtabs button").forEach((b) => (b.onclick = () => { this.tab = b.dataset.t; this.chartOf = null; this.render(); }));
    el.querySelectorAll(".rranges button").forEach((b) => (b.onclick = () => { this.range = b.dataset.r; this.render(); }));
    el.querySelectorAll(".rgnote").forEach((n) => (n.onclick = () => n.classList.toggle("open")));   // a long note unfolds
    // a click on any row with a series charts it; the tab's balance comes back with the title's ←
    el.querySelectorAll("tr.cl[data-s]").forEach((tr) => (tr.onclick = () => {
      const s = tr.dataset.s; if (!s) return;
      this.chartOf = { series: [s], labels: [tr.querySelector(".k")?.firstChild?.textContent || s] };
      this.chart();
    }));
    this.chart();
  }

  groupHtml(g, lines) {
    const rows = groupBars(g, lines);
    const unit = g.unit || rows.find((r) => r.unit)?.unit || "";
    // gross shares aren't of the line above (Bakken gas 262 MW is 14 % of the fuels, not of a 1,480 MW line): say so
    const basis = groupBasis(g, lines).mode === "gross" ? ` <span class="dim rgbasis">· % of ${esc(typeof g.gross === "string" ? g.gross : "rows' total")}</span>` : "";
    // "Other / not itemised": only against the line the group names (with no `of` the rows needn't add up to anything)
    const rest = showRest(g, lines) ? `<tr class="rest"><td class="k">Other / not itemised</td>
      <td class="bar"></td><td class="n">${fmt(g.rest)}</td></tr>` : "";
    return `<div class="rgroup"><div class="rgh">${esc(g.title || g.id)}${unit ? ` <span class="dim">· ${esc(unitLabel(unit))}</span>` : ""}${basis}</div>
      ${g.note ? `<div class="rgnote" title="${esc(g.note)}">${esc(g.note)}</div>` : ""}
      <table>${rows.map((r) => { const vt = valueText(r);
        if (r.fact) return `<tr class="fact"><td class="k">${esc(r.label)}<span class="rsrc">${esc([r.as_of, r.source].filter(Boolean).join(" · "))}</span></td>
          <td class="n" colspan="2">${esc(fmt(r.value))} <span class="fu">${esc(unitLabel(r.unit || ""))}</span></td></tr>`;
        const sub = /^of which\b/i.test(r.label || "") ? " sub" : "";   // a part of the row above ("of which power burn")
        return `<tr data-s="${esc(r.series || "")}" class="${r.series ? "cl" : ""}${vt.dim ? " dim" : ""}${sub}" title="${esc(vt.title)}">
          <td class="k">${esc(r.label)}${tagsHtml(r)}</td>
          <td class="bar${r.split ? " split" : ""}"><i class="${r.rel ? "rel" : ""}${r.split ? (r.neg ? " neg" : " pos") : ""}" style="width:${(r.bar * (r.split ? 50 : 100)).toFixed(1)}%"></i>${r.share != null ? `<span>${fmt(r.share * 100, r.share < 0.1 ? 1 : 0)} %</span>` : ""}</td>
          <td class="n">${vt.text}</td></tr>`; }).join("")}${rest}</table></div>`;
  }

  weatherHtml(w) {
    const vals = w?.values || {}, val = (sid) => (sid && vals[sid]) || null;
    const dd = ddRows(w);
    const cities = (w?.cities || []).filter((s) => val(s));
    if (!dd.length && !cities.length) return "";
    const cell = (sid) => { const vt = valueText(val(sid), 1); return `<td class="n${vt.dim ? " dim" : ""}" title="${esc(sid || "")}">${vt.text}</td>`; };
    return `<section class="rweather"><div class="rh">Weather</div>
      ${dd.length ? `<table class="rdd"><tr><th></th><th>today</th><th>normal</th><th>realized</th><th>days 1-7</th><th>days 8-15</th></tr>
        ${dd.map((r) => `<tr><td class="k">${r.label}</td>${cell(r.today)}${cell(r.normal)}${cell(r.realized)}${cell(r.d7)}${cell(r.d15)}</tr>`).join("")}</table>` : ""}
      ${cities.length ? `<div class="rwx">${cities.map((s) => { const vt = valueText(val(s), 1);
        return `<span class="rwv${vt.dim ? " dim" : ""}" title="${esc(s)}">${esc(cityName(s))} <b>${vt.text}</b> °C</span>`; }).join("")}</div>` : ""}</section>`;
  }

  async chart() {
    const el = this.el; if (!el) return;
    // the balance chart: the tab's box lines (the five gas lines, or generation); every other line is a click away
    const inBox = new Set((this.data.box || []).map((b) => b.line));
    let lines = signed(this.data.tabs?.[this.tab]?.lines || [], this.data.box, this.tab).filter((l) => l.series && l.have !== false);
    if (lines.some((l) => inBox.has(`${this.tab}.${l.line}`))) lines = lines.filter((l) => inBox.has(`${this.tab}.${l.line}`));
    const spec = this.chartOf || { series: lines.map((l) => l.series), labels: lines.map((l) => l.label || l.line), signs: lines.map((l) => l.sign ?? 1) };
    const box = el.querySelector(".rchart"), title = el.querySelector(".rctitle");
    if (title) title.innerHTML = this.chartOf ? `<button class="back" title="Back to the balance">←</button> ${esc(spec.labels[0])}` : "Balance";
    title?.querySelector(".back")?.addEventListener("click", () => { this.chartOf = null; this.chart(); });
    if (!box) return;
    if (!spec.series.length) { box.innerHTML = `<div class="dim">No history for this tab yet.</div>`; return; }
    let data;
    try { data = await api(`/api/series?ids=${encodeURIComponent(spec.series.slice(0, 50).join(","))}&start=${this.range}`); }
    catch (err) { box.innerHTML = `<div class="dim">history unavailable (${esc(err.message)})</div>`; return; }
    if (this.el !== el) return;
    const ids = spec.series.filter((s) => (data[s]?.ts || []).length);
    if (!ids.length) { box.innerHTML = `<div class="dim">No points in this range.</div>`; return; }
    const u0 = data[ids[0]]?.meta?.unit || "", same = ids.filter((s) => (data[s]?.meta?.unit || "") === u0);
    const xs = [...new Set(same.flatMap((s) => data[s].ts))].sort((a, b) => a - b);
    const cols = same.map((s) => { const k = spec.signs?.[spec.series.indexOf(s)] ?? 1, m = new Map(data[s].ts.map((t, j) => [t, data[s].v[j] == null ? null : k * data[s].v[j]]));
      return xs.map((t) => m.get(t) ?? null); });
    const daily = same.every((s) => /^(D|M|W)$/i.test(data[s]?.meta?.freq || ""));
    this.plot?.destroy(); box.innerHTML = "";
    this.plot = new uPlot({
      width: box.clientWidth || 480, height: 210, padding: [6, 6, 0, 0],
      tzDate: daily ? (ts) => { const d = new Date(ts * 1000); return new Date(d.getTime() + d.getTimezoneOffset() * 60000); } : wallDate,
      legend: { show: true, live: false }, cursor: { points: { size: 4 } }, scales: { x: { time: true } },
      axes: [{ stroke: "#6e8d7f", grid: { stroke: "rgba(29,50,39,.6)", width: 1 }, ticks: { show: false }, font: "10px JetBrains Mono", size: 22,
               values: (u, splits) => splits.map((s) => (daily ? fmtDay(s) : fmtTime(s, { date: true, time: false }))) },
             { stroke: "#6e8d7f", grid: { stroke: "rgba(29,50,39,.6)", width: 1 }, ticks: { show: false }, font: "10px JetBrains Mono", size: 52,
               values: (u, v) => v.map((x) => fmt(x)), label: unitLabel(u0), labelSize: 12, labelFont: "9px JetBrains Mono" }],
      series: [{}, ...same.map((s, i) => ({ label: spec.labels[spec.series.indexOf(s)] || s, stroke: COLORS[i % COLORS.length], width: 1.5,
        points: { show: false }, spanGaps: true }))],
    }, [xs, ...cols], box);
  }
}
