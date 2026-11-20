"""D29 read API on a real database: PostgresStore.day_rollup / series_bounds behind montney/readapi.py.
The daily means come from the real-time rollups (obs_hourly_1d), bucketed at 06:00 UTC (L-029)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from montney import readapi
from montney.db.pgstore import PostgresStore
from montney.db.testing import add_series, scratch_db


def _day0() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)


def test_day_means_from_rollups_and_stored_daily():
    day = _day0()                                  # inside the rollups' real-time window
    with scratch_db() as conn:
        pool = add_series(conn, "aeso.price.pool", "hourly")
        rows = [(pool, day + timedelta(hours=6 + h), 10.0 + (h % 2) * 20) for h in range(24)]          # full: mean 20
        rows += [(pool, day + timedelta(days=1, hours=6 + h), 50.0) for h in range(20)]               # 20/24: out
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO montney.obs_hourly (series_id, ts, as_of, value, fetched_at) "
                            "VALUES (%s, %s, %s, %s, now())", [(s, t, t, v) for s, t, v in rows])
        fr = add_series(conn, "ngtl.gdsr.field_receipts.flow", "daily")
        conn.execute("INSERT INTO montney.obs_daily (series_id, ts, as_of, value, fetched_at) VALUES "
                     "(%s, %s, %s, 14000, now()), (%s, %s, %s, 14100, now())",
                     (fr, day, day + timedelta(hours=10), fr, day, day + timedelta(hours=30)))
        st = PostgresStore(conn)
        d = day.date()
        out = readapi.series(st, "aeso.price.pool", (d - timedelta(days=2)).isoformat(),
                             (d + timedelta(days=2)).isoformat())
        assert out["points"] == [[d.isoformat(), 20.0]], out
        assert out["source"] == "test_src"
        out = readapi.series(st, "ngtl.gdsr.field_receipts.flow", d.isoformat(), d.isoformat())
        assert out["points"] == [[d.isoformat(), 14100.0]], out                    # latest vintage
        assert st.series_bounds("ngtl.gdsr.field_receipts.flow") == (d, d)
        assert st.series_bounds("aeso.csd.ail") == (None, None)
        rows = {r["metric"]: r for r in readapi.list_series(st, today=d + timedelta(days=3))}
        assert rows["aeso.price.pool"]["first_date"] == d.isoformat() == rows["aeso.price.pool"]["last_date"]
        assert rows["ngtl.gdsr.field_receipts.flow"]["last_date"] == d.isoformat()
