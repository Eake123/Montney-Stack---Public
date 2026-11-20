-- 0011_series_state_seen_range: which points the newest vintage SEEN covered (real-data parity, 2026-11-01).
-- series_state.last_as_of is now the newest vintage seen for the series (monotonic), including identical
-- re-fetches change-only didn't store. seen_from_ts / seen_to_ts are the ts range that vintage covered (the
-- rows of the batch with that as_of), so PostgresStore.series() credits last_as_of only to a window that
-- overlaps them, as v1 (which stored every re-fetched row) did. NULL for rows written before this migration:
-- series() then uses [last_ts, last_ts].
ALTER TABLE montney.series_state
    ADD COLUMN seen_from_ts timestamp,
    ADD COLUMN seen_to_ts   timestamp;
