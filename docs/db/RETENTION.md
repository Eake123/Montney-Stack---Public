# Retention, change-only writes and the storage budget (P3 draft)

**Status: DRAFT.** The tier rules come from PLAN P3 and are fixed. The intervals marked *(tune)* get
re-derived from measured sizes after the install, never widened to make a number fit (Wellsite §4.7). Tables:
[`ERD.md`](ERD.md).

SQL below targets TimescaleDB 2.31 (the hypercore/columnstore API: `enable_columnstore`,
`add_columnstore_policy`). Every statement is run against the CI service container before the freeze. If 2.31
rejects one, the migration test goes red. A doc can't drift from the engine unnoticed.

## The tiers

| PLAN rule | where it lives | Timescale mechanism |
|---|---|---|
| minute-level: full detail 60 days, then hourly | `obs_fast` → `obs_fast_1h` | retention policy 60 d on `obs_fast`, continuous aggregate (cagg) `obs_fast_1h` |
| hourly: kept 2 years, then daily | `obs_hourly`, `obs_fast_1h` → `*_1d` | retention policy 2 y; caggs `obs_fast_1d` and `obs_hourly_1d`, each built on its raw table |
| daily: forever | `obs_daily`, `obs_fast_1d`, `obs_hourly_1d` | no retention policy |
| forecasts: every run 14 days, then one run per day, deleted after 1 year | `obs_forecast` (time column = `as_of`) | job `thin_forecasts` (user-defined action), which also compresses each chunk once it's thinned; retention 1 y + 7 d, with unchanged live points re-anchored every 7 d |
| weather grids: current + previous run only | files in `<home>/grids/` | v1 `grids.py` already deletes older runs; no DB rows |
| raw payloads: 7 days | `raw_blob` | retention policy 7 d |

### `obs_fast` (sub-hourly)

```sql
SELECT create_hypertable('montney.obs_fast', by_range('ts', INTERVAL '1 day'));        -- (tune)
ALTER TABLE montney.obs_fast SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'series_id', timescaledb.orderby = 'ts, as_of');
CALL add_columnstore_policy('montney.obs_fast', after => INTERVAL '2 days');            -- (tune)
SELECT add_retention_policy('montney.obs_fast', drop_after => INTERVAL '60 days');      -- PLAN

-- Hourly rollup. Store sum + n (not avg) so the daily level on top stays exact.
CREATE MATERIALIZED VIEW montney.obs_fast_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT series_id,
       time_bucket(INTERVAL '1 hour', ts) AS ts,
       sum(value) AS sum, count(value)::int4 AS n,
       min(value) AS min, max(value) AS max, last(value, ts) AS last
FROM montney.obs_fast
GROUP BY 1, 2
WITH NO DATA;
SELECT add_continuous_aggregate_policy('montney.obs_fast_1h',
    start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes');
ALTER MATERIALIZED VIEW montney.obs_fast_1h SET (timescaledb.enable_columnstore = true);
CALL add_columnstore_policy('montney.obs_fast_1h', after => INTERVAL '14 days');
SELECT add_retention_policy('montney.obs_fast_1h', drop_after => INTERVAL '2 years');   -- PLAN

-- Daily rollup, kept forever, built on the raw table (not on obs_fast_1h; see below). Day = Alberta day.
CREATE MATERIALIZED VIEW montney.obs_fast_1d
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT series_id,
       time_bucket(INTERVAL '1 day', ts, INTERVAL '6 hours') AS ts,   -- L-029: Alberta day = UTC-6
       sum(value) AS sum, count(value)::integer AS n,
       min(value) AS min, max(value) AS max, last(value, ts) AS last
FROM montney.obs_fast
GROUP BY 1, 2
WITH NO DATA;
SELECT add_continuous_aggregate_policy('montney.obs_fast_1d',
    start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '6 hours');
```

Rules this depends on:
- **The daily rollups read the raw tables, not the hourly rollup** (changed in the build, migration
  0003). Stacking a 6-hour-offset day on a 0-offset hourly rollup risks a TimescaleDB bucket-origin mismatch.
  The 30-day refresh window sits inside the 60-day raw retention, so the raw table always covers it. The
  result is the same: sum/n/min/max/last per Alberta day.
- The refresh windows (7 d and 30 d) sit well inside the 60-day raw window, so a rollup never refreshes over
  raw data that's already been dropped. That would empty the bucket.
