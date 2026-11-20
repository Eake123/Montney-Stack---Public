-- 0006_usage: storage accounting for `montney du` and the budget guard (docs/db/RETENTION.md
-- "Budget accounting"). Timescale reports sizes per chunk, not per series, so du splits each chunk's
-- bytes by its rows per series (or per source) and caches the split here: a chunk is recounted only when
-- its byte size changed (compressed chunks almost never do).

CREATE TABLE montney.usage_chunk (
    chunk       text      NOT NULL,                  -- schema-qualified chunk (or rollup chunk) name
    tbl         text      NOT NULL,                  -- the hypertable or rollup it belongs to
    owner_kind  text      NOT NULL CHECK (owner_kind IN ('series', 'source')),
    owner_id    integer   NOT NULL,                  -- series.id or source.id
    rows        bigint    NOT NULL CHECK (rows >= 0),
    chunk_bytes bigint    NOT NULL CHECK (chunk_bytes >= 0),   -- the chunk's size when counted
    computed_at timestamp NOT NULL,
    PRIMARY KEY (chunk, owner_kind, owner_id)
);

CREATE TABLE montney.usage_snapshot (
    taken_at  timestamp NOT NULL,
    region_id smallint  REFERENCES montney.region (id),   -- NULL = _system / files outside a region
    source_id smallint  REFERENCES montney.source (id),   -- NULL = not attributable to one source
    item      text      NOT NULL,                          -- tier table, rollup, 'events', 'files:grids', ...
    rows      bigint    NOT NULL DEFAULT 0,
    bytes     bigint    NOT NULL CHECK (bytes >= 0)
);
CREATE INDEX usage_snapshot_taken_idx ON montney.usage_snapshot (taken_at DESC);
CREATE INDEX usage_snapshot_region_idx ON montney.usage_snapshot (region_id, taken_at DESC);
