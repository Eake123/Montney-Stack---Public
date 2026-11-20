"""Migrations and the frozen vocabularies (docs/db/INSTALL.md "Migrations", docs/db/ERD.md, L-033).

DB tests run against a scratch database per test (montney.db.testing). In CI MONTNEY_DB_REQUIRED=1
turns "no database" into a failure.
"""

from __future__ import annotations

import shutil

import pytest

from montney.db import check, migrate
from montney.db.testing import scratch_db


# ------------------------------------------------------------------ no database needed
def test_discover_finds_numbered_files_in_order():
    ms = migrate.discover()
    assert [m.version for m in ms] == sorted(m.version for m in ms)
    assert ms[0].version == 1 and ms[0].name == "core"
    print(f"{len(ms)} migrations: {[m.path.name for m in ms]}")


def test_discover_rejects_bad_names_and_duplicates(tmp_path):
    (tmp_path / "0001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "2_bad.sql").write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="NNNN_"):
        migrate.discover(tmp_path)
    (tmp_path / "2_bad.sql").unlink()
    (tmp_path / "0001_again.sql").write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.discover(tmp_path)


def test_checksum_ignores_line_endings(tmp_path):
    """A Windows checkout (CRLF) must hash the same as Linux, or every applied file would look changed."""
    (tmp_path / "0001_a.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    lf = migrate.discover(tmp_path)[0].sha256
    (tmp_path / "0001_a.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    assert migrate.discover(tmp_path)[0].sha256 == lf


# ------------------------------------------------------------------ database
def test_apply_from_empty_then_idempotent():
    with scratch_db(migrations=None) as conn:
        done = migrate.apply(conn)
        assert done == [m.path.name for m in migrate.discover()]
        assert all(r["state"] == "applied" for r in migrate.status(conn))
        assert migrate.apply(conn) == []          # second run: nothing to do
        hts = {r[0] for r in conn.execute(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'montney'").fetchall()}
        assert hts >= {"obs_fast", "obs_hourly", "obs_daily", "obs_forecast", "raw_blob"}, hts
        caggs = {r[0] for r in conn.execute(
            "SELECT view_name FROM timescaledb_information.continuous_aggregates "
            "WHERE view_schema = 'montney'").fetchall()}
        assert caggs == {"obs_fast_1h", "obs_fast_1d", "obs_hourly_1d"}, caggs
        print(f"applied {done}; hypertables {sorted(hts)}; rollups {sorted(caggs)}")


def test_changed_applied_migration_is_a_hard_error(tmp_path):
    d = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, d)
    with scratch_db(migrations=d) as conn:
        p = d / "0001_core.sql"
        p.write_text(p.read_text(encoding="utf-8") + "\n-- edited after apply\n", encoding="utf-8")
        with pytest.raises(migrate.MigrationError, match="changed after it was applied"):
            migrate.status(conn, d)


def test_broken_migration_rolls_back_and_leaves_ledger_unchanged(tmp_path):
    """Seeded red for `db init`: a failing file must leave nothing behind, not even its first statement."""
    d = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, d)
    n = max(m.version for m in migrate.discover(d)) + 1
    (d / f"{n:04d}_broken.sql").write_text(
        "CREATE TABLE montney.half_done (x integer);\nSELECT this_function_does_not_exist();\n")
    with scratch_db(migrations=None) as conn:
        with pytest.raises(migrate.MigrationError, match="broken.sql failed"):
            migrate.apply(conn, d)
        versions = [r[0] for r in conn.execute("SELECT version FROM montney.schema_migration").fetchall()]
        assert n not in versions and len(versions) == n - 1, versions
        assert conn.execute("SELECT to_regclass('montney.half_done')").fetchone()[0] is None


VOCAB_CASES = [
    # (sql, should_succeed)
    ("INSERT INTO montney.entity (type, key) VALUES ('gauge', 'gauge.tau_001')", True),
    ("INSERT INTO montney.entity (type, key) VALUES ('gauge', 'gauge.tau-001')", False),     # hyphen
    ("INSERT INTO montney.entity (type, key) VALUES ('gauge', 'Gauge.x')", False),           # case
    ("INSERT INTO montney.entity (type, key) VALUES ('unit', 'aeso.asset.egc1')", False),    # not a type
    ("INSERT INTO montney.entity_alias (system, alias, entity_id) VALUES ('ab_station', 'TAU-001', $1)", True),
    ("INSERT INTO montney.entity_alias (system, alias, entity_id) VALUES ('ngtl_station', '31127', $1)", False),
    ("INSERT INTO montney.entity_link (from_id, to_id, kind) VALUES ($1, $2, 'measured_by')", True),
    ("INSERT INTO montney.entity_link (from_id, to_id, kind) VALUES ($1, $2, 'supplies_gas_to')", False),
    ("INSERT INTO montney.entity_link (from_id, to_id, kind, confidence) VALUES ($1, $2, 'member_of', 1.5)", False),
]


def test_frozen_vocabularies_are_enforced():
    import psycopg

    with scratch_db() as conn:
        a = conn.execute("INSERT INTO montney.entity (type, key) VALUES ('dam', 'dam.ghost') RETURNING id").fetchone()[0]
        b = conn.execute("INSERT INTO montney.entity (type, key) VALUES ('gauge', 'gauge.05be005') RETURNING id").fetchone()[0]
        results = []
        for sql, want in VOCAB_CASES:
            params = [p for p in (a, b)][: sql.count("$")]
            try:
                with conn.transaction():
                    conn.execute(sql.replace("$1", "%s").replace("$2", "%s"), params)
                    raise _Rollback
            except _Rollback:
                got = True
            except psycopg.errors.CheckViolation:
                got = False
            results.append((sql, want, got))
        bad = [r for r in results if r[1] != r[2]]
        print(f"{len(results)} vocabulary cases, {len(bad)} wrong")
        assert len(results) == len(VOCAB_CASES) and not bad, bad


class _Rollback(Exception):
    pass


def test_db_check_passes_on_a_migrated_database():
    with scratch_db() as conn:
        results = check.run(conn)
        for r in results:
            print(f"{'PASS' if r.ok else 'FAIL'} {r.name}: {r.detail}")
        names = {r.name for r in results}
        assert {"connect", "postgres", "timescaledb", "license", "migrations", "retention", "flags", "thinning", "smoke"} <= names
        assert all(r.ok for r in results), [r for r in results if not r.ok]


def test_db_check_fails_closed_without_a_connection(monkeypatch):
    monkeypatch.setenv("MONTNEY_DB_PORT", "1")          # nothing listens there
    monkeypatch.setenv("MONTNEY_DB_PASSWORD", "x")
    results = check.run()
    assert results and not all(r.ok for r in results)
    assert results[0].name == "connect" and not results[0].ok
