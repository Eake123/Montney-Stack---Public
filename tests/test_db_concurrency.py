"""Forecast thinning runs while the runner writes (the orchestrator's blocker on #51, CI run 5b0f868).

The deadlock CI actually saw was thinning against thinning (fixed in 0004 by an advisory lock). This file
tests the other theoretical case: compression against a concurrent writer. 0004 compressed every thinned
chunk in ONE transaction, so it holds chunk 1's AccessExclusiveLock while asking for chunk 2's. A writer that
holds chunk 2 (RowExclusiveLock) and then asks for chunk 1 closes a cycle. 0005 commits after every chunk,
so it never holds one chunk's lock while asking for another's.

Measured (CI run 41268834503): 20 of 20 runs deadlocked on 0004, 0 of 20 on 0005.

Each run: a fresh database; three old Alberta days (three 1-day chunks, all due for thinning); 3 runs per
target per day (2 in one run-day, so thinning deletes); a writer thread whose transactions insert into the
NEWEST old chunk, hold it ~2 ms, then insert into the OLDEST (the reverse of compression order); then
thin_forecasts is called while it writes. Clean = no error on either side, thinning correct, every writer row
present, and the writer actually overlapped. MONTNEY_DB_CONCURRENCY_RUNS sets the count (default 20).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from montney.db import migrate
from montney.db.testing import add_series, pause_job, scratch_db

RUNS = int(os.environ.get("MONTNEY_DB_CONCURRENCY_RUNS", "20"))
TARGETS = 50
OLD_DAYS = (22, 21, 20)          # days ago: three chunks older than the 14 days kept in full


def _one_run(i: int, migrations: Path) -> dict:
    from montney.db import connect

    today = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=d) for d in OLD_DAYS]
    with scratch_db(migrations=migrations) as conn:
        pause_job(conn, "thin_forecasts", lock_key="montney.thin_forecasts")
        sid = add_series(conn, "test.conc.thinned", "forecast")
        wid = add_series(conn, "test.conc.writer", "forecast")
        rows = []
        for day in days:
            runs = [day + timedelta(hours=5), day + timedelta(hours=5, minutes=59), day + timedelta(hours=6)]
            # targets k minutes past day+30 d: distinct per day, so adjacent run-days never share a target
            rows += [(sid, r, day + timedelta(days=30, minutes=k), float(j), r)
                     for k in range(TARGETS) for j, r in enumerate(runs)]
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO montney.obs_forecast (series_id, as_of, ts, value, fetched_at) "
                            "VALUES (%s, %s, %s, %s, %s)", rows)

        stop, errors, written = threading.Event(), [], []
        newest, oldest = days[-1] + timedelta(hours=12), days[0] + timedelta(hours=12)

        def writer():
            w = connect(dbname=conn.info.dbname)
            try:
                n = 0
                while not stop.is_set():
                    a, b = newest + timedelta(microseconds=n), oldest + timedelta(microseconds=n)
                    with w.transaction():
                        w.execute("INSERT INTO montney.obs_forecast (series_id, as_of, ts, value, fetched_at) "
                                  "VALUES (%s, %s, %s, 1, %s)", (wid, a, a + timedelta(days=40), a))
                        time.sleep(0.002)                     # hold the newest old chunk a moment
                        w.execute("INSERT INTO montney.obs_forecast (series_id, as_of, ts, value, fetched_at) "
                                  "VALUES (%s, %s, %s, 1, %s)", (wid, b, b + timedelta(days=40), b))
                    written.append(2)
                    n += 1
            except Exception as e:                            # noqa: BLE001 - reported below
                errors.append(f"writer: {type(e).__name__}: {str(e).splitlines()[0]}")
            finally:
                w.close()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        try:
            conn.execute("CALL montney.thin_forecasts(0, '{\"keep_all_days\": 14}')")
        except Exception as e:                                # noqa: BLE001
            errors.append(f"thin_forecasts: {type(e).__name__}: {str(e).splitlines()[0]}")
        thin_s = time.monotonic() - t0
        time.sleep(0.05)
        stop.set()
        t.join(timeout=30)
        assert not t.is_alive(), "writer did not stop"

        kept = conn.execute("SELECT count(*) FROM montney.obs_forecast WHERE series_id = %s", (sid,)).fetchone()[0]
        wrote = conn.execute("SELECT count(*) FROM montney.obs_forecast WHERE series_id = %s", (wid,)).fetchone()[0]
        comp = conn.execute(
            "SELECT count(*) FILTER (WHERE is_compressed), count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = 'montney' AND hypertable_name = 'obs_forecast' AND range_end <= %s",
            (today - timedelta(days=14),)).fetchone()
        return {"run": i, "errors": errors, "kept": kept, "want_kept": 2 * TARGETS * len(days),
                "writer_rows": wrote, "writer_committed": sum(written),
                "old_chunks_compressed": f"{comp[0]}/{comp[1]}", "thin_s": round(thin_s, 2)}


def _bad(r: dict) -> bool:
    return bool(r["errors"]) or r["kept"] != r["want_kept"] or r["writer_rows"] != r["writer_committed"] \
        or r["writer_committed"] == 0


def _migrations_up_to(tmp_path: Path, last: int) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    for m in migrate.discover():
        if m.version <= last:
            shutil.copy(m.path, d / m.path.name)
    return d


def test_thinning_concurrent_with_writes_never_deadlocks():
    assert RUNS > 0
    results = [_one_run(i, migrate.MIGRATIONS_DIR) for i in range(RUNS)]
    for r in results:
        print(r)
    bad = [r for r in results if _bad(r)]
    print(f"0005: {RUNS} concurrent runs, {len(bad)} bad; writer rows per run: "
          f"{min(r['writer_committed'] for r in results)}-{max(r['writer_committed'] for r in results)}")
    assert not bad, bad


RED_RUNS = 5


def test_RED_same_load_deadlocks_on_0004(tmp_path):
    """Seeded red (Wellsite §4.1): the same load against 0004's thin_forecasts (0005 left out). 0004 holds one
    chunk's AccessExclusiveLock while asking for the next, so the reverse-order writer closes a cycle. First
    measured in CI run 41268834503: 20 of 20 runs deadlocked on 0004, 0 of 20 on 0005. If this ever stops
    reproducing, the load no longer tests what the guard above claims, and this test says so."""
    d = _migrations_up_to(tmp_path, 4)
    results = [_one_run(i, d) for i in range(RED_RUNS)]
    for r in results:
        print(r)
    dead = [r for r in results if any("Deadlock" in e for e in r["errors"])]
    print(f"0004: {RED_RUNS} concurrent runs, {len(dead)} deadlocked")
    assert dead, "0004 no longer deadlocks under this load: the concurrency guard has lost its red"
