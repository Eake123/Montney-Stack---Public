"""`montney du`: storage per source and region, and the budget state (docs/db/RETENTION.md "Budget").

Budgets (L-003, 10^9 bytes): ab_nebc 5 GB, rest 95 GB (table `region`).

* Bytes per chunk: the row-store part (`pg_total_relation_size`) plus the columnstore part
  (`chunk_columnstore_stats`). Timescale reports sizes per chunk, not per series, so each chunk's bytes are
  split by its rows per series (raw_blob: per source). The split is cached in `usage_chunk` and recounted
  only when the chunk's size changes.
* Plain tables split the same way (series_state, forecast_latest, obs_flag by series; fetch_run, event +
  event_scope by source). Catalogue tables, indexes on empty roots and unattributable bytes are `_system`:
  counted in the 100 GB total, not in a region.
* Files in the data dir: weather grids and the HTTP cache count toward ab_nebc (L-029). The legacy DuckDB
  file is shown but not counted (it goes away with the port).
* Backups (G:, MONTNEY_BACKUP_DIR) are reported, not counted, with a warning above 25 GB (L-029).
* State per region: warn at warn_pct (80 %), blocked at 100 %, and once blocked it stays blocked until usage
  is back under 95 % (hysteresis, so it doesn't flap). `apply_states()` writes it to `source.budget_state`
  for every source of the region; the runner stops writing for `blocked` sources (not the app).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERIES_HYPERTABLES = ("obs_fast", "obs_hourly", "obs_daily", "obs_forecast")
ROLLUPS = ("obs_fast_1h", "obs_fast_1d", "obs_hourly_1d")
SOURCE_HYPERTABLES = ("raw_blob",)
SERIES_PLAIN = ("series_state", "forecast_latest", "obs_flag")
SOURCE_PLAIN = ("fetch_run",)
UNBLOCK_PCT = 95.0
TOTAL_BUDGET = 100 * 10**9      # L-003: the 100 GB total, regions + _system (reviewer, #61)
TOTAL_WARN_PCT = 80
_STATE_RANK = {"ok": 0, "warn": 1, "blocked": 2}
BACKUP_WARN_BYTES = 25 * 10**9
DEFAULT_BACKUP_DIR = r"G:\My Drive\MontneyStack\backups"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Item:
    region_id: int | None
    source_id: int | None
    item: str
    rows: int
    bytes: int


@dataclass
class Region:
    id: int
    code: str
    budget: int
    warn_pct: int
    used: int = 0
    pct: float = 0.0
    state: str = "ok"
    growth_per_day: float | None = None
    days_to_full: float | None = None


@dataclass
class Report:
    taken_at: datetime
    regions: list[Region]
    items: list[Item]
    system_bytes: int
    files: dict = field(default_factory=dict)
    backups: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    total_used: int = 0
    total_pct: float = 0.0
    total_state: str = "ok"


def worst(*states: str) -> str:
    return max(states, key=lambda x: _STATE_RANK[x])


def next_state(prev: str, pct: float, warn_pct: float) -> str:
    """Budget state with hysteresis: blocked at 100 %, and it stays blocked until usage < UNBLOCK_PCT."""
    if pct >= 100.0:
        return "blocked"
    if prev == "blocked" and pct >= UNBLOCK_PCT:
        return "blocked"
    if pct >= warn_pct:
        return "warn"
    return "ok"


# ---------------------------------------------------------------- sizes
def _chunks(conn, schema: str, table: str) -> list[tuple[str, int]]:
    """(chunk name, bytes incl. columnstore) for every chunk of a hypertable."""
    stats = {}
    try:
        for s, n, after in conn.execute(
                "SELECT chunk_schema, chunk_name, after_compression_total_bytes "
                "FROM chunk_columnstore_stats(%s::regclass)", (f"{schema}.{table}",)).fetchall():
            stats[f"{s}.{n}"] = after or 0
    except Exception:  # noqa: BLE001 - no columnstore on this table: row store only
        pass
    out = []
    for s, n in conn.execute("SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                             "WHERE hypertable_schema = %s AND hypertable_name = %s", (schema, table)).fetchall():
        name = f"{s}.{n}"
        row = conn.execute("SELECT pg_total_relation_size(to_regclass(%s))", (f'"{s}"."{n}"',)).fetchone()
        if row[0] is None:          # dropped by retention since the listing
            continue
        out.append((name, int(row[0]) + int(stats.get(name, 0))))
    return out


def _split_chunk(conn, chunk: str, owner_col: str, size: int, kind: str, tbl: str) -> dict[int, int]:
    """Rows per owner in one chunk, from the usage_chunk cache when the chunk's size hasn't changed."""
    cached = conn.execute("SELECT owner_id, rows, chunk_bytes FROM montney.usage_chunk WHERE chunk = %s AND owner_kind = %s",
                          (chunk, kind)).fetchall()
    if cached and all(c[2] == size for c in cached):
        return {c[0]: c[1] for c in cached}
    s, n = chunk.split(".", 1)
    try:
        rows = conn.execute(f'SELECT {owner_col}, count(*) FROM "{s}"."{n}" GROUP BY 1').fetchall()
    except Exception:  # noqa: BLE001 - dropped meanwhile
        return {}
    now = _utcnow()
    with conn.transaction():
        conn.execute("DELETE FROM montney.usage_chunk WHERE chunk = %s AND owner_kind = %s", (chunk, kind))
        if rows:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO montney.usage_chunk (chunk, tbl, owner_kind, owner_id, rows, chunk_bytes, "
                                "computed_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                [(chunk, tbl, kind, o, r, size, now) for o, r in rows])
    return {o: r for o, r in rows}


def _apportion(total_bytes: int, rows: dict[int, int]) -> dict[int, tuple[int, int]]:
    """Split bytes by row share; the integer remainder goes to the largest owner so nothing is lost."""
    n = sum(rows.values())
    if n == 0 or total_bytes == 0:
        return {}
    out = {o: (r, total_bytes * r // n) for o, r in rows.items()}
    rest = total_bytes - sum(b for _, b in out.values())
    if rest:
        big = max(out, key=lambda o: out[o][0])
        out[big] = (out[big][0], out[big][1] + rest)
    return out


def _dir_bytes(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def compute(conn, home: Path | None = None, backup_dir: Path | None = None) -> Report:
    now = _utcnow()
    series = {sid: (src, reg) for sid, src, reg in
              conn.execute("SELECT id, source_id, region_id FROM montney.series").fetchall()}
    sources = {sid: reg for sid, reg in conn.execute("SELECT id, region_id FROM montney.source").fetchall()}
    regions = [Region(*r) for r in conn.execute(
        "SELECT id, code, budget_bytes, warn_pct FROM montney.region ORDER BY id").fetchall()]
    acc: dict[tuple, list[int]] = {}
    system = 0

    def add(region_id, source_id, item, rows, nbytes):
        k = (region_id, source_id, item)
        a = acc.setdefault(k, [0, 0])
        a[0] += rows
        a[1] += nbytes

    def add_split(item, total, rows_by_owner, kind):
        nonlocal system
        parts = _apportion(total, rows_by_owner)
        if not parts:
            system += total
            return
        for owner, (r, b) in parts.items():
            if kind == "series":
                src, reg = series.get(owner, (None, None))
            else:
                src, reg = owner, sources.get(owner)
            if reg is None:
                system += b                       # orphan: shows up as _system, `db check` names it
            else:
                add(reg, src, item, r, b)

    # hypertables and rollups
    mat = {v: (s, t) for v, s, t in conn.execute(
        "SELECT view_name, materialization_hypertable_schema, materialization_hypertable_name "
        "FROM timescaledb_information.continuous_aggregates WHERE view_schema = 'montney'").fetchall()}
    targets = [(t, "montney", t, "series_id", "series") for t in SERIES_HYPERTABLES]
    targets += [(v, *mat[v], "series_id", "series") for v in ROLLUPS if v in mat]
    targets += [(t, "montney", t, "source_id", "source") for t in SOURCE_HYPERTABLES]
    for item, schema, table, col, kind in targets:
        root = conn.execute("SELECT pg_total_relation_size(to_regclass(%s))", (f"{schema}.{table}",)).fetchone()[0] or 0
        system += int(root)                                   # the (empty) root table and its indexes
        for chunk, size in _chunks(conn, schema, table):
            add_split(item, size, _split_chunk(conn, chunk, col, size, kind, item), kind)

    # plain tables
    for t, col, kind in [(t, "series_id", "series") for t in SERIES_PLAIN] + \
                        [(t, "source_id", "source") for t in SOURCE_PLAIN]:
        size = conn.execute("SELECT pg_total_relation_size(%s::regclass)", (f"montney.{t}",)).fetchone()[0]
        rows = dict(conn.execute(f"SELECT {col}, count(*) FROM montney.{t} GROUP BY 1").fetchall())
        add_split(t, int(size), rows, kind)
    ev_size = sum(conn.execute("SELECT pg_total_relation_size(%s::regclass)", (f"montney.{t}",)).fetchone()[0]
                  for t in ("event", "event_scope"))
    add_split("events", int(ev_size), dict(conn.execute(
        "SELECT source_id, count(*) FROM montney.event GROUP BY 1").fetchall()), "source")

    # everything else in the schema: catalogue, entities, usage tables
    counted = set(SERIES_HYPERTABLES + SOURCE_HYPERTABLES + SERIES_PLAIN + SOURCE_PLAIN + ("event", "event_scope"))
    for (t,) in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'montney'").fetchall():
        if t not in counted:
            system += int(conn.execute("SELECT pg_total_relation_size(%s::regclass)", (f"montney.{t}",)).fetchone()[0])

    # files in the data dir (count toward ab_nebc, L-029) and backups (reported only)
    files = {}
    if home is not None:
        ab = next((r.id for r in regions if r.code == "ab_nebc"), None)
        for name in ("grids", "cache"):
            b = _dir_bytes(home / name)
            files[f"files:{name}"] = b
            if b:
                add(ab, None, f"files:{name}", 0, b)
        duck = home / "montney.duckdb"
        files["legacy duckdb (not counted)"] = duck.stat().st_size if duck.exists() else 0
    bdir = backup_dir or Path(os.environ.get("MONTNEY_BACKUP_DIR", DEFAULT_BACKUP_DIR))
    backups = {"dir": str(bdir), "bytes": _dir_bytes(bdir), "reachable": bdir.exists(), **_backup_runs(conn)}

    items = [Item(k[0], k[1], k[2], v[0], v[1]) for k, v in sorted(acc.items(), key=lambda kv: -kv[1][1])]
    prev_state = dict(conn.execute(
        "SELECT region_id, max(budget_state) FILTER (WHERE budget_state = 'blocked') FROM montney.source "
        "GROUP BY region_id").fetchall())
    warnings = []
    for r in regions:
        r.used = sum(i.bytes for i in items if i.region_id == r.id)
        r.pct = 100.0 * r.used / r.budget
        r.state = next_state(prev_state.get(r.id) or "ok", r.pct, r.warn_pct)
        old = conn.execute(
            "SELECT taken_at, sum(bytes) FROM montney.usage_snapshot WHERE region_id = %s AND taken_at = "
            "(SELECT max(taken_at) FROM montney.usage_snapshot WHERE taken_at <= %s) GROUP BY taken_at",
            (r.id, now - timedelta(days=7))).fetchone()
        if old:
            days = max((now - old[0]).total_seconds() / 86400, 1e-9)
            r.growth_per_day = (r.used - int(old[1])) / days
            if r.growth_per_day > 0:
                r.days_to_full = (r.budget - r.used) / r.growth_per_day
        if r.state != "ok":
            warnings.append(f"region {r.code} at {r.pct:.1f} % of {r.budget / 1e9:.2f} GB: {r.state}")
    # The 100 GB TOTAL (regions + _system): a guard on its own, so a full total stops writes even when no
    # single region is over its share. Hysteresis on the TOTAL's own previous state (montney.budget_guard,
    # 0009), never on whether some region happens to be blocked (reviewer, #85).
    total = sum(r.used for r in regions) + system
    row = conn.execute("SELECT state FROM montney.budget_guard WHERE scope = 'total'").fetchone()
    total_pct = 100.0 * total / TOTAL_BUDGET
    total_state = next_state(row[0] if row else "ok", total_pct, TOTAL_WARN_PCT)
    if total_state != "ok":
        warnings.append(f"TOTAL at {total_pct:.1f} % of {TOTAL_BUDGET / 1e9:.0f} GB: {total_state}")
    if backups.get("recorded") and (backups["last_dump"] is None
                                    or now - backups["last_dump"] > timedelta(hours=BACKUP_STALE_HOURS)):
        warnings.append(f"no good backup in the last {BACKUP_STALE_HOURS} h (last: {backups['last_dump'] or 'never'}); "
                        "see `montney db backups` and docs/db/BACKUPS.md")
    if backups["bytes"] > BACKUP_WARN_BYTES:
        warnings.append(f"backups use {backups['bytes'] / 1e9:.1f} GB (> {BACKUP_WARN_BYTES / 1e9:.0f} GB) in {bdir}")
    return Report(now, regions, items, system, files, backups, warnings, total, total_pct, total_state)


def save(conn, rep: Report) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO montney.usage_snapshot (taken_at, region_id, source_id, item, rows, bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [(rep.taken_at, i.region_id, i.source_id, i.item, i.rows, i.bytes) for i in rep.items]
            + [(rep.taken_at, None, None, "_system", 0, rep.system_bytes)])


def apply_states(conn, rep: Report) -> dict[str, str]:
    """Write each region's state, made no better than the TOTAL's, to every source of that region.
    Returns {source code: state}."""
    out = {}
    with conn.transaction():
        conn.execute("INSERT INTO montney.budget_guard (scope, state, pct, updated) VALUES ('total', %s, %s, %s) "
                     "ON CONFLICT (scope) DO UPDATE SET state = excluded.state, pct = excluded.pct, "
                     "updated = excluded.updated", (rep.total_state, rep.total_pct, rep.taken_at))
        want = {r.id: worst(r.state, rep.total_state) for r in rep.regions}
        # Lock the source rows in ONE fixed order (id) and rewrite only the ones whose state changes: du runs on
        # its own connection, so this transaction must never hold rows in an order the runner's connection could
        # cross (reviewer, #91), nor rewrite rows for nothing.
        rows = conn.execute("SELECT id, code, region_id, budget_state FROM montney.source ORDER BY id "
                            "FOR NO KEY UPDATE").fetchall()
        changed = [(want[reg], sid) for sid, code, reg, cur in rows if reg in want and cur != want[reg]]
        for sid, code, reg, cur in rows:
            if reg in want:
                out[code] = want[reg]
        if changed:
            with conn.cursor() as c:
                c.executemany("UPDATE montney.source SET budget_state = %s WHERE id = %s", changed)
    return out


def blocked_sources(conn) -> set[str]:
    """Sources the runner must not write for (it still fetches, so status stays live)."""
    return {c for (c,) in conn.execute("SELECT code FROM montney.source WHERE budget_state = 'blocked'").fetchall()}


BACKUP_STALE_HOURS = 36


def _backup_runs(conn) -> dict:
    """The last good dump and restore test from montney.backup_run (migration 0010), if it exists yet."""
    if conn.execute("SELECT to_regclass('montney.backup_run')").fetchone()[0] is None:
        return {"recorded": False, "last_dump": None, "last_restore_test": None}
    d, t = conn.execute("SELECT max(started) FILTER (WHERE kind = 'dump' AND ok), "
                        "max(started) FILTER (WHERE kind = 'restore_test' AND ok) FROM montney.backup_run").fetchone()
    return {"recorded": True, "last_dump": d, "last_restore_test": t}


def format_report(rep: Report, sources: dict[int, str]) -> str:
    gb = lambda b: f"{b / 1e9:.2f} GB" if b >= 1e8 else f"{b / 1e6:.1f} MB"  # noqa: E731
    lines = [f"{'region':<9} {'used':>10} {'budget':>9} {'%':>6}  state    7d growth    full in"]
    for r in rep.regions:
        growth = f"{r.growth_per_day / 1e6:+.1f} MB/d" if r.growth_per_day is not None else "-"
        full = f"{r.days_to_full:.0f} d" if r.days_to_full is not None else "-"
        lines.append(f"{r.code:<9} {gb(r.used):>10} {gb(r.budget):>9} {r.pct:>6.1f}  {r.state:<8} {growth:>10} {full:>9}")
    lines.append(f"{'_system':<9} {gb(rep.system_bytes):>10}")
    lines.append(f"{'TOTAL':<9} {gb(rep.total_used):>10} {gb(TOTAL_BUDGET):>9} {rep.total_pct:>6.1f}  {rep.total_state}")
    lines.append("")
    lines.append(f"{'source':<26} {'region':<8} {'item':<16} {'rows':>12} {'bytes':>10}")
    reg = {r.id: r.code for r in rep.regions}
    for i in rep.items:
        lines.append(f"{sources.get(i.source_id, '-'):<26} {reg.get(i.region_id, '-'):<8} {i.item:<16} "
                     f"{i.rows:>12,} {gb(i.bytes):>10}")
    lines.append("")
    for k, v in rep.files.items():
        lines.append(f"{k:<30} {gb(v):>10}")
    b = rep.backups
    lines.append(f"{'backups (not counted)':<30} {gb(b['bytes']):>10}  {b['dir']}"
                 f"{'' if b['reachable'] else '  (not reachable)'}")
    if b.get("recorded"):
        lines.append(f"{'last good dump / restore test':<30} {b['last_dump'] or 'never'} / "
                     f"{b['last_restore_test'] or 'never'}")
    for w in rep.warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)
