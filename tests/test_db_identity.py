"""Identity values are not burned by re-registering (2026-11-01: montney.source.id, a smallint identity, hit 32767
after about 6 h of runner fetches, because `INSERT ... ON CONFLICT` calls nextval on every call even when the row
exists). Registering existing sources and series N times must not advance their sequences. Seeded red: the old
statement does.
"""

from __future__ import annotations

from montney.db.pgstore import PostgresStore
from montney.db.store import PgStore
from montney.db.testing import scratch_db

N = 50


def _seq(conn, table: str) -> int:
    seq = conn.execute("SELECT pg_get_serial_sequence(%s, 'id')", (f"montney.{table}",)).fetchone()[0]
    last, called = conn.execute(f"SELECT last_value, is_called FROM {seq}").fetchone()
    return last if called else last - 1


def test_re_registering_existing_sources_and_series_takes_no_identity_values():
    with scratch_db() as conn:
        st, ps = PgStore(conn), PostgresStore(conn)
        st.ensure_source("test_identity")
        st.upsert_series("test.identity.a", source="test_identity", tier="fast", unit="MW")
        src0, ser0 = _seq(conn, "source"), _seq(conn, "series")
        for _ in range(N):
            st.ensure_source("test_identity")                       # the runner's catalogue seeding
            ps._source("test_identity")                             # every write(source=...) / upsert_meta
            st.upsert_series("test.identity.a", source="test_identity", tier="fast", unit="MW")
        src1, ser1 = _seq(conn, "source"), _seq(conn, "series")
        print(f"after {N} x 3 re-registrations: source seq {src0} -> {src1}, series seq {ser0} -> {ser1}")
        assert (src1, ser1) == (src0, ser0)
        # an update still lands (no nextval, and still effective), and a new code still gets an id
        st.upsert_series("test.identity.a", source="test_identity", tier="fast", unit="GW")
        st.ensure_source("test_identity", name="renamed")
        assert conn.execute("SELECT unit FROM montney.series WHERE key = 'test.identity.a'").fetchone()[0] == "GW"
        assert conn.execute("SELECT name FROM montney.source WHERE code = 'test_identity'").fetchone()[0] == "renamed"
        assert _seq(conn, "source") == src0
        new = st.ensure_source("test_identity_new")
        assert new > 0 and _seq(conn, "source") == src0 + 1


def test_RED_the_old_upsert_burns_a_value_per_call():
    """Seeded red: the pre-fix statement on an EXISTING code advances the sequence N times; the check above sees it."""
    with scratch_db() as conn:
        conn.execute("INSERT INTO montney.source (code) VALUES ('test_burn')")
        before = _seq(conn, "source")
        for _ in range(N):
            conn.execute("INSERT INTO montney.source (code) VALUES ('test_burn') ON CONFLICT (code) DO NOTHING")
        after = _seq(conn, "source")
        print(f"old statement, {N} calls on an existing code: source seq {before} -> {after}")
        assert after == before + N
