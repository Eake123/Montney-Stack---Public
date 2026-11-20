"""L-029: the Alberta day in SQL is a fixed UTC-6, i.e. daily buckets and forecast run-days start at 06:00 UTC.

1. Every offset `time_bucket(...)` in migrations/ carries the `-- L-029` marker, and all offsets are
   identical (seeded red: a '7 hours' copy and an unmarked copy must both be caught).
2. On a real database the boundary holds on both sides: 05:59Z belongs to the previous Alberta day and
   06:00Z to the new one, in the daily rollups and in forecast thinning.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from montney.db import migrate
from montney.db.testing import add_series, pause_job, release_session_lock, scratch_db, wait_advisory_released

WANT = "INTERVAL '6 hours'"


def _calls(text: str):
    """Yield (line_no, line, args) for every time_bucket(...) call, parenthesis- and quote-aware."""
    for m in re.finditer(r"time_bucket\s*\(", text):
        i, depth, quote, args, cur = m.end(), 1, False, [], ""
        while i < len(text) and depth:
            ch = text[i]
            if ch == "'":
                quote = not quote
            elif not quote and ch == "(":
                depth += 1
            elif not quote and ch == ")":
                depth -= 1
                if depth == 0:
                    break
            if not quote and depth == 1 and ch == ",":
                args.append(cur.strip())
                cur = ""
            else:
                cur += ch
            i += 1
        args.append(cur.strip())
        line_no = text.count("\n", 0, m.start()) + 1
        yield line_no, text.splitlines()[line_no - 1], args


def l029_problems(files: dict[str, str]) -> tuple[int, list[str]]:
    """(number of offset time_buckets found, problems). Fails closed: finding none is a problem."""
    found, problems = 0, []
    for name, text in files.items():
        for line_no, line, args in _calls(text):
            if len(args) < 3:
                continue                       # no offset: not an Alberta-day bucket
            found += 1
            where = f"{name}:{line_no}"
            if "L-029" not in line:
                problems.append(f"{where}: offset time_bucket without the -- L-029 marker")
            if " ".join(args[2].split()) != WANT:
                problems.append(f"{where}: offset {args[2]!r}, expected {WANT}")
    if found == 0:
        problems.append("no offset time_bucket found at all (NO-RESULT is not a pass)")
    return found, problems


def _migration_texts() -> dict[str, str]:
    return {m.path.name: m.sql for m in migrate.discover()}


def test_every_l029_offset_is_marked_and_identical():
    found, problems = l029_problems(_migration_texts())
    print(f"{found} offset time_bucket calls checked")
    # obs_fast_1d, obs_hourly_1d, and two in thin_forecasts
    assert found >= 4, found
    assert not problems, problems


def test_l029_check_catches_a_wrong_or_unmarked_offset():
    """Seeded red: the same checker must fail on a tampered copy of the real migrations."""
    texts = _migration_texts()
    name = next(n for n, t in texts.items() if "obs_fast_1d" in t)
    wrong = dict(texts)
    wrong[name] = texts[name].replace(WANT, "INTERVAL '7 hours'", 1)
    assert any("'7 hours'" in p for p in l029_problems(wrong)[1])
    unmarked = dict(texts)
    marked = "AS ts,   -- L-029: Alberta day = UTC-6"
    assert marked in texts[name]
    unmarked[name] = texts[name].replace(marked, "AS ts,", 1)
    assert any("marker" in p for p in l029_problems(unmarked)[1])
    assert l029_problems({"empty.sql": "SELECT 1;"})[1]       # nothing found = fail


def test_schedule_interval_is_not_an_l029_offset():
    sql = "SELECT add_continuous_aggregate_policy('x', schedule_interval => INTERVAL '6 hours');"
    found, problems = l029_problems({"a.sql": sql + "\nSELECT time_bucket(INTERVAL '1 day', ts, "
                                     "INTERVAL '6 hours') -- L-029\n"})
    assert found == 1 and not problems


# ------------------------------------------------------------------ database
def _series(conn, key: str, tier: str) -> int:
    return add_series(conn, key, tier)


def _utc_today() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


def test_daily_rollups_split_at_0600_utc():
    day = _utc_today() - timedelta(days=1)                  # inside every refresh/retention window
    before, at = day + timedelta(hours=5, minutes=59), day + timedelta(hours=6)
    with scratch_db() as conn:
        for table, rollup, tier in (("obs_fast", "obs_fast_1d", "fast"),
                                    ("obs_hourly", "obs_hourly_1d", "hourly")):
            sid = _series(conn, f"test.boundary.{tier}", tier)
            conn.execute(f"INSERT INTO montney.{table} (series_id, ts, as_of, value, fetched_at) "
                         "VALUES (%s, %s, %s, 1, %s), (%s, %s, %s, 2, %s)",
                         (sid, before, before, before, sid, at, at, at))
            from montney.db.backfill import refresh_with_retry    # waits out the scheduled policy refresh
            refresh_with_retry(conn, rollup, day - timedelta(days=2), day + timedelta(days=2))
            rows = conn.execute(f"SELECT ts, sum, n FROM montney.{rollup} WHERE series_id = %s ORDER BY ts",
                                (sid,)).fetchall()
            print(rollup, rows)
            assert rows == [(day - timedelta(hours=18), 1.0, 1),   # 05:59Z -> previous Alberta day
                            (day + timedelta(hours=6), 2.0, 1)], rows  # 06:00Z -> new Alberta day


def test_forecast_thinning_run_day_splits_at_0600_utc():
    """Two runs in one Alberta run-day keep only the later one; a run at 06:00Z starts a new run-day."""
    day = _utc_today() - timedelta(days=20)                 # older than the 14 days kept in full
    target = day + timedelta(days=60)
    runs = [day + timedelta(hours=5), day + timedelta(hours=5, minutes=59), day + timedelta(hours=6)]
    with scratch_db() as conn:
        _pause_thinning_job(conn)
        sid = _seed_runs(conn, runs, target)
        conn.execute("CALL montney.thin_forecasts(0, '{\"keep_all_days\": 14}')")
        kept = _kept(conn, sid)
        print("kept runs", kept)
        assert kept == runs[1:], kept      # 05:00 thinned (superseded at 05:59, same run-day); 06:00 kept


def test_forecast_thinning_skips_while_another_run_holds_the_lock():
    """The concurrency guard: a second caller must not thin (CI once deadlocked a manual CALL against the
    scheduled job). Red check: with the lock held elsewhere, nothing is deleted; once released, it is."""
    from montney.db import connect

    day = _utc_today() - timedelta(days=20)
    runs = [day + timedelta(hours=5), day + timedelta(hours=5, minutes=59)]
    with scratch_db() as conn:
        _pause_thinning_job(conn)
        sid = _seed_runs(conn, runs, day + timedelta(days=60))
        other = connect(dbname=conn.info.dbname)
        try:
            assert other.execute("SELECT pg_try_advisory_lock(hashtext('montney.thin_forecasts'))").fetchone()[0]
            conn.execute("CALL montney.thin_forecasts(0, '{}')")
            assert _kept(conn, sid) == runs                  # skipped: lock held by `other`
        finally:
            release_session_lock(other, "montney.thin_forecasts")   # unlock, THEN close (close is async)
        wait_advisory_released(conn, "montney.thin_forecasts")       # fail closed if still held
        conn.execute("CALL montney.thin_forecasts(0, '{}')")
        assert _kept(conn, sid) == runs[1:]


CLOSE_PROBES = 40


def test_evidence_close_releases_a_session_lock_asynchronously():
    """FLAKE_HISTORY (#121): confirm or refute "close() doesn't release the session lock synchronously".
    CLOSE_PROBES times: `other` takes the lock and closes; at once, a probe from `conn` tries the lock. Printed:
    how often it was still held (evidence; the count may be 0 on a quiet runner). Asserted: the explicit
    unlock path is never still held, and every probe ran (NO-RESULT is not a pass)."""
    from montney.db import connect

    key = "montney.test.close_probe"
    with scratch_db(migrations=None) as conn:
        held_after_close = held_after_unlock = probes = 0
        for explicit in (False, True):
            for _ in range(CLOSE_PROBES):
                other = connect(dbname=conn.info.dbname)
                assert other.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]
                if explicit:
                    release_session_lock(other, key)
                else:
                    other.close()
                got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]
                probes += 1
                if got:
                    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                elif explicit:
                    held_after_unlock += 1
                else:
                    held_after_close += 1
                wait_advisory_released(conn, key)
        print(f"close() only: still held {held_after_close}/{CLOSE_PROBES}; "
              f"unlock then close: still held {held_after_unlock}/{CLOSE_PROBES}")
        assert probes == 2 * CLOSE_PROBES
        assert held_after_unlock == 0


def test_RED_the_release_probe_fails_while_the_lock_is_held():
    """Seeded red: the new precondition really fails closed. With the lock still held elsewhere,
    wait_advisory_released must fail rather than let the second CALL run."""
    from montney.db import connect

    with scratch_db(migrations=None) as conn:
        other = connect(dbname=conn.info.dbname)
        try:
            assert other.execute("SELECT pg_try_advisory_lock(hashtext('montney.thin_forecasts'))").fetchone()[0]
            with pytest.raises(AssertionError, match="still held"):
                wait_advisory_released(conn, "montney.thin_forecasts", timeout_s=0.5)
        finally:
            release_session_lock(other, "montney.thin_forecasts")
        wait_advisory_released(conn, "montney.thin_forecasts")       # and once released, it passes


def _pause_thinning_job(conn) -> None:
    pause_job(conn, "thin_forecasts", lock_key="montney.thin_forecasts")


def _seed_runs(conn, runs, target) -> int:
    sid = _series(conn, "test.boundary.forecast", "forecast")
    for i, run in enumerate(runs):
        conn.execute("INSERT INTO montney.obs_forecast (series_id, as_of, ts, value, fetched_at) "
                     "VALUES (%s, %s, %s, %s, %s)", (sid, run, target, float(i), run))
    return sid


def _kept(conn, sid) -> list:
    return [r[0] for r in conn.execute("SELECT as_of FROM montney.obs_forecast WHERE series_id = %s "
                                       "ORDER BY as_of", (sid,)).fetchall()]
