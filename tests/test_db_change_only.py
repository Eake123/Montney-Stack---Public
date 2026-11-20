"""Change-only writes (docs/db/RETENTION.md "Change-only writes"): one comparator, the latest stored
vintage of (series, ts); filtered in the writer, cache updated only after commit; forecasts re-anchored.

Every case has a seeded red: the same assertions run with the protection switched off
(MONTNEY_DB_DEDUPE=off = v1 behaviour, MONTNEY_DB_REANCHOR_DAYS=off, PgStore.CACHE_AFTER_COMMIT=False)
and must then detect the failure. A count of zero alone can't fail for case 1 (the PK already stops
duplicates), so case 1 checks that the stored row's xmin and fetched_at did not move.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from montney.db.store import Flag, PgStore
from montney.db.testing import scratch_db
from montney.store import Obs

T0 = datetime(2026, 10, 30, 12, 0)
N = 6


def _store(conn) -> PgStore:
    st = PgStore(conn)
    st.ensure_source("test_src", retention="fast")
    st.upsert_series("test.fast.flow", source="test_src", tier="fast", freq="5min")
    st.upsert_series("test.daily.flow", source="test_src", tier="daily", freq="D")
    st.upsert_series("test.fc.wind", source="test_src", tier="forecast", freq="1h")
    return st


def _fast_batch(values=None):
    values = values or [float(i) for i in range(N)]
    return [Obs("test.fast.flow", T0 + timedelta(minutes=5 * i), v, T0 + timedelta(minutes=5 * i))
            for i, v in enumerate(values)]


def _xmins(conn, table="obs_fast") -> dict:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT ts, xmin::text, fetched_at FROM montney.{table} ORDER BY ts").fetchall()}


def _count(conn, table) -> int:
    return conn.execute(f"SELECT count(*) FROM montney.{table}").fetchone()[0]


@pytest.fixture(params=["on", "off"], ids=["dedupe", "RED-dedupe-off"])
def dedupe(request, monkeypatch):
    monkeypatch.setenv("MONTNEY_DB_DEDUPE", request.param)
    return request.param == "on"


# ------------------------------------------------------------------ case 1 / 1b
def test_case1_identical_refetch_touches_nothing(dedupe):
    with scratch_db() as conn:
        st = _store(conn)
        first = st.write(_fast_batch(), now=T0 + timedelta(hours=1))
        assert first.written == N
        before = _xmins(conn)
        again = st.write(_fast_batch(), now=T0 + timedelta(hours=2))
        after = _xmins(conn)
        moved = [ts for ts in before if before[ts] != after[ts]]
        print(f"dedupe={dedupe}: sent {again.written}, unchanged {again.unchanged}, rows moved {len(moved)}")
        if dedupe:
            assert again.written == 0 and again.unchanged == N and moved == []
        else:   # seeded red: v1 behaviour rewrites every row, and the check sees it
            assert again.written == N and len(moved) == N


def test_case1b_one_changed_value_moves_exactly_that_row():
    with scratch_db() as conn:
        st = _store(conn)
        st.write(_fast_batch(), now=T0 + timedelta(hours=1))
        before = _xmins(conn)
        vals = [float(i) for i in range(N)]
        vals[2] = 99.0
        res = st.write(_fast_batch(vals), now=T0 + timedelta(hours=2))
        after = _xmins(conn)
        moved = [ts for ts in before if before[ts] != after[ts]]
        assert res.written == 1 and moved == [T0 + timedelta(minutes=10)]
        assert conn.execute("SELECT value FROM montney.obs_fast WHERE ts = %s",
                            (T0 + timedelta(minutes=10),)).fetchone()[0] == 99.0


# ------------------------------------------------------------------ case 2
def test_case2_republished_daily_same_values_adds_no_vintage(dedupe):
    days = [datetime(2026, 9, d) for d in range(1, N + 1)]
    with scratch_db() as conn:
        st = _store(conn)
        v1, v2 = datetime(2026, 10, 1), datetime(2026, 10, 2)
        st.write([Obs("test.daily.flow", d, 10.0 + i, v1) for i, d in enumerate(days)], now=v1)
        res = st.write([Obs("test.daily.flow", d, 10.0 + i, v2) for i, d in enumerate(days)], now=v2)
        rows = _count(conn, "obs_daily")
        print(f"dedupe={dedupe}: vintages stored {rows}")
        if dedupe:
            assert res.written == 0 and rows == N
        else:
            assert res.written == N and rows == 2 * N
        # a real revision always lands as a new vintage
        rev = st.write([Obs("test.daily.flow", days[0], 55.0, datetime(2026, 10, 3))], now=datetime(2026, 10, 3))
        assert rev.written == 1


# ------------------------------------------------------------------ case 3
def test_case3_identical_forecast_run_writes_nothing(dedupe):
    r1, r2 = datetime(2026, 10, 30, 6), datetime(2026, 10, 30, 6, 30)
    targets = [datetime(2026, 11, 1) + timedelta(hours=h) for h in range(N)]
    with scratch_db() as conn:
        st = _store(conn)
        st.write([Obs("test.fc.wind", t, 100.0 + h, r1) for h, t in enumerate(targets)], now=r1)
        res = st.write([Obs("test.fc.wind", t, 100.0 + h, r2) for h, t in enumerate(targets)], now=r2)
        rows = _count(conn, "obs_forecast")
        print(f"dedupe={dedupe}: forecast rows {rows}")
        if dedupe:
            assert res.written == 0 and rows == N
        else:
            assert res.written == N and rows == 2 * N
        latest = conn.execute("SELECT max(as_of) FROM montney.forecast_latest").fetchone()[0]
        assert latest == (r1 if dedupe else r2)


# ------------------------------------------------------------------ case 4: re-anchoring
@pytest.mark.parametrize("anchor", ["7", "off"], ids=["reanchor", "RED-reanchor-off"])
def test_case4_long_horizon_forecast_keeps_a_recent_vintage(anchor, monkeypatch):
    """An unchanged point 2 years out: re-written after 7 days, so dropping its first vintage (as retention
    would) still leaves an answer for an as-of query 1 day ago."""
    monkeypatch.setenv("MONTNEY_DB_REANCHOR_DAYS", anchor)
    target, a0 = datetime(2028, 10, 1), datetime(2026, 10, 1, 6)
    with scratch_db() as conn:
        st = _store(conn)
        st.write([Obs("test.fc.wind", target, 42.0, a0)], now=a0)
        early = st.write([Obs("test.fc.wind", target, 42.0, a0 + timedelta(days=3))], now=a0 + timedelta(days=3))
        late = st.write([Obs("test.fc.wind", target, 42.0, a0 + timedelta(days=8))], now=a0 + timedelta(days=8))
        assert early.written == 0
        conn.execute("DELETE FROM montney.obs_forecast WHERE as_of = %s", (a0,))   # the first vintage ages out
        asked = a0 + timedelta(days=9)
        answer = conn.execute("SELECT value FROM montney.obs_forecast WHERE ts = %s AND as_of <= %s "
                              "ORDER BY as_of DESC LIMIT 1", (target, asked)).fetchone()
        print(f"anchor={anchor}: late write {late.written} (re-anchored {late.reanchored}), answer {answer}")
        if anchor == "7":
            assert late.written == 1 and late.reanchored == 1 and answer == (42.0,)
        else:   # seeded red: without re-anchoring the point has no vintage left
            assert late.written == 0 and answer is None


# ------------------------------------------------------------------ commit failure
@pytest.mark.parametrize("after_commit", [True, False], ids=["cache-after-commit", "RED-cache-before-commit"])
def test_failed_commit_does_not_poison_the_cache(after_commit, monkeypatch):
    monkeypatch.setattr(PgStore, "CACHE_AFTER_COMMIT", after_commit)
    with scratch_db() as conn:
        st = _store(conn)

        def boom():
            raise RuntimeError("simulated commit failure")

        st._before_commit = boom
        with pytest.raises(RuntimeError):
            st.write(_fast_batch(), now=T0 + timedelta(hours=1))
        assert _count(conn, "obs_fast") == 0                    # the transaction rolled back
        st._before_commit = None
        retry = st.write(_fast_batch(), now=T0 + timedelta(hours=2))
        stored = _count(conn, "obs_fast")
        print(f"cache_after_commit={after_commit}: retry sent {retry.written}, stored {stored}")
        if after_commit:
            assert retry.written == N and stored == N
        else:   # seeded red: the cache claimed rows that never landed, so the retry skipped them
            assert retry.written == 0 and stored == 0


# ------------------------------------------------------------------ flags, state, cache
def test_flag_is_written_with_its_observation_in_one_transaction():
    month, v1 = datetime(2026, 9, 1), datetime(2026, 10, 5)
    obs = [Obs("test.daily.flow", month, 10.0, v1)]
    flag = Flag("test.daily.flow", month, v1, "hv_fallback", "39.0 MJ/m3 fallback")
    with scratch_db() as conn:
        st = _store(conn)
        st._before_commit = lambda: (_ for _ in ()).throw(RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            st.write(obs, flags=[flag], now=v1)
        assert _count(conn, "obs_daily") == 0 and _count(conn, "obs_flag") == 0   # neither, not one
        st._before_commit = None
        st.write(obs, flags=[flag], now=v1)
        assert _count(conn, "obs_daily") == 1 and _count(conn, "obs_flag") == 1
        # a re-issue with the published heat value: new vintage, no flag inherited
        v2 = datetime(2026, 11, 5)
        st.write([Obs("test.daily.flow", month, 10.4, v2)], now=v2)
        flags = conn.execute("SELECT as_of FROM montney.obs_flag").fetchall()
        assert flags == [(v1,)]
        # a flag for a row skipped as unchanged is not written on its own
        st.write([Obs("test.daily.flow", month, 10.4, datetime(2026, 12, 5))],
                 flags=[Flag("test.daily.flow", month, datetime(2026, 12, 5), "hv_fallback")], now=v2)
        assert _count(conn, "obs_flag") == 1


def test_last_seen_moves_even_when_nothing_is_written():
    with scratch_db() as conn:
        st = _store(conn)
        st.write(_fast_batch(), now=T0 + timedelta(hours=1))
        res = st.write(_fast_batch(), now=T0 + timedelta(hours=5))
        seen, last_ts = conn.execute(
            "SELECT last_seen_at, last_ts FROM montney.series_state s JOIN montney.series r ON r.id = s.series_id "
            "WHERE r.key = 'test.fast.flow'").fetchone()
        assert res.written == 0 and seen == T0 + timedelta(hours=5)
        assert last_ts == T0 + timedelta(minutes=5 * (N - 1))


def test_cold_cache_reads_the_comparator_from_the_database():
    """A restarted runner (empty cache) must still skip unchanged rows: misses load from the DB."""
    with scratch_db() as conn:
        _store(conn).write(_fast_batch(), now=T0 + timedelta(hours=1))
        fresh = _store(conn)                                     # new process, empty cache
        res = fresh.write(_fast_batch(), now=T0 + timedelta(hours=2))
        assert res.written == 0 and res.unchanged == N


def test_duplicates_inside_one_batch_collapse():
    with scratch_db() as conn:
        st = _store(conn)
        b = _fast_batch() + _fast_batch([float(i) + 0.5 for i in range(N)])
        res = st.write(b, now=T0 + timedelta(hours=1))
        assert res.written == N and _count(conn, "obs_fast") == N
        assert conn.execute("SELECT min(value) FROM montney.obs_fast").fetchone()[0] == 0.5   # last one wins


def test_tier_is_fixed_once_registered():
    """Fixed once the series HAS ROWS (L-080/#304: an empty series' tier may move; tests/test_db_tier_seeding.py)."""
    with scratch_db() as conn:
        st = _store(conn)
        st.write(_fast_batch([1.0]))
        with pytest.raises(ValueError, match="tier is fixed"):
            st.upsert_series("test.fast.flow", source="test_src", tier="hourly")


def test_unknown_series_is_an_error_not_a_silent_drop():
    from montney.db.store import UnknownSeries

    with scratch_db() as conn:
        st = _store(conn)
        with pytest.raises(UnknownSeries):
            st.write([Obs("test.not.registered", T0, 1.0, T0)])


# ------------------------------------------------------------------ retry after losing a lock fight
@pytest.mark.parametrize("retries", ["5", "0"], ids=["retry", "RED-no-retry"])
def test_write_retries_after_an_injected_deadlock(retries, monkeypatch):
    """deadlock_timeout (1 s) < thin_forecasts' lock_timeout (2 s), so the runner can be the deadlock victim
    (reviewer, #56). The write must retry the whole transaction; with retries off the error surfaces."""
    import psycopg

    monkeypatch.setenv("MONTNEY_DB_WRITE_RETRIES", retries)
    with scratch_db() as conn:
        st = _store(conn)
        fails = {"left": 1}

        def once():
            if fails["left"]:
                fails["left"] -= 1
                raise psycopg.errors.DeadlockDetected("injected")

        st._before_commit = once
        if retries == "0":
            with pytest.raises(psycopg.errors.DeadlockDetected):
                st.write(_fast_batch(), now=T0)
            assert _count(conn, "obs_fast") == 0
            return
        res = st.write(_fast_batch(), now=T0)
        assert res.retries == 1 and res.written == N and _count(conn, "obs_fast") == N


@pytest.mark.parametrize("retries", ["5", "0"], ids=["retry", "RED-no-retry"])
def test_write_survives_a_real_deadlock(retries, monkeypatch):
    """A real deadlock with the writer as the victim: another session holds obs_daily and then asks for
    obs_fast, while the writer holds obs_fast (written first) and waits for obs_daily. The other session's
    deadlock_timeout is raised so the writer's check always fires first."""
    import threading
    import time

    import psycopg

    from montney.db import connect

    monkeypatch.setenv("MONTNEY_DB_WRITE_RETRIES", retries)
    with scratch_db() as conn:
        st = _store(conn)
        other = connect(dbname=conn.info.dbname)
        other.execute("SET deadlock_timeout = '10s'")
        result, error = {}, {}
        try:
            other.execute("BEGIN")
            other.execute("LOCK TABLE montney.obs_daily IN EXCLUSIVE MODE")

            def write():
                try:
                    batch = _fast_batch() + [Obs("test.daily.flow", datetime(2026, 9, 1), 1.0, datetime(2026, 10, 1))]
                    result["res"] = st.write(batch, now=T0)
                except Exception as e:                             # noqa: BLE001
                    error["e"] = e

            t = threading.Thread(target=write)
            t.start()
            pid_writer = conn.info.backend_pid
            deadline = time.monotonic() + 20
            while not other.execute("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = %s",
                                    (pid_writer,)).fetchone()[0]:
                assert time.monotonic() < deadline, "writer never blocked on obs_daily"
                time.sleep(0.05)
            other.execute("LOCK TABLE montney.obs_fast IN EXCLUSIVE MODE")   # closes the cycle
            other.execute("COMMIT")
            t.join(timeout=60)
            assert not t.is_alive()
        finally:
            other.close()
        print(f"retries={retries}: result={result.get('res')}, error={type(error.get('e')).__name__ if error else None}")
        if retries == "0":
            assert isinstance(error.get("e"), psycopg.errors.DeadlockDetected)
        else:
            assert not error, error
            assert result["res"].retries >= 1 and result["res"].written == N + 1
            assert _count(conn, "obs_fast") == N and _count(conn, "obs_daily") == 1


# ------------------------------------------------------------------ backfill: older vintages
@pytest.mark.parametrize("dedupe_older", [False, True], ids=["keep-older", "RED-dedupe-older"])
def test_older_vintage_is_not_deduped_against_a_newer_one(dedupe_older, monkeypatch):
    """GDSR republishes a gas day: A=10 (v1), B=12 (v2), C=10 (v3). The live runner saw only C; the backfill
    then sends B and A. A equals the latest value (C) but is OLDER, so it must be written, or the as-of
    answer between v1 and v2 is lost (reviewer, #57)."""
    monkeypatch.setattr(PgStore, "DEDUPE_OLDER_VINTAGES", dedupe_older)
    day = datetime(2026, 9, 1)
    v1, v2, v3 = datetime(2026, 9, 2), datetime(2026, 9, 3), datetime(2026, 9, 4)
    with scratch_db() as conn:
        st = _store(conn)
        st.write([Obs("test.daily.flow", day, 10.0, v3)], now=v3)          # the live runner: C
        b = st.write([Obs("test.daily.flow", day, 12.0, v2)], now=v3)      # backfill: B
        a = st.write([Obs("test.daily.flow", day, 10.0, v1)], now=v3)      # backfill: A (== C, but older)

        def as_of(t):
            r = conn.execute("SELECT value FROM montney.obs_daily WHERE ts = %s AND as_of <= %s "
                             "ORDER BY as_of DESC LIMIT 1", (day, t)).fetchone()
            return r[0] if r else None

        answers = [as_of(v1), as_of(v2), as_of(v3)]
        print(f"dedupe_older={dedupe_older}: B written {b.written}, A written {a.written}, as-of answers {answers}")
        assert b.written == 1
        if not dedupe_older:
            assert a.written == 1 and answers == [10.0, 12.0, 10.0]
        else:   # seeded red: A skipped, the as-of history before v2 is gone
            assert a.written == 0 and answers == [None, 12.0, 10.0]
        # the comparator stays the newest vintage: a re-fetch of C is still a no-op
        assert st.write([Obs("test.daily.flow", day, 10.0, v3)], now=v3).written == 0


def test_older_forecast_run_is_not_deduped_and_does_not_move_forecast_latest():
    target = datetime(2026, 12, 1)
    r1, r2 = datetime(2026, 10, 1, 6), datetime(2026, 10, 2, 6)
    with scratch_db() as conn:
        st = _store(conn)
        st.write([Obs("test.fc.wind", target, 5.0, r2)], now=r2)
        older = st.write([Obs("test.fc.wind", target, 5.0, r1)], now=r2)
        assert older.written == 1 and _count(conn, "obs_forecast") == 2
        assert conn.execute("SELECT as_of FROM montney.forecast_latest").fetchone()[0] == r2


def test_second_writer_fails_fast():
    """Single-writer assumption (the change-only cache): a second runner must not write alongside."""
    from montney.db import connect
    from montney.db.store import WriterBusy

    with scratch_db() as conn:
        first = PgStore(conn)
        first.acquire_writer_lock()
        other = connect(dbname=conn.info.dbname)
        try:
            with pytest.raises(WriterBusy):
                PgStore(other).acquire_writer_lock()
            first.release_writer_lock()
            PgStore(other).acquire_writer_lock()               # free again: the next runner may start
        finally:
            other.close()
