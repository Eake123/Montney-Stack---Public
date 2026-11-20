-- 0005_thin_forecasts_chunk_locks: thin_forecasts compresses one chunk per transaction and never waits long
-- for a chunk lock (the orchestrator's blocker on #51). 0004 compressed every thinned chunk in ONE
-- transaction: it held one chunk's AccessExclusiveLock while asking for the next, so a writer holding the
-- next chunk and asking for the first closed a cycle. REPRODUCED in CI run 41268834503: 20 of 20 runs
-- deadlocked on 0004, 0 of 20 on 0005 (tests/test_db_concurrency.py; the 0004 run is kept as a seeded
-- red). The deadlock first seen in CI (5b0f868) was a different one, thinning vs thinning, fixed in 0004
-- by the advisory lock. A busy chunk is now skipped (lock_timeout 2 s) and retried by the next run. Everything else is unchanged from 0004 (advisory lock, L-029 run-days,
-- watermark). 0004 is on main, so it is replaced here, not edited (INSTALL.md "Migrations").
-- Tested by tests/test_db_concurrency.py (thinning while a writer hammers the same chunk, 20 runs).

CREATE OR REPLACE PROCEDURE montney.thin_forecasts(job_id integer, config jsonb)
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

    -- Chunks entirely before the cutoff are thinned: compress them. Never hold one chunk's lock while
    -- asking for another's (0004 did, and deadlocked against a writer; see the header):
    --   * one transaction per chunk (COMMIT after each), so no lock is held across chunks;
    --   * never wait long for a chunk lock (lock_timeout 2 s); a busy chunk is skipped and the next
    --     run retries it (show_chunks lists it again until it's compressed).
    -- compress_chunk (a function) rather than convert_to_columnstore (a procedure), because it runs
    -- inside the exception block that catches the timeout.
    FOR ch IN SELECT c FROM show_chunks('montney.obs_forecast', older_than => cutoff) c LOOP
        BEGIN
            PERFORM set_config('lock_timeout', '2s', true);
            PERFORM compress_chunk(ch, if_not_compressed => true);
        EXCEPTION WHEN lock_not_available OR deadlock_detected THEN
            RAISE NOTICE 'thin_forecasts: chunk % is busy, retrying next run', ch;
        END;
        COMMIT;
    END LOOP;

    DELETE FROM montney.forecast_latest WHERE ts < now_utc - INTERVAL '1 day';

    IF job_id IS NOT NULL AND job_id > 0 THEN
        PERFORM alter_job(job_id, config => jsonb_set(coalesce(config, '{}'::jsonb),
                                                      '{thinned_through}', to_jsonb(cutoff)));
    END IF;
    PERFORM pg_advisory_unlock(hashtext('montney.thin_forecasts'));
END
$$;
