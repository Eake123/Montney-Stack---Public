// Search (L-076, Erik: "a search bar that searches for all the nodes ... so I can search for wells on it. Ensure it's
// optimized for phone use"). One box for everything with a name: map.js hands over the items (network features, gauges,
// rivers, map.json boxes, top producers), each {label, sub, group, icon, keys: [text...], pick()}; nothing is listed here.
// Matching is prefix / word-prefix / substring / in-order fuzzy on the label, a little weaker on the other keys (id,
// operator). Desktop: an input in the top bar, ↑↓ Enter Esc. Phone: a 🔍 button that opens a full-width bar (16 px
// input, so the browser doesn't zoom; 44 px result rows; the keyboard's Search key picks the first result; ✕ or Back closes).
import { esc } from "./lib.js";

export const LIMIT = 20;

/** Lower-case, accents and punctuation gone, single spaces: "Kakwa 5-26 (Pembina)" → "kakwa 5 26 pembina". */
export function norm(s) {
  return String(s ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/** How well a normalized query matches a normalized text: 0 (no match) to 100 (the same). */
export function score(q, t) {
  if (!q || !t) return 0;
  if (t === q) return 100;
  if (t.startsWith(q)) return 90;
  if ((" " + t).includes(" " + q)) return 80;                            // a word starts with it
  const words = q.split(" ");
  if (words.length > 1 && words.every((w) => (" " + t).includes(" " + w))) return 70;   // every word, any order
  if (t.includes(q)) return 60;
  let i = 0;                                                            // the letters in order ("kkwa" → "kakwa")
  for (const ch of t) if (ch === q[i]) i++;
  return i === q.length && q.length >= 3 ? Math.max(5, 30 - t.length / 8) : 0;
}

/** The best ≤ LIMIT items for a query, best first; ties go to the shorter label. */
export function find(items, query, limit = LIMIT) {
  const q = norm(query);
  if (!q) return [];
  const hits = [];
  for (const it of items) {
    const n = it._n ??= norm(it.label), alt = it._alt ??= (it.keys || []).map(norm).filter(Boolean);
    const s = Math.max(score(q, n), ...alt.map((a) => score(q, a) * 0.85));
    if (s > 0) hits.push([s, it]);
  }
  return hits.sort((a, b) => b[0] - a[0] || a[1].label.length - b[1].label.length).slice(0, limit).map(([, it]) => it);
}

/** Results grouped by type, groups in the order of their best hit. */
export function grouped(hits) {
  const g = new Map();
  for (const it of hits) { if (!g.has(it.group)) g.set(it.group, []); g.get(it.group).push(it); }
  return [...g.entries()];
}

export class Search {
  /** @param ctx {items: () => Promise<item[]>} @param el the .search element (index.html) */
  constructor(el, ctx) {
    Object.assign(this, { el, ctx, hits: [], cur: -1, items: null });
    this.input = el.querySelector("input");
    this.res = el.querySelector(".sres");
    el.querySelector(".sbtn").onclick = () => this.openBar();
    el.querySelector(".sx").onclick = () => this.closeBar();
    el.querySelector("form").onsubmit = (e) => { e.preventDefault(); this.pick(this.hits[Math.max(0, this.cur)]); };
    this.input.addEventListener("focus", () => this.load(true));   // rebuilt on focus: new features are found; producers stay cached
    this.input.addEventListener("input", () => this.run());
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); this.move(e.key === "ArrowDown" ? 1 : -1); }
      else if (e.key === "Enter") { e.preventDefault(); this.pick(this.hits[Math.max(0, this.cur)]); }   // also the phone's Search key
      else if (e.key === "Escape") { if (this.input.value) { this.input.value = ""; this.run(); } else this.closeBar(); }
    });
    addEventListener("popstate", () => { if (this.el.classList.contains("open")) this.closeBar(true); });   // phone Back
    document.addEventListener("pointerdown", (e) => { if (!this.el.contains(e.target)) this.res.hidden = true; });
  }

  /** The index, built on first use and again when asked (new snapshot / network). */
  async load(force = false) {
    if (this.items && !force) return this.items;
    this.items = await this.ctx.items();
    if (this.input.value) this.run();
    return this.items;
  }

  async run() {
    const items = this.items || (await this.load());
    this.hits = find(items, this.input.value);
    this.cur = this.hits.length ? 0 : -1;
    this.render();
  }

  render() {
    if (!this.input.value.trim()) { this.res.hidden = true; return; }
    let k = -1;
    this.res.innerHTML = this.hits.length ? grouped(this.hits).map(([g, list]) => `<div class="sg">${esc(g)}</div>` + list.map((it) => {
      k = this.hits.indexOf(it);
      return `<button type="button" class="sr${k === this.cur ? " on" : ""}" data-k="${k}">${it.icon || ""}<span class="sl">${esc(it.label)}</span>` +
        `${it.sub ? `<span class="ss">${esc(it.sub)}</span>` : ""}</button>`;
    }).join("")).join("") : `<div class="sg">No match</div>`;
    this.res.hidden = false;
    this.res.querySelectorAll(".sr").forEach((b) => (b.onclick = () => this.pick(this.hits[+b.dataset.k])));
  }

  move(d) {
    if (!this.hits.length) return;
    this.cur = (this.cur + d + this.hits.length) % this.hits.length;
    this.render();
    this.res.querySelector(".sr.on")?.scrollIntoView({ block: "nearest" });
  }

  pick(it) {
    if (!it) return;
    this.res.hidden = true;
    this.input.blur();
    if (this.el.classList.contains("open")) this.closeBar();
    it.pick();
  }

  openBar() {
    this.el.classList.add("open");
    history.pushState({ search: 1 }, "");   // Back closes the bar instead of leaving the page
    this.input.focus();
  }

  closeBar(fromBack = false) {
    if (!this.el.classList.contains("open")) { this.input.value = ""; this.res.hidden = true; return; }
    this.el.classList.remove("open");
    this.res.hidden = true;
    this.input.blur();
    if (!fromBack && history.state?.search) history.back();
  }
}
