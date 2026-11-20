// Top producers popup (L-075, Erik: "when I click the ab prod or bc prod it should open ... a popup ... when I can click
// on the wells and see a bit of it"). An in-page modal: its own small map, fitted to the province's points, a sortable
// list, and a detail view (name, operator, rank, latest MMcf/d, monthly history). One code path for every screen size.
// Data: Gas's GET /api/top-producers?province=ab|bc, a runtime GeoJSON FeatureCollection, never committed:
//   {province, unit, as_of, basis, source, reason?, features: [{geometry: Point, properties: {id, rank, name, operator,
//    kind, latest, history: {t: ["YYYY-MM", ...], v: [...]}, ...optional fields shown as label/value}}]}
import * as ml from "./vendor/maplibre/maplibre-gl.mjs";
import { api, esc, fmt } from "./lib.js";
import { buildStyle } from "./style.js";
import { loadSprites } from "./sprites.js";

export const PROVINCES = { ab: "Alberta", bc: "British Columbia" };
const SHOWN = new Set(["id", "rank", "name", "operator", "kind", "latest", "history", "series"]);   // the rest: extra label/value rows
const COLS = [["rank", "#"], ["name", "Name"], ["operator", "Operator"], ["latest", "MMcf/d"]];
const monthSec = (t) => { const [y, m] = String(t).split("-").map(Number); return Date.UTC(y, (m || 1) - 1, 15) / 1000; };

/** Sort a copy of the features by a property; text compares case-blind, numbers numerically, missing values last. */
export function sortRows(features, key, dir = 1) {
  const val = (f) => f.properties?.[key];
  return [...features].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
    return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { sensitivity: "base" })) * dir;
  });
}

export class Producers {
  constructor(root = document.body) {
    this.root = root;
    addEventListener("keydown", (e) => { if (e.key === "Escape" && this.el) this.close(); });
  }

  /** `focus`: a feature id to select once loaded (e.g. picked in the search) */
  async open(prov, focus) {
    this.close();
    const name = PROVINCES[prov] || prov;
    const el = (this.el = document.createElement("div"));
    el.className = "modal-back";
    el.innerHTML = `<div class="modal" role="dialog" aria-label="Top producers, ${esc(name)}">
      <div class="mh"><div><div class="h">TOP PRODUCERS · ${esc(name.toUpperCase())}</div><div class="sub dim">loading…</div></div>
        <button class="x" title="Close (Esc)">✕</button></div>
      <div class="mb"><div class="pmap"></div>
        <div class="side"><div class="detail dim">Click a point or a row.</div>
          <table class="plist"><thead><tr>${COLS.map(([k, l]) => `<th data-k="${k}">${l}</th>`).join("")}</tr></thead><tbody></tbody></table>
          <div class="src dim"></div></div></div></div>`;
    el.addEventListener("click", (e) => { if (e.target === el) this.close(); });   // the backdrop closes it
    el.querySelector(".x").onclick = () => this.close();
    this.root.appendChild(el);
    let fc;
    try { fc = await api(`/api/top-producers?province=${encodeURIComponent(prov)}`); }
    catch (err) { if (this.el === el) this.empty(`Top producers aren't available yet (${err.message}).`); return; }
    if (this.el !== el) return;
    if (!fc?.features?.length) { this.empty(fc?.reason || "No producers to show."); return; }
    this.fc = fc; this.sort = { key: "rank", dir: 1 };
    // a facility without a location keeps its rank in the list but has no point (`unplaced`, Gas #287)
    el.querySelector(".sub").textContent = `${fc.features.length} largest · ${fc.unit || "MMcf/d"}${fc.as_of ? ` · ${fc.as_of}` : ""}` +
      (fc.unplaced ? ` · ${fc.unplaced} not on the map (no location)` : "");
    el.querySelector(".src").innerHTML = [fc.basis, fc.source].filter(Boolean).map((t) => `<div>${esc(t)}</div>`).join("");
    el.querySelectorAll("th").forEach((th) => (th.onclick = () => {
      const k = th.dataset.k;
      this.sort = { key: k, dir: this.sort.key === k ? -this.sort.dir : k === "latest" ? -1 : 1 };
      this.list();
    }));
    this.list();
    this.mapOf(fc);
    if (focus != null) {
      this.select(focus, false);
      const f = fc.features.find((x) => String(x.properties.id) === String(focus));
      if (f?.geometry?.coordinates) this.map?.once("load", () => this.map?.easeTo({ center: f.geometry.coordinates, zoom: 8, duration: 600 }));
    }
  }

