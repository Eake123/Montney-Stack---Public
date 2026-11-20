"""Memory and I/O settings for Erik's machine (L-040 gap b; docs/db/TUNING.md).

    uv run python -m montney.db.tune show      # current vs proposed, and which need a restart
    uv run python -m montney.db.tune bench     # time the workloads (N runs each); nothing written to the store
    uv run python -m montney.db.tune apply     # ALTER SYSTEM SET ... + pg_reload_conf() (restart for the rest)
    uv run python -m montney.db.tune verify    # exit 1 unless every proposed value is live
    uv run python -m montney.db.tune revert    # ALTER SYSTEM RESET for each (back to the conf file's values)

Settings go through ALTER SYSTEM (postgresql.auto.conf), never a postgresql.conf edit. That needs a superuser:
the `restore` role's credentials (POSTGRES_SUPERUSER_PASSWORD, L-042), on the `postgres` database only.
scripts/db/apply-tuning.ps1 is Erik's one-click launcher. It stops the runner, applies, restarts the service
(the one admin step), verifies, and starts the runner again.
"""

from __future__ import annotations

import statistics
import sys
import time

# Sized to the DATA, not the box (orchestrator, L-049): the DB is about 20 MB and the AB/NE BC budget is 5 GB, and the
# same 64 GB machine runs T3 and the agents (T3 alone spiked to 9 GB, L-047). shared_buffers is reserved up front.
# Revisit when the database passes about 1.5 GB (docs/db/TUNING.md). NVMe SSD (D:), 24 logical CPUs.
PROPOSED: dict[str, tuple[str, bool, str]] = {
    # name: (value, needs a restart, why)
    "shared_buffers": ("2GB", True, "room for the whole 5 GB budget's hot set; reserved up front, so sized to the data"),
    "effective_cache_size": ("16GB", False, "planner hint: a quarter of RAM as OS file cache, the rest is T3/agents"),
    "work_mem": ("32MB", False, "per sort/hash node; 100 connections x a few nodes stays far below RAM (default 4MB)"),
    "maintenance_work_mem": ("512MB", False, "index builds, restores, compression (default 64MB)"),
    "wal_buffers": ("16MB", True, "the usual cap for -1 with large shared_buffers (now 4MB)"),
    "max_wal_size": ("4GB", False, "fewer forced checkpoints under bulk writes such as an import or restore (1GB)"),
    "random_page_cost": ("1.1", False, "NVMe SSD: random reads cost about the same as sequential (default 4)"),
    "effective_io_concurrency": ("200", False, "NVMe SSD can serve many concurrent reads (PG 18 default 16)"),
    "max_parallel_workers_per_gather": ("4", False, "24 threads; up to 4 workers per query (default 2)"),
}
UNCHANGED = ("max_worker_processes", "timescaledb.max_background_workers", "max_parallel_workers", "max_connections")

# L-049: `apply` may ALTER only these settings (the superuser can change anything, so the code limits it).
ALLOWED_SETTINGS = frozenset({
    "shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem", "wal_buffers", "max_wal_size",
    "random_page_cost", "effective_io_concurrency", "max_parallel_workers_per_gather",
})


class NotAllowed(RuntimeError):
    pass


def _check_allowed(names) -> None:
    bad = sorted(set(names) - ALLOWED_SETTINGS)
    if bad:
        raise NotAllowed(f"tune may ALTER only {sorted(ALLOWED_SETTINGS)}; refusing {bad} (L-049)")


def _superuser():
    from montney.db import connect

    return connect(dbname="postgres", role="restore")


def current(conn) -> dict[str, dict]:
    names = list(PROPOSED) + list(UNCHANGED)
    rows = conn.execute("SELECT name, current_setting(name), pending_restart FROM pg_settings "
                        "WHERE name = ANY(%s)", (names,)).fetchall()
    return {n: {"value": v, "pending_restart": p} for n, v, p in rows}


def mismatches(cur: dict[str, dict]) -> list[str]:
    """Proposed values that aren't live (SHOW-formatted strings: '8GB', '32MB', '1.1', '200')."""
    return [f"{n}: live {cur.get(n, {}).get('value')!r}, proposed {v!r}"
            + (" (restart pending)" if cur.get(n, {}).get("pending_restart") else "")
            for n, (v, _r, _w) in PROPOSED.items() if cur.get(n, {}).get("value") != v]


