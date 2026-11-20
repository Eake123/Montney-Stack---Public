-- 0004_jobs: user-defined Timescale jobs (docs/db/RETENTION.md).
--   thin_forecasts:   every forecast run for 14 days, then the last run of each Alberta day; compresses
--                     each chunk once it is thinned; prunes forecast_latest rows that are in the past.
--   prune_fetch_runs: fetch_run rows older than a year.
-- Both are idempotent and catch up after downtime. They COMMIT per day, so call them outside a
-- transaction block (as the job scheduler does).

CREATE PROCEDURE montney.thin_forecasts(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
DECLARE
    keep_all  interval  := make_interval(days => coalesce((config ->> 'keep_all_days')::integer, 14));
    now_utc   timestamp := now() AT TIME ZONE 'UTC';
    cutoff    timestamp;
    day_start timestamp;
    watermark timestamp := (config ->> 'thinned_through')::timestamp;
    ch        regclass;
BEGIN
    -- One run at a time: a manual CALL racing the scheduled job deadlocked (DELETE vs columnstore
    -- conversion) in CI. A second caller skips; the lock goes with the session if the run fails.
    IF NOT pg_try_advisory_lock(hashtext('montney.thin_forecasts')) THEN
        RAISE NOTICE 'thin_forecasts: another run is in progress, skipping';
        RETURN;
    END IF;

    -- Whole Alberta run-days that ended at least keep_all ago.
    cutoff := time_bucket(INTERVAL '1 day', now_utc - keep_all, INTERVAL '6 hours');            -- L-029
    SELECT time_bucket(INTERVAL '1 day', min(as_of), INTERVAL '6 hours')                        -- L-029
      INTO day_start FROM montney.obs_forecast;
    IF watermark IS NOT NULL AND (day_start IS NULL OR watermark > day_start) THEN
        day_start := watermark;
    END IF;

    WHILE day_start IS NOT NULL AND day_start < cutoff LOOP
        -- Keep, per (series, ts), only the newest row of this run-day.
        DELETE FROM montney.obs_forecast f
        WHERE f.as_of >= day_start AND f.as_of < day_start + INTERVAL '1 day'
          AND EXISTS (SELECT 1 FROM montney.obs_forecast g
                      WHERE g.series_id = f.series_id AND g.ts = f.ts
                        AND g.as_of > f.as_of AND g.as_of < day_start + INTERVAL '1 day');
        day_start := day_start + INTERVAL '1 day';
        COMMIT;
    END LOOP;

    -- Chunks entirely before the cutoff are thinned: compress them.
    FOR ch IN SELECT show_chunks('montney.obs_forecast', older_than => cutoff) LOOP
        CALL convert_to_columnstore(ch, if_not_columnstore => true);
    END LOOP;

    DELETE FROM montney.forecast_latest WHERE ts < now_utc - INTERVAL '1 day';

    IF job_id IS NOT NULL AND job_id > 0 THEN
        PERFORM alter_job(job_id, config => jsonb_set(coalesce(config, '{}'::jsonb),
                                                      '{thinned_through}', to_jsonb(cutoff)));
    END IF;
    PERFORM pg_advisory_unlock(hashtext('montney.thin_forecasts'));
END
$$;

SELECT add_job('montney.thin_forecasts', INTERVAL '1 hour', config => '{"keep_all_days": 14}');

CREATE PROCEDURE montney.prune_fetch_runs(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM montney.fetch_run
    WHERE started < (now() AT TIME ZONE 'UTC')
                    - make_interval(days => coalesce((config ->> 'keep_days')::integer, 365));
END
$$;

SELECT add_job('montney.prune_fetch_runs', INTERVAL '1 day', config => '{"keep_days": 365}');

-- A flag lives exactly as long as its observation vintage (docs/db/ERD.md obs_flag): once tier retention
-- or forecast thinning has removed the row, its flags go too.
CREATE PROCEDURE montney.prune_obs_flags(job_id integer, config jsonb)
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM montney.obs_flag f
    WHERE NOT EXISTS (SELECT 1 FROM montney.obs_fast o
                      WHERE o.series_id = f.series_id AND o.ts = f.ts AND o.as_of = f.as_of)
      AND NOT EXISTS (SELECT 1 FROM montney.obs_hourly o
                      WHERE o.series_id = f.series_id AND o.ts = f.ts AND o.as_of = f.as_of)
      AND NOT EXISTS (SELECT 1 FROM montney.obs_daily o
                      WHERE o.series_id = f.series_id AND o.ts = f.ts AND o.as_of = f.as_of)
      AND NOT EXISTS (SELECT 1 FROM montney.obs_forecast o
                      WHERE o.series_id = f.series_id AND o.as_of = f.as_of AND o.ts = f.ts);
END
$$;

SELECT add_job('montney.prune_obs_flags', INTERVAL '1 day');
