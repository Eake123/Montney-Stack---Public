"""PORT steps 3 + 4b (docs/db/PORT.md, LOG L-035): with MONTNEY_DB=postgres, `montney serve` is READ-ONLY. It uses the
`montney_reader` role, with no scheduler, no writer connection and no writer lock, because `montney run` (#85) is the only
writer. Refreshes are queued in refresh_request (202), and /api/status carries per-source budget states.
Owner: orchestrator (server.py/cli.py); it's named test_db_* so CI's `db` job runs it.

The discriminating check is a marker series inserted straight into Postgres before the app starts:
  - MONTNEY_DB=postgres: the API's catalog must contain it, and a `montney-reader` connection must be open;
  - MONTNEY_DB=duckdb (the permanent seeded red / contrast): the same check must NOT see it. That proves the marker
    tells the two backends apart, so an app that ignored open_store() would fail the postgres case.
MONTNEY_HOME points at tmp_path, so the DuckDB side never touches Erik's data dir.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from montney.db.testing import add_series, admin_connection, scratch_db

READER_PW = "ci-reader-throwaway"
MARKER = "test.port_step3.marker"


@pytest.fixture
def pg(monkeypatch, tmp_path):
    admin = admin_connection()
    try:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'montney_reader'").fetchone():
            admin.execute(f"CREATE ROLE montney_reader LOGIN PASSWORD '{READER_PW}'")
        else:
            admin.execute(f"ALTER ROLE montney_reader PASSWORD '{READER_PW}'")
    finally:
        admin.close()
    monkeypatch.setenv("MONTNEY_DB_READER_PASSWORD", READER_PW)
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    with scratch_db() as conn:
        monkeypatch.setenv("MONTNEY_DB_NAME", conn.info.dbname)
        add_series(conn, MARKER, "hourly", source="port_step3")
        yield conn


def _app_view(pg_conn):
    from montney.server import create_app

    with TestClient(create_app(schedule=False)) as c:
        ids = {row["id"] for row in c.get("/api/catalog").json()}
        status = c.get("/api/status")
        roles = _other_roles(pg_conn)
    return ids, status, roles


def _other_roles(pg_conn) -> set[str]:
    """application_name of every OTHER session on this database. The fixture's own connection is the writer role, so it
    must be excluded, or a "no montney-writer" check could never pass and a "has montney-writer" check could never fail."""
    return {r[0] for r in pg_conn.execute(
        "SELECT application_name FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchall()}


def test_role_detection_sees_a_real_writer(pg):
    """The red for the writer check: an extra writer session IS reported, and the fixture's own session is not."""
    from montney.db import connect

    assert "montney-writer" not in _other_roles(pg)
    extra = connect(dbname=pg.info.dbname, role="writer")
    try:
        assert "montney-writer" in _other_roles(pg)
    finally:
        extra.close()


def test_api_reads_postgres_read_only(pg, monkeypatch):
    monkeypatch.setenv("MONTNEY_DB", "postgres")
    ids, status, roles = _app_view(pg)
    assert status.status_code == 200
    assert MARKER in ids, "the API must read the Postgres catalog when MONTNEY_DB=postgres"
    assert "montney-reader" in roles, "the API must hold a read-only (montney_reader) connection"
    assert "montney-writer" not in roles, "serve must not open a writer in postgres mode: the runner is the only writer"
    assert "budget" in status.json() and status.json()["observations_label"] == "stored observations"


def test_contrast_duckdb_default_does_not_see_postgres(pg, monkeypatch):
    """The seeded red / contrast: on the DuckDB default, the same check can't see the Postgres marker."""
    monkeypatch.setenv("MONTNEY_DB", "duckdb")
    ids, status, roles = _app_view(pg)
    assert status.status_code == 200
    assert MARKER not in ids
    assert "montney-reader" not in roles


def test_serve_leaves_the_writer_lock_to_the_runner(pg, monkeypatch):
    """With the runner (#85) as the only writer, serve must NOT take the writer lock, or `montney run` started after
    serve would fail with WriterBusy. While the app is up, a writer (here cli._store, standing in for the runner) must
    get the lock. Seeded red: if serve still took the lock (as #81 did), _store() would exit with "dashboard is running"."""
    from montney.cli import _store
    from montney.server import create_app

    monkeypatch.setenv("MONTNEY_DB", "postgres")
    with TestClient(create_app(schedule=False)):
        st = _store()                   # must not SystemExit: nobody else holds the lock
        try:
            assert hasattr(st, "w")
        finally:
            st.close()