  empty(msg) {
    this.el.querySelector(".mb").innerHTML = `<div class="detail">${esc(msg)}</div>`;
    this.el.querySelector(".sub").textContent = "";
  }

  list() {
    const { key, dir } = this.sort;
    this.el.querySelectorAll("th").forEach((th) => th.classList.toggle("on", th.dataset.k === key));
    const tb = this.el.querySelector(".plist tbody");
    tb.innerHTML = sortRows(this.fc.features, key, dir).map((f) => { const p = f.properties;
      return `<tr data-id="${esc(p.id)}" class="${p.id === this.sel ? "sel" : ""}"><td>${p.rank ?? ""}</td><td>${esc(p.name ?? p.id)}</td>
        <td>${esc(p.operator ?? "")}</td><td class="n">${p.latest == null ? "--" : fmt(p.latest, p.latest < 10 ? 1 : 0)}</td></tr>`; }).join("");
    tb.querySelectorAll("tr").forEach((tr) => (tr.onclick = () => this.select(tr.dataset.id, true)));
  }

  mapOf(fc) {
    const box = this.el.querySelector(".pmap"), placed = fc.features.filter((f) => f.geometry?.coordinates);
    if (!placed.length) { box.innerHTML = '<div class="detail dim">No locations to map.</div>'; return; }
    const xs = placed.map((f) => f.geometry.coordinates[0]), ys = placed.map((f) => f.geometry.coordinates[1]);
    const map = (this.map = new ml.Map({ container: box, style: buildStyle(), attributionControl: { compact: true },
      bounds: [[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], fitBoundsOptions: { padding: 30, maxZoom: 9 },
      dragRotate: false, pitchWithRotate: false, touchPitch: false }));
    map.touchZoomRotate.disableRotation();
    // the credits start folded to the ⓘ: expanded they cover a small map (and a tap on them would follow a link)
    const fold = () => box.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
    fold(); map.once("idle", fold);
    map.on("load", () => {
      const max = Math.max(...fc.features.map((f) => f.properties.latest || 0), 1);
      map.addSource("tp", { type: "geojson", data: { type: "FeatureCollection", features: placed } });
      // volume: a circle sized by latest MMcf/d (area ∝ volume), the gas orange; under Art's sprite once that loads
      const r = ["interpolate", ["linear"], ["sqrt", ["/", ["coalesce", ["get", "latest"], 0], max]], 0, 3, 1, 11];
      map.addLayer({ id: "tp", type: "circle", source: "tp", paint: { "circle-radius": r, "circle-color": "#ff6b1a",
        "circle-opacity": 0.85, "circle-stroke-color": "#020504", "circle-stroke-width": 1 } });
      // Art's well sprites (#284, docs/art/WELLS.md): a facility is a pad (an aggregation), a well a well; 16 px, 32 px
      // zoomed in; bottom-centre on the point, pixel-exact (device-fit loadSprites, icon-size 1). Without them: the circle.
      loadSprites(map, "/static/sprites/wells").then(() => {
        if (this.map !== map) return;
        map.setPaintProperty("tp", "circle-opacity", 0.35); map.setPaintProperty("tp", "circle-stroke-width", 0);
        const kind = ["case", ["==", ["get", "kind"], "facility"], "gas-pad", "gas-well"];
        map.addLayer({ id: "tp-icon", type: "symbol", source: "tp", layout: { "icon-image": ["step", ["zoom"],
          ["concat", kind, "-16-static"], 7.5, ["concat", kind, "-32-static"]], "icon-anchor": "bottom", "icon-size": 1,
          "icon-allow-overlap": true, "icon-ignore-placement": true, "symbol-sort-key": ["get", "rank"] } }, "tp-sel");
      }).catch(() => {});
      map.addLayer({ id: "tp-sel", type: "circle", source: "tp", filter: ["==", ["get", "id"], ""], paint: { "circle-radius": ["+", r, 4],
        "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#d3ece0", "circle-stroke-width": 2 } });
      map.addLayer({ id: "tp-rank", type: "symbol", source: "tp", filter: ["<=", ["get", "rank"], 10],
        layout: { "text-field": ["to-string", ["get", "rank"]], "text-font": ["Noto Sans Regular"], "text-size": 9, "text-offset": [0, -2.4] },
        paint: { "text-color": "#e2efe8", "text-halo-color": "rgba(2,5,4,0.9)", "text-halo-width": 1.2 } });
      const slop = matchMedia("(pointer: coarse)").matches ? 12 : 4;   // a finger is wider than a cursor
      map.on("click", (e) => {
        const p = e.point, hit = map.queryRenderedFeatures([[p.x - slop, p.y - slop], [p.x + slop, p.y + slop]],
          { layers: ["tp", "tp-icon"].filter((l) => map.getLayer(l)) });
        // the nearest point to the tap wins (dense at province scale); ties go to the better rank
        const d = (f) => { const q = map.project(f.geometry.coordinates); return Math.hypot(q.x - p.x, q.y - p.y); };
        if (hit.length) this.select(hit.sort((a, b) => d(a) - d(b) || (a.properties.rank ?? 1e9) - (b.properties.rank ?? 1e9))[0].properties.id, false);
      });
      for (const l of ["tp", "tp-icon"]) {
        map.on("mousemove", l, () => (map.getCanvas().style.cursor = "pointer"));
        map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
      }
    });
  }

  /** "A bit of it": name, operator, rank, latest, the optional fields, and its monthly history. */
  select(id, fromList) {
    const f = this.fc.features.find((x) => String(x.properties.id) === String(id));
    if (!f) return;
    this.sel = f.properties.id;
    const p = f.properties, unit = this.fc.unit || "MMcf/d";
    this.el.querySelectorAll(".plist tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === String(this.sel)));
    if (!fromList) this.el.querySelector(`.plist tr[data-id="${CSS.escape(String(this.sel))}"]`)?.scrollIntoView({ block: "nearest" });
    if (this.map?.getLayer("tp-sel")) this.map.setFilter("tp-sel", ["==", ["get", "id"], p.id]);
    if (fromList && f.geometry?.coordinates) this.map?.easeTo({ center: f.geometry.coordinates, duration: 400 });
    const extra = Object.entries(p).filter(([k, v]) => !SHOWN.has(k) && v != null && typeof v !== "object");
    const det = this.el.querySelector(".detail");
    det.classList.remove("dim");
    det.innerHTML = `<div class="name">${esc(p.name ?? p.id)}</div>
      <div class="dim">${[p.kind, p.operator].filter(Boolean).map(esc).join(" · ")}</div>
      <div><span class="big">${p.latest == null ? "--" : fmt(p.latest, p.latest < 10 ? 1 : 0)}</span> <span class="u">${esc(unit)}</span>
        <span class="dim">· rank ${p.rank ?? "?"} of ${this.fc.features.length}${this.fc.as_of ? ` · ${esc(this.fc.as_of)}` : ""}</span></div>
      <div class="hist"></div>
      <table>${[["ID", p.id], ...extra].map(([k, v]) => `<tr><td>${esc(k.replace(/_/g, " "))}</td><td>${esc(v)}</td></tr>`).join("")}</table>`;
    this.chart(det.querySelector(".hist"), p.history, unit);
  }

  chart(box, h, unit) {
    this.plot?.destroy(); this.plot = null;
    const t = (h?.t || []).map(monthSec), v = (h?.v || []).map((x) => (x == null ? null : +x));
    if (!t.length || !globalThis.uPlot) { box.textContent = "no history"; return; }
    this.plot = new globalThis.uPlot({ width: Math.max(220, box.clientWidth || 300), height: 110, legend: { show: false }, cursor: { y: false },
      scales: { x: { time: true } }, axes: [{ stroke: "#6e8d7f", grid: { stroke: "#142219" }, ticks: { stroke: "#142219" }, font: "9px monospace" },
        { stroke: "#6e8d7f", grid: { stroke: "#142219" }, ticks: { stroke: "#142219" }, font: "9px monospace", size: 42 }],
      series: [{}, { label: unit, stroke: "#ff6b1a", width: 1.5, spanGaps: false, points: { size: 3 } }] }, [t, v], box);
  }

  close() {
    this.plot?.destroy(); this.plot = null;
    this.map?.remove(); this.map = null;
    this.el?.remove(); this.el = null; this.fc = null; this.sel = null;
  }
}
