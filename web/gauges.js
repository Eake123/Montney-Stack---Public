// River gauges (PLAN roadmap step 4, P5d): flow-vs-normal markers from /api/snapshot `gauges`
// (montney/connectors/weather_percentile.py gauge_states). No imports, so tests/test_web.py runs it under node.

/**
 * Percentile band colours, used ONLY when state is "ok". Dry browns to river blues, chosen not to collide with an
 * existing meaning (L-028 G2): not receipt jade #3dffb8, intertie purple #c9a2ff, storage violet #b392f0, or the
 * alert amber #ffb547 / critical red #ff4d6d. "normal" is the neutral house highlight.
 */
export const BAND_COLOR = {
  "very low": "#8c5a2b", low: "#a9a46c",   // low: cooler khaki (hue ~57°), clear of the alert amber's 36° (Reviewer, #149)
  normal: "#d3ece0", high: "#5aa0dc", "very high": "#1f5fae",
};
export const GAUGE_RING = "#5aa9e6";   // marks "river gauge" (outline), whatever the state
export const NEUTRAL = "#3b4742";      // no-result / stale fill (dark slate, so "filled" reads against the terrain): no percentile

/**
 * Marker encoding for one gauge entry {value, p, band, n, state, reason}:
 *   ok        → filled with its band colour
 *   seasonal  → hollow ring ("seasonal, no data"; never an error)
 *   stale     → neutral fill, dimmed (no real-time point in 3 h)
 *   no-result → neutral fill (data too thin for a percentile; the reason is in the panel)
 * Missing entry (no snapshot yet): hollow and dimmed. Returns {fill, fillOpacity, opacity}.
 */
export function gaugeStyle(g) {
  const st = g?.state;
  if (st === "ok" && BAND_COLOR[g.band]) return { fill: BAND_COLOR[g.band], fillOpacity: 1, opacity: 1 };
  if (st === "seasonal") return { fill: NEUTRAL, fillOpacity: 0, opacity: 1 };
  if (st === "stale") return { fill: NEUTRAL, fillOpacity: 1, opacity: 0.4 };
  if (st === "no-result") return { fill: NEUTRAL, fillOpacity: 1, opacity: 1 };
  return { fill: NEUTRAL, fillOpacity: 0, opacity: 0.4 };
}

/** The gauge's callout lines: discharge m³/s (with its % of normal, step 7, when Weather has one), "percentile NN (band)" and
 * n, or the state's plain-language reason. */
export function gaugeLines(g, pctNormal = null) {
  const q = (g?.value != null ? `${g.value >= 100 ? Math.round(g.value) : (+g.value).toFixed(1)} m³/s` : "-- m³/s") +
    (pctNormal != null && g?.value != null ? ` · ${Math.round(pctNormal)} % of normal` : "");
  // the rank among past flows for these dates, not a second "normal" figure next to the % of normal (L-067)
  if (g?.state === "ok") return [q, `percentile ${Math.round(g.p)} (${g.band})`, `n ${g.n}`];
  if (g?.state === "seasonal") return ["seasonal, no data", g.reason || ""].filter(Boolean);
  if (g?.state === "stale") return [q, "stale", g.reason || ""].filter(Boolean);
  if (g?.state === "no-result") return [q, "no result", g.reason || "", `n ${g.n ?? 0}`].filter(Boolean);
  return ["no data yet"];
}