def test_refresh_is_queued_for_the_runner(pg, monkeypatch):
    """POST /api/refresh in postgres mode inserts into refresh_request (the reader role's one write) and returns 202;
    an unknown source is a 404 and queues nothing."""
    from montney import connectors
    from montney.server import create_app

    monkeypatch.setenv("MONTNEY_DB", "postgres")
    known = sorted(connectors.load_specs())[0]
    with TestClient(create_app(schedule=False)) as c:
        r = c.post(f"/api/refresh/{known}")
        bad = c.post("/api/refresh/no_such_source")
    assert r.status_code == 202 and r.json() == {"queued": True, "source": known}
    assert bad.status_code == 404
    rows = pg.execute("SELECT source FROM montney.refresh_request").fetchall()
    assert rows == [(known,)], rows


def test_status_reports_the_runner_heartbeat(pg, monkeypatch):
    """/api/status carries the runner's heartbeat state: no row → down; a fresh beat → running (#90)."""
    from montney.server import create_app

    monkeypatch.setenv("MONTNEY_DB", "postgres")
    with TestClient(create_app(schedule=False)) as c:
        assert c.get("/api/status").json()["runner"]["state"] == "down"
        pg.execute("INSERT INTO montney.runner_state (id, last_beat, started_at, pid) "
                   "VALUES (1, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', 4242)")
        r = c.get("/api/status").json()["runner"]
    assert r["state"] == "running" and r["pid"] == 4242


def test_second_cli_writer_is_told_about_the_runner(pg, monkeypatch):
    """While another writer (the runner) holds the lock, cli._store() exits naming the runner, not the dashboard."""
    from montney.cli import _store

    monkeypatch.setenv("MONTNEY_DB", "postgres")
    first = _store()                    # stands in for `montney run`, which holds the writer lock
    try:
        with pytest.raises(SystemExit) as e:
            _store()
        assert "montney run" in str(e.value)
    finally:
        first.close()


def test_snapshot_cache_follows_the_runner_writes(pg, monkeypatch):
    """L-081: the reader-role API's snapshot is cached per data_version. A fetch_run that WROTE (the runner logs it after
    its writes) moves the ETag; one that wrote nothing doesn't. A runner coming up flips the cached status at once."""
    from montney.server import create_app

    monkeypatch.setenv("MONTNEY_DB", "postgres")
    src = pg.execute("SELECT id FROM montney.source WHERE code = 'port_step3'").fetchone()[0]
    run = lambda n: pg.execute("INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written) VALUES "
                               "(%s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', true, %s)", (src, n))
    with TestClient(create_app(schedule=False)) as c:
        tag = c.get("/api/snapshot").headers["etag"]
        run(0)
        assert c.get("/api/snapshot", headers={"If-None-Match": tag}).status_code == 304
        run(5)
        r = c.get("/api/snapshot", headers={"If-None-Match": tag})
        assert r.status_code == 200 and r.headers["etag"] != tag
        assert c.get("/api/status").json()["runner"]["state"] == "down"
        pg.execute("INSERT INTO montney.runner_state (id, last_beat, started_at, pid) "
                   "VALUES (1, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', 4242)")
        assert c.get("/api/status").json()["runner"]["state"] == "running"


def test_status_follows_every_run_even_one_that_wrote_nothing(pg, monkeypatch):
    """#312 review: /api/status shows each run's outcome, so a failed manual refresh (n_written = 0) moves it at once."""
    from montney.server import create_app

    monkeypatch.setenv("MONTNEY_DB", "postgres")
    src = pg.execute("SELECT id FROM montney.source WHERE code = 'port_step3'").fetchone()[0]
    with TestClient(create_app(schedule=False)) as c:
        tag = c.get("/api/status").headers["etag"]
        pg.execute("INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written, error) VALUES "
                   "(%s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', false, 0, 'HTTP 503')", (src,))
        assert c.get("/api/status", headers={"If-None-Match": tag}).status_code == 200
        snap = c.get("/api/snapshot").headers["etag"]
        pg.execute("INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written, error) VALUES "
                   "(%s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', false, 0, 'HTTP 503')", (src,))
        assert c.get("/api/snapshot", headers={"If-None-Match": snap}).status_code == 304   # no data: no rebuild
