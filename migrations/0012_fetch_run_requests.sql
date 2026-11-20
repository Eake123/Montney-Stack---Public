-- 0012_fetch_run_requests: how many HTTP requests each run made (L-081, docs/ARCHITECTURE-REQUESTS.md §3).
-- bytes_in (0001) is now filled too: the bytes downloaded (on the wire, before decompression), every attempt counted.
-- NULL for runs logged before this migration and for runs that made no counted request.
ALTER TABLE montney.fetch_run
    ADD COLUMN n_requests integer;
