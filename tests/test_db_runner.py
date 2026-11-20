"""The Postgres runner (`montney run`, montney/runner.py PgRunner; PORT step 4, L-002, L-036, L-037).

All against a scratch database with a network-free FakeConnector (montney/db/testing.py). Seeded reds:
an uncovered series must fail closed, a blocked source must not write, a refresh request must run its
source, fill_realized must be scheduled hourly and fill a real gap, and a second runner must not start.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from montney import runner
from montney.db.pgstore import PostgresStore
from montney.db.testing import fake_spec, pause_job, scratch_db


def _runner(conn, *specs, home=None):
    return runner.PgRunner(PostgresStore(conn), specs={s["id"]: s for s in specs}, home=home)


def _tiers(conn) -> dict:
    return dict(conn.execute("SELECT key, tier FROM montney.series").fetchall())


def test_tiers_come_from_the_spec_at_write_time():
    spec = fake_spec(series={"test.fake.mw": "5min", "test.fake.fc": "1h"},
                     retention={"default": "fast", "test.fake.fc": "forecast"})
    with scratch_db() as conn:
        res = _runner(conn, spec).run_now("test_fake")
        print(res, _tiers(conn))
        assert res["ok"], res
        assert _tiers(conn) == {"test.fake.mw": "fast", "test.fake.fc": "forecast"}
        n = conn.execute("SELECT (SELECT count(*) FROM montney.obs_fast), (SELECT count(*) FROM montney.obs_forecast)").fetchone()
        assert n == (1, 1)


@pytest.mark.parametrize("retention", [{"test.other.*": "fast"}, {"default": "fast", "test.fake.*": "fast",
                                                                   "test.fake.mw": "hourly"}],
                         ids=["no-match-no-default", "two-globs"])
def test_RED_an_uncovered_series_fails_closed_and_writes_nothing(retention):
    """L-036: an unresolvable series is an error at write time, never a silent tier."""
    spec = fake_spec(series={"test.fake.mw": "5min"}, retention=retention)
    with scratch_db() as conn:
        res = _runner(conn, spec).run_now("test_fake")
        print(res)
        assert not res["ok"] and "RetentionConflict" in res["error"]
        assert conn.execute("SELECT count(*) FROM montney.observation").fetchone()[0] == 0
        last = conn.execute("SELECT ok, error FROM montney.fetch_run").fetchall()
        assert last and last[-1][0] is False and "RetentionConflict" in last[-1][1]


@pytest.mark.parametrize("blocked", [True, False], ids=["blocked", "RED-not-blocked-writes"])
def test_a_blocked_source_fetches_but_writes_nothing(blocked):
    spec = fake_spec(retention="fast")
    with scratch_db() as conn:
        r = _runner(conn, spec)
        r.store.w.ensure_source("test_fake")
        if blocked:
            conn.execute("UPDATE montney.source SET budget_state = 'blocked' WHERE code = 'test_fake'")
        r.task_budget_states()
        res = r.run_now("test_fake")
        rows = conn.execute("SELECT count(*) FROM montney.observation").fetchone()[0]
        print(blocked, res, rows)
        if blocked:
            assert not res["ok"] and res.get("blocked") and rows == 0
            assert "budget" in conn.execute("SELECT error FROM montney.fetch_run").fetchone()[0]
        else:
            assert res["ok"] and rows == 1


def test_refresh_requests_run_their_source_and_are_consumed():
    spec = fake_spec(retention="fast")
    with scratch_db() as conn:
        r = _runner(conn, spec)
        conn.execute("INSERT INTO montney.refresh_request (source) VALUES ('test_fake'), ('no_such_source')")
        out = r.run_periodic("refresh_requests")
        print(out)
        assert out == {"ran": ["test_fake"], "unknown": ["no_such_source"]}
        assert conn.execute("SELECT count(*) FROM montney.refresh_request").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM montney.observation").fetchone()[0] == 1
        assert r.run_periodic("refresh_requests") == {"ran": [], "unknown": []}      # nothing queued: no run


def test_fill_realized_is_scheduled_hourly_and_fills_a_gap():
    """Reviewer/orchestrator: nothing scheduled fill_realized; the runner does, hourly, and it really runs."""
    from montney.db import backfill
    from montney.db.store import Obs as PO

    assert runner.PgRunner.PERIODIC["fill_realized"] == 3600.0
    spec = fake_spec(retention="fast")
    with scratch_db() as conn:
        pause_job(conn, "thin_forecasts", lock_key="montney.thin_forecasts")
        r = _runner(conn, spec)
        st = r.store.w
        st.ensure_source("aeso_ets_outages")
        st.upsert_series("aeso.outage.cc", source="aeso_ets_outages", tier="forecast", freq="D")
        st.upsert_series("aeso.outage.cc.realized", source="aeso_ets_outages", tier="daily", freq="D")
        from zoneinfo import ZoneInfo
        d0 = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Edmonton")).date()
        day = datetime(d0.year, d0.month, d0.day) - timedelta(days=3)
        st.write([PO("aeso.outage.cc", day, 12.0, backfill.alberta_day_end(day) - timedelta(hours=4))])
        out = r.run_periodic("fill_realized")
        print(out, r.last_periodic["fill_realized"])
        assert out["filled"] == 1 and r.last_periodic["fill_realized"]["ok"]


def test_du_task_sets_budget_states_and_the_runner_honours_them(tmp_path, monkeypatch):
    from montney.db import du

    spec = fake_spec(retention="fast")
    with scratch_db() as conn:
        r = _runner(conn, spec, home=tmp_path)
        r.run_now("test_fake")
        monkeypatch.setattr(du, "TOTAL_BUDGET", 1000)                 # the total is "full"
        out = r.run_periodic("du")
        print(out)
        assert out["states"]["test_fake"] == "blocked" and "test_fake" in r.blocked
        res = r.run_now("test_fake")
        assert res.get("blocked")


def test_a_second_runner_fails_fast():
    from montney.db import connect
    from montney.db.store import WriterBusy

    spec = fake_spec(retention="fast")
    with scratch_db() as conn:
        first = _runner(conn, spec)
        first.store.w.acquire_writer_lock()
        other = connect(dbname=conn.info.dbname)
        try:
            second = _runner(other, spec)
            with pytest.raises(WriterBusy):
                second.store.w.acquire_writer_lock()
        finally:
            other.close()


def test_montney_run_refuses_without_postgres(monkeypatch, capsys):
    monkeypatch.setenv("MONTNEY_DB", "duckdb")
    assert runner.run_forever() == runner.EXIT_CONFIG == 78
    assert "MONTNEY_DB=postgres" in capsys.readouterr().out


def test_api_helpers_work_on_the_reader_role(monkeypatch):
    """The API (reader role) queues a refresh and reads budget states; the runner consumes the request."""
    from montney.db import connect
    from montney.db.testing import admin_connection

    admin = admin_connection()
    try:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'montney_reader'").fetchone():
            admin.execute("CREATE ROLE montney_reader LOGIN PASSWORD 'ci-reader-throwaway'")
        else:
            admin.execute("ALTER ROLE montney_reader PASSWORD 'ci-reader-throwaway'")
    finally:
        admin.close()
    monkeypatch.setenv("MONTNEY_DB_READER_PASSWORD", "ci-reader-throwaway")
    spec = fake_spec(retention="fast")
    with scratch_db() as conn:
        r = _runner(conn, spec)
        r.store.w.ensure_source("test_fake")
        conn.execute("UPDATE montney.source SET budget_state = 'warn' WHERE code = 'test_fake'")
        api = PostgresStore(connect(dbname=conn.info.dbname, role="reader"))
        try:
            api.request_refresh("test_fake")
            assert api.budget_states() == {"test_fake": "warn"}
        finally:
            api.close()
        assert r.run_periodic("refresh_requests")["ran"] == ["test_fake"]


def test_events_or_grid_tier_on_observations_is_a_clear_error():
    """Reviewer (#85): an observation series pinned to a non-observation tier fails closed with a clear message."""
    spec = fake_spec(retention="events")
    with scratch_db() as conn:
        res = _runner(conn, spec).run_now("test_fake")
        print(res)
        assert not res["ok"] and "not an observation tier" in res["error"]
        assert conn.execute("SELECT count(*) FROM montney.observation").fetchone()[0] == 0
# ------------------------------------------------------------------ heartbeat (orchestrator/reviewer, #88)
def test_heartbeat_is_periodic_and_frequent():
    assert runner.PgRunner.PERIODIC["heartbeat"] <= 10.0


@pytest.mark.parametrize("beat", [True, False], ids=["beat", "RED-no-beat"])
def test_heartbeat_updates_and_the_reader_can_read_it(beat, monkeypatch):
    import os

    from montney.db import connect
    from montney.db.testing import admin_connection

    admin = admin_connection()
    try:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'montney_reader'").fetchone():
            admin.execute("CREATE ROLE montney_reader LOGIN PASSWORD 'ci-reader-throwaway'")
        else:
            admin.execute("ALTER ROLE montney_reader PASSWORD 'ci-reader-throwaway'")
    finally:
        admin.close()
    monkeypatch.setenv("MONTNEY_DB_READER_PASSWORD", "ci-reader-throwaway")
    with scratch_db() as conn:
        r = _runner(conn, fake_spec(retention="fast"))
        api = PostgresStore(connect(dbname=conn.info.dbname, role="reader"))
        try:
            if not beat:     # seeded red: no runner has ever beaten
                assert api.runner_status() is None
                return
            r.run_periodic("heartbeat")
            first = api.runner_status()
            r.run_periodic("heartbeat")
            second = api.runner_status()
            print(first, second)
            assert first["pid"] == os.getpid() and first["age_s"] < 5
            assert second["last_beat"] > first["last_beat"]                  # the beat moves
            conn.execute("UPDATE montney.runner_state SET last_beat = last_beat - INTERVAL '120 seconds'")
            assert api.runner_status()["age_s"] > 60                         # a stopped runner shows its age
        finally:
            api.close()


def test_heartbeat_does_not_wait_for_the_db_lock():
    """Reviewer (#90): a long write or du holding db_lock must not stall the beat (else the UI falsely says
    "runner not running"). Hold db_lock in another thread for 3 s: the beat still lands at once."""
    import threading
    import time

    with scratch_db() as conn:
        r = _runner(conn, fake_spec(retention="fast"))
        r.run_periodic("heartbeat")
        before = PostgresStore(conn).runner_status()["last_beat"]
        held = threading.Event()

        def hog():
            with r.db_lock:
                held.set()
                time.sleep(3)

        t = threading.Thread(target=hog)
        t.start()
        held.wait(5)
        t0 = time.monotonic()
        r.run_periodic("heartbeat")
        took = time.monotonic() - t0
        still_held = t.is_alive()                          # the hog still holds db_lock (a Python lock only)
        after = PostgresStore(conn).runner_status()["last_beat"]
        t.join()
        assert still_held, "the hog let go before the beat: the test would prove nothing"
        print(f"beat took {took:.3f} s while db_lock was held")
        assert took < 1.0 and after > before
        r._beat_conn.close()
