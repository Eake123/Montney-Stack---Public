-- 0002_observation_tiers: the observation hypertables, one per retention tier, plus raw_blob.
-- Design: docs/db/ERD.md "Hypertables", docs/db/RETENTION.md "The tiers". Intervals marked (tune) are
-- re-derived from measured sizes later (Wellsite §4.7: never widened to make a number fit).
-- No FK from these tables to series: it costs an index probe per inserted row; `db check` finds orphans.

-- Sub-hourly points (5-30 min), one vintage per point. Raw kept 60 days (PLAN), then rollups (0003).
CREATE TABLE montney.obs_fast (
    ts         timestamp NOT NULL,
    as_of      timestamp NOT NULL,
    value      double precision,
    fetched_at timestamp NOT NULL,
    series_id  integer   NOT NULL,
    PRIMARY KEY (series_id, ts, as_of)
);
SELECT create_hypertable('montney.obs_fast', by_range('ts', INTERVAL '1 day'));              -- (tune)
ALTER TABLE montney.obs_fast SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'series_id', timescaledb.orderby = 'ts, as_of');
CALL add_columnstore_policy('montney.obs_fast', after => INTERVAL '2 days');                  -- (tune)
SELECT add_retention_policy('montney.obs_fast', drop_after => INTERVAL '60 days');            -- PLAN

-- Hourly points. Raw kept 2 years (PLAN), then the daily rollup (0003).
CREATE TABLE montney.obs_hourly (
    ts         timestamp NOT NULL,
    as_of      timestamp NOT NULL,
    value      double precision,
    fetched_at timestamp NOT NULL,
    series_id  integer   NOT NULL,
    PRIMARY KEY (series_id, ts, as_of)
);
SELECT create_hypertable('montney.obs_hourly', by_range('ts', INTERVAL '7 days'));            -- (tune)
ALTER TABLE montney.obs_hourly SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'series_id', timescaledb.orderby = 'ts, as_of');
CALL add_columnstore_policy('montney.obs_hourly', after => INTERVAL '7 days');                -- (tune)
SELECT add_retention_policy('montney.obs_hourly', drop_after => INTERVAL '2 years');          -- PLAN

-- Daily, monthly and longer; revisions are kept as vintages. Kept forever (PLAN): no retention policy.
CREATE TABLE montney.obs_daily (
    ts         timestamp NOT NULL,
    as_of      timestamp NOT NULL,
    value      double precision,
    fetched_at timestamp NOT NULL,
    series_id  integer   NOT NULL,
    PRIMARY KEY (series_id, ts, as_of)
);
SELECT create_hypertable('montney.obs_daily', by_range('ts', INTERVAL '365 days'));           -- (tune)
ALTER TABLE montney.obs_daily SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'series_id', timescaledb.orderby = 'ts, as_of');
CALL add_columnstore_policy('montney.obs_daily', after => INTERVAL '90 days');                -- (tune)

-- Forecast runs (as_of: fetch). Partitioned on as_of (the run), not ts. Thinned and compressed by the
-- job in 0004 (so no columnstore policy here). Dropped after 1 year + the 7-day re-anchor age.
CREATE TABLE montney.obs_forecast (
    as_of      timestamp NOT NULL,
    ts         timestamp NOT NULL,
    value      double precision,
    fetched_at timestamp NOT NULL,
    series_id  integer   NOT NULL,
    PRIMARY KEY (series_id, as_of, ts)
);
SELECT create_hypertable('montney.obs_forecast', by_range('as_of', INTERVAL '1 day'));
ALTER TABLE montney.obs_forecast SET (timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'series_id', timescaledb.orderby = 'as_of, ts');
SELECT add_retention_policy('montney.obs_forecast', drop_after => INTERVAL '1 year 7 days');  -- PLAN + re-anchor

-- Raw payloads, kept 7 days (PLAN). The runner zstd-compresses them, so TOAST must not compress again.
CREATE TABLE montney.raw_blob (
    id           bigint    GENERATED ALWAYS AS IDENTITY,
    fetched_at   timestamp NOT NULL,
    source_id    smallint  NOT NULL REFERENCES montney.source (id),
    fetch_run_id bigint,
    content_type text      NOT NULL DEFAULT '',
    sha256       bytea     NOT NULL,
    bytes_raw    integer   NOT NULL CHECK (bytes_raw >= 0),
    payload      bytea,                                -- NULL when sha256 equals the previous blob's
    PRIMARY KEY (id, fetched_at)                       -- a hypertable's unique keys include its time column
);
ALTER TABLE montney.raw_blob ALTER COLUMN payload SET STORAGE EXTERNAL;
SELECT create_hypertable('montney.raw_blob', by_range('fetched_at', INTERVAL '1 day'));
CREATE INDEX raw_blob_source_idx ON montney.raw_blob (source_id, fetched_at DESC);
SELECT add_retention_policy('montney.raw_blob', drop_after => INTERVAL '7 days');             -- PLAN

-- Readers: the union of the tiers, with v1's column names exactly (L-029), so the snapshot/server
-- port is mechanical. series_id here is the text key.
CREATE VIEW montney.observation AS
SELECT s.key AS series_id, o.ts, o.value, o.as_of, o.fetched_at
FROM (
    SELECT series_id, ts, value, as_of, fetched_at FROM montney.obs_fast
    UNION ALL
    SELECT series_id, ts, value, as_of, fetched_at FROM montney.obs_hourly
    UNION ALL
    SELECT series_id, ts, value, as_of, fetched_at FROM montney.obs_daily
    UNION ALL
    SELECT series_id, ts, value, as_of, fetched_at FROM montney.obs_forecast
) o
JOIN montney.series s ON s.id = o.series_id;
