"""Nightly backups and the monthly restore test (PLAN P3, docs/db/BACKUPS.md).

A backup is a compressed custom-format `pg_dump -Fc` of the montney database, written to
`<dir>/montney-<UTC stamp>[-<tag>].dump.partial` and moved to `.dump` only once it is complete and readable
(`pg_restore --list`). A manifest `<same name>.json` sits beside it: the sha256 and size of the dump, the
server and TimescaleDB versions, the last migration, and the row count of every montney table. All of it is
taken in the SAME snapshot pg_dump reads (`pg_export_snapshot` + `pg_dump --snapshot`), so a restore must
reproduce the counts exactly. A dump without its manifest is incomplete and is never restored or kept.

Directory: MONTNEY_BACKUP_DIR, default `G:\\My Drive\\MontneyStack\\backups` (du.DEFAULT_BACKUP_DIR).
Retention (prune): the newest backup of each of the 7 most recent days, 4 most recent ISO weeks and 12 most
recent months that have one (Alberta calendar), so at most 23 files; the newest is always kept.

The restore test (monthly in the runner; `montney db restore-test` by hand) FAILS on: a missing dump or
manifest, a size or sha256 mismatch, an unreadable archive, any pg_restore error, a different TimescaleDB
version or last migration, or any table whose row count differs. It restores into a scratch database with
TimescaleDB's `timescaledb_pre_restore()` / `timescaledb_post_restore()`, pauses the restored jobs before
anything can run, compares, and drops the scratch database.

Tools: pg_dump / pg_restore from MONTNEY_PG_BIN, else PATH, else `C:\\Program Files\\PostgreSQL\\18\\bin`.
They must be version 18 (the server's major version or newer).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from montney.db.conn import LiveDatabaseRefused

log = logging.getLogger("montney.backup")

ALBERTA = ZoneInfo("America/Edmonton")
KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 4, 12
KEEP_PRE_MIGRATE = 3        # pre-migrate dumps live OUTSIDE the 7/4/12 rotation: the newest 3 are kept
NIGHTLY_HOUR = 2                      # Alberta local hour from which the day's dump is due
RETRY_AFTER = timedelta(hours=1)      # after a failed attempt, the runner waits this long before trying again
STALE_PARTIAL = timedelta(hours=24)   # a .partial or an unpaired file this old is a crashed run: deleted
NAME_RE = re.compile(r"^montney-(\d{8}T\d{6}Z)(?:-([a-z_]+))?\.dump$")
STAMP = "%Y%m%dT%H%M%SZ"


class BackupError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def backup_dir() -> Path:
    from montney.db.du import DEFAULT_BACKUP_DIR

    return Path(os.environ.get("MONTNEY_BACKUP_DIR", DEFAULT_BACKUP_DIR))


def pg_tool(name: str) -> str:
    exe = name + (".exe" if sys.platform == "win32" else "")
    d = os.environ.get("MONTNEY_PG_BIN")
    if d:
        p = Path(d) / exe
        if p.exists():
            return str(p)
        raise BackupError(f"{p} not found (MONTNEY_PG_BIN)")
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        p = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL" / "18" / "bin" / exe
        if p.exists():
            return str(p)
    raise BackupError(f"{name} not found: put PostgreSQL 18's bin folder on PATH or set MONTNEY_PG_BIN")


def _pg_cli(dbname: str | None, role: str = "writer") -> tuple[list[str], dict, str]:
    """Connection arguments and environment for pg_dump/pg_restore. The password goes in the child's
    environment only (PGPASSWORD), never on a command line or in a log."""
    from montney.db.conn import conninfo

    ci = conninfo(dbname, role=role)
    env = dict(os.environ, PGPASSWORD=ci["password"], PGCONNECT_TIMEOUT="10")
    return ["-h", ci["host"], "-p", str(ci["port"]), "-U", ci["user"]], env, ci["dbname"]


# ---------------------------------------------------------------- files
@dataclass(frozen=True)
class Backup:
    path: Path
    taken: datetime          # naive UTC, from the name
    tag: str = ""

    @property
    def manifest(self) -> Path:
        return manifest_path(self.path)

    @property
    def alberta_day(self) -> date:
        return self.taken.replace(tzinfo=timezone.utc).astimezone(ALBERTA).date()


def manifest_path(dump: Path) -> Path:
    return dump.with_suffix(".json")


def parse_name(path: Path) -> Backup | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    return Backup(path, datetime.strptime(m.group(1), STAMP), m.group(2) or "")


def list_backups(d: Path | None = None) -> list[Backup]:
    """Complete backups (dump AND manifest present), oldest first."""
    d = Path(d or backup_dir())
    if not d.exists():
        return []
    out = [b for p in d.glob("montney-*.dump") if (b := parse_name(p)) and b.manifest.exists()]
    return sorted(out, key=lambda b: b.taken)


def plan_prune(backups: list[Backup]) -> tuple[list[Backup], list[Backup]]:
    """(keep, delete): the newest backup of each of the KEEP_DAILY most recent days, KEEP_WEEKLY most recent
    ISO weeks and KEEP_MONTHLY most recent months that have a backup (Alberta calendar). Counting only periods
    that HAVE a backup means a stopped schedule never ages every copy out; the newest is always kept.
    Pre-migrate dumps are a migration's rollback point, so they are NOT in that rotation (a later dump the
    same day would otherwise replace one within seconds, as on Erik's machine on 2026-11-01): the newest
    KEEP_PRE_MIGRATE of them are kept, whatever the rotation does."""
    pre = sorted((b for b in backups if b.tag == "pre_migrate"), key=lambda b: b.taken)
    # pre[-k:], not pre[len(pre) - k:]: with fewer than k dumps the latter is a NEGATIVE start and keeps too
    # few (2 dumps kept 1; reviewer, #126)
    keep_pre = {b.path for b in pre[-KEEP_PRE_MIGRATE:]} if KEEP_PRE_MIGRATE > 0 else set()
    rotated = [b for b in backups if b.tag != "pre_migrate"]
    keep_rot, _ = _plan_rotation(rotated)
    keep = keep_pre | {b.path for b in keep_rot}
    kept = [b for b in backups if b.path in keep]
    return sorted(kept, key=lambda b: b.taken), sorted((b for b in backups if b.path not in keep),
                                                       key=lambda b: b.taken)


def _plan_rotation(backups: list[Backup]) -> tuple[list[Backup], list[Backup]]:
    by_day: dict = {}
    by_week: dict = {}
    by_month: dict = {}
    for b in sorted(backups, key=lambda b: b.taken):           # later ones overwrite: the newest per period
        d = b.alberta_day
        by_day[d] = b
        by_week[tuple(d.isocalendar())[:2]] = b
        by_month[(d.year, d.month)] = b
    keep = set()
    for buckets, n in ((by_day, KEEP_DAILY), (by_week, KEEP_WEEKLY), (by_month, KEEP_MONTHLY)):
        for k in sorted(buckets, reverse=True)[:n]:
            keep.add(buckets[k].path)
    if backups:
        keep.add(max(backups, key=lambda b: b.taken).path)
    kept = [b for b in backups if b.path in keep]
    return sorted(kept, key=lambda b: b.taken), sorted((b for b in backups if b.path not in keep),
                                                       key=lambda b: b.taken)


def prune(d: Path | None = None, now: datetime | None = None) -> dict:
    """Apply plan_prune, and clear what crashed runs left (old .partial files, a dump without a manifest or a
    manifest without a dump). Returns what was deleted."""
    d = Path(d or backup_dir())
    now = now or utcnow()
    keep, drop = plan_prune(list_backups(d))
    deleted = []
    for b in drop:
        for p in (b.path, b.manifest):
            p.unlink(missing_ok=True)
        deleted.append(b.path.name)
    stale = []
    for p in list(d.glob("montney-*")):
        age = now - datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).replace(tzinfo=None)
        if age < STALE_PARTIAL:
            continue
        unpaired = (p.suffix == ".dump" and not manifest_path(p).exists()) or \
                   (p.suffix == ".json" and not p.with_suffix(".dump").exists())
        if p.name.endswith(".partial") or unpaired:
            p.unlink(missing_ok=True)
            stale.append(p.name)
    return {"kept": len(keep), "deleted": deleted, "stale": stale}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- dump
def _count_tables(conn) -> dict[str, int]:
    """Row counts of every montney table AND of every continuous aggregate's materialization hypertable
    (keyed "cagg:<view>"). The rollups' rows live in _timescaledb_internal, not schema montney, and once raw
    retention drops old chunks they are the only copy of that history (reviewer, #104)."""
    tables = [t for (t,) in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'montney' "
        "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
    out = {t: conn.execute(f'SELECT count(*) FROM montney."{t}"').fetchone()[0] for t in tables}
    for view, sch, mat in conn.execute(
            "SELECT view_name, materialization_hypertable_schema, materialization_hypertable_name "
            "FROM timescaledb_information.continuous_aggregates WHERE view_schema = 'montney' "
            "ORDER BY view_name").fetchall():
        out[f"cagg:{view}"] = conn.execute(f'SELECT count(*) FROM "{sch}"."{mat}"').fetchone()[0]
    return out


def _versions(conn) -> dict:
    row = conn.execute(
        "SELECT current_setting('server_version'), "
        "(SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'), "
        "(SELECT max(version) FROM montney.schema_migration)").fetchone()
    return {"server": row[0], "timescaledb": row[1], "migration": row[2]}


def _record(dbname: str | None, kind: str, reason: str, started: datetime, ok: bool, path: str = "",
            nbytes: int | None = None, detail: str = "") -> None:
    """One montney.backup_run row; skipped silently if the table doesn't exist yet (the pre-migrate dump
    before 0010 is applied)."""
    from montney.db import connect

    try:
        conn = connect(dbname)
    except Exception as e:                                     # noqa: BLE001
        log.warning("backup: could not record the %s run: %s", kind, e)
        return
    try:
        if conn.execute("SELECT to_regclass('montney.backup_run')").fetchone()[0] is None:
            return
        conn.execute("INSERT INTO montney.backup_run (kind, reason, started, finished, ok, path, bytes, detail) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (kind, reason, started, utcnow(), ok, path, nbytes, detail[:2000]))
    except Exception as e:                                     # noqa: BLE001 - recording never fails a backup
        log.warning("backup: could not record the %s run: %s", kind, e)
    finally:
        conn.close()


@dataclass
class DumpResult:
    path: Path
    bytes: int
    seconds: float
    counts: dict = field(default_factory=dict)
    pruned: dict = field(default_factory=dict)


def dump(dbname: str | None = None, dest: Path | None = None, reason: str = "manual",
         now: datetime | None = None, prune_after: bool = True) -> DumpResult:
    """Write one backup (see the module doc). Raises BackupError; a failed run leaves no .dump behind."""
    from montney.db import connect

    started = utcnow()
    now = now or started
    dest = Path(dest or backup_dir())
    tag = "pre_migrate" if reason == "pre_migrate" else ""
    name = f"montney-{now:{STAMP}}{'-' + tag if tag else ''}.dump"
    final, tmp = dest / name, dest / (name + ".partial")
    try:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackupError(f"backup folder {dest} is not reachable: {e}") from e
        args, env, db = _pg_cli(dbname)
        conn = connect(dbname, autocommit=False)
        try:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            counts = _count_tables(conn)                       # the very rows pg_dump will read
            versions = _versions(conn)
            p = subprocess.run([pg_tool("pg_dump"), *args, "-d", db, "-Fc", "--snapshot", snapshot,
                                "-f", str(tmp)], env=env, capture_output=True, text=True)
        finally:
            conn.rollback()
            conn.close()
        if p.returncode != 0:
            raise BackupError(f"pg_dump failed ({p.returncode}): {p.stderr.strip()[-1500:]}")
        lst = subprocess.run([pg_tool("pg_restore"), "--list", str(tmp)], capture_output=True, text=True)
        if lst.returncode != 0:
            raise BackupError(f"the new dump is not readable (pg_restore --list): {lst.stderr.strip()[-800:]}")
        size = tmp.stat().st_size
        manifest = {"file": name, "bytes": size, "sha256": sha256(tmp), "taken": f"{now:%Y-%m-%dT%H:%M:%SZ}",
                    "reason": reason, "database": db, **versions, "counts": counts}
        os.replace(tmp, final)
        mtmp = manifest_path(final).with_name(manifest_path(final).name + ".partial")
        mtmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(mtmp, manifest_path(final))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        if not manifest_path(final).exists():
            final.unlink(missing_ok=True)
        detail = str(e) if isinstance(e, BackupError) else f"{type(e).__name__}: {e}"
        _record(dbname, "dump", reason, started, False, str(final), None, detail)
        if isinstance(e, BackupError):
            raise
        raise BackupError(detail) from e
    secs = (utcnow() - started).total_seconds()
    pruned = prune(dest, now) if prune_after else {}
    _record(dbname, "dump", reason, started, True, str(final), size,
            f"{sum(counts.values())} rows in {len(counts)} tables, {secs:.0f} s")
    log.info("backup: %s (%.1f MB, %.0f s)", final, size / 1e6, secs)
    return DumpResult(final, size, secs, counts, pruned)


# ---------------------------------------------------------------- restore test
@dataclass
class RestoreResult:
    path: Path | None
    checks: list = field(default_factory=list)      # (name, ok, detail)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        return ok


def latest(d: Path | None = None) -> Path | None:
    b = list_backups(d)
    return b[-1].path if b else None


def restore_test(path: Path | None = None, dbname: str | None = None, reason: str = "manual",
                 keep_scratch: bool = False) -> RestoreResult:
    """Restore `path` (default: the newest complete backup) into a scratch database and compare it with its
    manifest. Never raises for a bad dump: the result says FAIL, with every check listed."""
    from montney.db import connect

    started = utcnow()
    res = RestoreResult(Path(path) if path else latest())
    try:
        _restore_checks(res, connect, keep_scratch)
    except Exception as e:                                     # noqa: BLE001 - any error is a FAIL, not a crash
        res.add("error", False, f"{type(e).__name__}: {e}")
    res.seconds = (utcnow() - started).total_seconds()
    bad = "; ".join(f"{n}: {d}" for n, ok, d in res.checks if not ok)
    _record(dbname, "restore_test", reason, started, res.ok, str(res.path or ""),
            res.path.stat().st_size if res.path and res.path.exists() else None,
            bad or f"{len(res.checks)} checks passed, {res.seconds:.0f} s")
    return res


RESTORE_LOCK = "montney.restore_test"


RESTORE_DB_RE = re.compile(r"montney_restore_[a-z0-9_]{1,40}")


def restore_db_name_ok(name: str) -> bool:
    """The ONLY databases the restore path may create or drop: names built from RESTORE_DB_PREFIX, matched
    exactly in Python (never a LIKE pattern). The restore role is the superuser, and conninfo limits only where
    it connects, not what it may drop (reviewer / L-042): `montney`, a test db or anything else is refused."""
    from montney.db.conn import RESTORE_DB_PREFIX

    return (isinstance(name, str) and name.startswith(RESTORE_DB_PREFIX)
            and RESTORE_DB_RE.fullmatch(name) is not None)


def _restore_db_ddl(admin, verb: str, name: str) -> None:
    from psycopg import sql

    if not restore_db_name_ok(name):
        raise LiveDatabaseRefused(f"refusing to {verb} database {name!r}: the restore path touches only "
                                  f"{RESTORE_DB_RE.pattern} databases")
    q = {"create": "CREATE DATABASE {}", "drop": "DROP DATABASE IF EXISTS {} WITH (FORCE)"}[verb]
    admin.execute(sql.SQL(q).format(sql.Identifier(name)))


def drop_leftovers(admin) -> list[str]:
    """Drop every montney_restore_* database: what a restore test that crashed (or kept its scratch) left
    behind. du can't see them, and each is a full copy (reviewer, #104). Call only while holding RESTORE_LOCK,
    so a running restore test's scratch is never taken. The listing compares the exact prefix (no LIKE), and
    every name is checked again in Python before its DROP."""
    from montney.db.conn import RESTORE_DB_PREFIX

    names = [d for (d,) in admin.execute(
        "SELECT datname FROM pg_database WHERE left(datname, %s) = %s ORDER BY 1",
        (len(RESTORE_DB_PREFIX), RESTORE_DB_PREFIX)).fetchall()]
    dropped = []
    for d in names:
        if restore_db_name_ok(d):                      # anything odd under the prefix is left alone
            _restore_db_ddl(admin, "drop", d)
            dropped.append(d)
    return dropped


def _restore_checks(res: RestoreResult, connect, keep_scratch: bool) -> None:
    # The restore role (the superuser; conn.py): timescaledb_pre_restore() stops background workers, which no
    # grant allows a non-superuser (CI #104). It may open only `postgres` and montney_restore_* databases.
    admin = connect(dbname="postgres", role="restore")
    try:
        # one restore test at a time per server (a manual one vs the runner's monthly one): the session lock
        # is released when `admin` closes
        if not admin.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (RESTORE_LOCK,)).fetchone()[0]:
            res.add("lock", False, "another restore test is running on this server")
            return
        dropped = drop_leftovers(admin)
        res.add("leftovers", True, f"dropped {', '.join(dropped)}" if dropped else "none")
        _file_and_db_checks(res, connect, admin, keep_scratch)
    finally:
        admin.close()


def _file_and_db_checks(res: RestoreResult, connect, admin, keep_scratch: bool) -> None:
    p = res.path
    if not res.add("dump", p is not None and p.exists(), str(p) if p else "no complete backup found"):
        return
    m = manifest_path(p)
    if not res.add("manifest", m.exists(), str(m)):
        return
    man = json.loads(m.read_text(encoding="utf-8"))
    size = p.stat().st_size
    if not res.add("size", size == man["bytes"], f"{size} B, manifest {man['bytes']} B"):
        return
    digest = sha256(p)
    if not res.add("sha256", digest == man["sha256"], digest[:16]):
        return
    lst = subprocess.run([pg_tool("pg_restore"), "--list", str(p)], capture_output=True, text=True)
    if not res.add("archive", lst.returncode == 0,
                   "readable" if lst.returncode == 0 else (lst.stderr.strip()[-500:] or f"exit {lst.returncode}")):
        return

    from montney.db.conn import RESTORE_DB_PREFIX

    scratch = f"{RESTORE_DB_PREFIX}{uuid.uuid4().hex[:10]}"
    _restore_db_ddl(admin, "create", scratch)
    try:
        _restore_into(res, connect, scratch, man, p)
    finally:
        if not keep_scratch:
            _restore_db_ddl(admin, "drop", scratch)


def _restore_into(res: RestoreResult, connect, scratch: str, man: dict, p: Path) -> None:
    c = connect(dbname=scratch, role="restore")
    try:
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        c.execute("SELECT timescaledb_pre_restore()")
    finally:
        c.close()
    args, env, _ = _pg_cli(scratch, role="restore")
    r = subprocess.run([pg_tool("pg_restore"), *args, "-d", scratch, "--no-owner", "--no-comments", str(p)],
                       env=env, capture_output=True, text=True)
    errors = [ln for ln in r.stderr.splitlines() if "error" in ln.lower()]
    res.add("pg_restore", r.returncode == 0, (" | ".join(errors[:5]) or r.stderr.strip()[-500:]) or "clean")
    c = connect(dbname=scratch, role="restore")
    try:
        # Unschedule every restored job BEFORE post_restore starts the scheduler: retention, thinning and the
        # prune jobs delete rows, and a run between restore and count would fail a good backup.
        jobs = [j for (j,) in c.execute("SELECT id FROM _timescaledb_config.bgw_job WHERE id >= 1000 "
                                        "ORDER BY id").fetchall()]
        when = "before post_restore"
        try:
            for j in jobs:
                c.execute("SELECT alter_job(%s, scheduled => false)", (j,))
            c.execute("SELECT timescaledb_post_restore()")
        except Exception as e:                                 # noqa: BLE001 - restoring mode refused it
            when = f"right after post_restore (restoring mode refused alter_job: {type(e).__name__})"
            c.execute("SELECT timescaledb_post_restore()")
            for j in jobs:
                c.execute("SELECT alter_job(%s, scheduled => false)", (j,))
        res.add("jobs paused", True, f"{len(jobs)} restored jobs unscheduled {when}")
        v = _versions(c)
        res.add("timescaledb", v["timescaledb"] == man["timescaledb"],
                f"{v['timescaledb']} (dump {man['timescaledb']})")
        res.add("migration", v["migration"] == man["migration"], f"{v['migration']} (dump {man['migration']})")
        got = _count_tables(c)
        want = man["counts"]
        diff = {t: (want.get(t), got.get(t)) for t in sorted(set(want) | set(got)) if want.get(t) != got.get(t)}
        res.add("row counts", not diff,
                f"{len(want)} tables, {sum(want.values())} rows" if not diff else
                "; ".join(f"{t}: dump {a}, restored {b}" for t, (a, b) in list(diff.items())[:8]))
    finally:
        c.close()


# ---------------------------------------------------------------- the runner's schedule
def due(now: datetime, runs: dict) -> list[str]:
    """What the runner should do now. `runs`: {"dump" | "restore_test": {"last_ok": dt|None,
    "last_try": dt|None, "last_failed": bool}} (naive UTC). A dump is due from NIGHTLY_HOUR (Alberta) when
    there is no ok dump this Alberta day; a restore test when there is none this Alberta month (and after the
    dump, so it tests the newest). After a failure, wait RETRY_AFTER."""
    local = now.replace(tzinfo=timezone.utc).astimezone(ALBERTA)

    def alberta(dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(ALBERTA)

    def cooling(kind):
        r = runs.get(kind) or {}
        return bool(r.get("last_failed")) and r.get("last_try") is not None and now - r["last_try"] < RETRY_AFTER

    out = []
    d = (runs.get("dump") or {}).get("last_ok")
    if local.hour >= NIGHTLY_HOUR and (d is None or alberta(d).date() < local.date()) and not cooling("dump"):
        out.append("dump")
    t = (runs.get("restore_test") or {}).get("last_ok")
    if (t is None or (alberta(t).year, alberta(t).month) < (local.year, local.month)) and not cooling("restore_test"):
        out.append("restore_test")
    return out


def run_state(conn) -> dict:
    out = {}
    for kind in ("dump", "restore_test"):
        ok = conn.execute("SELECT max(started) FROM montney.backup_run WHERE kind = %s AND ok", (kind,)).fetchone()[0]
        last = conn.execute("SELECT started, ok FROM montney.backup_run WHERE kind = %s ORDER BY started DESC "
                            "LIMIT 1", (kind,)).fetchone()
        out[kind] = {"last_ok": ok, "last_try": last[0] if last else None,
                     "last_failed": bool(last) and not last[1]}
    return out
