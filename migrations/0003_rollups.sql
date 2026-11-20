-- 0003_rollups: continuous aggregates for the downsampling tiers (docs/db/RETENTION.md).
--   obs_fast   -> obs_fast_1h (kept 2 years)   and obs_fast_1d (forever)
--   obs_hourly -> obs_hourly_1d (forever)
-- Each rollup stores sum + n (not avg) plus min, max, last, so averages over any span stay exact.
-- The daily rollups read the raw tables directly (not the hourly rollup): refresh windows (7 d / 30 d)
-- sit inside raw retention (60 d / 2 y), and a raw-based daily avoids a bucket-origin mismatch between a
-- 0-offset hour and a 6-hour-offset day.
-- Never refresh a rollup manually over a range older than its raw retention: that empties the buckets.
--
-- L-029: the Alberta day is a fixed UTC-6, i.e. daily buckets start at 06:00 UTC. That offset appears
-- only in rollup and forecast-thinning SQL, every occurrence is marked "L-029", and
-- tests/test_db_day_boundary.py checks they all match.

CREATE MATERIALIZED VIEW montney.obs_fast_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT series_id,
       time_bucket(INTERVAL '1 hour', ts) AS ts,
       sum(value) AS sum, count(value)::integer AS n,
       min(value) AS min, max(value) AS max, last(value, ts) AS last
FROM montney.obs_fast
GROUP BY 1, 2
WITH NO DATA;
SELECT add_continuous_aggregate_policy('montney.obs_fast_1h',
    start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes');
ALTER MATERIALIZED VIEW montney.obs_fast_1h SET (timescaledb.enable_columnstore = true);
CALL add_columnstore_policy('montney.obs_fast_1h', after => INTERVAL '14 days');
SELECT add_retention_policy('montney.obs_fast_1h', drop_after => INTERVAL '2 years');          -- PLAN

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
ALTER MATERIALIZED VIEW montney.obs_fast_1d SET (timescaledb.enable_columnstore = true);
CALL add_columnstore_policy('montney.obs_fast_1d', after => INTERVAL '45 days');

CREATE MATERIALIZED VIEW montney.obs_hourly_1d
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT series_id,
       time_bucket(INTERVAL '1 day', ts, INTERVAL '6 hours') AS ts,   -- L-029: Alberta day = UTC-6
       sum(value) AS sum, count(value)::integer AS n,
       min(value) AS min, max(value) AS max, last(value, ts) AS last
FROM montney.obs_hourly
GROUP BY 1, 2
WITH NO DATA;
SELECT add_continuous_aggregate_policy('montney.obs_hourly_1d',
    start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '6 hours');
ALTER MATERIALIZED VIEW montney.obs_hourly_1d SET (timescaledb.enable_columnstore = true);
CALL add_columnstore_policy('montney.obs_hourly_1d', after => INTERVAL '45 days');
