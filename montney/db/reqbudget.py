"""Upstream request budget per region, MEASURED (L-081, docs/ARCHITECTURE-REQUESTS.md §3).

`sources/BUDGET.yaml` sets one cap per region in requests/hour (regions as in the `region` table, sources mapped by
`source.region_id`, the same mapping as the storage budget). The runner records every run's HTTP requests and bytes
(`fetch_run.n_requests` / `bytes_in`, migration 0012). This module sums them over the last WINDOW_H hours per source
and per region, and WARNS when a region is over its cap, naming its top sources so the fix is obvious.

Only runs the system makes count against a cap: `trigger` schedule or refresh (migration 0013). Manual fetches and
importers (history back-fills, hand-runs after a merge) are reported separately, so they can't distort the caps. Runs
logged before 0013 (trigger NULL) count as "unknown", also reported apart.

It's a warning, never a block and never a test (L-055: no CI gate; Erik wants few tests): `montney du` prints it,
`montney db check` shows it as a WARN detail on a passing row. A window with under MIN_COVERAGE_H hours of data is
reported but not judged (one restart burst isn't a rate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

WINDOW_H = 24
COUNTED = ("schedule", "refresh")           # what the system does on its own; manual / import / unknown are reported
MIN_COVERAGE_H = 2.0
TOP_N = 3
BUDGET_FILE = Path(__file__).resolve().parents[2] / "sources" / "BUDGET.yaml"


@dataclass
class RegionUse:
    code: str
    req_per_h: float = 0.0
    mb_per_h: float = 0.0
    cap: float | None = None
    by_source: dict[str, tuple[float, float]] = field(default_factory=dict)   # code -> (req/h, MB/h), counted runs
    other: dict[str, float] = field(default_factory=dict)     # trigger (manual | import | unknown) -> req/h, not counted

    @property
    def over(self) -> bool:
        return self.cap is not None and self.req_per_h > self.cap

    def top(self, n: int = TOP_N) -> list[tuple[str, float, float]]:
        return sorted(((s, r, b) for s, (r, b) in self.by_source.items()), key=lambda x: -x[1])[:n]


@dataclass
class Usage:
    hours: float                      # hours of data actually covered (<= WINDOW_H)
    regions: dict[str, RegionUse]

    @property
    def judged(self) -> bool:
        return self.hours >= MIN_COVERAGE_H

    def warnings(self) -> list[str]:
        if not self.judged:
            return []
        return [f"upstream requests: {r.code} {r.req_per_h:.0f} req/h over its cap {r.cap:.0f} "
                f"(top: {', '.join(f'{s} {q:.0f}' for s, q, _ in r.top())}); see sources/BUDGET.yaml"
                for r in self.regions.values() if r.over]


def caps(path: Path = BUDGET_FILE) -> dict[str, float]:
    import yaml

    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): float(v) for k, v in (doc.get("requests_per_hour") or {}).items()}


def measure(conn, window_h: int = WINDOW_H, cap_by_region: dict[str, float] | None = None) -> Usage:
    """Requests/hour and MB/hour per source and region over the last `window_h` hours of measured runs."""
    cap_by_region = caps() if cap_by_region is None else cap_by_region
    has_trigger = conn.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
                               "'montney' AND table_name = 'fetch_run' AND column_name = 'trigger')").fetchone()[0]
    trig = "coalesce(f.trigger, 'unknown')" if has_trigger else "'unknown'"
    rows = conn.execute(f"""
        SELECT r.code, s.code, {trig}, sum(f.n_requests), sum(coalesce(f.bytes_in, 0)),
               extract(epoch FROM (now() AT TIME ZONE 'UTC') - min(f.started)) / 3600.0
        FROM montney.fetch_run f
        JOIN montney.source s ON s.id = f.source_id
        JOIN montney.region r ON r.id = s.region_id
        WHERE f.n_requests IS NOT NULL AND f.started > (now() AT TIME ZONE 'UTC') - make_interval(hours => %s)
        GROUP BY 1, 2, 3""", (window_h,)).fetchall()
    counted_rows = [r for r in rows if r[2] in COUNTED]
    span = max((float(r[5] or 0) for r in counted_rows), default=0.0)       # coverage: the counted runs' span
    hours = min(float(window_h), span)
    regions: dict[str, RegionUse] = {code: RegionUse(code, cap=cap) for code, cap in cap_by_region.items()}
    per = max(hours, 1e-9)
    for region, source, trigger, n, b, _ in rows:
        ru = regions.setdefault(region, RegionUse(region, cap=cap_by_region.get(region)))
        rq, mb = float(n or 0) / per, float(b or 0) / 1e6 / per
        if trigger not in COUNTED:
            ru.other[trigger] = ru.other.get(trigger, 0.0) + rq
            continue
        r0, b0 = ru.by_source.get(source, (0.0, 0.0))
        ru.by_source[source] = (r0 + rq, b0 + mb)
        ru.req_per_h += rq
        ru.mb_per_h += mb
    return Usage(hours=hours, regions=regions)


def format_usage(u: Usage) -> str:
    head = f"Upstream requests (measured, last {u.hours:.1f} h of fetch_run.n_requests"
    head += ")" if u.judged else f"; under {MIN_COVERAGE_H:.0f} h, not judged)"
    lines = [head]
    for r in sorted(u.regions.values(), key=lambda r: r.code):
        cap = f"cap {r.cap:.0f} req/h" if r.cap is not None else "no cap"
        state = "OVER" if r.over and u.judged else "ok"
        lines.append(f"  {r.code:<10} {r.req_per_h:8.1f} req/h  {r.mb_per_h:8.2f} MB/h  {cap:<16} {state}")
        for s, q, b in r.top():
            lines.append(f"      {s:<28} {q:7.1f} req/h  {b:7.2f} MB/h")
        if r.other:
            lines.append("      not counted: " + ", ".join(f"{t} {q:.1f} req/h" for t, q in sorted(r.other.items())))
    return "\n".join(lines)
