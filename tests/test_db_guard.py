"""DB tests never reach a live store (after L-040 put a real Postgres, with the live store, on Erik's machine).

Three gates, each with a seeded red:
1. DB tests run only when asked for (CI's MONTNEY_DB_REQUIRED=1, or MONTNEY_DB_TESTS=1). A reachable server
   alone is not enough: without the opt-in they skip WITHOUT connecting.
2. Under pytest, montney.db.conn refuses any database except `postgres` and the scratch montney_t_* ones, so
   a code path that forgets dbname= fails instead of reaching "montney".
3. A server holding any other database is refused unless declared throwaway (CI's container is).
These run without a database (fakes), so they also run in the plain `test` job and on every laptop.
"""

from __future__ import annotations

import pytest

from montney.db import conn as dbconn
from montney.db import testing


class _FakeAdmin:
    def __init__(self, dbs):
        self.dbs = dbs
        self.closed = False

    def execute(self, sql, params=None):
        assert "pg_database" in sql
        return iter([(d,) for d in self.dbs])

    def close(self):
        self.closed = True


@pytest.fixture
def no_opt_in(monkeypatch):
    for k in ("MONTNEY_DB_REQUIRED", testing.OPT_IN, testing.THROWAWAY):
        monkeypatch.delenv(k, raising=False)


def _patch_connect(monkeypatch, dbs):
    calls = []

    def fake_connect(dbname=None, autocommit=True, role="writer"):
        calls.append(dbname)
        return _FakeAdmin(dbs)

    import montney.db as db

    monkeypatch.setattr(db, "connect", fake_connect)
    return calls


# ------------------------------------------------------------------ 1. opt-in
@pytest.mark.parametrize("opt_in", [False, True], ids=["no-opt-in-skips", "RED-opt-in-connects"])
def test_without_an_opt_in_db_tests_skip_without_connecting(no_opt_in, monkeypatch, opt_in):
    calls = _patch_connect(monkeypatch, [])                   # a server that WOULD answer
    if not opt_in:
        with pytest.raises(pytest.skip.Exception, match="DB tests are off"):
            testing.admin_connection()
        assert calls == []                                    # not even a connection attempt
    else:                                                     # the red: the same path does connect once opted in
        monkeypatch.setenv(testing.OPT_IN, "1")
        testing.admin_connection()
        assert calls == ["postgres"]                          # the maintenance db, never the default


# ------------------------------------------------------------------ 2. only postgres + scratch dbs under pytest
def test_under_pytest_the_live_dbname_is_refused(monkeypatch):
    monkeypatch.delenv("MONTNEY_DB_NAME", raising=False)
    monkeypatch.setenv("MONTNEY_DB_PASSWORD", "x")
    for forgot in (None, "montney", "anything_else"):
        with pytest.raises(dbconn.LiveDatabaseRefused, match="refusing"):
            dbconn.conninfo(forgot)
    monkeypatch.setenv("MONTNEY_DB_NAME", "montney")          # the env default is refused too
    with pytest.raises(dbconn.LiveDatabaseRefused):
        dbconn.conninfo()
    assert dbconn.conninfo("postgres")["dbname"] == "postgres"
    assert dbconn.conninfo("montney_t_0123456789ab")["dbname"] == "montney_t_0123456789ab"
    monkeypatch.setenv("MONTNEY_DB_NAME", "montney_t_abc")    # how test_db_server points the API at scratch
    assert dbconn.conninfo()["dbname"] == "montney_t_abc"


def test_RED_outside_pytest_the_default_dbname_is_allowed(monkeypatch):
    """The guard keys on pytest: the app itself must still reach its own database."""
    import sys

    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv("MONTNEY_DB_NAME", raising=False)
    monkeypatch.setenv("MONTNEY_DB_PASSWORD", "x")
    assert dbconn.conninfo()["dbname"] == "montney"


def test_a_refused_connection_reads_no_secret(monkeypatch):
    from montney import secrets

    def boom(name):
        raise AssertionError(f"secret {name} read for a refused connection")

    monkeypatch.setattr(secrets, "get", boom)
    with pytest.raises(dbconn.LiveDatabaseRefused):
        dbconn.conninfo("montney")


# ------------------------------------------------------------------ 3. a server with a live store is refused
@pytest.mark.parametrize("throwaway", [False, True], ids=["live-server-refused", "RED-declared-throwaway"])
def test_a_server_holding_other_databases_is_refused(no_opt_in, monkeypatch, throwaway):
    monkeypatch.setenv(testing.OPT_IN, "1")
    _patch_connect(monkeypatch, ["montney", "postgres", "montney_t_leftover"])
    if not throwaway:
        with pytest.raises(pytest.fail.Exception, match=r"other databases \['montney'\]"):
            testing.admin_connection()
    else:
        monkeypatch.setenv(testing.THROWAWAY, "1")
        assert isinstance(testing.admin_connection(), _FakeAdmin)


def test_a_bare_test_server_is_accepted(no_opt_in, monkeypatch):
    monkeypatch.setenv(testing.OPT_IN, "1")
    _patch_connect(monkeypatch, ["postgres", "montney_t_x", "montney_restore_y"])
    assert isinstance(testing.admin_connection(), _FakeAdmin)


def test_the_guard_also_holds_at_import_and_collection_time(monkeypatch):
    """Reviewer (#101): PYTEST_CURRENT_TEST is unset while modules import / tests collect; a module-level
    connect must still be refused because pytest is imported."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with pytest.raises(dbconn.LiveDatabaseRefused):
        dbconn.conninfo("montney")
