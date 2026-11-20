-- 0008_runner_state: the runner's heartbeat (orchestrator/reviewer on #88: with `serve` read-only, refresh
-- requests queue silently if no runner is running). One row, updated by `montney run` at start and every
-- 10 s; the API reads it (montney_reader has SELECT on every table, migrate.ensure_reader_grants) and the UI
-- flags "runner not running" when the beat is older than 60 s.
CREATE TABLE montney.runner_state (
    id         smallint  PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- exactly one row
    last_beat  timestamp NOT NULL,                               -- naive UTC
    started_at timestamp NOT NULL,
    pid        integer   NOT NULL,
    version    text      NOT NULL DEFAULT '',
    host       text      NOT NULL DEFAULT ''
);
