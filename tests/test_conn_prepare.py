"""L-102: montney/db connections never auto-prepare. A prepared PgStore.series query flips to a generic plan that spills
to disk (snapshot builds 1 s -> 37 s from the 3rd build on). No database needed: psycopg.connect is faked."""

from __future__ import annotations

import pytest

from montney.db import conn as dbconn


class _FakeConn:
    prepare_threshold = 5                      # psycopg's default


@pytest.mark.parametrize("role", ["writer", "reader"])
def test_connect_turns_auto_prepare_off(monkeypatch, role):
    import psycopg

    seen = {}

    def fake_connect(**kw):
        seen.update(kw)
        return _FakeConn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(dbconn, "conninfo", lambda dbname, role: {"dbname": dbname or "montney", "user": role})
    c = dbconn.connect(role=role)
    assert c.prepare_threshold is None, "auto-prepare must be off (L-102)"      # seeded red: drop the line -> 5
    assert seen["autocommit"] is True
