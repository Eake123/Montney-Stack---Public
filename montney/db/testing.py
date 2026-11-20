"""Test helpers for tests/test_db*.py.

Each scratch database is created fresh and dropped afterwards, so tests never touch a real store.
Creating one needs a role allowed to CREATE DATABASE (the CI container's superuser).

Fail closed (Wellsite §4.2, §4.9): without a reachable database the tests skip on a laptop, but when
MONTNEY_DB_REQUIRED=1 (set in CI) a missing database is a test FAILURE, never a skip.

Never against a live store (after L-040): DB tests run only in CI or with MONTNEY_DB_TESTS=1, connect only to
`postgres` and montney_t_* databases (montney.db.conn refuses anything else under pytest), and refuse a server
that holds any other database unless MONTNEY_DB_TESTS_SERVER_IS_THROWAWAY=1. Local DB tests on Erik's machine
are not supported: its server holds the live store, and `montney` has no CREATEDB there anyway.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from montney.db import migrate


OPT_IN = "MONTNEY_DB_TESTS"                              # =1: run DB tests outside CI, on a THROWAWAY server
THROWAWAY = "MONTNEY_DB_TESTS_SERVER_IS_THROWAWAY"       # =1: the server may hold other databases (CI's container)
OFF_MSG = (f"DB tests are off: they run in CI (MONTNEY_DB_REQUIRED=1) or with {OPT_IN}=1 against a throwaway "
           "server. Not on Erik's machine: its server holds the live store (docs/db/INSTALL.md).")


def required() -> bool:
    return os.environ.get("MONTNEY_DB_REQUIRED") == "1"


def enabled() -> bool:
    """DB tests run only when asked for: CI's MONTNEY_DB_REQUIRED=1 or an explicit opt-in. A reachable server
    alone is not a reason (after L-040 the reachable server on Erik's machine is the live one)."""
    return required() or os.environ.get(OPT_IN) == "1"


def other_databases(admin) -> list[str]:
    """Databases on this server that are not the maintenance db, a template, or a test scratch db: where a
    live Montney store would be."""
    from montney.db.conn import TEST_DB_PREFIXES, TEST_MAINTENANCE_DB

    names = [d for (d,) in admin.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")]
    return [d for d in names if d != TEST_MAINTENANCE_DB and not d.startswith(TEST_DB_PREFIXES)]


def admin_connection():
    """A connection to the maintenance database `postgres` (never the default dbname, which is the live store's),
    or skip/fail. Three gates: DB tests must be asked for (enabled()); the server must be reachable (else skip,
    or FAIL under MONTNEY_DB_REQUIRED=1); and a server that holds any other database is refused unless it is
    declared throwaway (MONTNEY_DB_TESTS_SERVER_IS_THROWAWAY=1, as CI's container is). The tests create
    scratch databases and reset montney_reader's password, so they must never run where a live store is."""
    if not enabled():
        pytest.skip(OFF_MSG)
    from montney.db import connect
    from montney.db.conn import TEST_MAINTENANCE_DB

    try:
        admin = connect(dbname=TEST_MAINTENANCE_DB)
    except Exception as e:
        msg = f"no Postgres for DB tests ({type(e).__name__}: {e})"
        if required():
            pytest.fail(msg + " and MONTNEY_DB_REQUIRED=1", pytrace=False)
        pytest.skip(msg)
    others = other_databases(admin)
    if others and os.environ.get(THROWAWAY) != "1":
        admin.close()
        pytest.fail(f"this server holds other databases {others}: it may have a live Montney store. DB tests "
                    f"create databases and reset montney_reader's password: use a separate throwaway "
                    f"cluster/port, or set {THROWAWAY}=1 if this one really is throwaway", pytrace=False)
    return admin


@contextmanager
def scratch_db(migrations: Path | None = migrate.MIGRATIONS_DIR):
    """Yield an autocommit connection to a new, empty database, with `migrations` applied (None: none).
    The database is dropped afterwards."""
    from montney.db import connect

    admin = admin_connection()
    name = f"montney_t_{uuid.uuid4().hex[:12]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    conn = None
    try:
        conn = connect(dbname=name)
        if migrations is not None:
            migrate.apply(conn, migrations)
        yield conn
    finally:
        if conn is not None:
            conn.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def release_session_lock(conn, key: str) -> None:
    """Release a session advisory lock SYNCHRONOUSLY, then close. Closing alone is not enough: the server
    backend frees its session locks when it processes the disconnect, after close() has already returned, so
    the next statement on another connection can still find the lock held (FLAKE_HISTORY via #121, db job
    run 41325141973)."""
    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
    conn.close()


def wait_advisory_released(conn, key: str, timeout_s: float = 10.0) -> None:
    """Fail closed unless the advisory lock `key` is free: probe it from `conn` (take it and give it back at
    once) until it is, or fail after `timeout_s`."""
    import time

    deadline = time.monotonic() + timeout_s
    while not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]:
        assert time.monotonic() < deadline, f"advisory lock {key!r} still held after {timeout_s} s"
        time.sleep(0.05)
    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))


SETTLE_S = 1.5      # pause_job: how long no backend of the paused job must be seen (a launched run's window)


