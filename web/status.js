// Header status: per-source dots (fetch health + storage budget) and the runner heartbeat badge (PLAN roadmap step 2).
// No imports, so tests/test_web.py runs it under node. map.js renders the HTML and escapes the text.

/** What a source's budget state means (server.py /api/status `budget`, set by the runner's du task). */
export const BUDGET_TEXT = {
  warn: "budget warning: its region is over 80 % of its storage budget",
  blocked: "budget full: writes are stopped for this source (region at 100 %)",
};

/**
 * A source's dot. Fetch health gives ok / err; the budget can raise it. Precedence: blocked (writes stopped, the worst:
 * nothing new is stored) > err (the last fetch failed) > warn (nearing the budget) > ok. Never fetched: no class.
 * Returns { cls, title }; `agoText(s)` formats the last fetch age (map.js).
 */
export function sourceDot(s, budget = {}, agoText = () => "") {
  const fetchCls = !s.last ? "" : s.last.ok === false ? "err" : "ok";
  const b = budget?.[s.id] || "ok";
  const cls = b === "blocked" ? "blocked" : fetchCls === "err" ? "err" : b === "warn" ? "warn" : fetchCls;
  const fetchText = s.last ? `${s.last.ok === false ? "error" : "ok"}, ${agoText(s)} ago` : "not run yet";
  const title = `${s.name}: ${fetchText}` + (BUDGET_TEXT[b] ? ` · ${BUDGET_TEXT[b]}` : "");
  return { cls, title };
}

/**
 * The runner badge (server.py runner_state: running ≤ 60 s since the last heartbeat, stale ≤ 5 min, else down).
 * Running: no badge (normal). Stale: "RUNNER BUSY" (likely a long du; data may lag). Down: "RUNNER DOWN" (nothing is
 * being fetched or stored). No `runner` in /api/status (DuckDB mode): no badge.
 */
export function runnerBadge(r) {
  if (!r || r.state === "running") return null;
  const age = r.age_s != null ? ` (last heartbeat ${Math.round(r.age_s)} s ago)` : "";
  if (r.state === "stale") return { cls: "stale", text: "RUNNER BUSY", title: `The runner is slow to report${age}; data may lag.` };
  return { cls: "down", text: "RUNNER DOWN",
    title: `The runner isn't running${age}${r.reason ? ` (${r.reason})` : ""}: no new data is fetched or stored.` };
}
