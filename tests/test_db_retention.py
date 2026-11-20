"""Retention tiers as ruled by PLAN P3 (docs/db/RETENTION.md "The tiers").

The live Timescale policies must match the PLAN numbers exactly, and the checker must be able to fail
(seeded red: tamper with one policy and expect the mismatch to be reported).
"""

from __future__ import annotations

import json
import os

import pytest

from montney.db import check
from montney.db.testing import add_series, pause_job, scratch_db


def test_policies_match_plan():
    with scratch_db() as conn:
        problems = check.retention_mismatches(conn)
        print(f"{len(check.EXPECTED_RETENTION)} tables checked, problems: {problems}")
        assert problems == []


def test_retention_check_catches_a_changed_policy():
    with scratch_db() as conn:
        conn.execute("SELECT remove_retention_policy('montney.obs_fast')")
        conn.execute("SELECT add_retention_policy('montney.obs_fast', drop_after => INTERVAL '30 days')")
        conn.execute("SELECT add_retention_policy('montney.obs_daily', drop_after => INTERVAL '10 years')")
        problems = check.retention_mismatches(conn)
        print(problems)
        assert any(p.startswith("obs_fast:") for p in problems), problems
        assert any(p.startswith("obs_daily:") and "forever" in p for p in problems), problems


def test_retention_check_fails_closed_on_a_missing_table():
    with scratch_db() as conn:
        conn.execute("DROP MATERIALIZED VIEW montney.obs_hourly_1d")
        assert any("obs_hourly_1d: table or rollup not found" in p for p in check.retention_mismatches(conn))


def test_jobs_and_columnstore_policies_exist():
    with scratch_db() as conn:
        procs = {r[0]: r[1] for r in conn.execute(
            "SELECT proc_name, config FROM timescaledb_information.jobs "
            "WHERE proc_schema = 'montney'").fetchall()}
        assert procs.get("thin_forecasts", {}).get("keep_all_days") == 14, procs
        assert procs.get("prune_fetch_runs", {}).get("keep_days") == 365, procs
        assert "prune_obs_flags" in procs, procs
        compress = {r[0] for r in conn.execute(
            "SELECT hypertable_name FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_schema = 'montney'").fetchall()}
        # obs_forecast is compressed by thin_forecasts only after thinning, never by a policy.
        assert {"obs_fast", "obs_hourly", "obs_daily"} <= compress and "obs_forecast" not in compress, compress


# How often to repeat the flag-lifetime test in one run (flake proof for FLAKE_HISTORY / #59). Each repeat is
# a fresh scratch database, which is where the race with the scheduler happened.
FLAG_REPEATS = int(os.environ.get("MONTNEY_DB_FLAG_REPEATS", "1"))


@pytest.mark.parametrize("rep", range(FLAG_REPEATS))
def test_obs_flags_live_exactly_as_long_as_their_vintage(rep):
    """A flag on a vintage that's gone is an orphan: `db check` reports it and prune_obs_flags removes it,
    while the flag on a live vintage (and a newer, unflagged vintage of the same month) stay as they are."""
    with scratch_db() as conn:
        # The scheduled job starts as the database is created and would delete the seeded orphan before the
        # check below can see it (flake on #59, CI run 41269057354).
        pause_job(conn, "prune_obs_flags")
        src = conn.execute("INSERT INTO montney.source (code) VALUES ('t') RETURNING id").fetchone()[0]
        sid = conn.execute("INSERT INTO montney.series (key, source_id, tier) VALUES "
                           "('ngtl.station.31127.delivery', %s, 'daily') RETURNING id", (src,)).fetchone()[0]
        month, v1, v2 = "2026-09-01", "2026-10-05", "2026-11-05"
        conn.execute("INSERT INTO montney.obs_daily (series_id, ts, as_of, value, fetched_at) VALUES "
                     "(%s, %s, %s, 10, %s), (%s, %s, %s, 11, %s)", (sid, month, v1, v1, sid, month, v2, v2))
        conn.execute("INSERT INTO montney.obs_flag (series_id, ts, as_of, flag) VALUES (%s, %s, %s, 'hv_fallback')",
                     (sid, month, v1))                    # v1 used the fallback HV; v2 (re-issue) did not
        assert check.orphan_flags(conn) == 0
        conn.execute("DELETE FROM montney.obs_daily WHERE as_of = %s", (v1,))   # as if dropped/thinned
        assert check.orphan_flags(conn) == 1                                     # red: check sees it
        conn.execute("CALL montney.prune_obs_flags(0, '{}')")
        assert check.orphan_flags(conn) == 0
        assert conn.execute("SELECT count(*) FROM montney.obs_flag").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM montney.obs_daily WHERE series_id = %s", (sid,)).fetchone()[0] == 1


