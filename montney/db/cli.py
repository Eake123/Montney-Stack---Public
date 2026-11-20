"""`montney db init | status | check`. Returns a process exit code (0 = ok)."""

from __future__ import annotations

from pathlib import Path

from montney.db import check, migrate


def main(action: str, args=None) -> int:
    if action == "import-duckdb":
        return import_duckdb(args)
    if action in ("backup", "restore-test", "backups"):
        return backup_main(action, args)
    if action == "check":
        results = check.run()
        for r in results:
            print(f"{'PASS' if r.ok else 'FAIL'}  {r.name:<11} {r.detail}")
        ok = bool(results) and all(r.ok for r in results)
        print("db check:", "PASS" if ok else "FAIL")
        return 0 if ok else 1

    from montney.db import connect

    try:
        conn = connect()
    except Exception as e:
        print(f"cannot connect: {type(e).__name__}: {e}")
        print("see docs/db/INSTALL.md (is the service running? is MONTNEY_DB_PASSWORD set?)")
        return 1
    try:
        if action == "init":
            if not pre_migrate_backup(conn, skip=bool(getattr(args, "no_backup", False))):
                return 1
            done = migrate.apply(conn)
            print("applied: " + ", ".join(done) if done else "nothing to apply: the schema is current")
        rows = migrate.status(conn)
        for r in rows:
            when = f"  {r['applied_at']:%Y-%m-%d %H:%M}Z" if r["applied_at"] else ""
            print(f"{r['version']:04d}  {r['name']:<24} {r['state']}{when}")
        return 0 if all(r["state"] == "applied" for r in rows) or action == "status" else 1
    except migrate.MigrationError as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()


def pre_migrate_backup(conn, skip: bool = False) -> bool:
    """Before applying anything to a database that already has migrations, dump it (INSTALL.md "Migrations":
    rolling back = restoring this). An empty database (first init) needs none. False = stop: no migration
    without its backup, unless --no-backup."""
    from montney.db import backup

    try:
        rows = migrate.status(conn)
    except migrate.MigrationError:
        return True                       # init reports this itself
    applied = any(r["state"] == "applied" for r in rows)
    pending = [r for r in rows if r["state"] == "pending"]
    if not (applied and pending):
        return True
    if skip:
        print("--no-backup: applying migrations WITHOUT a pre-migrate backup")
        return True
    print(f"{len(pending)} pending migration(s) on a database with data: backing up first ...")
    try:
        r = backup.dump(dbname=conn.info.dbname, reason="pre_migrate")
    except backup.BackupError as e:
        print(f"ERROR: pre-migrate backup failed, nothing applied: {e}")
        print("fix it, or run `montney db init --no-backup` to migrate without one")
        return False
    print(f"backup: {r.path} ({r.bytes / 1e6:.1f} MB)")
    return True


def backup_main(action: str, args=None) -> int:
    """`montney db backup | restore-test | backups` (docs/db/BACKUPS.md)."""
    from montney.db import backup

    if action == "backup":
        try:
            r = backup.dump(reason="manual")
        except backup.BackupError as e:
            print(f"backup FAILED: {e}")
            return 1
        print(f"backup: {r.path} ({r.bytes / 1e6:.1f} MB, {sum(r.counts.values()):,} rows, {r.seconds:.0f} s)")
        if r.pruned.get("deleted") or r.pruned.get("stale"):
            print(f"pruned: {', '.join(r.pruned['deleted'] + r.pruned['stale'])}")
        return 0
    if action == "restore-test":
        path = getattr(args, "dump", None)
        res = backup.restore_test(path=Path(path) if path else None, reason="manual")
        print(f"restore test of {res.path}")
        for name, ok, detail in res.checks:
            print(f"{'PASS' if ok else 'FAIL'}  {name:<12} {detail}")
        print("restore test:", "PASS" if res.ok else "FAIL", f"({res.seconds:.0f} s)")
        return 0 if res.ok else 1
    d = backup.backup_dir()
    items = backup.list_backups(d)
    print(f"{len(items)} complete backup(s) in {d}")
    for b in items:
        print(f"  {b.path.name:<44} {b.path.stat().st_size / 1e6:>10.1f} MB")
    try:
        from montney.db import connect

        conn = connect()
        try:
            for kind, r in backup.run_state(conn).items():
                print(f"{kind:<13} last ok {r['last_ok'] or 'never'}; last try {r['last_try'] or 'never'}"
                      + ("  (FAILED)" if r["last_failed"] else ""))
        finally:
            conn.close()
    except Exception as e:                       # noqa: BLE001 - listing files works without the database
        print(f"(run history unavailable: {type(e).__name__}: {e})")
    return 0


def du_main(save: bool = True) -> int:
    """`montney du`: compute usage, record a snapshot, update source budget states, print the report."""
    from montney import paths
    from montney.db import connect, du

    try:
        conn = connect()
    except Exception as e:
        print(f"cannot connect: {type(e).__name__}: {e}")
        return 1
    try:
        rep = du.compute(conn, home=paths.home())
        if save:
            du.save(conn, rep)
            du.apply_states(conn, rep)
        names = dict(conn.execute("SELECT id, code FROM montney.source").fetchall())
        print(du.format_report(rep, names))
        try:                      # L-081: measured upstream requests vs sources/BUDGET.yaml (a warning, never a block)
            from montney.db import reqbudget
            use = reqbudget.measure(conn)
            print()
            print(reqbudget.format_usage(use))
            for w in use.warnings():
                print(f"WARNING: {w}")
        except Exception as e:
            print(f"upstream requests: not measured ({type(e).__name__}: {e})")
        return 0
    finally:
        conn.close()


def import_duckdb(args) -> int:
    """`montney db import-duckdb`: DuckDB → Postgres, then verify (docs in montney/db/backfill.py)."""
    from pathlib import Path

    from montney import paths
    from montney.db import backfill, connect

    duck = Path(getattr(args, "duckdb", None) or paths.db_path())
    if not duck.exists():
        print(f"no DuckDB file at {duck}")
        return 1
    p = backfill.plan(duck)
    print(p.summary())
    if getattr(args, "dry_run", False):
        return 0
    conn = connect()
    try:
        res = backfill.run(conn, duck)
        print(f"read {res.read:,} rows; {res.after_vintage_dedupe:,} after dropping repeated vintages; "
              f"written {res.written:,}, already there {res.unchanged:,}, retries {res.retries}; "
              f"events {res.events}; fetch log rows {res.fetch_runs}; last-seen state synced for "
              f"{res.state_synced} series; rollups refreshed {res.refreshed}")
        problems = backfill.verify(conn, duck)
        print(f"verify vs DuckDB: {'PASS' if not problems else 'FAIL'}")
        for x in problems[:20]:
            print(f"  {x}")
        ok = not problems
        if getattr(args, "csv", None):
            n, cp = backfill.verify_csv(conn, Path(args.csv))
            print(f"verify vs CSV ({n:,} values): {'PASS' if not cp else 'FAIL'}")
            for x in cp[:20]:
                print(f"  {x}")
            ok = ok and not cp
        return 0 if ok else 1
    finally:
        conn.close()