def pause_job(conn, proc_name: str, lock_key: str | None = None, timeout_s: float = 30,
              proc_schema: str | None = "montney") -> None:
    """Stop the scheduled montney.<proc_name> job, then wait until no run of it is in flight, so a test can
    seed data and call the procedure itself without racing the scheduler. Every job's first run starts as
    soon as its database is created, so tests that seed what a job deletes must pause it first (a flake on
    #59: prune_obs_flags deleted a seeded orphan before the test looked).

    In flight = its advisory lock is held (`lock_key`), or the backend running it is still active."""
    import time

    import psycopg

    deadline = time.monotonic() + timeout_s
    # The scheduler updates the same job catalog row when it starts the job's first run (right after the
    # database is created), so alter_job can lose that race with "tuple concurrently updated" (CI run
    # 41271091152). Retry until it sticks.
    while True:
        try:
            conn.execute("SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
                         "WHERE proc_name = %s AND (%s::text IS NULL OR proc_schema = %s)",
                         (proc_name, proc_schema, proc_schema))
            break
        except psycopg.errors.InternalError_ as e:
            if "tuple concurrently updated" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    # A run the scheduler LAUNCHED just before the pause may not have reached its query yet, so neither the query
    # check nor the lock below sees it, and it runs after the test seeds data (local CI on Windows, where process
    # start-up is slow: 2026-11-01). Its backend exists from the start with the job id in its application_name,
    # so wait until no backend of the job has been seen for SETTLE_S in a row.
    ids = [j for (j,) in conn.execute("SELECT job_id FROM timescaledb_information.jobs WHERE proc_name = %s "
                                      "AND (%s::text IS NULL OR proc_schema = %s)",
                                      (proc_name, proc_schema, proc_schema)).fetchall()]
    quiet_since = time.monotonic()
    while ids and time.monotonic() - quiet_since < SETTLE_S:
        if conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND (" + " OR ".join(["application_name LIKE %s"] * len(ids)) + ")",
                        [f"%[{j}]%" for j in ids]).fetchone()[0]:
            quiet_since = time.monotonic()
            assert time.monotonic() < deadline, f"a launched {proc_name} run did not finish within {timeout_s} s"
        time.sleep(0.1)
    # A running job shows in pg_stat_activity with its proc name in the query text.
    while conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                       "AND pid <> pg_backend_pid() AND state = 'active' AND query ILIKE %s",
                       (f"%{proc_name}%",)).fetchone()[0]:
        assert time.monotonic() < deadline, f"a scheduled {proc_name} run did not finish within {timeout_s} s"
        time.sleep(0.1)
    if lock_key is None:
        return
    while not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,)).fetchone()[0]:
        assert time.monotonic() < deadline, f"a scheduled {proc_name} run did not finish within {timeout_s} s"
        time.sleep(0.2)
    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))


def pause_all_jobs(conn, timeout_s: float = 30) -> None:
    """Unschedule EVERY job of this scratch database (thinning, prunes, rollup refreshes, compression), then wait
    until no job backend has been seen for SETTLE_S in a row. For tests that measure bytes: a refresh or
    compression changes the sizes under them (test_db_du's hysteresis red saw 'warn' at 50 % in local CI)."""
    import time

    import psycopg

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            conn.execute("SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs")
            break
        except psycopg.errors.InternalError_ as e:
            if "tuple concurrently updated" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    quiet_since = time.monotonic()
    while time.monotonic() - quiet_since < SETTLE_S:
        if conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND application_name ~ '\\[[0-9]+\\]$'").fetchone()[0]:
            quiet_since = time.monotonic()
            assert time.monotonic() < deadline, f"job runs still active after {timeout_s} s"
        time.sleep(0.1)


def add_series(conn, key: str, tier: str, source: str = "test_src") -> int:
    row = conn.execute("SELECT id FROM montney.source WHERE code = %s", (source,)).fetchone()
    src = row[0] if row else conn.execute("INSERT INTO montney.source (code) VALUES (%s) ON CONFLICT (code) DO UPDATE "
                       "SET code = excluded.code RETURNING id", (source,)).fetchone()[0]
    return conn.execute("INSERT INTO montney.series (key, source_id, tier) VALUES (%s, %s, %s) RETURNING id",
                        (key, src, tier)).fetchone()[0]


# ---------------------------------------------------------------- a network-free connector for runner tests
from montney.connectors.base import Connector as _Connector, FetchResult as _FetchResult  # noqa: E402


class FakeConnector(_Connector):
    """`type: python` connector for runner tests. `params.series`: {series id: freq}; every fetch emits one
    point per series at ctx.now (as_of = ctx.now), value `params.value` (default 1.0). No network."""

    def declared_series(self):
        from montney.store import SeriesMeta

        return [SeriesMeta(sid, self.id, sid, "u", freq) for sid, freq in self.spec["params"]["series"].items()]

    def fetch(self, ctx):
        from montney.store import Obs

        v = float(self.spec["params"].get("value", 1.0))
        return _FetchResult(obs=[Obs(sid, ctx.now, v, ctx.now) for sid in self.spec["params"]["series"]],
                            meta=self.declared_series())


def fake_spec(sid: str = "test_fake", series: dict | None = None, retention=None, **extra) -> dict:
    spec = {"id": sid, "type": "python", "module": "montney.db.testing", "class": "FakeConnector",
            "mode": "cached", "interval": "1h", "params": {"series": series or {"test.fake.a": "5min"}}}
    if retention is not None:
        spec["retention"] = retention
    spec.update(extra)
    return spec