def test_RED_scheduled_prune_in_the_window_reproduces_the_flake():
    """Pins the cause of the #59 flake deterministically (reviewer, #62): with the pause skipped, the
    SCHEDULED prune_obs_flags job run (run_job, the scheduler's own entry point) landing between the DELETE
    and the check produces exactly the failure CI saw, `orphan_flags == 0` where 1 was expected."""
    with scratch_db() as conn:
        src = conn.execute("INSERT INTO montney.source (code) VALUES ('t') RETURNING id").fetchone()[0]
        sid = conn.execute("INSERT INTO montney.series (key, source_id, tier) VALUES "
                           "('ngtl.station.31127.delivery', %s, 'daily') RETURNING id", (src,)).fetchone()[0]
        month, v1 = "2026-09-01", "2026-10-05"
        conn.execute("INSERT INTO montney.obs_daily (series_id, ts, as_of, value, fetched_at) VALUES "
                     "(%s, %s, %s, 10, %s)", (sid, month, v1, v1))
        conn.execute("INSERT INTO montney.obs_flag (series_id, ts, as_of, flag) VALUES (%s, %s, %s, 'hv_fallback')",
                     (sid, month, v1))
        conn.execute("DELETE FROM montney.obs_daily WHERE as_of = %s", (v1,))
        job = conn.execute("SELECT job_id FROM timescaledb_information.jobs "
                           "WHERE proc_schema = 'montney' AND proc_name = 'prune_obs_flags'").fetchone()[0]
        conn.execute("CALL run_job(%s)", (job,))                 # the scheduled job, in the window
        got = check.orphan_flags(conn)
        print(f"orphan_flags after a scheduled prune in the window: {got} (the flake's 'assert 0 == 1')")
        assert got == 0


def test_db_check_fails_when_thinning_lags():
    """A stuck advisory lock makes every scheduled thin_forecasts run skip silently; db check must say so.
    Seeded reds: a watermark 10 days behind, and old runs with no watermark at all."""
    from datetime import datetime, timedelta, timezone

    with scratch_db() as conn:
        pause_job(conn, "thin_forecasts", lock_key="montney.thin_forecasts")
        job = conn.execute("SELECT job_id FROM timescaledb_information.jobs "
                           "WHERE proc_schema = 'montney' AND proc_name = 'thin_forecasts'").fetchone()[0]
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        def set_watermark(ts):
            cfg = {"keep_all_days": 14} if ts is None else {"keep_all_days": 14, "thinned_through": ts.isoformat()}
            conn.execute("SELECT alter_job(%s, config => %s::jsonb)", (job, json.dumps(cfg)))

        set_watermark(now - timedelta(days=14, hours=12))          # half a day behind: fine
        ok, detail = check.thinning_lag(conn)
        assert ok, detail
        set_watermark(now - timedelta(days=24))                    # 10 days behind: FAIL
        ok, detail = check.thinning_lag(conn)
        print(detail)
        assert not ok, detail
        set_watermark(None)                                         # never ran, nothing due: fine
        assert check.thinning_lag(conn)[0]
        sid = add_series(conn, "test.lag.forecast", "forecast")
        old = now - timedelta(days=30)
        conn.execute("INSERT INTO montney.obs_forecast (series_id, as_of, ts, value, fetched_at) "
                     "VALUES (%s, %s, %s, 1, %s)", (sid, old, old + timedelta(days=60), old))
        ok, detail = check.thinning_lag(conn)                      # never ran, but runs are due: FAIL
        print(detail)
        assert not ok, detail
