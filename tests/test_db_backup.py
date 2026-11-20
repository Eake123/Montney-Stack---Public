"""Nightly backups and the monthly restore test (montney/db/backup.py, docs/db/BACKUPS.md, PLAN P3).

No database: names, the 7 daily / 4 weekly / 12 monthly retention, stale-file cleanup, and the runner's
schedule (what is due when). Seeded red: a plan that ages out a month must be caught by the same checker.

Database (CI's service container, pg_dump/pg_restore 18 from MONTNEY_PG_BIN): a real round trip (dump, then
restore into a scratch database, then every check PASS, including exact row counts and compressed chunks),
and the restore check FAILS on a corrupt, truncated, re-signed-corrupt, missing, unpaired or miscounted dump.
A failed pg_dump leaves no .dump behind (temp file, then move). The runner does a due dump + restore test and
then nothing more that day. And Erik's split works: a NON-superuser writes and dumps, and only the restore test
uses the superuser (role "restore", confined to scratch databases).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from montney.db import backup

UTC_NOON = datetime(2026, 11, 1, 18, 0)          # 12:00 in Alberta (UTC-6)


def _b(dt: datetime, tag: str = "") -> backup.Backup:
    name = f"montney-{dt:%Y%m%dT%H%M%SZ}{'-' + tag if tag else ''}.dump"
    return backup.Backup(Path(name), dt, tag)


# ------------------------------------------------------------------ names and retention (no database)
def test_names_round_trip():
    b = backup.parse_name(Path("montney-20261101T090000Z-pre_migrate.dump"))
    assert b.taken == datetime(2026, 11, 1, 9) and b.tag == "pre_migrate"
    assert backup.parse_name(Path("montney-20261101T090000Z.dump")).tag == ""
    for bad in ("montney-2026.dump", "other-20261101T090000Z.dump", "montney-20261101T090000Z.dump.partial"):
        assert backup.parse_name(Path(bad)) is None


def gfs_violations(all_: list, kept: list) -> list[str]:
    """What a correct 7/4/12 plan must keep: the newest of each of the 7 newest days, 4 newest ISO weeks and
    12 newest months that have a backup, plus the newest overall; and nothing else."""
    def newest(keyf, n):
        groups: dict = {}
        for b in sorted(all_, key=lambda b: b.taken):
            groups[keyf(b.alberta_day)] = b
        return {groups[k].path for k in sorted(groups, reverse=True)[:n]}

    want = (newest(lambda d: d, 7) | newest(lambda d: tuple(d.isocalendar())[:2], 4)
            | newest(lambda d: (d.year, d.month), 12) | {max(all_, key=lambda b: b.taken).path})
    got = {b.path for b in kept}
    return [f"missing {p.name}" for p in sorted(want - got)] + [f"extra {p.name}" for p in sorted(got - want)]


def _nightly(days: int, end: datetime = UTC_NOON) -> list:
    return [_b(end - timedelta(days=i)) for i in range(days)]


def test_retention_keeps_7_daily_4_weekly_12_monthly():
    all_ = _nightly(500)
    keep, drop = backup.plan_prune(all_)
    print(f"{len(all_)} nightly backups -> keep {len(keep)}: {[b.path.name[8:16] for b in keep]}")
    assert gfs_violations(all_, keep) == []
    assert len(keep) <= 7 + 4 + 12 and len(keep) + len(drop) == len(all_)
    newest_days = {b.alberta_day for b in all_[:7]}
    assert newest_days <= {b.alberta_day for b in keep}                      # the last 7 days, every one
    oldest_kept = min(b.taken for b in keep)
    assert oldest_kept >= UTC_NOON - timedelta(days=366)                     # nothing older than ~12 months


def test_RED_a_plan_that_drops_the_monthlies_is_caught(monkeypatch):
    monkeypatch.setattr(backup, "KEEP_MONTHLY", 3)
    all_ = _nightly(500)
    keep, _ = backup.plan_prune(all_)
    problems = gfs_violations(all_, keep)
    print(problems[:3])
    assert any(p.startswith("missing") for p in problems)


def test_a_stopped_schedule_never_ages_every_copy_out():
    """Backups that stopped a year ago: periods are counted among those that HAVE a backup, so the newest
    ones stay (it is not "nothing in the last 7 days, delete all")."""
    old = _nightly(40, end=UTC_NOON - timedelta(days=400))
    keep, _ = backup.plan_prune(old)
    assert len(keep) >= 7 and max(b.taken for b in keep) == max(b.taken for b in old)
    assert backup.plan_prune([])[0] == []


def test_alberta_day_buckets_not_utc():
    """Days are Alberta days: 00:00Z and 05:00Z on Nov 2 are 18:00 and 23:00 on Nov 1 in Alberta (UTC-6)."""
    a, b = _b(datetime(2026, 11, 2, 0, 0)), _b(datetime(2026, 11, 2, 5, 0))   # 18:00 and 23:00 on Nov 1, Alberta
    assert a.alberta_day == b.alberta_day == datetime(2026, 11, 1).date()


def test_prune_deletes_pairs_and_stale_leftovers(tmp_path):
    def touch(name, age_h=0):
        p = tmp_path / name
        p.write_bytes(b"x")
        t = (datetime.now() - timedelta(hours=age_h)).timestamp()
        os.utime(p, (t, t))
        return p

    for b in _nightly(30, end=datetime.utcnow()):
        touch(b.path.name)
        touch(b.path.with_suffix(".json").name)
    partial_old = touch("montney-20200101T000000Z.dump.partial", age_h=48)
    partial_new = touch("montney-20990101T000000Z.dump.partial", age_h=0)   # a run in progress: keep
    orphan = touch("montney-20200102T000000Z.dump", age_h=48)                # no manifest: incomplete
    out = backup.prune(tmp_path)
    print(out)
    assert out["kept"] == len(backup.list_backups(tmp_path)) <= 7 + 4 + 12
    assert not partial_old.exists() and partial_new.exists() and not orphan.exists()
    for d in out["deleted"]:
        assert not (tmp_path / d).exists() and not (tmp_path / d).with_suffix(".json").exists()


# ------------------------------------------------------------------ the runner's schedule (no database)
def _at(local_h: int, day: int = 1) -> datetime:
    return datetime(2026, 11, day, local_h) + timedelta(hours=6)              # Alberta UTC-6 -> UTC


@pytest.mark.parametrize("now, runs, want", [
    (_at(1), {}, ["restore_test"]),                                   # before 02:00: no dump yet
    (_at(2), {}, ["dump", "restore_test"]),
    (_at(3), {"dump": {"last_ok": _at(2)}, "restore_test": {"last_ok": _at(2)}}, []),
    (_at(3, day=2), {"dump": {"last_ok": _at(2)}, "restore_test": {"last_ok": _at(2)}}, ["dump"]),
    (_at(3), {"dump": {"last_ok": None, "last_try": _at(2, ) + timedelta(minutes=30), "last_failed": True},
              "restore_test": {"last_ok": _at(2)}}, []),            # failed 30 min ago: wait an hour
    (_at(4), {"dump": {"last_ok": None, "last_try": _at(2), "last_failed": True},
              "restore_test": {"last_ok": _at(2)}}, ["dump"]),      # 2 h later: try again
    (datetime(2026, 12, 1, 12), {"dump": {"last_ok": datetime(2026, 12, 1, 9)},
                                 "restore_test": {"last_ok": datetime(2026, 11, 30, 9)}}, ["restore_test"]),
], ids=["01h", "02h", "done-today", "next-day", "cooling", "retry", "new-month"])
def test_due(now, runs, want):
    assert backup.due(now, runs) == want


# ------------------------------------------------------------------ the database (CI)
def _seed(conn) -> None:
    from montney.db.testing import add_series, pause_job

    for job in ("thin_forecasts", "prune_fetch_runs", "prune_obs_flags"):
        pause_job(conn, job)
    # the columnstore policy too: it compressed the seeded chunk between compress_chunk's own "already
    # compressed?" check and its work (CI on #104, twice), so the test compresses alone
    pause_job(conn, "policy_compression", proc_schema="_timescaledb_functions")
    fast = add_series(conn, "test.backup.fast", "fast", source="test_backup")
    fc = add_series(conn, "test.backup.fc", "forecast", source="test_backup")
    day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO montney.obs_fast (series_id, ts, as_of, value, fetched_at) VALUES (%s, %s, %s, %s, %s)",
                        [(fast, day + timedelta(minutes=5 * i), day + timedelta(minutes=5 * i), float(i), day)
                         for i in range(500)])
        cur.executemany("INSERT INTO montney.obs_forecast (series_id, ts, as_of, value, fetched_at) VALUES (%s, %s, %s, %s, %s)",
                        [(fc, day + timedelta(days=5, hours=h), day, float(h), day) for h in range(48)])
    from montney.db.backfill import refresh_with_retry

    for view in ("obs_fast_1h", "obs_fast_1d"):                         # the rollups hold rows of their own
        refresh_with_retry(conn, view, day - timedelta(days=2), day + timedelta(days=2))
    for (c,) in conn.execute("SELECT show_chunks('montney.obs_fast')").fetchall():
        conn.execute("SELECT compress_chunk(%s::regclass, if_not_compressed => true)", (c,))  # the policy job may race us


def _tools_or_fail():
    try:
        backup.pg_tool("pg_dump"), backup.pg_tool("pg_restore")
    except backup.BackupError as e:
        from montney.db.testing import required

        if required():
            pytest.fail(f"{e} (CI installs the PostgreSQL 18 client: MONTNEY_PG_BIN)", pytrace=False)
        pytest.skip(str(e))


@pytest.fixture
def dumped(tmp_path):
    """A seeded scratch database and one backup of it: (conn, DumpResult)."""
    from montney.db.testing import scratch_db

    with scratch_db() as conn:
        _tools_or_fail()
        _seed(conn)
        r = backup.dump(dbname=conn.info.dbname, dest=tmp_path / "bk", reason="manual")
        yield conn, r


def _report(res) -> str:
    return "\n".join(f"{'PASS' if ok else 'FAIL'} {n}: {d}" for n, ok, d in res.checks)


def test_round_trip_dump_then_restore_passes_every_check(dumped):
    conn, r = dumped
    man = json.loads(r.path.with_suffix(".json").read_text())
    print(r.path.name, r.bytes, man["counts"])
    assert r.path.exists() and not list(r.path.parent.glob("*.partial"))
    assert man["counts"]["obs_fast"] == 500 and man["counts"]["obs_forecast"] == 48
    # reviewer (#104): the rollups' materialized rows are counted (and compared) too
    assert man["counts"]["cagg:obs_fast_1h"] > 0 and man["counts"]["cagg:obs_fast_1d"] > 0, man["counts"]
    assert man["timescaledb"] and man["migration"] >= 10
    res = backup.restore_test(r.path, dbname=conn.info.dbname)
    print(_report(res))
    assert res.ok, _report(res)
    names = {n for n, _, _ in res.checks}
    assert {"sha256", "archive", "pg_restore", "row counts", "timescaledb", "migration"} <= names
    left = conn.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'montney\\_restore\\_%'"
                        ).fetchone()[0]
    assert left == 0                                                     # the scratch restore db is dropped
    runs = conn.execute("SELECT kind, ok FROM montney.backup_run ORDER BY id").fetchall()
    assert runs == [("dump", True), ("restore_test", True)]


def _corrupt_middle(p: Path) -> None:
    data = bytearray(p.read_bytes())
    i = len(data) // 2
    data[i:i + 64] = bytes(b ^ 0xFF for b in data[i:i + 64])
    p.write_bytes(bytes(data))


@pytest.mark.parametrize("damage, failing", [
    ("flip-bytes", "sha256"),
    ("truncate", "size"),
    ("flip-bytes-and-re-sign", None),            # the manifest "fixed": pg_restore itself must catch it
    ("missing-dump", "dump"),
    ("missing-manifest", "manifest"),
    ("miscounted", "row counts"),
    ("miscounted-rollup", "row counts"),         # a rollup's materialized rows differ
], ids=lambda v: str(v))
def test_RED_a_bad_backup_fails_the_restore_check(dumped, damage, failing):
    conn, r = dumped
    p, m = r.path, r.path.with_suffix(".json")
    man = json.loads(m.read_text())
    if damage == "flip-bytes":
        _corrupt_middle(p)
    elif damage == "truncate":
        p.write_bytes(p.read_bytes()[: r.bytes // 2])
    elif damage == "flip-bytes-and-re-sign":
        _corrupt_middle(p)
        man["sha256"] = backup.sha256(p)
        m.write_text(json.dumps(man))
    elif damage == "missing-dump":
        p.unlink()
    elif damage == "missing-manifest":
        m.unlink()
    elif damage == "miscounted":
        man["counts"]["obs_fast"] += 1
        m.write_text(json.dumps(man))
    elif damage == "miscounted-rollup":
        man["counts"]["cagg:obs_fast_1h"] += 1
        m.write_text(json.dumps(man))
    res = backup.restore_test(p, dbname=conn.info.dbname)
    print(damage, "\n" + _report(res))
    assert not res.ok
    bad = [n for n, ok, _ in res.checks if not ok]
    if failing:
        assert failing in bad, bad
    else:
        assert set(bad) & {"archive", "pg_restore", "row counts", "error"}, bad
    assert conn.execute("SELECT ok FROM montney.backup_run WHERE kind = 'restore_test' ORDER BY id DESC "
                        "LIMIT 1").fetchone()[0] is False


def test_no_backup_at_all_fails(tmp_path, monkeypatch):
    from montney.db.testing import admin_connection

    admin_connection().close()                                          # DB tests only (it connects)
    monkeypatch.setenv("MONTNEY_BACKUP_DIR", str(tmp_path / "empty"))
    res = backup.restore_test(None, dbname="montney_t_none_recorded")   # nothing to restore: FAIL, not pass
    assert not res.ok and ("dump", False) in [(n, ok) for n, ok, _ in res.checks]


@pytest.mark.parametrize("lock_held", [False, True], ids=["drops-leftovers", "RED-lock-held-drops-nothing"])
def test_leftover_restore_copies_are_dropped_first(dumped, lock_held):
    """Reviewer (#104): a crashed restore test leaves a full copy du can't see. The next one drops every
    montney_restore_* first and says so. Under the lock only: with another restore test running (lock held),
    it refuses and drops nothing (that copy may be in use)."""
    from montney.db import connect
    from montney.db.testing import admin_connection

    conn, r = dumped
    admin = admin_connection()
    left = "montney_restore_leftover_from_a_crash"
    admin.execute(f'CREATE DATABASE "{left}"')
    other = connect(dbname="postgres", role="restore")
    try:
        if lock_held:
            assert other.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (backup.RESTORE_LOCK,)).fetchone()[0]
        res = backup.restore_test(r.path, dbname=conn.info.dbname)
        print(_report(res))
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (left,)).fetchone() is not None
        if not lock_held:
            assert res.ok and ("leftovers", True, f"dropped {left}") in res.checks and not exists
        else:
            assert not res.ok and res.checks[0][:2] == ("lock", False) and exists
    finally:
        other.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{left}" WITH (FORCE)')
        admin.close()


FAKE_PG_DUMP = """
import sys
args = sys.argv[1:]
if "-f" in args:                                  # write half a file to whatever follows -f, then die
    with open(args[args.index("-f") + 1], "w") as f:
        f.write("partial")
print("pg_dump: error: connection lost", file=sys.stderr)
sys.exit(1)
"""


def test_a_failed_pg_dump_leaves_no_dump(tmp_path, monkeypatch):
    """A crash mid-dump (half a file, exit 1) leaves no .dump and no .partial. Portable fake (a Python script behind a
    .cmd or sh wrapper, returned by pg_tool), so it runs on Windows too: local CI's DB gate allows no skips (L-052)."""
    from montney.db.testing import scratch_db

    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "fake_pg_dump.py"
    script.write_text(FAKE_PG_DUMP)
    if sys.platform == "win32":
        wrapper = fake / "pg_dump.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        wrapper = fake / "pg_dump"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(0o755)
    monkeypatch.setattr(backup, "pg_tool", lambda name: str(wrapper))
    with scratch_db() as conn:
        with pytest.raises(backup.BackupError, match="pg_dump failed"):
            backup.dump(dbname=conn.info.dbname, dest=tmp_path / "bk")
        left = sorted(p.name for p in (tmp_path / "bk").iterdir())
        print(left)
        assert left == []                                                # no .dump, no .partial
        assert conn.execute("SELECT ok, detail FROM montney.backup_run").fetchall()[0][0] is False


def test_the_runner_backs_up_when_due_then_not_again(tmp_path, monkeypatch):
    from montney import runner
    from montney.db.pgstore import PostgresStore
    from montney.db.testing import fake_spec, scratch_db

    monkeypatch.setenv("MONTNEY_BACKUP_DIR", str(tmp_path / "bk"))
    with scratch_db() as conn:
        _tools_or_fail()
        _seed(conn)
        r = runner.PgRunner(PostgresStore(conn), specs={"test_fake": fake_spec(retention="fast")})
        monkeypatch.setattr(backup, "NIGHTLY_HOUR", 0)                   # due at any hour CI happens to run
        now = datetime.utcnow()
        first = r.task_backup(now=now)
        print(first)
        assert first["due"] == ["dump", "restore_test"] and first["restore_test"]["ok"], first
        assert r.task_backup(now=now)["due"] == []                       # done for this day and month
        assert "backup" in runner.PgRunner.PERIODIC


def test_pre_migrate_backup_only_when_there_is_data_to_lose(tmp_path, monkeypatch):
    from montney.db import cli, migrate
    from montney.db.testing import scratch_db

    monkeypatch.setenv("MONTNEY_BACKUP_DIR", str(tmp_path / "bk"))
    older = tmp_path / "migrations"
    older.mkdir()
    for m in migrate.discover():
        if m.version < 10:
            shutil.copy(m.path, older / m.path.name)
    with scratch_db(migrations=None) as empty:
        assert cli.pre_migrate_backup(empty) is True
        assert not (tmp_path / "bk").exists()                            # first init: nothing to back up
    with scratch_db(migrations=older) as conn:                           # 0001-0009 applied, 0010 pending
        _tools_or_fail()
        assert cli.pre_migrate_backup(conn) is True
        made = backup.list_backups(tmp_path / "bk")
        print([b.path.name for b in made])
        assert len(made) == 1 and made[0].tag == "pre_migrate"


def test_eriks_split_non_superuser_writes_and_dumps_superuser_restores(tmp_path, monkeypatch):
    """Erik's machine: `montney` is NOT a superuser (it owns the database; `postgres` created timescaledb). It
    migrates, writes and DUMPS. Only the restore test uses the superuser (role "restore",
    POSTGRES_SUPERUSER_PASSWORD), because timescaledb_pre_restore() stops background workers and no grant lets
    a non-superuser do that (CI on #104). Mirror it: a probe role plays montney; CI's superuser plays postgres."""
    import uuid

    from montney.db import connect, migrate
    from montney.db.testing import admin_connection

    _tools_or_fail()
    pw = "probe-throwaway-not-a-secret"
    admin = admin_connection()                                          # skips here unless DB tests are on
    su_user = os.environ.get("MONTNEY_DB_USER", "montney")             # CI's superuser
    su_pw = os.environ["MONTNEY_DB_PASSWORD"]
    db = f"montney_t_{uuid.uuid4().hex[:12]}"
    try:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'montney_probe'").fetchone():
            admin.execute(f"ALTER ROLE montney_probe LOGIN NOCREATEDB NOSUPERUSER PASSWORD '{pw}'")
        else:
            admin.execute(f"CREATE ROLE montney_probe LOGIN NOSUPERUSER PASSWORD '{pw}'")
        admin.execute(f'CREATE DATABASE "{db}" OWNER montney_probe')    # as INSTALL.md step 3 does for montney
        ext = connect(dbname=db)
        ext.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")        # by the superuser, as on Erik's machine
        ext.close()
        monkeypatch.setenv("MONTNEY_DB_USER", "montney_probe")
        monkeypatch.setenv("MONTNEY_DB_PASSWORD", pw)
        monkeypatch.setenv("MONTNEY_DB_RESTORE_USER", su_user)
        monkeypatch.setenv("POSTGRES_SUPERUSER_PASSWORD", su_pw)
        conn = connect(dbname=db)
        try:
            assert not conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
            migrate.apply(conn)
            _seed(conn)
            r = backup.dump(dbname=db, dest=tmp_path / "bk")              # as the non-superuser
            res = backup.restore_test(r.path, dbname=db)                  # the restore role: the superuser
            print(_report(res))
            assert res.ok, _report(res)
            assert conn.execute("SELECT kind, ok FROM montney.backup_run ORDER BY id").fetchall() ==                 [("dump", True), ("restore_test", True)]
        finally:
            conn.close()
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        admin.close()


def test_the_restore_role_is_only_for_scratch_restores(monkeypatch):
    """The superuser credentials open only `postgres` and montney_restore_* (seeded red: the live store and the
    test scratch dbs are refused), and never through the writer/reader roles."""
    from montney.db import conn as dbconn

    monkeypatch.setenv("POSTGRES_SUPERUSER_PASSWORD", "x")
    monkeypatch.delenv("MONTNEY_DB_RESTORE_USER", raising=False)
    for k in ("MONTNEY_DB_REQUIRED", "MONTNEY_DB_TESTS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(dbconn.LiveDatabaseRefused, match="off in tests"):   # never from a test not opted in
        dbconn.conninfo("postgres", role="restore")
    monkeypatch.setenv("MONTNEY_DB_TESTS", "1")
    assert dbconn.conninfo("postgres", role="restore")["user"] == "postgres"
    assert dbconn.conninfo("montney_restore_ab12", role="restore")["dbname"] == "montney_restore_ab12"
    for other in ("montney", "montney_t_0123456789ab"):
        with pytest.raises(dbconn.LiveDatabaseRefused):
            dbconn.conninfo(other, role="restore")


def test_du_reports_the_backup_state_and_warns_when_stale(dumped, tmp_path):
    """Reviewer (#104, non-blocking): the backup state shows in `montney du`; a last good dump older than 36 h
    is a warning (seeded red: age the only dump by 3 days)."""
    from montney.db import du

    conn, _ = dumped
    rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
    print(rep.backups, rep.warnings)
    assert rep.backups["last_dump"] is not None and not any("no good backup" in w for w in rep.warnings)
    assert "last good dump" in du.format_report(rep, {})
    conn.execute("UPDATE montney.backup_run SET started = started - INTERVAL '3 days'")
    rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
    assert any("no good backup in the last 36 h" in w for w in rep.warnings), rep.warnings


# ------------------------------------------------------------------ the superuser may drop only its scratch dbs (L-042)
class _RecordingAdmin:
    """A fake admin connection: pg_database lists `names`; every other statement is recorded, not run."""

    def __init__(self, names):
        self.names, self.ran = names, []

    def execute(self, q, params=None):
        text = q if isinstance(q, str) else q.as_string(None)
        if "pg_database" in text:
            return _Rows([(n,) for n in self.names])
        self.ran.append(text)
        return _Rows([])


class _Rows(list):
    def fetchall(self):
        return list(self)


@pytest.mark.parametrize("name", ["montney", "montney_t_0123456789ab", "postgres", "montney_restore",
                                  "montney_restore_", "Montney_restore_x", "montney_restore_x; DROP DATABASE montney",
                                  'montney_restore_x" WITH (FORCE); DROP DATABASE "montney'])
def test_RED_the_restore_path_refuses_to_create_or_drop_anything_else(name):
    """Reviewer/orchestrator (L-042): conninfo limits where the superuser connects, not what it may DROP. Every
    CREATE/DROP DATABASE on the restore path goes through one strict name check (exact prefix + pattern, in
    Python, identifier-quoted); `montney`, a test db, an empty or near-miss name, or an injection is refused
    and no SQL is issued."""
    from montney.db.conn import LiveDatabaseRefused

    admin = _RecordingAdmin([])
    for verb in ("drop", "create"):
        with pytest.raises(LiveDatabaseRefused):
            backup._restore_db_ddl(admin, verb, name)
    assert admin.ran == [] and not backup.restore_db_name_ok(name)


def test_leftover_cleanup_drops_only_strict_restore_names():
    """Even if the listing returned something else (it compares the exact prefix, no LIKE), only strictly
    valid montney_restore_* names are dropped, each quoted as an identifier."""
    admin = _RecordingAdmin(["montney", "montney_t_abc", "montney_restore_ok1", "montney_restore_BAD", "postgres"])
    dropped = backup.drop_leftovers(admin)
    print(dropped, admin.ran)
    assert dropped == ["montney_restore_ok1"]
    assert admin.ran == ['DROP DATABASE IF EXISTS "montney_restore_ok1" WITH (FORCE)']


# ------------------------------------------------------------------ pre-migrate dumps outside the rotation
@pytest.mark.parametrize("keep", [3, 0], ids=["kept-outside-rotation", "RED-in-the-rotation"])
def test_pre_migrate_dumps_survive_a_later_same_day_dump(keep, monkeypatch):
    """Erik's machine, 2026-11-01: `db init`'s pre-migrate dump was pruned 9 s later because the next dump the
    same day took the day's slot. Pre-migrate dumps are a rollback point: the newest KEEP_PRE_MIGRATE are kept
    outside the 7/4/12 rotation and take no slot. Seeded red: KEEP_PRE_MIGRATE = 0 (back in the rotation)."""
    monkeypatch.setattr(backup, "KEEP_PRE_MIGRATE", keep)
    day = datetime(2026, 11, 1, 16, 5, 15)
    pre = _b(day, "pre_migrate")
    later = _b(day + timedelta(seconds=9))
    old_pres = [_b(day - timedelta(days=d), "pre_migrate") for d in (40, 80, 120)]
    nightly = _nightly(30, end=day - timedelta(hours=1))
    keep_, drop = backup.plan_prune(nightly + old_pres + [pre, later])
    kept = {b.path for b in keep_}
    print(keep, sorted(b.path.name for b in keep_ if b.tag))
    if keep:
        assert pre.path in kept and later.path in kept                     # both survive
        assert {b.path for b in keep_ if b.tag == "pre_migrate"} == {pre.path, old_pres[0].path, old_pres[1].path}
        assert old_pres[2].path not in kept                                # only the newest 3
        rot = [b for b in nightly + [later]]                               # the rotation is unchanged by them
        assert {b.path for b in keep_ if not b.tag} == {b.path for b in backup.plan_prune(rot)[0]}
    else:
        assert pre.path not in kept                                        # the bug is back: caught


@pytest.mark.parametrize("n, kept", [(1, 1), (2, 2), (3, 3), (4, 3), (5, 3)])
def test_every_pre_migrate_dump_is_kept_up_to_three(n, kept):
    """Reviewer (#126): a slice from len(pre) - 3 goes negative under 3 dumps, so 2 dumps kept 1, which is
    the state of Erik's second `db init`. Each count up to KEEP_PRE_MIGRATE keeps all, then the newest 3."""
    t0 = datetime(2026, 11, 1, 16, 5)
    pres = [_b(t0 - timedelta(days=30 * i), "pre_migrate") for i in range(n)]
    keep_, _ = backup.plan_prune(pres + [_b(t0 + timedelta(seconds=9))])
    got = sorted(b.taken for b in keep_ if b.tag == "pre_migrate")
    assert len(got) == kept and got == sorted(b.taken for b in pres)[-kept:], got
