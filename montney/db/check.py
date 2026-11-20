"""`montney db check`: the install checks from docs/db/INSTALL.md step 4, plus a smoke test.

Fails closed: a check that can't run (no connection, missing extension, an error) is a FAIL, never
"skipped" (Wellsite §4.2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from montney.db import migrate

PG_MAJOR = 18
MIN_TIMESCALE = (2, 31, 0)

# Retention as ruled by PLAN P3 (+ the 7-day forecast re-anchor age, RETENTION.md). None = kept forever.
EXPECTED_RETENTION = {
    "obs_fast": "60 days",
    "obs_hourly": "2 years",
    "obs_daily": None,
    "obs_forecast": "1 year 7 days",
    "raw_blob": "7 days",
    "obs_fast_1h": "2 years",
    "obs_fast_1d": None,
    "obs_hourly_1d": None,
}


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split("-")[0].split(".")[:3])


def retention_mismatches(conn) -> list[str]:
    """Compare the live retention policies with EXPECTED_RETENTION. Empty list = all as ruled.
    Fails closed: a table that isn't found at all is reported, not skipped."""
    rows = conn.execute("""
        WITH tables AS (
            -- (name, schema/name the jobs view may use: the table itself, or for a rollup either
            --  the view or its materialization hypertable, depending on the Timescale version)
            SELECT hypertable_name AS name, hypertable_schema AS s1, hypertable_name AS n1,
                   hypertable_schema AS s2, hypertable_name AS n2
            FROM timescaledb_information.hypertables WHERE hypertable_schema = 'montney'
            UNION ALL
            SELECT view_name, view_schema, view_name,
                   materialization_hypertable_schema, materialization_hypertable_name
            FROM timescaledb_information.continuous_aggregates WHERE view_schema = 'montney'
        )
        SELECT t.name,
               array_agg(DISTINCT (j.config ->> 'drop_after')::interval) FILTER (WHERE j.job_id IS NOT NULL)
        FROM tables t
        LEFT JOIN timescaledb_information.jobs j
               ON j.proc_name = 'policy_retention'
              AND ((j.hypertable_schema = t.s1 AND j.hypertable_name = t.n1)
                OR (j.hypertable_schema = t.s2 AND j.hypertable_name = t.n2))
        GROUP BY t.name
    """).fetchall()
    live = {name: drops or [] for name, drops in rows}
    problems = []
    for name, want in EXPECTED_RETENTION.items():
        if name not in live:
            problems.append(f"{name}: table or rollup not found")
            continue
        got = live[name]
        if want is None:
            if got:
                problems.append(f"{name}: should be kept forever, has retention {got}")
            continue
        want_iv = conn.execute("SELECT %s::interval", (want,)).fetchone()[0]
        if got != [want_iv]:
            problems.append(f"{name}: retention {got or 'none'}, expected {want}")
    return problems


