"""L-080: the catalogue is seeded with the spec's retention tier, and an EMPTY series' tier can move.

After #303 the whole `weather` source failed: `wx.north_bay.cdd: tier is fixed at 'daily'; moving it to 'forecast'
needs a migration`. seed_catalog had registered the declared series with the freq-derived tier (daily), and the
first write wanted the spec's (forecast). Two fixes, each tested:
1. seed_catalog applies spec_tier on Postgres (as resolve_tiers does at write time); events/grid series dropped.
2. upsert_series moves the tier of a series with NO rows in its old tier's table (which self-heals the series
   already mis-seeded on Erik's machine). Seeded red: with rows it still refuses.
"""

from __future__ import annotations

import pytest

from montney import runner
from montney.db.testing import fake_spec

SPEC = fake_spec(series={"test.fake.fc": "D", "test.fake.mw": "5min"},
                 retention={"default": "fast", "test.fake.fc": "forecast"})


class PostgresStore:                       # the name is what runner._is_postgres checks; records what is seeded
    def __init__(self):
        self.metas = []

    def upsert_meta(self, metas):
        self.metas += list(metas)


class Store(PostgresStore):                # the DuckDB code default: a different class name
    pass


def test_seed_catalog_uses_the_spec_tier_on_postgres():
    st = PostgresStore()
    runner.seed_catalog(st, {SPEC["id"]: SPEC})
    tiers = {m.series_id: (m.extra or {}).get("tier") for m in st.metas}
    print(tiers)
    assert tiers == {"test.fake.fc": "forecast", "test.fake.mw": "fast"}       # not the freq-derived daily


def test_seed_catalog_drops_events_series_and_leaves_duckdb_alone():
    st = PostgresStore()
    runner.seed_catalog(st, {"ev": fake_spec(sid="ev", retention="events")})
    assert st.metas == []
    duck = Store()
    runner.seed_catalog(duck, {SPEC["id"]: SPEC})
    assert all("tier" not in (m.extra or {}) for m in duck.metas)              # DuckDB has no tiers


# ------------------------------------------------------------------ the database (CI / local CI)
def test_an_empty_series_moves_tier_and_one_with_rows_does_not():
    from datetime import datetime

    from montney.db.store import Obs, PgStore
    from montney.db.testing import scratch_db

    with scratch_db() as conn:
        st = PgStore(conn)
        st.ensure_source("test_src")
        st.upsert_series("test.tier.empty", source="test_src", tier="daily")
        st.upsert_series("test.tier.empty", source="test_src", tier="forecast")      # no rows: it moves
        assert conn.execute("SELECT tier FROM montney.series WHERE key = 'test.tier.empty'").fetchone()[0] == "forecast"
        st.upsert_series("test.tier.full", source="test_src", tier="daily")
        st.write([Obs("test.tier.full", datetime(2026, 11, 1), 1.0, datetime(2026, 11, 2))])
        with pytest.raises(ValueError, match="tier is fixed at 'daily'"):           # seeded red: rows exist
            st.upsert_series("test.tier.full", source="test_src", tier="forecast")


def test_seed_then_first_write_and_a_mis_seeded_series_heals():
    """End to end: the runner seeds then writes (a declared daily-freq series pinned forecast); and a series
    already mis-seeded `daily` (as on Erik's machine before this fix) heals on the next run."""
    from montney.db.pgstore import PostgresStore as PG
    from montney.db.store import PgStore
    from montney.db.testing import scratch_db

    with scratch_db() as conn:
        store = PG(conn)
        runner.seed_catalog(store, {SPEC["id"]: SPEC})
        tiers = dict(conn.execute("SELECT key, tier FROM montney.series").fetchall())
        assert tiers == {"test.fake.fc": "forecast", "test.fake.mw": "fast"}
        r = runner.PgRunner(store, specs={SPEC["id"]: SPEC})
        assert r.run_now(SPEC["id"])["ok"]
    with scratch_db() as conn:
        PgStore(conn).ensure_source("test_fake")
        PgStore(conn).upsert_series("test.fake.fc", source="test_fake", tier="daily")   # the old mis-seeding
        res = runner.PgRunner(PG(conn), specs={SPEC["id"]: SPEC}).run_now(SPEC["id"])
        print(res)
        assert res["ok"], res
        assert conn.execute("SELECT tier FROM montney.series WHERE key = 'test.fake.fc'").fetchone()[0] == "forecast"
