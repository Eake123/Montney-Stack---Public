"""The frozen vocabularies have one source (montney/db/vocab.py); the SQL CHECK lists must match it.

Seeded reds: a tampered copy of the migration (an extra alias system, a dropped entity type, a hyphen
allowed in the key grammar) must be caught, and so must an unknown value at insert time.
"""

from __future__ import annotations

import pytest

from montney.db import migrate, vocab
from montney.db.testing import scratch_db


def _core_sql() -> str:
    return next(m.sql for m in migrate.discover() if m.version == 1)


def test_sql_check_lists_match_vocab():
    found = vocab.sql_check_lists(_core_sql())
    print({f"{t}.{c}": len(v) for (t, c), v in found.items()})
    assert vocab.sql_mismatches(_core_sql()) == []
    assert (len(vocab.ENTITY_TYPES), len(vocab.ALIAS_SYSTEMS), len(vocab.LINK_KINDS)) == (15, 21, 8)


@pytest.mark.parametrize("tamper, needle", [
    (lambda s: s.replace("'usgs_site', 'nhn'", "'usgs_site', 'nhn', 'ngtl_station'"), "entity_alias.system"),
    (lambda s: s.replace("'river', 'reach'", "'river'"), "entity.type"),
    (lambda s: s.replace("'derived_from'", "'derived_from', 'supplies_gas_to'"), "entity_link.kind"),
    (lambda s: s.replace(vocab.KEY_RE, r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$"), "key grammar"),
])
def test_vocab_check_catches_a_drifted_migration(tamper, needle):
    sql = _core_sql()
    tampered = tamper(sql)
    assert tampered != sql, "tamper did not apply: the seeded red would test nothing"
    assert any(needle in p for p in vocab.sql_mismatches(tampered))


def test_unknown_values_are_rejected_by_the_database():
    import psycopg

    bad = [
        ("INSERT INTO montney.entity (type, key) VALUES ('gauge', 'gauge.tau-001')", ()),
        ("INSERT INTO montney.entity (type, key) VALUES ('ngtl_station', 'ngtl.station.31127')", ()),
        ("INSERT INTO montney.entity_alias (system, alias, entity_id) VALUES ('petrinex_ms', 'ABMS0001', %s)", 1),
        ("INSERT INTO montney.entity_link (from_id, to_id, kind) VALUES (%s, %s, 'part_of')", 2),
        ("INSERT INTO montney.event (id, source_id, kind, title, first_seen, updated) "
         "VALUES ('x:1', %s, 'cruise', 't', now(), now())", "src"),
        # series.key follows the same grammar as entity.key (L-033): no hyphens, no capitals
        ("INSERT INTO montney.series (key, source_id, tier) VALUES ('abrivers.tau-001.outflow', %s, 'hourly')", "src"),
        ("INSERT INTO montney.series (key, source_id, tier) VALUES ('aeso.unit.EGC1.tng', %s, 'fast')", "src"),
    ]
    with scratch_db() as conn:
        a = conn.execute("INSERT INTO montney.entity (type, key) VALUES ('gauge', 'gauge.tau_001') RETURNING id").fetchone()[0]
        b = conn.execute("INSERT INTO montney.entity (type, key) VALUES ('dam', 'dam.bighorn') RETURNING id").fetchone()[0]
        src = conn.execute("INSERT INTO montney.source (code) VALUES ('t') RETURNING id").fetchone()[0]
        rejected = 0
        for sql, n in bad:
            params = {(): (), 1: (a,), 2: (a, b), "src": (src,)}[n]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(sql, params)
            rejected += 1
        # the good twins of the bad rows go in
        conn.execute("INSERT INTO montney.entity_alias (system, alias, entity_id) VALUES ('ab_station', 'TAU-001', %s)", (a,))
        conn.execute("INSERT INTO montney.entity_link (from_id, to_id, kind) VALUES (%s, %s, 'measured_by')", (b, a))
        conn.execute("INSERT INTO montney.series (key, source_id, tier) VALUES ('abrivers.tau_001.outflow', %s, 'hourly')", (src,))
        print(f"{rejected} unknown values rejected")
        assert rejected == len(bad)