def apply(conn) -> None:
    """ALTER SYSTEM SET each proposed setting, then reload. Checked against ALLOWED_SETTINGS FIRST, so nothing
    is altered if any name is outside it. Runs from Erik's click or by hand, never from the runner (L-049)."""
    _check_allowed(PROPOSED)
    for n, (v, _r, _w) in PROPOSED.items():
        conn.execute(f"ALTER SYSTEM SET {n} = '{v}'")                # names/values are constants above
    conn.execute("SELECT pg_reload_conf()")


def revert(conn) -> None:
    _check_allowed(PROPOSED)
    for n in PROPOSED:
        conn.execute(f"ALTER SYSTEM RESET {n}")
    conn.execute("SELECT pg_reload_conf()")


# ---------------------------------------------------------------- the benchmark (nothing written to the store)
def _time(fn, n: int) -> list[float]:
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


def bench(n: int = 5) -> dict[str, list[float]]:
    """Workloads over the real data, n runs each:
    * restore: the newest backup restored into a scratch montney_restore_* database (COPY, index builds,
      pre/post_restore) and dropped; backup._restore_checks, so no backup_run row is recorded;
    * history: PostgresStore.series() over every series, all history (the reader role);
    * daily_agg: a 1-day time_bucket aggregate over every observation (hash aggregate, sort);
    * catalog: PostgresStore.catalog() (the per-tier UNION with distinct counts)."""
    from montney.db import backup, connect
    from montney.db.pgstore import PostgresStore

    path = backup.latest()
    if path is None:
        raise SystemExit("no complete backup to restore (montney db backup first)")

    def restore():
        res = backup.RestoreResult(path)
        backup._restore_checks(res, connect, keep_scratch=False)
        if not res.ok:
            raise SystemExit(f"restore failed during the benchmark: {res.checks}")

    rd = connect(role="reader")
    try:
        ps = PostgresStore(rd)
        ids = [k for (k,) in rd.execute("SELECT key FROM montney.series").fetchall()]
        out = {
            "restore": _time(restore, n),
            "history": _time(lambda: ps.series(ids), n),
            "daily_agg": _time(lambda: rd.execute(
                "SELECT series_id, time_bucket('1 day', ts) d, avg(value), count(*) FROM montney.observation "
                "GROUP BY 1, 2 ORDER BY 1, 2").fetchall(), n),
            "catalog": _time(ps.catalog, n),
        }
    finally:
        rd.close()
    return out


def _fmt(results: dict[str, list[float]]) -> str:
    lines = [f"{'workload':<10} {'median':>9} {'min':>9} {'max':>9}   (s, n={len(next(iter(results.values())))})"]
    for k, v in results.items():
        lines.append(f"{k:<10} {statistics.median(v):>9.3f} {min(v):>9.3f} {max(v):>9.3f}")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "show"
    if cmd == "bench":
        n = int(argv[1]) if len(argv) > 1 else 5
        print(_fmt(bench(n)))
        return 0
    from montney.db import connect

    try:
        conn = _superuser() if cmd in ("apply", "revert") else connect(dbname="postgres")
    except Exception as e:                                       # noqa: BLE001 - say what to check, plainly
        print(f"could not connect ({type(e).__name__}: {e}).")
        print("Check: the service postgresql-x64-18 is running; for apply/revert, POSTGRES_SUPERUSER_PASSWORD is in "
              "THIS account's Credential Manager (`uv run montney secrets check POSTGRES_SUPERUSER_PASSWORD`).")
        return 2
    try:
        if cmd == "apply":
            apply(conn)
            print("applied by ALTER SYSTEM; reloaded. Restart the service for:",
                  ", ".join(n for n, (_v, r, _w) in PROPOSED.items() if r))
            return 0
        if cmd == "revert":
            revert(conn)
            print("reset by ALTER SYSTEM; reloaded. Restart the service to finish.")
            return 0
        cur = current(conn)
        if cmd == "verify":
            bad = mismatches(cur)
            for b in bad:
                print("NOT LIVE", b)
            print("verify:", "PASS" if not bad else "FAIL")
            return 0 if not bad else 1
        print(f"{'setting':<34} {'live':>8} {'proposed':>9}  restart  why")
        for n, (v, r, w) in PROPOSED.items():
            c = cur.get(n, {})
            print(f"{n:<34} {c.get('value', '?'):>8} {v:>9}  {'yes' if r else 'no':<7}  {w}"
                  + ("  [restart pending]" if c.get("pending_restart") else ""))
        for n in UNCHANGED:
            print(f"{n:<34} {cur.get(n, {}).get('value', '?'):>8} (unchanged)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
