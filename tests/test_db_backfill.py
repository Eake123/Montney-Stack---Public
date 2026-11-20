"""DuckDB → Postgres backfill (montney/db/backfill.py).

The source DuckDB is built by v1's own Store in each test (never Erik's data). Checks: every latest value
lands (verify, with seeded reds), the as-of history survives (oldest vintage first, never deduped against a
newer live vintage), repeated vintages are dropped, events and their refs move, TAU is excluded, a re-run
writes nothing, rollups are refreshed, and the backfill holds thinning off while it runs, against concurrent
compression and thinning, N runs (orchestrator).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from montney.db import backfill
from montney.db.store import Obs as PgObs
from montney.db.store import PgStore
from montney.db.testing import pause_job, scratch_db
from montney.store import Event, Obs, SeriesMeta, Store

NOW = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
DAY0 = NOW.replace(hour=0) - timedelta(days=1)
GAS_DAY = datetime(2026, 9, 1)
A, B, C, D = (GAS_DAY + timedelta(days=n) for n in (1, 2, 3, 4))      # vintages of one gas day


def make_duck(path: Path, *, history_days: int = 3) -> Path:
    st = Store(path)
    st.upsert_meta([
        SeriesMeta("aeso.csd.test_gen", "aeso_csd", unit="MW", freq="5min"),
        SeriesMeta("aeso.price.test_actual", "aeso_price", unit="$/MWh", freq="1h"),
        SeriesMeta("ngtl.gdsr.test_flow", "ngtl_gdsr", unit="MMcf/d", freq="D", day="gas"),
        SeriesMeta("wx.test.temp", "weather", unit="degC", freq="D"),
        SeriesMeta("abrivers.tau_001.outflow", "abrivers", unit="m3/s", freq="1h"),
    ])
    obs = [Obs("aeso.csd.test_gen", DAY0 + timedelta(minutes=5 * i), float(i % 7), DAY0 + timedelta(minutes=5 * i))
           for i in range(288)]
    obs += [Obs("aeso.price.test_actual", DAY0 + timedelta(hours=h), 50.0 + h, DAY0 + timedelta(hours=h))
            for h in range(24)]
    # GDSR revisions of one gas day: A=10, B=12, C=10, D=10 (D repeats C: dropped as a repeated vintage)
    obs += [Obs("ngtl.gdsr.test_flow", GAS_DAY, v, a) for v, a in ((10.0, A), (12.0, B), (10.0, C), (10.0, D))]
    # daily history across several yearly chunks
    obs += [Obs("ngtl.gdsr.test_flow", datetime(2021, 11, 16) + timedelta(days=d), 100.0 + d % 9,
                datetime(2021, 11, 17) + timedelta(days=d)) for d in range(0, 365 * history_days, 3)]
    # a forecast: two runs, as_of before ts
    for run_h, base in ((6, 1.0), (12, 2.0)):
        r = DAY0 + timedelta(hours=run_h)
        obs += [Obs("wx.test.temp", DAY0 + timedelta(days=d), base + d, r) for d in range(2, 8)]
    obs += [Obs("abrivers.tau_001.outflow", DAY0 + timedelta(hours=h), 5.0, DAY0 + timedelta(hours=h)) for h in range(6)]
    st.write(obs)
    st.upsert_events([Event("ngtl_dop:1", "ngtl_dop", "outage", "Test outage", refs=["gas.ngtl.egat"],
                            starts=DAY0, ends=DAY0 + timedelta(days=2))])
    st.close()
    return path


def _asof(conn, key, ts, at):
    r = conn.execute("SELECT o.value FROM montney.obs_daily o JOIN montney.series s ON s.id = o.series_id "
                     "WHERE s.key = %s AND o.ts = %s AND o.as_of <= %s ORDER BY o.as_of DESC LIMIT 1",
                     (key, ts, at)).fetchone()
    return r[0] if r else None


def test_backfill_loads_verifies_and_is_idempotent(tmp_path):
    duck = make_duck(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        res = backfill.run(conn, duck)
        print(f"read {res.read}, after vintage dedupe {res.after_vintage_dedupe}, written {res.written}, "
              f"events {res.events}, refreshed {res.refreshed}")
        assert "abrivers.tau_001.outflow" in res.plan.excluded                  # TAU waits (orchestrator)
        assert res.after_vintage_dedupe == res.read - 1                          # D repeats C
        assert res.written == res.after_vintage_dedupe and res.events == 1
        tiers = dict(conn.execute("SELECT key, tier FROM montney.series").fetchall())
        assert tiers == {"aeso.csd.test_gen": "fast", "aeso.price.test_actual": "hourly",
                         "ngtl.gdsr.test_flow": "daily", "wx.test.temp": "forecast"}
        assert backfill.verify(conn, duck) == []
        assert [_asof(conn, "ngtl.gdsr.test_flow", GAS_DAY, t) for t in (A, B, C, D)] == [10.0, 12.0, 10.0, 10.0]
        assert conn.execute("SELECT ref FROM montney.event_scope").fetchall() == [("gas.ngtl.egat",)]
        assert res.refreshed["obs_fast_1h"] is not None and res.refreshed["obs_hourly_1d"] is not None
        again = backfill.run(conn, duck)
        assert again.written == 0 and again.events == 0                         # idempotent


@pytest.mark.parametrize("tamper", ["changed", "missing"])
def test_verify_catches_a_wrong_or_missing_value(tmp_path, tamper):
    """Seeded red: verify must name the broken point."""
    duck = make_duck(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        backfill.run(conn, duck)
        sid = conn.execute("SELECT id FROM montney.series WHERE key = 'aeso.price.test_actual'").fetchone()[0]
        ts = DAY0 + timedelta(hours=5)
        if tamper == "changed":
            conn.execute("UPDATE montney.obs_hourly SET value = value + 1 WHERE series_id = %s AND ts = %s", (sid, ts))
        else:
            conn.execute("DELETE FROM montney.obs_hourly WHERE series_id = %s AND ts = %s", (sid, ts))
        problems = backfill.verify(conn, duck)
        print(problems)
        assert len(problems) == 1 and "aeso.price.test_actual" in problems[0]


def test_backfill_under_a_newer_live_vintage_keeps_the_as_of_history(tmp_path):
    """The live runner already wrote C=10; the backfill's older A=10 must still land (reviewer, #57)."""
    duck = make_duck(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        st = PgStore(conn)
        st.ensure_source("ngtl_gdsr")
        st.upsert_series("ngtl.gdsr.test_flow", source="ngtl_gdsr", tier="daily", freq="D", day="gas")
        st.write([PgObs("ngtl.gdsr.test_flow", GAS_DAY, 10.0, C)])
        backfill.run(conn, duck)
        assert [_asof(conn, "ngtl.gdsr.test_flow", GAS_DAY, t) for t in (A, B, C)] == [10.0, 12.0, 10.0]


def test_verify_csv_reads_the_ngtl_csv_as_of_queries(tmp_path):
    """A small CSV in TC's wide layout (synthetic, never Erik's file): all values match; one wrong one fails."""
    csv = tmp_path / "gdsr.csv"
    csv.write_text("PipelineSystem,FromGasDay,ToGasDay,Volume,GasDay,Empress,NGTLFieldReceipts\n"
                   "NGTL,2026-Sep-01,2026-Sep-02,Imperial,2026-Sep-01,\"-3,725\",\"12,746\"\n"
                   "NGTL,2026-Sep-01,2026-Sep-02,Imperial,2026-Sep-02,\"-3,673\",\"12,845\"\n")
    from montney.importers import ngtl_csv

    parsed = ngtl_csv.parse(csv)
    obs = ngtl_csv.to_observations(parsed, ngtl_csv.guess_units(parsed, csv))
    with scratch_db() as conn:
        st = PgStore(conn)
        st.ensure_source("ngtl_gdsr")
        for key in sorted({o.series_id for o in obs}):
            st.upsert_series(key, source="ngtl_gdsr", tier="daily", freq="D", day="gas")
        st.write([PgObs(o.series_id, o.ts, o.value, o.as_of) for o in obs])
        n, problems = backfill.verify_csv(conn, csv)
        assert n == len(obs) == 4 and problems == []
        st.write([PgObs(obs[0].series_id, obs[0].ts, obs[0].value + 5, obs[0].as_of - timedelta(hours=1))])
        conn.execute("DELETE FROM montney.obs_daily WHERE as_of = %s AND ts = %s AND series_id = "
                     "(SELECT id FROM montney.series WHERE key = %s)", (obs[0].as_of, obs[0].ts, obs[0].series_id))
        n, problems = backfill.verify_csv(conn, csv)                             # seeded red
        assert len(problems) == 1, problems


# ------------------------------------------------------------------ concurrency (orchestrator)
RUNS = int(os.environ.get("MONTNEY_DB_BACKFILL_RUNS", "10"))


def _concurrent_run(tmp: Path, hold_lock: bool) -> dict:
    """Backfill while another session compresses obs_daily/obs_forecast chunks and calls thin_forecasts
    in a loop. Thinnable old forecast runs are seeded first, so a thinning that sneaks in shows up."""
    from montney.db import connect

    duck = make_duck(tmp / "v1.duckdb", history_days=4)
    with scratch_db() as conn:
        pause_job(conn, "thin_forecasts", lock_key="montney.thin_forecasts")
        st = PgStore(conn)
        st.ensure_source("weather")
        st.upsert_series("wx.old.temp", source="weather", tier="forecast", freq="D")
        old = DAY0 - timedelta(days=30)
        st.write([PgObs("wx.old.temp", old + timedelta(days=40), 1.0, old + timedelta(hours=7))])
        st.write([PgObs("wx.old.temp", old + timedelta(days=40), 2.0, old + timedelta(hours=8))])  # same run-day
        stop, errors, thinned_during = threading.Event(), [], []
        running = threading.Event()

        def other():
            o = connect(dbname=conn.info.dbname)
            running.wait(30)                                    # start once the backfill is under way
            try:
                while not stop.is_set():
                    for (c,) in o.execute("SELECT show_chunks('montney.obs_daily')").fetchall():
                        try:
                            o.execute("SET lock_timeout = '200ms'")
                            o.execute("SELECT compress_chunk(%s::regclass, if_not_compressed => true)", (c,))
                        except Exception:                               # noqa: BLE001 - busy: try again later
                            pass
                    before = o.execute("SELECT count(*) FROM montney.obs_forecast WHERE as_of < %s",
                                       (DAY0 - timedelta(days=20),)).fetchone()[0]
                    o.execute("CALL montney.thin_forecasts(0, '{}')")
                    after = o.execute("SELECT count(*) FROM montney.obs_forecast WHERE as_of < %s",
                                      (DAY0 - timedelta(days=20),)).fetchone()[0]
                    if running.is_set() and after < before:
                        thinned_during.append(before - after)
                    time.sleep(0.01)
            except Exception as e:                                      # noqa: BLE001
                errors.append(f"other: {type(e).__name__}: {e}")
            finally:
                o.close()

        t = threading.Thread(target=other, daemon=True)
        t.start()
        try:
            res = backfill.run(conn, duck, hold_thin_lock=hold_lock, on_start=running.set)
        except Exception as e:                                          # noqa: BLE001
            errors.append(f"backfill: {type(e).__name__}: {e}")
            res = None
        running.clear()
        stop.set()
        t.join(timeout=60)
        problems = backfill.verify(conn, duck) if res else ["backfill failed"]
        return {"errors": errors, "verify": problems[:3], "retries": res.retries if res else None,
                "written": res.written if res else None, "thinned_during_backfill": sum(thinned_during)}


def test_backfill_holds_thinning_off_against_concurrent_compression(tmp_path):
    results = []
    for i in range(RUNS):
        d = tmp_path / f"r{i}"
        d.mkdir()
        results.append(_concurrent_run(d, hold_lock=True))
    for i, r in enumerate(results):
        print(i, r)
    bad = [r for r in results if r["errors"] or r["verify"] or r["thinned_during_backfill"]]
    print(f"{RUNS} concurrent backfills, {len(bad)} bad; retries per run: {[r['retries'] for r in results]}")
    assert not bad, bad


def test_RED_without_the_lock_thinning_runs_during_the_backfill(tmp_path):
    """Seeded red: the same load without the lock lets thinning act while the backfill runs."""
    hits = 0
    for i in range(3):
        d = tmp_path / f"r{i}"
        d.mkdir()
        r = _concurrent_run(d, hold_lock=False)
        print(i, r)
        hits += r["thinned_during_backfill"] > 0
    assert hits >= 1, "thinning never ran during an unlocked backfill: the guard above would test nothing"


# ------------------------------------------------------------------ rollup refresh window (reviewer, #64)
@pytest.mark.parametrize("clip", [True, False], ids=["clip-inward", "RED-past-retention"])
def test_refresh_never_empties_a_bucket_whose_raw_rows_are_gone(clip, monkeypatch):
    """A bucket at -60.5 d was materialized while its raw rows existed; raw retention (60 d) has since dropped
    them. A backfill whose rows reach back past retention must not refresh that bucket to empty."""
    monkeypatch.setattr(backfill, "CLIP_INWARD", clip)
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    bucket = now - timedelta(days=60, hours=12)
    with scratch_db() as conn:
        for job in ("policy_refresh_continuous_aggregate", "policy_retention"):
            pause_job(conn, job, proc_schema=None)             # Timescale's own policy jobs
        st = PgStore(conn)
        st.ensure_source("aeso_csd")
        sid = st.upsert_series("aeso.csd.test_gen", source="aeso_csd", tier="fast", freq="5min")
        conn.execute("INSERT INTO montney.obs_fast (series_id, ts, as_of, value, fetched_at) VALUES (%s, %s, %s, 7, %s)",
                     (sid, bucket, bucket, bucket))
        backfill.refresh_with_retry(conn, "obs_fast_1h", bucket - timedelta(hours=1), bucket + timedelta(hours=2))
        conn.execute("DELETE FROM montney.obs_fast WHERE series_id = %s", (sid,))     # as raw retention would
        window = backfill.refresh_rollups(conn, {"fast": [bucket - timedelta(days=5), now]}, now)
        left = conn.execute("SELECT sum FROM montney.obs_fast_1h WHERE series_id = %s AND ts = %s",
                            (sid, bucket)).fetchall()
        print(f"clip={clip}: refreshed {window['obs_fast_1h']}, bucket at -60.5 d -> {left}")
        if clip:
            assert window["obs_fast_1h"][0] >= now - timedelta(days=59) and left == [(7.0,)]
        else:   # seeded red: the old window reached 61 d back and emptied the bucket
            assert left == []


def test_refresh_skips_a_tier_with_nothing_written(tmp_path):
    duck = make_duck(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        backfill.run(conn, duck)
        again = backfill.run(conn, duck)                 # nothing written: no tier is refreshed
        assert again.written == 0 and all(v is None for v in again.refreshed.values()), again.refreshed


def test_backfill_fails_fast_while_a_runner_holds_the_writer_lock(tmp_path):
    from montney.db import connect
    from montney.db.store import WriterBusy

    duck = make_duck(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        runner = connect(dbname=conn.info.dbname)
        try:
            PgStore(runner).acquire_writer_lock()
            with pytest.raises(WriterBusy):
                backfill.run(conn, duck)
            assert conn.execute("SELECT count(*) FROM montney.observation").fetchone()[0] == 0
        finally:
            runner.close()
        assert backfill.run(conn, duck).written > 0      # runner gone: the backfill proceeds


# ------------------------------------------------------------------ spec tiers (Power, #70; L-035 Q-P3)
def test_spec_tier_wins_and_pinned_rows_known_early_are_listed(tmp_path):
    """No database needed: plan() only reads DuckDB and the specs."""
    duck = make_duck(tmp_path / "v1.duckdb")
    specs = {"weather": {"id": "weather", "retention": "daily"},                 # pins a forecast-looking series
             "aeso_price": {"id": "aeso_price", "retention": "hourly"}}
    p = backfill.plan(duck, specs=specs)
    wx = p.series["wx.test.temp"]
    assert (wx["tier"], wx["declared"], wx["derived"]) == ("daily", "daily", "forecast")
    summary = p.summary()
    print(summary)
    assert "pinned non-forecast by spec, but with rows known before their ts" in summary
    assert "wx.test.temp" in summary.split("pinned non-forecast")[1]
    assert p.series["ngtl.gdsr.test_flow"]["declared"] is None                   # no spec: derived
    assert p.series["ngtl.gdsr.test_flow"]["tier"] == "daily"


def test_block_retention_overrides_the_source_rule(tmp_path):
    duck = make_duck(tmp_path / "v1.duckdb")
    specs = {"aeso_price": {"id": "aeso_price", "retention": "hourly", "extract": [
        {"retention": "forecast", "fields": {"x": {"id": "aeso.price.test_actual"}}}]}}
    assert backfill.plan(duck, specs=specs).series["aeso.price.test_actual"]["tier"] == "forecast"


def test_two_matching_globs_are_an_error_not_a_pick(tmp_path):
    duck = make_duck(tmp_path / "v1.duckdb")
    specs = {"weather": {"id": "weather", "retention": {"default": "daily", "wx.*": "forecast", "wx.test.*": "daily"}}}
    with pytest.raises(backfill.RetentionConflict, match="2 retention patterns"):
        backfill.plan(duck, specs=specs)


def test_a_non_observation_tier_on_an_observation_series_is_excluded(tmp_path):
    duck = make_duck(tmp_path / "v1.duckdb")
    p = backfill.plan(duck, specs={"weather": {"id": "weather", "retention": "events"}})
    assert "wx.test.temp" not in p.series and "events" in p.excluded["wx.test.temp"]


# ------------------------------------------------------------------ L-036 realized outage history (gate after #73)
def _alberta_today():
    from zoneinfo import ZoneInfo

    d = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Edmonton")).date()
    return datetime(d.year, d.month, d.day)


def make_outage_duck(path: Path) -> tuple[Path, dict]:
    """Outage vintages around each day's end (06:00Z the next day). Returns the file and expectations."""
    today = _alberta_today()
    d, d_early, d_late, d_conn = (today - timedelta(days=n) for n in (3, 4, 5, 6))
    end = backfill.alberta_day_end(d)
    st = Store(path)
    st.upsert_meta([SeriesMeta("aeso.outage.cc", "aeso_ets_outages", unit="MW", freq="D"),
                    SeriesMeta("aeso.outage.gas_total", "aeso_ets_outages", unit="MW", freq="D"),
                    SeriesMeta("aeso.outage.cc.realized", "aeso_ets_outages", unit="MW", freq="D")])
    obs = [Obs("aeso.outage.cc", d, v, a) for v, a in (
        (10.0, end - timedelta(days=1, hours=1)), (20.0, end - timedelta(hours=3)),
        (30.0, end - timedelta(minutes=1)),        # 05:59Z: the last one known on the day
        (40.0, end),                               # 06:00Z: the next day already, not known on the day
        (50.0, end + timedelta(hours=1)))]
    obs.append(Obs("aeso.outage.cc", d_early, 7.0, backfill.alberta_day_end(d_early) - timedelta(days=2)))
    obs.append(Obs("aeso.outage.cc", d_late, 9.0, backfill.alberta_day_end(d_late) + timedelta(hours=2)))   # first after
    obs.append(Obs("aeso.outage.cc", d_late, 9.5, backfill.alberta_day_end(d_late) + timedelta(days=3)))    # a later one
    obs.append(Obs("aeso.outage.cc", today, 11.0, backfill.alberta_day_end(today) - timedelta(hours=2)))
    obs.append(Obs("aeso.outage.gas_total", d, 300.0, end - timedelta(hours=2)))
    obs.append(Obs("aeso.outage.cc", d_conn, 12.0, backfill.alberta_day_end(d_conn) - timedelta(hours=1)))
    obs.append(Obs("aeso.outage.cc.realized", d_conn, 12.5, d_conn))       # the connector already realized it
    st.write(obs)
    st.close()
    return path, {"d": d, "d_early": d_early, "d_late": d_late, "today": today, "d_conn": d_conn}


@pytest.mark.parametrize("cmp", ["lt", "le"], ids=["before-day-end", "RED-at-day-end"])
def test_realized_is_the_last_vintage_known_on_the_day(tmp_path, monkeypatch, cmp):
    """L-036: the newest vintage with as_of BEFORE 06:00Z the next day. No database needed."""
    monkeypatch.setattr(backfill, "_REALIZED_CMP", cmp)
    duck, x = make_outage_duck(tmp_path / "out.duckdb")
    p = backfill.plan(duck, specs={})
    cc = dict(p.realized["aeso.outage.cc.realized"])
    print(cmp, sorted(cc.items()), p.summary().splitlines()[-2:])
    if cmp == "lt":
        assert cc[x["d"]] == 30.0                                  # 05:59Z counts, 06:00Z doesn't
    else:   # seeded red: counting the vintage AT the day end picks 06:00Z's value
        assert cc[x["d"]] == 40.0
        return
    assert cc[x["d_early"]] == 7.0                                 # no fetch that day: the latest earlier vintage
    assert cc[x["d_late"]] == 9.0                                  # L-037: only post-day vintages: the EARLIEST
    assert p.realized_late == {("aeso.outage.cc.realized", x["d_late"])}   # ... and only that day is flagged
    assert x["today"] not in cc and p.realized_not_ended == 1      # not ended: left to the connector
    assert x["d_conn"] not in cc and p.realized_skipped_existing == 1   # the connector's row wins
    assert dict(p.realized["aeso.outage.gas_total.realized"]) == {x["d"]: 300.0}
    assert p.series["aeso.outage.cc.realized"]["tier"] == "daily"
    assert "realized outage history derived (L-036): 4 rows for 2 series" in p.summary()


@pytest.mark.parametrize("pick", ["earliest", "latest"], ids=["earliest-after", "RED-latest-after"])
def test_realized_late_takes_the_earliest_post_day_vintage(tmp_path, monkeypatch, pick):
    """L-037: a day first seen only after it ended takes the EARLIEST later vintage, flagged realized_late."""
    monkeypatch.setattr(backfill, "_LATE_PICK", pick)
    duck, x = make_outage_duck(tmp_path / "out.duckdb")
    p = backfill.plan(duck, specs={})
    got = dict(p.realized["aeso.outage.cc.realized"])[x["d_late"]]
    assert got == (9.0 if pick == "earliest" else 9.5)             # the red picks the later vintage


def test_backfill_writes_the_realized_history(tmp_path):
    duck, x = make_outage_duck(tmp_path / "out.duckdb")
    with scratch_db() as conn:
        res = backfill.run(conn, duck)                             # the repo's specs: realized -> daily
        rows = dict(conn.execute(
            "SELECT o.ts, o.value FROM montney.obs_daily o JOIN montney.series s ON s.id = o.series_id "
            "WHERE s.key = 'aeso.outage.cc.realized' AND o.as_of = o.ts").fetchall())
        print(res.realized, rows)
        assert rows == {x["d"]: 30.0, x["d_early"]: 7.0, x["d_late"]: 9.0, x["d_conn"]: 12.5}   # + connector's own
        flags = conn.execute(
            "SELECT f.ts, f.as_of, f.flag FROM montney.obs_flag f JOIN montney.series s ON s.id = f.series_id "
            "WHERE s.key = 'aeso.outage.cc.realized'").fetchall()
        assert flags == [(x["d_late"], x["d_late"], "realized_late")]   # only the late day; same-day ones: none
        tiers = dict(conn.execute("SELECT key, tier FROM montney.series WHERE key LIKE 'aeso.outage.%'").fetchall())
        assert tiers["aeso.outage.cc"] == "forecast" and tiers["aeso.outage.cc.realized"] == "daily"
        assert backfill.run(conn, duck).written == 0               # idempotent


def test_a_glob_map_without_a_match_or_default_fails_closed(tmp_path):
    """L-036: a series a retention map doesn't cover is an error, never a silent (data-derived) tier."""
    duck = make_duck(tmp_path / "v1.duckdb")
    specs = {"weather": {"id": "weather", "retention": {"aeso.*": "fast"}}}           # no default, no match
    with pytest.raises(backfill.RetentionConflict, match="no retention pattern and there is no default"):
        backfill.plan(duck, specs=specs)


# ------------------------------------------------------------------ L-037 (3): later gaps, filled live
@pytest.mark.parametrize("run_fill", [True, False], ids=["fill_realized", "RED-gap-left-unfilled"])
def test_fill_realized_fills_every_ended_gap_in_the_window(run_fill):
    """Runner downtime leaves days without a realized value. fill_realized(), the same derive_realized() as the
    backfill, fills each ended day in the last 14 d: an on-day vintage gives realized; only post-day vintages
    give the earliest one, flagged realized_late."""
    from montney.db import check
    from montney.db.store import Obs as PO
    from montney.db.testing import pause_job

    today = _alberta_today()
    g1, g2, have, old = (today - timedelta(days=n) for n in (2, 3, 4, 20))
    with scratch_db() as conn:
        pause_job(conn, "thin_forecasts", lock_key="montney.thin_forecasts")
        st = PgStore(conn)
        st.ensure_source("aeso_ets_outages")
        st.upsert_series("aeso.outage.cc", source="aeso_ets_outages", tier="forecast", freq="D")
        st.upsert_series("aeso.outage.cc.realized", source="aeso_ets_outages", tier="daily", freq="D")
        e = backfill.alberta_day_end
        st.write([PO("aeso.outage.cc", g1, 10.0, e(g1) - timedelta(hours=3)),     # on the day
                  PO("aeso.outage.cc", g1, 11.0, e(g1) + timedelta(hours=1)),     # after: ignored for g1
                  PO("aeso.outage.cc", g2, 20.0, e(g2) + timedelta(hours=1)),     # only after: late, earliest
                  PO("aeso.outage.cc", g2, 21.0, e(g2) + timedelta(hours=9)),
                  PO("aeso.outage.cc", have, 30.0, e(have) - timedelta(hours=2)),
                  PO("aeso.outage.cc", old, 40.0, e(old) - timedelta(hours=2)),     # outside the 14 d window
                  PO("aeso.outage.cc", today, 50.0, e(today) - timedelta(hours=20))])   # not ended
        st.write([PO("aeso.outage.cc.realized", have, 31.0, have)])               # the connector's own row
        gaps_before = check.realized_gaps(conn)
        res = backfill.fill_realized(conn) if run_fill else None
        gaps_after = check.realized_gaps(conn)
        rows = dict(conn.execute(
            "SELECT o.ts, o.value FROM montney.obs_daily o JOIN montney.series s ON s.id = o.series_id "
            "WHERE s.key = 'aeso.outage.cc.realized' AND o.as_of = o.ts").fetchall())
        flags = conn.execute("SELECT ts FROM montney.obs_flag WHERE flag = 'realized_late'").fetchall()
        print(f"run_fill={run_fill}: {res}; gaps before {gaps_before}, after {gaps_after}, rows {rows}, flags {flags}")
        assert {ts for _k, ts in gaps_before} == {g1, g2}
        if not run_fill:    # seeded red: the gap check reports what nobody filled
            assert {ts for _k, ts in gaps_after} == {g1, g2}
            return
        assert gaps_after == [] and res["filled"] == 2 and res["late"] == 1 and res["already"] == 1
        assert rows == {g1: 10.0, g2: 20.0, have: 31.0}      # earliest post-day for g2; connector's row kept
        assert flags == [(g2,)]                              # only the late one is flagged
        assert old not in rows and today not in rows
        assert backfill.fill_realized(conn)["filled"] == 0   # idempotent
        assert conn.execute("SELECT count(*) FROM montney.obs_flag").fetchone()[0] == 1


# ------------------------------------------------------------------ monthly realized (L-037, gates the runner)
def _months_back(n: int) -> datetime:
    t = _alberta_today().replace(day=1)
    for _ in range(n):
        t = (t - timedelta(days=1)).replace(day=1)
    return t


@pytest.mark.parametrize("month_end", ["month", "day"], ids=["month-end", "RED-day-end-for-a-month"])
def test_monthly_realized_uses_the_alberta_month_end(month_end, monkeypatch):
    """A month's realized value: the newest vintage before the Alberta month end, else the earliest after it
    (realized_late). Seeded red: taking the month's FIRST day end as its end picks a different vintage."""
    m1, m2 = _months_back(2), _months_back(3)
    end1, end2 = backfill.alberta_month_end(m1), backfill.alberta_month_end(m2)   # the true month ends
    rows = [("aeso.outage.monthly.cc", m1, m1 + timedelta(days=2), 5.0),
            ("aeso.outage.monthly.cc", m1, end1 - timedelta(hours=1), 6.0),       # last one known in the month
            ("aeso.outage.monthly.cc", m1, end1 + timedelta(days=1), 7.0),
            ("aeso.outage.monthly.cc", m2, end2 + timedelta(hours=3), 8.0),       # only after: late, earliest
            ("aeso.outage.monthly.cc", m2, end2 + timedelta(days=5), 9.0)]
    if month_end == "day":
        monkeypatch.setattr(backfill, "alberta_month_end", backfill.alberta_day_end)
    out, _skipped, late, _ne = backfill.derive_realized(rows, set(), datetime.now(timezone.utc).replace(tzinfo=None))
    got = dict(out["aeso.outage.monthly.cc.realized"])
    print(month_end, got, sorted(late))
    if month_end == "month":
        assert got == {m1: 6.0, m2: 8.0} and late == {("aeso.outage.monthly.cc.realized", m2)}
    else:   # seeded red: the month "ended" on its first day, so the in-month vintages look late
        assert got[m1] == 5.0 and ("aeso.outage.monthly.cc.realized", m1) in late


def test_fill_realized_fills_a_missed_month():
    """The live fill derives a monthly realized value whose month END falls inside the 14-day window (the
    month itself started before it)."""
    from montney.db.store import Obs as PO

    m = _months_back(2)
    end = backfill.alberta_month_end(m)
    now = end + timedelta(days=3)                                   # the month ended 3 days "ago"
    with scratch_db() as conn:
        st = PgStore(conn)
        st.ensure_source("aeso_ets_outages_monthly")
        st.upsert_series("aeso.outage.monthly.cc", source="aeso_ets_outages_monthly", tier="forecast", freq="M")
        st.upsert_series("aeso.outage.monthly.cc.realized", source="aeso_ets_outages_monthly", tier="daily", freq="M")
        st.write([PO("aeso.outage.monthly.cc", m, 60.0, end - timedelta(days=4)),
                  PO("aeso.outage.monthly.cc", m, 61.0, end - timedelta(hours=2)),
                  PO("aeso.outage.monthly.cc", m, 62.0, end + timedelta(hours=2))], now=end)
        res = backfill.fill_realized(conn, now=now)
        rows = conn.execute("SELECT o.ts, o.value FROM montney.obs_daily o JOIN montney.series s ON s.id = o.series_id "
                            "WHERE s.key = 'aeso.outage.monthly.cc.realized'").fetchall()
        print(res, rows)
        assert rows == [(m, 61.0)] and res["late"] == 0
        assert backfill.fill_realized(conn, now=end + timedelta(days=20))["filled"] == 0   # outside the window: no-op


# ------------------------------------------------------------------ refresh vs a concurrent policy refresh
class _BusyConn:
    """Wraps a connection: the first `busy` refresh calls fail as TimescaleDB does while its scheduled policy
    refresh holds the rollup (SQLSTATE 55P03), then they go through to the real database."""

    def __init__(self, conn, busy: int):
        self.conn, self.busy, self.calls = conn, busy, 0

    def execute(self, sql, params=None):
        import psycopg

        if "refresh_continuous_aggregate" in sql:
            self.calls += 1
            if self.calls <= self.busy:
                raise psycopg.errors.LockNotAvailable(
                    "could not refresh continuous aggregate 'obs_fast_1h' due to a concurrent refresh")
        return self.conn.execute(sql, params)


@pytest.mark.parametrize("retry", [True, False], ids=["retry", "RED-no-retry"])
def test_refresh_waits_out_a_concurrent_policy_refresh(retry, monkeypatch):
    """Reviewer (main red on 5976529/c20abbe): an import's refresh colliding with the scheduled cagg refresh must
    wait and succeed, not fail. Seeded red: without the retry the first collision fails the import."""
    import psycopg

    monkeypatch.setattr(backfill, "_REFRESH_RETRY", retry)
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    with scratch_db() as conn:
        busy = _BusyConn(conn, busy=3)
        if not retry:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                backfill.refresh_rollups(busy, {"fast": [now - timedelta(days=2), now]}, now)
            return
        out = backfill.refresh_rollups(busy, {"fast": [now - timedelta(days=2), now]}, now)
        print(out, "calls", busy.calls)
        assert out["obs_fast_1h"] is not None and busy.calls == 3 + 2      # 3 collisions, then 1h and 1d refresh


def test_refresh_gives_up_after_its_deadline():
    import psycopg

    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    with scratch_db() as conn:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            backfill.refresh_with_retry(_BusyConn(conn, busy=10_000), "obs_fast_1h", now - timedelta(days=1), now,
                                        deadline_s=1.0)


CONC_RUNS = int(os.environ.get("MONTNEY_DB_REFRESH_RUNS", "8"))


def _real_concurrent_refreshes(conn, retry: bool, runs: int) -> list[dict]:
    """Each run: fresh invalidations in obs_fast, then the REAL scheduled policy job for obs_fast_1h (run_job,
    the scheduler's own entry point) in one session while refresh_with_retry refreshes the same rollup in this
    one, started a moment later. Returns per-run outcomes."""
    import threading
    import time

    from montney.db import connect
    from montney.db.store import Obs as PO

    st = PgStore(conn)
    st.ensure_source("test_src")
    st.upsert_series("test.refresh.mw", source="test_src", tier="fast", freq="5min")
    sid = conn.execute("SELECT id FROM montney.series WHERE key = 'test.refresh.mw'").fetchone()[0]
    job = conn.execute(
        # this Timescale lists a rollup's policy jobs under the VIEW name (as montney/db/check.py handles too)
        "SELECT j.job_id FROM timescaledb_information.jobs j JOIN timescaledb_information.continuous_aggregates c "
        "ON j.hypertable_name IN (c.view_name, c.materialization_hypertable_name) "
        "WHERE j.proc_name = 'policy_refresh_continuous_aggregate' AND c.view_name = 'obs_fast_1h'").fetchone()[0]
    pause_job(conn, "policy_refresh_continuous_aggregate", proc_schema=None)    # only OUR run_job refreshes now
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    out = []
    for i in range(runs):
        with conn.cursor() as cur:                     # ~40 k fresh points over the policy's 7-day window
            with cur.copy("COPY montney.obs_fast (series_id, ts, as_of, value, fetched_at) FROM STDIN") as cp:
                for k in range(40_000):
                    t = now - timedelta(days=6) + timedelta(seconds=13 * k + i)
                    cp.write_row((sid, t, t, float(k % 97 + i), now))
        policy_err = []

        def policy():
            o = connect(dbname=conn.info.dbname)
            try:
                o.execute("CALL run_job(%s)", (job,))
            except Exception as e:                     # noqa: BLE001
                policy_err.append(f"{type(e).__name__}: {e}")
            finally:
                o.close()

        t = threading.Thread(target=policy)
        t.start()
        time.sleep(0.05)
        try:
            n = backfill.refresh_with_retry(conn, "obs_fast_1h", now - timedelta(days=7), now, deadline_s=60)
            out.append({"run": i, "ok": True, "retries": n, "policy_err": policy_err})
        except Exception as e:                         # noqa: BLE001
            out.append({"run": i, "ok": False, "error": f"{type(e).__name__}", "policy_err": policy_err})
        t.join(timeout=120)
    return out


@pytest.mark.parametrize("retry", [True, False], ids=["retry", "RED-no-retry"])
def test_refresh_against_the_real_policy_refresh_repeatedly(retry, monkeypatch):
    """Orchestrator (main red since c20abbe): the import's refresh against the REAL concurrent policy refresh,
    N runs. With the retry every run succeeds and contention really happened; without it, it fails."""
    monkeypatch.setattr(backfill, "_REFRESH_RETRY", retry)
    with scratch_db() as conn:
        res = _real_concurrent_refreshes(conn, retry, CONC_RUNS)
    for r in res:
        print(r)
    collided = sum(1 for r in res if (r.get("retries") or 0) > 0 or not r["ok"])
    print(f"{CONC_RUNS} runs, retry={retry}: {sum(r['ok'] for r in res)} ok, {collided} collided")
    assert collided > 0, "no run collided with the policy refresh: the test proved nothing (NO-RESULT)"
    if retry:
        assert all(r["ok"] for r in res), res
    else:   # seeded red
        assert any(not r["ok"] and r["error"] == "LockNotAvailable" for r in res), res


# ------------------------------------------------------------------ parity on the imported store (step 5 gaps)
def make_parity_duck(path: Path) -> Path:
    """make_duck plus what Erik's real DuckDB had and the first import missed (2026-11-01 parity run):
    a later identical re-fetch of a point (a dropped repeated vintage), a metadata-only series, a fetch log."""
    make_duck(path)
    st = Store(path)
    st.upsert_meta([SeriesMeta("ngtl.gdsr.test_meta_only.nom", "ngtl_gdsr", unit="MMcf/d", freq="D", day="gas")])
    last = DAY0 + timedelta(hours=23)
    st.write([Obs("aeso.price.test_actual", DAY0 + timedelta(hours=23), 50.0 + 23, last + timedelta(hours=2))])
    for src, ok in (("aeso_csd", True), ("aeso_price", True), ("ngtl_gdsr", False), ("weather", True)):
        st.log_fetch(src, last, ok, 3, None if ok else "HTTP 503")
    st.close()
    return path


@pytest.mark.parametrize("gap", [None, "sync_state", "import_fetch_log", "meta_only"],
                         ids=["imported-store-has-parity", "RED-no-state-sync", "RED-no-fetch-log",
                              "RED-no-metadata-only-series"])
def test_the_imported_store_answers_like_duckdb(tmp_path, monkeypatch, gap):
    """Import, then the real-data parity tool (montney/db/parity.py) over the result. Each gap the first real
    import had (Erik's machine, 2026-11-01) is a seeded red: put it back and parity must fail."""
    from montney.db import parity

    duck = make_parity_duck(tmp_path / "v1.duckdb")
    if gap in ("sync_state", "import_fetch_log"):
        monkeypatch.setattr(backfill, gap, lambda *a, **k: 0)
    if gap == "meta_only":
        real_plan = backfill.plan

        def plan_without_meta_only(*a, **k):
            p = real_plan(*a, **k)
            p.series = {key: s for key, s in p.series.items() if not s.get("meta_only")}
            return p
        monkeypatch.setattr(backfill, "plan", plan_without_meta_only)
    with scratch_db() as conn:
        res = backfill.run(conn, duck)
        r = parity.run(duck, conn, min_comparisons=100)
        print(gap, f"fetch_runs {res.fetch_runs}, state_synced {res.state_synced}, {r['comparisons']} comparisons,",
              f"{len(r['problems'])} problems, allowed {r['allowed_hits']}")
        for x in r["problems"][:12]:
            print("  ", x.replace("\n", " | ")[:300])
        if gap is None:
            assert r["ok"], r["problems"][:5]
            assert res.fetch_runs == 4 and res.state_synced > 0
            state = "SELECT series_id, last_ts, last_as_of, last_value, last_seen_at, seen_from_ts, seen_to_ts "                     "FROM montney.series_state ORDER BY 1"
            before = conn.execute(state).fetchall()
            again = backfill.run(conn, duck)                                     # idempotent, also these
            assert again.written == 0 and again.fetch_runs == 0
            assert conn.execute(state).fetchall() == before                      # same state after a re-run
            assert parity.run(duck, conn, min_comparisons=100)["ok"]
        else:
            # the failure must be THIS gap's, not anything else (a red failing for another reason proves nothing)
            want = {"sync_state": lambda x: " as_of " in x or x.startswith("catalog"),
                    "import_fetch_log": lambda x: x.startswith("last_fetch"),
                    "meta_only": lambda x: x.startswith("catalog")}[gap]
            assert not r["ok"] and r["problems"], f"parity did not notice the {gap} gap"
            assert all(want(x) for x in r["problems"]), f"{gap}: unrelated problems {r['problems'][:3]}"
        assert r["excluded"] == ["abrivers.tau_001.outflow"]                    # TAU: excluded on purpose, listed


def test_state_sync_never_moves_a_runner_value_back(tmp_path):
    """Once the runner has fetched a source natively (a fetch_run newer than DuckDB's last fetch of it), a
    re-run of the import leaves that source's series_state alone."""
    duck = make_parity_duck(tmp_path / "v1.duckdb")
    with scratch_db() as conn:
        backfill.run(conn, duck)
        st = PgStore(conn)
        later = NOW + timedelta(days=1)
        conn.execute("INSERT INTO montney.fetch_run (source_id, started, finished, ok) "
                     "SELECT id, %s, %s, true FROM montney.source WHERE code = 'aeso_price'", (later, later))
        conn.execute("UPDATE montney.series_state SET last_seen_at = %s, last_as_of = %s WHERE series_id = "
                     "(SELECT id FROM montney.series WHERE key = 'aeso.price.test_actual')", (later, later))
        assert backfill.sync_state(conn, duck, ["aeso.price.test_actual"]) == 0
        assert conn.execute("SELECT last_seen_at FROM montney.series_state WHERE series_id = (SELECT id FROM "
                            "montney.series WHERE key = 'aeso.price.test_actual')").fetchone()[0] == later
        _ = st
