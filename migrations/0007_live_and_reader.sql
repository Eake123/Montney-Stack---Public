-- 0007_live_and_reader: live sources and the read-only API role (docs/db/PORT.md; L-035).
--
-- obs_live: v1's `live_obs` (sources with `mode: live`): replaced on every fetch, never persisted across a
-- crash and never backed up, hence UNLOGGED. Same columns as the tier tables.
CREATE UNLOGGED TABLE montney.obs_live (
    ts         timestamp NOT NULL,
    as_of      timestamp NOT NULL,
    value      double precision,
    fetched_at timestamp NOT NULL,
    series_id  integer   NOT NULL REFERENCES montney.series (id) ON DELETE CASCADE,
    PRIMARY KEY (series_id, ts, as_of)
);

-- The reader role (`montney_reader`, SELECT only, L-035 Q-P2) is created once by the superuser at install
-- (INSTALL.md); roles are cluster-wide, so a migration doesn't create it. `montney db init` grants it
-- SELECT on everything in the schema after every migration run (montney/db/migrate.py
-- ensure_reader_grants), so new tables are covered too.

-- Event refs keep their order (PORT.md, L-035): `refs` is array_agg(ref ORDER BY ord), as DuckDB kept them.
ALTER TABLE montney.event_scope ADD COLUMN ord smallint NOT NULL DEFAULT 0;

-- Refresh requests from the API (L-035 Q-P1): the API inserts, the runner polls, validates the source
-- against the specs, runs it, and deletes the request. montney_reader may INSERT here and nowhere else.
CREATE TABLE montney.refresh_request (
    source       text      NOT NULL CHECK (source ~ '^[a-z0-9_]+$'),
    requested_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    PRIMARY KEY (source, requested_at)
);
