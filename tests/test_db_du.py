"""`montney du` and the budget state (docs/db/RETENTION.md "Budget accounting", L-003, L-029).

Byte attribution is checked for conservation (every chunk byte lands somewhere, nothing twice) and split by
row share; the state machine (warn 80 %, blocked 100 %, unblock < 95 %) is checked both as a function and
end to end on a real database, with seeded reds (no hysteresis = it flaps).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from montney.db import du
from montney.db.testing import pause_all_jobs, scratch_db

DAY = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)


# ------------------------------------------------------------------ the state machine (no database)
@pytest.mark.parametrize("prev, pct, want", [
    ("ok", 10, "ok"), ("ok", 79.9, "ok"), ("ok", 80, "warn"), ("warn", 99.9, "warn"),
    ("warn", 100, "blocked"), ("ok", 140, "blocked"),
    ("blocked", 97, "blocked"),          # hysteresis: stays blocked until < 95 %
    ("blocked", 94.9, "warn"), ("blocked", 50, "ok"),
    ("warn", 97, "warn"),                # never blocked below 100 % unless it already was
])
def test_next_state(prev, pct, want):
    assert du.next_state(prev, pct, 80) == want


def test_next_state_red_without_hysteresis(monkeypatch):
    """Seeded red: with UNBLOCK_PCT at 100 the state flaps blocked -> warn at 97 %, which the table above
    forbids."""
    monkeypatch.setattr(du, "UNBLOCK_PCT", 100.0)
    assert du.next_state("blocked", 97, 80) == "warn"


# ------------------------------------------------------------------ attribution
def _setup(conn, rows_a=300, rows_b=100):
    # sizes are what these tests measure: no refresh or compression may change them mid-test (local CI flake, L-053)
    pause_all_jobs(conn)
    a = conn.execute("INSERT INTO montney.source (code, region_id) VALUES ('src_a', 1) RETURNING id").fetchone()[0]
    b = conn.execute("INSERT INTO montney.source (code, region_id) VALUES ('src_b', 2) RETURNING id").fetchone()[0]
    sa = conn.execute("INSERT INTO montney.series (key, source_id, region_id, tier) VALUES "
                      "('test.du.a', %s, 1, 'fast') RETURNING id", (a,)).fetchone()[0]
    sb = conn.execute("INSERT INTO montney.series (key, source_id, region_id, tier) VALUES "
                      "('test.du.b', %s, 2, 'fast') RETURNING id", (b,)).fetchone()[0]
    with conn.cursor() as cur:
        for sid, n in ((sa, rows_a), (sb, rows_b)):
            cur.executemany("INSERT INTO montney.obs_fast (series_id, ts, as_of, value, fetched_at) VALUES (%s, %s, %s, %s, %s)",
                            [(sid, DAY + timedelta(minutes=i), DAY + timedelta(minutes=i), float(i), DAY) for i in range(n)])
    return a, b


def _item(rep, item, source_id):
    return next((i for i in rep.items if i.item == item and i.source_id == source_id), None)


def test_chunk_bytes_are_split_by_rows_and_conserved(tmp_path):
    with scratch_db() as conn:
        a, b = _setup(conn)
        chunks = du._chunks(conn, "montney", "obs_fast")
        total = sum(size for _, size in chunks)
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path / "nope")
        ia, ib = _item(rep, "obs_fast", a), _item(rep, "obs_fast", b)
        print(f"obs_fast {total} B over {len(chunks)} chunk(s): a {ia.bytes} B / {ia.rows} rows, b {ib.bytes} B / {ib.rows} rows")
        assert (ia.rows, ib.rows) == (300, 100)
        assert ia.bytes + ib.bytes == total > 0                      # conserved: nothing lost, nothing twice
        assert abs(ia.bytes - 3 * ib.bytes) <= 3                     # 3:1 by rows
        ab, rest = rep.regions[0], rep.regions[1]
        assert ab.code == "ab_nebc" and rest.code == "rest"
        assert ab.used >= ia.bytes and rest.used >= ib.bytes
        assert rep.system_bytes > 0                                  # catalogue, empty roots


def test_compressed_chunks_are_counted_and_recounted(tmp_path):
    with scratch_db() as conn:
        a, _ = _setup(conn)
        before = _item(du.compute(conn, home=tmp_path, backup_dir=tmp_path), "obs_fast", a)
        for (c,) in conn.execute("SELECT show_chunks('montney.obs_fast')").fetchall():
            conn.execute("SELECT compress_chunk(%s::regclass)", (c,))
        after = _item(du.compute(conn, home=tmp_path, backup_dir=tmp_path), "obs_fast", a)
        print(f"src_a obs_fast: {before.bytes} B row store -> {after.bytes} B columnstore")
        assert after.rows == before.rows == 300
        assert 0 < after.bytes != before.bytes                       # recounted after the size changed


def test_files_count_toward_ab_nebc_and_backups_are_reported_not_counted(tmp_path, monkeypatch):
    (tmp_path / "grids").mkdir()
    (tmp_path / "grids" / "t2m.json.gz").write_bytes(b"x" * 1000)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "montney.dump").write_bytes(b"y" * 5000)
    monkeypatch.setattr(du, "BACKUP_WARN_BYTES", 4000)
    with scratch_db() as conn:
        rep = du.compute(conn, home=tmp_path, backup_dir=backups)
        grids = _item(rep, "files:grids", None)
        assert grids is not None and grids.bytes == 1000 and grids.region_id == 1
        assert rep.backups["bytes"] == 5000
        assert all(i.item != "backups" for i in rep.items)           # not counted in any region
        assert any("backups use" in w for w in rep.warnings)


# ------------------------------------------------------------------ the guard, end to end
def _set_pct(conn, rep_used: int, pct: float) -> None:
    conn.execute("UPDATE montney.region SET budget_bytes = %s WHERE code = 'ab_nebc'", (int(rep_used * 100 / pct),))


@pytest.mark.parametrize("hysteresis", [True, False], ids=["hysteresis", "RED-no-hysteresis"])
def test_budget_guard_blocks_writes_and_unblocks_with_hysteresis(hysteresis, tmp_path, monkeypatch):
    if not hysteresis:
        monkeypatch.setattr(du, "UNBLOCK_PCT", 100.0)
    with scratch_db() as conn:
        a, _ = _setup(conn)
        used = du.compute(conn, home=tmp_path, backup_dir=tmp_path).regions[0].used
        seen = []
        for pct in (50, 85, 120, 97, 90, 40):
            _set_pct(conn, used, pct)
            rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
            du.apply_states(conn, rep)
            seen.append((pct, rep.regions[0].state, "src_a" in du.blocked_sources(conn)))
        print(f"hysteresis={hysteresis}: {seen}")
        want = [(50, "ok", False), (85, "warn", False), (120, "blocked", True),
                (97, "blocked", True), (90, "warn", False), (40, "ok", False)]
        if hysteresis:
            assert seen == want
        else:   # seeded red: at 97 % it flaps back to warn
            assert seen[3] == (97, "warn", False)
        # the other region's source is never touched by ab_nebc's state
        assert conn.execute("SELECT budget_state FROM montney.source WHERE code = 'src_b'").fetchone()[0] == "ok"


def test_growth_and_days_to_full_from_snapshots(tmp_path):
    with scratch_db() as conn:
        _setup(conn)
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        conn.execute("INSERT INTO montney.usage_snapshot (taken_at, region_id, source_id, item, rows, bytes) "
                     "VALUES (%s, 1, NULL, 'obs_fast', 0, 0)", (rep.taken_at - timedelta(days=8),))
        rep2 = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        r = rep2.regions[0]
        print(f"growth {r.growth_per_day:.0f} B/d, full in {r.days_to_full:.0f} d")
        assert r.growth_per_day == pytest.approx(r.used / 8, rel=0.01)   # 0 B eight days ago
        assert r.days_to_full == pytest.approx((r.budget - r.used) / r.growth_per_day)
        du.save(conn, rep2)
        assert conn.execute("SELECT count(*) FROM montney.usage_snapshot WHERE taken_at = %s",
                            (rep2.taken_at,)).fetchone()[0] == len(rep2.items) + 1


@pytest.mark.parametrize("total_budget", ["tiny", "real"], ids=["total-over", "RED-no-total-pressure"])
def test_the_100_gb_total_blocks_every_region(total_budget, tmp_path, monkeypatch):
    """Reviewer (#61): a guard on the TOTAL (regions + _system), not only per region. With the total over its
    budget, every source is blocked even though each region is far below its own share."""
    with scratch_db() as conn:
        a, b = _setup(conn)
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        if total_budget == "tiny":
            monkeypatch.setattr(du, "TOTAL_BUDGET", int(rep.total_used / 1.2))   # total at 120 %
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        states = du.apply_states(conn, rep)
        print(total_budget, f"{rep.total_pct:.1f} %", rep.total_state, [r.state for r in rep.regions], states)
        assert all(r.state == "ok" for r in rep.regions)                    # no region over its own budget
        if total_budget == "tiny":
            assert rep.total_state == "blocked" and set(states.values()) == {"blocked"}
            assert du.blocked_sources(conn) == {"src_a", "src_b"}
        else:   # seeded red: without total pressure nothing is blocked
            assert rep.total_state == "ok" and du.blocked_sources(conn) == set()


def test_total_guard_keeps_its_own_state_not_any_blocked_region(tmp_path, monkeypatch):
    """Reviewer (#85): one region blocked + a total at 97 % must NOT block the other region (the total was
    never blocked); with the total's OWN previous state 'blocked', 97 % keeps it blocked (hysteresis)."""
    with scratch_db() as conn:
        a, b = _setup(conn)
        used = du.compute(conn, home=tmp_path, backup_dir=tmp_path).regions[0].used
        _set_pct(conn, used, 120)                                           # ab_nebc over its own budget
        first = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        monkeypatch.setattr(du, "TOTAL_BUDGET", int(first.total_used / 0.97))   # total at 97 %
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        du.apply_states(conn, rep)
        states = dict(conn.execute("SELECT code, budget_state FROM montney.source").fetchall())
        print("own state ok:", rep.total_state, states)
        assert rep.regions[0].state == "blocked" and rep.total_state == "warn"
        assert states == {"src_a": "blocked", "src_b": "warn"}             # the other region is NOT blocked
        conn.execute("UPDATE montney.budget_guard SET state = 'blocked' WHERE scope = 'total'")
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        du.apply_states(conn, rep)
        print("own state blocked:", rep.total_state)
        assert rep.total_state == "blocked"                                 # hysteresis on its own state
        assert du.blocked_sources(conn) == {"src_a", "src_b"}


# ------------------------------------------------------------------ apply_states locking (reviewer, #91)
def _xmins(conn) -> dict:
    return dict(conn.execute("SELECT code, xmin::text FROM montney.source ORDER BY code").fetchall())


def test_apply_states_rewrites_only_changed_rows(tmp_path):
    """Rows whose budget state doesn't change are not rewritten (their xmin stays). Seeded red: the old blanket
    UPDATE rewrites every row, and the same check sees it."""
    with scratch_db() as conn:
        _setup(conn)
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        du.apply_states(conn, rep)
        before = _xmins(conn)
        du.apply_states(conn, rep)                                    # nothing changes
        assert _xmins(conn) == before
        used = rep.regions[0].used
        _set_pct(conn, used, 85)                                      # ab_nebc -> warn; rest unchanged
        du.apply_states(conn, du.compute(conn, home=tmp_path, backup_dir=tmp_path))
        after = _xmins(conn)
        assert after["src_a"] != before["src_a"] and after["src_b"] == before["src_b"]
        # Seeded red: the pre-#91 loop (one blanket UPDATE per region) rewrites src_b although its state is the same.
        rep = du.compute(conn, home=tmp_path, backup_dir=tmp_path)
        for r in rep.regions:
            conn.execute("UPDATE montney.source SET budget_state = %s WHERE region_id = %s",
                         (du.worst(r.state, rep.total_state), r.id))
        red = _xmins(conn)
        print("xmin before/after/old-loop:", before, after, red)
        assert red["src_b"] != after["src_b"]


def test_upsert_meta_retries_a_lost_lock_fight(monkeypatch):
    import psycopg

    from montney.db.pgstore import PostgresStore
    from montney.store import SeriesMeta

    with scratch_db() as conn:
        ps = PostgresStore(conn)
        real, calls = ps._upsert_meta, {"n": 0}

        def flaky(metas):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.errors.DeadlockDetected("injected")
            return real(metas)

        monkeypatch.setattr(ps, "_upsert_meta", flaky)
        ps.upsert_meta([SeriesMeta("test.lock.a", "src_lock", freq="5min")])
        assert calls["n"] == 2
        assert conn.execute("SELECT count(*) FROM montney.series WHERE key = 'test.lock.a'").fetchone()[0] == 1


STRESS_ROUNDS = int(os.environ.get("MONTNEY_DB_STRESS_ROUNDS", "40"))


def test_apply_states_and_runner_catalog_writes_coexist(tmp_path):
    """Regression guard (not a reproduced deadlock): du's apply_states on its own connection, flipping budgets,
    against the runner's upsert_meta/log_fetch on another, STRESS_ROUNDS rounds each. No lock errors."""
    import threading
    from datetime import datetime

    from montney.db import connect
    from montney.db.pgstore import PostgresStore
    from montney.store import SeriesMeta

    with scratch_db() as conn:
        _setup(conn)
        errors = []

        def du_side():
            c = connect(dbname=conn.info.dbname)
            try:
                for i in range(STRESS_ROUNDS):
                    rep = du.compute(c, home=tmp_path, backup_dir=tmp_path)
                    c.execute("UPDATE montney.region SET budget_bytes = %s WHERE code = 'ab_nebc'",
                              (10**12 if i % 2 else max(1, rep.regions[0].used // 2),))
                    du.apply_states(c, du.compute(c, home=tmp_path, backup_dir=tmp_path))
            except Exception as e:                                       # noqa: BLE001
                errors.append(f"du: {type(e).__name__}: {e}")
            finally:
                c.close()

        t = threading.Thread(target=du_side)
        t.start()
        ps = PostgresStore(connect(dbname=conn.info.dbname))
        try:
            for i in range(STRESS_ROUNDS):
                ps.upsert_meta([SeriesMeta(f"test.stress.s{i % 7}", f"src_s{i % 5}", freq="5min"),
                                SeriesMeta(f"test.stress.t{i % 3}", "src_a", freq="1h")])
                ps.log_fetch(f"src_s{i % 5}", datetime.utcnow(), True, 1, None)
        except Exception as e:                                           # noqa: BLE001
            errors.append(f"runner: {type(e).__name__}: {e}")
        finally:
            ps.close()
        t.join(timeout=120)
        print(f"{STRESS_ROUNDS} rounds each side, errors: {errors}")
        assert not t.is_alive() and not errors, errors
