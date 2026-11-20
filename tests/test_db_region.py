"""L-085/L-090/L-091/L-094/L-095/L-106: a spec's `region:` puts its source (and its series) in that budget region;
migrations 0014-0018 add the 2 GB regions in SMALL below.
Named test_db_* (it runs on local CI's throwaway cluster)."""

from __future__ import annotations

from montney import runner
from montney.db.testing import fake_spec, scratch_db


SMALL = ("pnw", "cal", "ont", "qc", "rockies", "midwest", "upne", "nyc", "bakken", "sunbelt", "tx")


def _region(conn, sql: str, key: str) -> str:
    return conn.execute(f"SELECT r.code FROM {sql} JOIN montney.region r ON r.id = x.region_id WHERE {key}").fetchone()[0]


def test_the_regions_exist_and_the_total_stays_100_gb():
    with scratch_db() as conn:
        regions = dict(conn.execute("SELECT code, budget_bytes FROM montney.region").fetchall())
        assert set(regions) == {"ab_nebc", "rest", *SMALL}
        assert sum(regions.values()) == 100 * 10**9
        assert all(regions[r] == 2 * 10**9 for r in SMALL)
        assert regions["rest"] == 73 * 10**9


def test_every_budget_region_exists():
    """A BUDGET.yaml region the table lacks would never be measured (its sources' `region:` is refused)."""
    from montney.db import reqbudget

    with scratch_db() as conn:
        codes = {c for (c,) in conn.execute("SELECT code FROM montney.region").fetchall()}
        assert set(reqbudget.caps()) <= codes


def test_a_spec_region_moves_the_source_and_its_series_and_new_series_follow():
    from montney.db.pgstore import PostgresStore
    from montney.db.store import PgStore

    with scratch_db() as conn:
        PgStore(conn).ensure_source("test_fake")
        PgStore(conn).upsert_series("test.fake.old", source="test_fake", tier="fast")   # registered under ab_nebc
        spec = fake_spec(series={"test.fake.a": "5min"}, region="pnw")
        runner.seed_catalog(PostgresStore(conn), {spec["id"]: spec})
        assert _region(conn, "montney.source x", "x.code = 'test_fake'") == "pnw"
        assert _region(conn, "montney.series x", "x.key = 'test.fake.old'") == "pnw"          # moved
        assert _region(conn, "montney.series x", "x.key = 'test.fake.a'") == "pnw"            # new: inherits


def test_an_unknown_region_changes_nothing_and_no_region_key_leaves_it_alone(caplog):
    from montney.db.pgstore import PostgresStore

    with scratch_db() as conn:
        spec = fake_spec(series={"test.fake.a": "5min"}, region="mars")
        runner.seed_catalog(PostgresStore(conn), {spec["id"]: spec})
        assert _region(conn, "montney.source x", "x.code = 'test_fake'") == "ab_nebc"
        assert "region 'mars' is not in the region table" in caplog.text
        conn.execute("UPDATE montney.source SET region_id = 3 WHERE code = 'test_fake'")
        spec = fake_spec(series={"test.fake.a": "5min"})                                     # no region: key
        runner.seed_catalog(PostgresStore(conn), {spec["id"]: spec})
        assert _region(conn, "montney.source x", "x.code = 'test_fake'") == "pnw"            # not reset


def test_a_yaml_boolean_region_is_refused_loudly(caplog):
    """`region: on` (or yes/no/off) is a YAML 1.1 boolean, not a code: it must warn by name, never vanish (L-091)."""
    from montney.db.pgstore import PostgresStore

    with scratch_db() as conn:
        spec = fake_spec(series={"test.fake.a": "5min"}, region=True)          # what `region: on` parses to
        runner.seed_catalog(PostgresStore(conn), {spec["id"]: spec})
        assert _region(conn, "montney.source x", "x.code = 'test_fake'") == "ab_nebc"
        assert "is a YAML boolean" in caplog.text and "quote it" in caplog.text


def test_each_new_region_code_maps_through_the_seed():
    """PLAN step 8 (L-106): a spec with `region: <code>` for each new region lands there via seed_catalog ->
    set_source_region (the source and its series)."""
    from montney.db.pgstore import PostgresStore

    with scratch_db() as conn:
        for code in ("upne", "nyc", "bakken", "sunbelt", "tx"):
            spec = fake_spec(sid=f"test_{code}", series={f"test.{code}.a": "5min"}, region=code)
            runner.seed_catalog(PostgresStore(conn), {spec["id"]: spec})
            assert _region(conn, "montney.source x", f"x.code = 'test_{code}'") == code
            assert _region(conn, "montney.series x", f"x.key = 'test.{code}.a'") == code