- **The converse rule:** never run a manual `refresh_continuous_aggregate` over a range older than the raw
  retention (60 d for `obs_fast`, 2 y for `obs_hourly`). It would replace good buckets with empty ones.
  `montney db` refuses such a range.
- **Backfills refresh explicitly.** Policies only cover their own offsets, so the DuckDB migration (and any
  later backfill) calls `refresh_continuous_aggregate` over the backfilled range itself, inside the raw window,
  before raw retention can drop it.
- `obs_fast` holds one vintage per point (`as_of: ts` style). A sub-hourly series with run vintages is a
  forecast and goes to `obs_forecast`. CI enforces this ([spec contract](#spec-contract)), because a rollup over
  several vintages would average them together.
- **Daily buckets use a fixed +6 h offset (ruled: L-029).** Alberta midnight = 06:00 UTC; Alberta is
  permanent UTC-6 from 2026-11-01. That's exact for every sub-daily row we hold: the oldest is from
  2026-10-24, and everything before 2026-11-01 is MDT, which is also UTC-6. The conditions L-029 sets, which
  the build follows:
  - The offset appears **only** in rollup and forecast-thinning SQL. Python keeps using `America/Edmonton`.
  - Continuous aggregates need literal `time_bucket` arguments, so `INTERVAL '6 hours'` repeats in each daily
    cagg and in `thin_forecasts`. **Every occurrence carries an `-- L-029` comment.**
  - `test_db_day_boundary` checks two things. All the L-029 occurrences in `migrations/` are identical. It
    finds **every `time_bucket(...)` call that has an offset argument** in `migrations/`. Each one must carry the
    `-- L-029` marker, so an unmarked offset fails too, and all offsets must be equal. `schedule_interval =>
    INTERVAL '6 hours'` isn't a `time_bucket` call, so it doesn't count. And a
    known instant pins the boundary on both sides: 05:59Z goes to the previous Alberta day, 06:00Z to the new
    one. Seeded red: change one occurrence to `'7 hours'`, and the test must fail.
  - **Any backfill of pre-2026-11-01 sub-daily history needs its own ruling first,** because its MST winter
    days would roll up an hour off for good.
- Gas-day series are daily, so they're never in this tier. Their day is the NGTL gas day already
  (`ts` = gas day).

### `obs_hourly`

Same shape: chunk 7 d *(tune)*, columnstore after 7 d *(tune)*, retention 2 y (PLAN), and a cagg
`obs_hourly_1d` (the same SQL as `obs_fast_1d`, built straight on `obs_hourly` with `sum/count/min/max/last`),
refreshed with `start_offset => 30 days`.

### `obs_daily`

Chunk 365 d, columnstore after 90 d *(tune)*. **No retention.** Revisions (GDSR re-publishes a gas day) land
as new vintages. On a compressed chunk that's an insert into the columnstore, which 2.31 supports.

### `obs_forecast`

```sql
SELECT create_hypertable('montney.obs_forecast', by_range('as_of', INTERVAL '1 day'));
ALTER TABLE montney.obs_forecast SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'series_id', timescaledb.orderby = 'as_of, ts');
-- PLAN's 1 year, plus the 7-day re-anchor age (below), so an as-of query anywhere in the last year still finds
-- a vintage at most 7 days older than the moment asked about.
SELECT add_retention_policy('montney.obs_forecast', drop_after => INTERVAL '1 year 7 days');
SELECT add_job('montney.thin_forecasts', INTERVAL '1 hour',
               config => '{"keep_all_days": 14}');
```

`thin_forecasts(job_id, config)` is a PL/pgSQL procedure. For every whole run-day older than 14 days that
hasn't been thinned yet (a watermark in `config`, so it catches up after downtime and is idempotent), it
keeps, per `(series_id, ts)`, only the **last row of that run-day** and deletes the rest. Then it compresses
each thinned chunk (`compress_chunk`). That's why `obs_forecast` has no columnstore policy: a chunk is only
compressed after it's thinned.

**Concurrency (migrations 0004-0005).**
- **One run at a time:** an advisory lock means a second caller skips. `db check` FAILs if the
  thinned-through watermark lags more than 2 days, so a stuck lock can't stop thinning silently.
- **Chunk compression (0005):** each chunk is compressed in its own transaction with `lock_timeout = 2s`,
  and a chunk a writer is holding is skipped and retried on the next run. 0004 compressed all thinned chunks
  in one transaction, holding one chunk's lock while asking for the next. Against a writer touching two old
  chunks in reverse order, that deadlocked in **20 of 20 runs; 0005 in 0 of 20** (CI run 41268834503).
  `tests/test_db_concurrency.py` runs that load on 0005 (20 runs, all clean) and keeps the 0004 run as a
  seeded red (5 runs, must deadlock). The writer side (the write path, #57) retries on `deadlock_detected` / `lock_not_available`, because
  `deadlock_timeout` (1 s) is shorter than the 2 s lock wait, so the writer can be the one chosen as victim.

```sql
DELETE FROM montney.obs_forecast f
WHERE f.as_of >= $day AND f.as_of < $day + INTERVAL '1 day'
  AND EXISTS (SELECT 1 FROM montney.obs_forecast g
              WHERE g.series_id = f.series_id AND g.ts = f.ts
                AND g.as_of > f.as_of AND g.as_of < $day + INTERVAL '1 day');
```

The run-day is the Alberta day of `as_of`: `$day` starts at 06:00 UTC, the same fixed +6 h boundary as the
rollups. It's computed with `time_bucket(INTERVAL '1 day', as_of, INTERVAL '6 hours')`, and that literal is
commented `-- L-029` too. With change-only writes, a run that matched the
previous one wrote no row. The value "as known at the end of day D" is still the latest vintage at or before
D's end, and thinning keeps exactly that, so the two rules compose.

**Thinning ratio (an assumption, to be measured).** The ~0.15 GB forecast figure below assumes thinning
keeps roughly one run's worth of rows per day (~4,000 of ~75,000 change-only rows). That holds when most
changes within a day get superseded by a later run the same day. Worst case, thinning gains nothing: ~0.11 GB
for the 14 uncompressed days plus 75,400 × 351 × 10 B ≈ 0.26 GB, so **~0.6 GB in total, about 12 % of 5
GB**. Either way it fits. The build measures the real ratio.

### Re-anchoring long-horizon forecasts

Change-only writes plus chunk drops on `as_of` would lose a forecast point that never changes. Take an
`aeso.outage` value for 2028-06, first written in 2026-10 and republished unchanged ever since. Its only
vintage sits in a 2026-10 chunk. When that chunk drops, the point has no vintage left, and `forecast_latest`
still says "unchanged", so nothing would ever rewrite it. The baseline shows this is real: `aeso.outage` runs
to 2028-10, and 66 % of its rows are unchanged repeats.

**Rule:** a forecast point that's still in the feed is written again, unchanged, once its newest written
vintage (`forecast_latest.as_of`) is more than **7 days** old. The cost is about one row per live point per
week (≈ 630 rows/day for `aeso.outage`'s ~4,400 points), next to ~75,000 change-only rows/day. With
retention at 1 year + 7 days:
- every point still being published keeps a vintage at most 7 days old;
- an as-of query anywhere in the last year finds the vintage that was current then;
- a point that drops out of the feed keeps its last vintage until that chunk ages out, which is correct
  history.

**Seeded red:** `test_db_forecast_reanchor` writes a 2-years-ahead point and re-fetches it unchanged with the
clock advanced 8 days, expecting exactly 1 new vintage. It then drops the original chunk and checks that an
as-of query 3 days ago still returns the value. With `MONTNEY_DB_REANCHOR_DAYS=off` (disabled; `off`, not `0`, so a zero can never be read as "re-anchor on every fetch") the test must
fail.

### `raw_blob`, `fetch_run`, events

- `raw_blob`: chunk 1 d, retention 7 d (PLAN). The payload is zstd-compressed by the runner. A blob whose
  sha256 matches the source's previous blob stores `payload = NULL` (a pointer, not a copy). **Bulk sources**
  (Petrinex and the like) set `raw: none` in the spec: the file is parsed as a stream, only mapped facilities
  are kept (aggregated), and the download is deleted after parsing (PLAN P3).
- `fetch_run`: a plain table (see ERD.md). A daily `prune_fetch_runs` job deletes rows older than 1 y
  *(proposal)*.
- `event` / `event_scope`: **kept forever (ruled: L-029).** `du` counts them; revisit if events pass 1 % of a
  region's budget.
- `grid_run`: metadata only (L-029). The grid files themselves keep v1's current + previous run rule.

## Change-only writes

**Rule (PLAN):** an unchanged value from the last fetch writes no row.

**One comparator.** An incoming point `(series, ts, as_of, value)` is compared with **the latest stored
vintage of the same `(series, ts)`**. If they're equal (`IS NOT DISTINCT FROM`), nothing is sent. That's
true in every tier. Only where the comparator is read from differs:

| case | where the latest vintage is read from |
|---|---|
| 1. `as_of: ts` series re-fetched (CSD, CSR, AESO actuals): same `(series, ts, as_of)` | the tier table, over the batch's `ts` range |
| 2. new vintage of a daily series (GDSR revision) | `obs_daily`, over the batch's `ts` range (PK prefix scan) |
| 3. new forecast run | `forecast_latest(series_id, ts) → (as_of, value)` |
| 4. forecast point whose newest vintage is more than 7 days old | same as 3, but **written anyway** ([re-anchoring](#re-anchoring-long-horizon-forecasts)) |

**Where the filtering happens: in the runner, before anything is sent.**
1. Per source, the runner keeps the last written `(series, ts) → value` for the window it re-fetches, in
   memory. On a cold start, or on a cache miss, it loads them with one range query per tier (`ts` between the
   batch's min and max). That query only reads. Even when it touches a compressed chunk, nothing gets
   rewritten.
2. Only new or changed rows go to Postgres (`COPY` into a temp table, then `INSERT`). **The cache is
   updated only after the transaction commits.** If the commit fails, the cache would otherwise claim rows
   that were never stored, and the next fetch would skip them as "unchanged". Seeded red:
   `test_db_change_only` forces a commit failure, then re-fetches the same batch, and expects the rows to be
   sent again.
3. `ON CONFLICT` stays as the backstop for a race or a stale cache:
   ```sql
   INSERT INTO montney.obs_fast AS t (series_id, ts, as_of, value, fetched_at)
   SELECT series_id, ts, as_of, value, fetched_at FROM _stage
   ON CONFLICT (series_id, ts, as_of) DO UPDATE
     SET value = excluded.value, fetched_at = excluded.fetched_at
     WHERE t.value IS DISTINCT FROM excluded.value;
   ```
   The backstop isn't free. A conflicting row is still locked (WAL-logged, page dirtied) even when the
   `WHERE` is false. On a compressed chunk, Timescale decompresses the conflicting batch to check it. That's
   why the pre-filter is the mechanism and the backstop is only the safety net. A re-fetch window that
   crosses the columnstore boundary (2 d for `obs_fast`, 90 d for `obs_daily`) then costs reads, not
   rewrites. The build measures this. A real revision to an old point does decompress a batch, and that's
   accepted: it's rare and it's a real change.

- Equality is exact `float8` (`IS NOT DISTINCT FROM`, so NULL = NULL). The same input through the same unit
  conversion gives the same bits. A source whose numbers jitter in the last digit gets `round:` in its spec,
  never a tolerance in the store.
- Every fetch still updates `series_state.last_seen_at` (and `fetch_run.n_unchanged`). Staleness, "last
  fetched" and the status dots therefore stay right when nothing was written.
- **No "same as the previous timestamp" (step) skipping.** It would cut another ~50 % of rows (baseline
  below). But it biases hourly averages (the cagg would average only the change points), and it breaks "newest
  point at or before now". Compression already stores a run of identical floats in about a bit each, so on
  disk it saves almost nothing past the 2-day uncompressed window (about 12 MB today). Not worth the
  semantics.
- **Tests, with a seeded red per case** (`tests/test_db_change_only.py`). `MONTNEY_DB_DEDUPE=off` means v1
  behaviour: no pre-filter, and an unconditional upsert (no `WHERE` on the backstop). Each case below must
  pass with dedupe on and **fail** with it off. A count-of-zero assertion alone can't fail for case 1, because
  the PK already stops duplicates (Wellsite §4.1).

  | case | asserts (dedupe on) | why the red fails |
  |---|---|---|
  | 1. identical re-fetch of an `as_of: ts` batch | rows sent = 0; the existing row's `xmin` and `fetched_at` are unchanged | off: the unconditional upsert rewrites the row, so `xmin` and `fetched_at` move |
  | 1b. one value changed | exactly that row's `xmin` moves | (positive control) |
  | 2. GDSR re-published, same values, new `as_of` | 0 new vintages | off: one new vintage per point |
  | 3. identical forecast run | 0 new rows in `obs_forecast` | off: a full run of rows |
  | 4. re-anchor | see [re-anchoring](#re-anchoring-long-horizon-forecasts) | `MONTNEY_DB_REANCHOR_DAYS=off` |

  Each test prints its row counts, and an empty batch counts as NO-RESULT, not a pass (Wellsite §4.2).

## Spec contract

Every source spec must declare `retention:`. CI fails otherwise (a new `tests/test_db_retention.py`, owned by
database). Values:

| value | meaning |
|---|---|
| `fast` | sub-hourly points, one vintage per point |
| `hourly` | hourly points |
| `daily` | daily, monthly or longer; revisions are kept as vintages |
| `forecast` | `as_of: fetch` runs |
| `live` | `mode: live`, never persisted |
| `events` / `grid` | sources that only emit events or grids |

A spec sets a source default and can override it per block (`aeso_price` has actuals and a forecast in one
spec). `type: python` specs may give a glob → tier map with a mandatory `default` (L-036).

**The cross-check** (`montney/db/tiercheck.py`, `tests/test_db_spec_contract.py`, L-037 (2)) runs over every
spec that declares `retention:` and checks each declared tier against its series' `freq` and `as_of`:
- `fast` needs sub-hourly, `hourly` needs `1h`, and `daily` needs `D`/`W`/`M`/`Q`/`Y`;
- `as_of: fetch` must be `forecast` (or `live`), and `forecast` can't be `as_of: ts`;
- `live` ⇔ `mode: live`;
- an unknown or missing freq fails closed.

Python connectors declare ids and freqs but not an as_of mode, so for them only the freq is checked. Their
run-time ids are resolved per write by the runner (L-036). Seeded reds cover each rule. Presence of the key and
its syntax are validated by the orchestrator in `connectors/base.py` / `tests/test_specs.py` (L-035 Q-P3).

## Budget accounting (`montney du`)

Budgets (L-003): **`ab_nebc` = 5 GB, `rest` = 95 GB**, with GB = 10⁹ bytes. Each series belongs to a region
(`series.region_id`, defaulting to its source's). Today every source is `ab_nebc`.

**What counts:** heap + index + compressed bytes of every observation tier and its rollups, `event`/
`event_scope`, `raw_blob`, `fetch_run`, plus files under `<home>` (grids, cache) attributed by source.
Catalog/dimension tables and indexes form a `_system` line, counted toward the 100 GB total but not a region.
WAL (capped by `max_wal_size`) and the backups on G: are shown but not counted. **Backups are reported
separately, and `du` warns when they pass 25 GB** (L-029; Erik may veto).

**How bytes are attributed to a source** (Timescale reports size per chunk, not per series):
1. Per chunk: bytes from `chunks_detailed_size()` (plus `chunk_columnstore_stats()` for compressed chunks).
2. Rows per series in that chunk: `count(*) GROUP BY series_id`. That's cheap: chunks are segmented by
   `series_id`, and only chunks changed since the last pass are recounted (compressed chunks are close to
   immutable). Cached in `usage_chunk(chunk, series_id, rows, bytes)`.
3. The chunk's bytes are split by row share. Sum to series → source → region.
4. The result goes to `usage_snapshot(taken_at, region_id, source_id, tier, rows, bytes)`, refreshed by a
   job every 6 h and on demand by `montney du --refresh`. Between passes, the runner adds `rows written ×
   bytes/row of that tier` to the last snapshot, so the guard reacts within one fetch.

**Thresholds:**

| region usage | effect |
|---|---|
| < 80 % | `ok` |
| ≥ 80 % | every source in the region → `budget_state = 'warn'`: amber dot, a warning in the runner log and `montney du` |
| ≥ 100 % | → `blocked`: the runner still fetches (status stays live) but **writes nothing** for those sources. `fetch_run.ok = false`, error `budget: ab_nebc at 100.4 % of 5.00 GB, writes stopped`. The app and the API keep running |
| back under 95 % (retention freed space) | unblocked automatically (hysteresis, so it doesn't flap) |

An optional per-spec cap (`budget: 500MB`) blocks one runaway source before it fills the region. `du` also
shows each region's 7-day growth and its "days to 100 %". The status dot wiring (`/api/status`) is
orchestrator-owned, so it's a contract item in the build.

```
$ montney du
region    used      budget   %     7d growth  full in
ab_nebc   412 MB    5.00 GB  8.2   +9.1 MB/d  >1 y
rest      0 B       95.0 GB  0.0   -          -
_system   38 MB
source              region   tier      rows        bytes    state
ngtl_csr            ab_nebc  fast      1,203,114   21 MB    ok
...
```

(Illustrative only. The real numbers come after the install.)

## Size baseline (measured 2026-10-31, read-only)

Measured on Erik's machine without changing anything: DuckDB opened `read_only=True`, the CSV parsed in
memory. No data was copied or committed.

**Today's DuckDB store** (`%LOCALAPPDATA%\MontneyStack\montney.duckdb`):

| item | value |
|---|---|
| file | 10,235,904 B (9.8 MiB). 25 of 39 blocks used: about 3.8 MiB free inside the file |
| `observations` | 43,944 rows, 491 series |
| `events` / `fetch_log` | 192 / 75 rows |
| other files in the data dir | grids 6.3 MB (1 run × 4 vars), cache 3.6 MB |
| rows that are an unchanged repeat of an earlier vintage | 5,952 (13.5 %) |

**Rows by family** (`<family>.<report>`):

| family | series | rows | ts range |
|---|---|---|---|
| ngtl.gdsr | 52 | 17,730 | 2021-11-16 .. 2026-10-31 (CSV history + live) |
| ngtl.csr | 16 | 10,239 | 2026-10-24 .. 2026-11-01 |
| aeso.outage | 23 | 4,692 | 2026-10 .. 2028-10 (forecast) |
| aeso.wind / aeso.solar | 7 + 7 | 2,154 + 2,154 | forecast to 2026-11-07 |
| aeso.unit / aeso.plant / aeso.gen | 198 / 28 / 19 | 1,980 / 280 / 228 | 2026-10-31 .. |
| aeso.price / aeso.load | 3 / 2 | 727 / 691 | 2026-10-28 .. |
| wx.* (11 regions) | 99 | 2,469 | 2026-10-24 .. 2026-11-15 |
| ngtl.dop | 12 | 212 | |
| westcoast.* | 2 | 262 | 2026-08-05 .. |
| aeso.csd / aeso.intertie / power.ab | 5 / 3 / 3 | 60 / 36 / 30 | |

**Erik's CSV history:** `MultiDayGasDaySummaryReport.csv` (in his Downloads folder) is 243,905 B with 1,812
lines. It covers NGTL, 2021-11-16 .. 2026-10-30, 1,810 gas days, 17 columns. The v1 importer maps 10 of them
to 17,210 values. Those are the 16,910 pre-October `ngtl.gdsr` rows already in DuckDB, so **the history is
already imported**. Migrating means moving DuckDB rows, then spot-checking against this CSV. A one-day 2023
CSV (1,161 B) is also there, and it's a subset. No other NGTL/AESO CSVs were found under Downloads,
Documents, Desktop, `D:\montney` or `G:\My Drive` (depth 4-5).

**Growth at the steady runner cadence** (net new keys per successful fetch × scheduled fetches per day; a
short 2-6 h sample, so rough):

| source | interval | rows/day, all | unchanged-vintage % | rows/day, change-only |
|---|---|---|---|---|
| aeso_ets_wind_solar | 30m | 68,448 | 39 | 41,800 |
| aeso_ets_outages | 30m | 70,848 | 66 | 24,100 |
| aeso_ets_units | 5m | 58,579 | 0 | 58,579 |
| weather | 3h | 8,404 | 10 | 7,560 |
| aeso_csd | 5m | 7,848 | 0 | 7,848 |
| aeso_price | 15m | 7,472 | 95 | 370 |
| aeso_load | 30m | 4,656 | 78 | 1,020 |
| ngtl_csr | 15m | 3,840 | 0 | 3,840 |
| aeso_ets_outages_monthly | 6h | 1,056 | n/a (1 fetch) | ≤1,056 |
| ngtl_dop, ngtl_gdsr, gas_westcoast | 1h-30m | < 300 | | |
| **total** | | **≈ 231,000** | | **≈ 146,000** |

By tier (change-only): fast ≈ 70,700/day (274 series), forecast ≈ 75,400/day (134), hourly ≈ 180/day (3),
daily ≈ 80/day (68).

**What that means for the 5 GB `ab_nebc` budget.** These are planning figures, not measurements. They assume
~100 B/row uncompressed (heap + PK) and ~10 B/row compressed, and get replaced by real numbers after the
install.

| design | steady-state size |
|---|---|
| v1 behaviour in Postgres (keep every row, no compression, no tiers) | ~8.4 GB/year and growing. **Over budget in ~7 months** |
| change-only, no tiers, no compression | ~5.3 GB/year. Over budget in a year |
| **this design** (change-only + tiers + columnstore) | fast 60 d raw ≈ 55 MB; `obs_fast_1h` 2 y ≈ 0.1 GB; forecasts (14 d all runs, uncompressed until thinned, then ~1 run/day for a year) ≈ 0.15 GB, assuming thinning keeps ~1 run's worth per day ([thinning ratio](#obs_forecast)); daily + `*_1d` rollups < 10 MB/yr; raw_blob 7 d and grids ≈ 50 MB. **≈ 0.4 GB total, about 8 % of 5 GB**, growing only by the forever-daily tier. **Worst case** (thinning gains nothing): **≈ 0.6 GB, about 12 %** |

The forecast tier (wind/solar, outages) and the AESO unit snapshots are the biggest writers. P5's station-level
NGTL history and the Petrinex aggregates are mostly monthly/daily, so they're small. P2's entity inventories
will confirm.

### Re-projection after L-036 (outages) and Power's #70 pins (2026-11-01)

Same planning assumptions (~100 B/row uncompressed, ~10 B/row compressed), using the measured row rates above:
- **AESO daily outage revisions → `forecast`** (L-036). The baseline above already counted them in the forecast
  tier, because their data has as_of < ts. At ≈ 24,100 change-only rows/day: 14 days kept in full, uncompressed,
  is ≈ 337 k rows ≈ 34 MB. After thinning to ~1 run/day (≈ 1,476 points per run), a year is ≈ 0.52 M rows
  ≈ 5 MB. Then they're dropped at 1 y 7 d. **No change to the projection.** Had they stayed `daily` (every
  revision forever), they would have grown by ≈ 8.8 M rows ≈ 90 MB a year without bound.
- **Realized outage history kept forever** (`aeso.outage.<fuel>.realized`, 11 fuels + `gas_total`, daily
  tier): 12 rows/day ≈ 4.4 k rows/yr ≈ **0.05 MB/yr**. Negligible.
- **Monthly outage revisions** (`forecast`): ≈ 1,056 rows/day before change-only; a year after thinning is
  ≈ 0.1 M rows ≈ 1 MB.
- **Power's actual pins** (fast/hourly for units, CSD, price/load actuals) match the tiers the baseline already
  assumed.

**Result: unchanged at ≈ 0.4 GB steady state (worst case ≈ 0.6 GB), about 8-12 % of the 5 GB `ab_nebc`
budget.** The real bytes/row come from Erik's import and replace these planning figures.

**Mechanism (L-036 (2), chosen):** a derived realized series in `obs_daily`, not re-anchoring.
- **Live:** Power's connector writes `aeso.outage.<fuel>.realized` (as_of = ts) for the current Alberta day,
  with the last fetch of the day winning (#73).
- **History:** the backfill derives the same series for every outage day already in DuckDB. For each day,
  it's the newest vintage with as_of before the Alberta day end (06:00Z the next day). A realized row the
  connector already wrote wins. The dry run reports derived, seen-only-after-the-day and not-ended counts.
- **Monthly outages:** L-036 also defines a realized value per month (the latest vintage known by the month
  end), but nothing writes it live yet. That needs the connector to emit `aeso.outage.monthly.<fuel>.realized`
  (Power's); the backfill can then derive the pre-flip months the same way.

## Questions (all ruled: L-029)

- **Q-R1, day boundary: ruled, L-029.** A fixed UTC-6, in rollup and thinning SQL only, with each literal
  commented and tested (see [`obs_fast`](#obs_fast-sub-hourly)). It's listed in AGENTS.md's exceptions.
- **Q-R2, backups: ruled, L-029. Not counted in the 100 GB.** `du` reports them and warns above 25 GB. Erik
  may veto.
- **Q-R3, tier hypertables: ruled, L-029. Accepted for the draft.** The `observation` view keeps v1's column
  names.