def run(conn=None) -> list[Result]:
    out: list[Result] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append(Result(name, bool(ok), detail))

    try:
        if conn is None:
            from montney.db import connect
            conn = connect()
    except Exception as e:  # no connection = FAIL, not skip
        add("connect", False, f"{type(e).__name__}: {e}")
        return out
    add("connect", True, "ok")

    try:
        v = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()[0]
        add("postgres", v // 10000 == PG_MAJOR, f"server_version_num {v}, need {PG_MAJOR}.x")
        row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        if not row:
            add("timescaledb", False, "extension not created in this database")
        else:
            add("timescaledb", _ver(row[0]) >= MIN_TIMESCALE,
                f"version {row[0]}, need >= {'.'.join(map(str, MIN_TIMESCALE))}")
        lic = conn.execute("SELECT current_setting('timescaledb.license', true)").fetchone()[0]
        add("license", lic == "timescale",
            f"timescaledb.license = {lic!r}; 'apache' (the -oss build) has no columnstore, rollups or policies")
    except Exception as e:
        add("versions", False, f"{type(e).__name__}: {e}")
        return out

    try:
        st = migrate.status(conn)
        pending = [r["name"] for r in st if r["state"] != "applied"]
        add("migrations", not pending, "all applied" if not pending else f"pending: {pending}")
    except Exception as e:
        add("migrations", False, f"{type(e).__name__}: {e}")

    try:
        problems = retention_mismatches(conn)
        add("retention", not problems, "as ruled (PLAN P3)" if not problems else "; ".join(problems))
    except Exception as e:
        add("retention", False, f"{type(e).__name__}: {e}")

    try:
        n = orphan_flags(conn)
        add("flags", n == 0, "no orphan obs_flag rows" if n == 0
            else f"{n} obs_flag rows have no observation (prune_obs_flags not running?)")
    except Exception as e:
        add("flags", False, f"{type(e).__name__}: {e}")

    try:
        add("thinning", *thinning_lag(conn))
    except Exception as e:
        add("thinning", False, f"{type(e).__name__}: {e}")

    try:
        gaps = realized_gaps(conn)
        add("realized", not gaps, "no ended day without its realized value (L-037)" if not gaps
            else f"{len(gaps)} ended (series, day) with vintages but no realized value, e.g. {gaps[:3]} "
                 "(is fill_realized running?)")
    except Exception as e:
        add("realized", False, f"{type(e).__name__}: {e}")

    try:   # L-081: a WARN detail on a passing row (it's a budget to watch, not an install fault)
        from montney.db import reqbudget
        use = reqbudget.measure(conn)
        ws = use.warnings()
        tot = sum(r.req_per_h for r in use.regions.values())
        add("requests", True, ("WARN: " + "; ".join(ws)) if ws else
            f"{tot:.0f} req/h over {use.hours:.1f} h, within sources/BUDGET.yaml"
            + ("" if use.judged else " (too little data to judge)"))
    except Exception as e:
        add("requests", False, f"{type(e).__name__}: {e}")

    add(*_smoke(conn))
    return out


REALIZED_GAPS_SQL = """
    SELECT DISTINCT r.key, f.ts
    FROM montney.series b
    JOIN montney.series r ON r.key = b.key || '.realized' AND r.tier = 'daily'
    JOIN montney.obs_forecast f ON f.series_id = b.id
    WHERE b.tier = 'forecast'
      AND f.ts >= (now() AT TIME ZONE 'UTC') - make_interval(days => %s) - INTERVAL '32 days'
      AND NOT EXISTS (SELECT 1 FROM montney.obs_daily d WHERE d.series_id = r.id AND d.ts = f.ts)
    ORDER BY 1, 2
"""


def realized_gaps(conn, window_days: int = 14, grace_hours: int = 2) -> list[tuple]:
    """Ended outage periods (Alberta days, or months for the monthly series) whose end falls inside
    fill_realized's window, that have vintages but no realized value (L-036/L-037). `grace_hours` after the
    end gives the hourly fill time to run. Period ends come from America/Edmonton (L-029: Python keeps the
    zone). A period with no vintage at all can't be filled and isn't listed."""
    from datetime import timedelta

    from montney.db.backfill import REALIZED_SUFFIX, period_end

    now = conn.execute("SELECT now() AT TIME ZONE 'UTC'").fetchone()[0]
    out = []
    for k, ts in conn.execute(REALIZED_GAPS_SQL, (window_days,)).fetchall():
        end = period_end(k[:-len(REALIZED_SUFFIX)], ts)
        if end is not None and now - timedelta(days=window_days) <= end <= now - timedelta(hours=grace_hours):
            out.append((k, ts))
    return out


THIN_MAX_LAG_DAYS = 2


def thinning_lag(conn) -> tuple[bool, str]:
    """thin_forecasts skips silently when another session holds its advisory lock. A stuck lock would
    stop thinning for good, so FAIL when the thinned-through watermark lags the due point by more than
    THIN_MAX_LAG_DAYS. With no watermark yet, FAIL only if forecast runs exist that should have been
    thinned by now."""
    from datetime import datetime, timedelta

    row = conn.execute("SELECT config FROM timescaledb_information.jobs "
                       "WHERE proc_schema = 'montney' AND proc_name = 'thin_forecasts'").fetchone()
    if row is None:
        return False, "thin_forecasts job is missing"
    cfg = row[0] or {}
    keep = int(cfg.get("keep_all_days", 14))
    now = conn.execute("SELECT now() AT TIME ZONE 'UTC'").fetchone()[0]
    due = now - timedelta(days=keep)
    slack = timedelta(days=THIN_MAX_LAG_DAYS)
    if cfg.get("thinned_through"):
        through = datetime.fromisoformat(str(cfg["thinned_through"]))
        lag = due - through
        return lag <= slack, f"thinned through {through:%Y-%m-%d %H:%M}Z, lag {lag.days} d (max {THIN_MAX_LAG_DAYS})"
    oldest = conn.execute("SELECT min(as_of) FROM montney.obs_forecast").fetchone()[0]
    if oldest is None or oldest > due - slack:
        return True, "not run yet; nothing due"
    return False, f"never ran, but forecast runs from {oldest:%Y-%m-%d} are due for thinning"


ORPHAN_FLAGS_SQL = """
    SELECT count(*) FROM montney.obs_flag f
    WHERE NOT EXISTS (SELECT 1 FROM montney.obs_fast o
                      WHERE o.series_id = f.series_id AND o.ts = f.ts AND o.as_of = f.as_of)
      AND NOT EXISTS (SELECT 1 FROM montney.obs_hourly o
                      WHERE o.series_id = f.series_id AND o.ts = f.ts AND o.as_of = f.as_of)
      AND NOT EXISTS (SELECT 1 FROM montney.obs_daily o
                      WHERE o.series_id = f.series_id AND o.ts = f.ts AND o.as_of = f.as_of)
      AND NOT EXISTS (SELECT 1 FROM montney.obs_forecast o
                      WHERE o.series_id = f.series_id AND o.as_of = f.as_of AND o.ts = f.ts)
"""


def orphan_flags(conn) -> int:
    """obs_flag rows whose observation vintage no longer exists (ERD.md: a flag lives as long as its row)."""
    return conn.execute(ORPHAN_FLAGS_SQL).fetchone()[0]


def _smoke(conn) -> tuple[str, bool, str]:
    """Hypertable + columnstore round trip in a scratch schema, dropped afterwards."""
    sch = f"montney_check_{os.getpid()}"
    try:
        conn.execute(f"CREATE SCHEMA {sch}")
        conn.execute(f"CREATE TABLE {sch}.t (ts timestamp NOT NULL, k integer NOT NULL, v double precision)")
        conn.execute(f"SELECT create_hypertable('{sch}.t', by_range('ts', INTERVAL '1 day'))")
        conn.execute(f"ALTER TABLE {sch}.t SET (timescaledb.enable_columnstore = true, "
                     "timescaledb.segmentby = 'k', timescaledb.orderby = 'ts')")
        conn.execute(f"INSERT INTO {sch}.t SELECT timestamp '2026-01-01' + i * INTERVAL '1 hour', i % 3, i "
                     "FROM generate_series(0, 71) i")
        chunks = [r[0] for r in conn.execute(f"SELECT show_chunks('{sch}.t')").fetchall()]
        for c in chunks:
            conn.execute("CALL convert_to_columnstore(%s::regclass)", (c,))
        n, total = conn.execute(f"SELECT count(*), sum(v) FROM {sch}.t").fetchone()
        compressed = conn.execute(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = %s AND is_compressed", (sch,)).fetchone()[0]
        ok = n == 72 and total == sum(range(72)) and compressed == len(chunks) > 0
        return ("smoke", ok, f"{n} rows, {compressed}/{len(chunks)} chunks in the columnstore")
    except Exception as e:
        return ("smoke", False, f"{type(e).__name__}: {e}")
    finally:
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
        except Exception:
            pass
