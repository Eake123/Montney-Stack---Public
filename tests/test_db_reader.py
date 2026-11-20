"""The read-only API role `montney_reader` (L-035 Q-P2): SELECT everywhere in the schema, nothing else.

`montney db init` grants it after every migration run (montney/db/migrate.py ensure_reader_grants), and the
schema's default privileges cover tables created later. Seeded red: without the grants it can't read.
"""

from __future__ import annotations

import pytest

from montney.db import migrate
from montney.db.testing import admin_connection, scratch_db

READER_PW = "ci-reader-throwaway"


@pytest.fixture
def reader_role(monkeypatch):
    admin = admin_connection()
    try:
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'montney_reader'").fetchone()
        if not exists:
            admin.execute(f"CREATE ROLE montney_reader LOGIN PASSWORD '{READER_PW}'")
        else:
            admin.execute(f"ALTER ROLE montney_reader PASSWORD '{READER_PW}'")
    finally:
        admin.close()
    monkeypatch.setenv("MONTNEY_DB_READER_PASSWORD", READER_PW)


def test_reader_can_select_and_nothing_else(reader_role):
    import psycopg

    from montney.db import connect
    from montney.db.pgstore import PostgresStore

    with scratch_db() as conn:                                  # migrate.apply() grants the reader
        conn.execute("INSERT INTO montney.source (code) VALUES ('t')")
        conn.execute("CREATE TABLE montney.added_later (x integer)")   # default privileges cover it
        r = connect(dbname=conn.info.dbname, role="reader")
        try:
            assert r.info.user == "montney_reader"
            assert r.execute("SELECT count(*) FROM montney.source").fetchone()[0] == 1
            assert r.execute("SELECT count(*) FROM montney.observation").fetchone()[0] == 0
            assert r.execute("SELECT count(*) FROM montney.obs_fast_1h").fetchone()[0] == 0
            assert r.execute("SELECT count(*) FROM montney.added_later").fetchone()[0] == 0
            r.execute("INSERT INTO montney.refresh_request (source) VALUES ('ngtl_gdsr')")   # its one write (L-035 a)
            assert conn.execute("SELECT source FROM montney.refresh_request").fetchall() == [("ngtl_gdsr",)]
            ps = PostgresStore(r)
            assert ps.catalog() == [] and ps.last_fetch() == {}
            for sql in ("INSERT INTO montney.source (code) VALUES ('x')",
                        "DELETE FROM montney.refresh_request",
                        "UPDATE montney.refresh_request SET source = 'x'",
                        "UPDATE montney.source SET name = 'x'",
                        "DELETE FROM montney.source",
                        "CREATE TABLE montney.nope (x integer)"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    r.execute(sql)
        finally:
            r.close()


def test_RED_without_grants_the_reader_cannot_read(reader_role, tmp_path):
    import psycopg

    from montney.db import connect

    with scratch_db() as conn:
        conn.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA montney FROM montney_reader")
        r = connect(dbname=conn.info.dbname, role="reader")
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                r.execute("SELECT count(*) FROM montney.source")
        finally:
            r.close()
        assert migrate.ensure_reader_grants(conn) is True             # re-granting restores it
        r = connect(dbname=conn.info.dbname, role="reader")
        try:
            assert r.execute("SELECT count(*) FROM montney.source").fetchone()[0] == 0
        finally:
            r.close()
