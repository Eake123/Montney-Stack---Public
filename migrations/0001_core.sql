-- 0001_core: schema, dimension tables and the frozen vocabularies.
-- Design: docs/db/ERD.md (frozen, L-033). Plain tables only; hypertables are in 0002.
-- Times are naive UTC (`timestamp`), as in v1.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS montney;

-- Storage budget regions (L-003). GB = 10^9 bytes.
CREATE TABLE montney.region (
    id           smallint PRIMARY KEY,
    code         text     NOT NULL UNIQUE,
    name         text     NOT NULL,
    budget_bytes bigint   NOT NULL CHECK (budget_bytes > 0),
    warn_pct     smallint NOT NULL DEFAULT 80 CHECK (warn_pct BETWEEN 1 AND 100)
);
INSERT INTO montney.region (id, code, name, budget_bytes) VALUES
    (1, 'ab_nebc', 'Alberta + NE BC', 5000000000),
    (2, 'rest',    'Everything outside AB + NE BC', 95000000000);

CREATE TABLE montney.source (
    id           smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code         text     NOT NULL UNIQUE,          -- spec id, e.g. ngtl_gdsr
    name         text     NOT NULL DEFAULT '',
    region_id    smallint NOT NULL DEFAULT 1 REFERENCES montney.region (id),
    retention    text CHECK (retention IN ('fast', 'hourly', 'daily', 'forecast', 'live', 'events', 'grid')),
    interval_s   integer CHECK (interval_s > 0),
    mode         text     NOT NULL DEFAULT 'cached' CHECK (mode IN ('cached', 'live')),
    spec_sha     text,
    budget_state text     NOT NULL DEFAULT 'ok' CHECK (budget_state IN ('ok', 'warn', 'blocked')),
    last_ok_at   timestamp
);
CREATE INDEX source_region_idx ON montney.source (region_id);

CREATE TABLE montney.entity (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type       text   NOT NULL CHECK (type IN (
                   'station', 'segment', 'area', 'facility', 'operator', 'well', 'pipeline', 'terminal',
                   'ship', 'gen_unit', 'plant', 'dam', 'gauge', 'river', 'reach')),
    key        text   NOT NULL UNIQUE CHECK (key ~ '^[a-z0-9_]+(\.[a-z0-9_]+)+$'),
    name       text   NOT NULL DEFAULT '',
    parent_id  bigint REFERENCES montney.entity (id),
    region_id  smallint NOT NULL DEFAULT 1 REFERENCES montney.region (id),
    lon        double precision CHECK (lon BETWEEN -180 AND 180),
    lat        double precision CHECK (lat BETWEEN -90 AND 90),
    geometry   jsonb,                                 -- GeoJSON for lines/areas; no PostGIS (L-029)
    attrs      jsonb  NOT NULL DEFAULT '{}',
    valid_from date,
    valid_to   date,
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX entity_type_idx   ON montney.entity (type);
CREATE INDEX entity_parent_idx ON montney.entity (parent_id);
CREATE INDEX entity_region_idx ON montney.entity (region_id);
CREATE INDEX entity_lonlat_idx ON montney.entity (lon, lat);

CREATE TABLE montney.entity_alias (
    system     text   NOT NULL CHECK (system IN (
                   'tc_station', 'petrinex_facility', 'bcer_facility', 'petrinex_ba', 'tc_segment',
                   'tc_dop_area', 'uwi', 'bc_wa', 'well_licence', 'imo', 'mmsi', 'ppa_berth',
                   'aeso_asset', 'ghgrp', 'npri', 'bch_code', 'usace', 'wsc_station', 'ab_station',
                   'usgs_site', 'nhn')),
    alias      text   NOT NULL CHECK (alias <> ''),   -- kept verbatim (case, hyphens)
    valid_from date   NOT NULL DEFAULT '-infinity',
    entity_id  bigint NOT NULL REFERENCES montney.entity (id),
    valid_to   date,
    PRIMARY KEY (system, alias, valid_from),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX entity_alias_entity_idx ON montney.entity_alias (entity_id);

CREATE TABLE montney.entity_link (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_id    bigint NOT NULL REFERENCES montney.entity (id),
    to_id      bigint NOT NULL REFERENCES montney.entity (id),
    kind       text   NOT NULL CHECK (kind IN (
                   'delivers_to', 'member_of', 'operated_by', 'generates_at', 'measured_by',
                   'on_reach', 'flows_into', 'derived_from')),
    confidence real   NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
    method     text   NOT NULL DEFAULT '',
    evidence   jsonb,
    attrs      jsonb  NOT NULL DEFAULT '{}',          -- role, distance_km, split_method, formula
    valid_from date   NOT NULL DEFAULT '-infinity',
    valid_to   date,
    UNIQUE (from_id, to_id, kind, valid_from),
    CHECK (from_id <> to_id),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX entity_link_to_idx   ON montney.entity_link (to_id);
CREATE INDEX entity_link_kind_idx ON montney.entity_link (kind);

CREATE TABLE montney.series (
    id        integer  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key       text     NOT NULL UNIQUE CHECK (key ~ '^[a-z0-9_]+(\.[a-z0-9_]+)+$'),  -- v1 series_id
    source_id smallint NOT NULL REFERENCES montney.source (id),
    entity_id bigint   REFERENCES montney.entity (id),
    region_id smallint NOT NULL DEFAULT 1 REFERENCES montney.region (id),
    label     text     NOT NULL DEFAULT '',
    unit      text     NOT NULL DEFAULT '',
    freq      text     NOT NULL DEFAULT '',
    day       text     NOT NULL DEFAULT '' CHECK (day IN ('', 'gas')),
    -- Fixed once the series has rows: moving tiers is a migration, not a spec edit (ERD.md).
    tier      text     NOT NULL CHECK (tier IN ('fast', 'hourly', 'daily', 'forecast', 'live')),
    attrs     jsonb    NOT NULL DEFAULT '{}'
);
CREATE INDEX series_source_idx      ON montney.series (source_id);
CREATE INDEX series_entity_idx      ON montney.series (entity_id);
CREATE INDEX series_region_tier_idx ON montney.series (region_id, tier);

-- Newest point per series: staleness and "last fetched". Not the change-only comparator (RETENTION.md).
CREATE TABLE montney.series_state (
    series_id    integer PRIMARY KEY REFERENCES montney.series (id) ON DELETE CASCADE,
    last_ts      timestamp,
    last_as_of   timestamp,
    last_value   double precision,
    last_seen_at timestamp
);

-- Newest written vintage of every future forecast point: the change-only comparator for forecasts and
-- the re-anchoring clock (RETENTION.md).
CREATE TABLE montney.forecast_latest (
    series_id integer   NOT NULL REFERENCES montney.series (id) ON DELETE CASCADE,
    ts        timestamp NOT NULL,
    as_of     timestamp NOT NULL,
    value     double precision,
    PRIMARY KEY (series_id, ts)
);

-- Sparse quality flags on one observation VINTAGE, e.g. hv_fallback (L-031): a re-issued month converted
-- with the published heat value is a new vintage and doesn't inherit the flag. A flag lives exactly as
-- long as its observation row (job prune_obs_flags in 0004; `db check` reports orphans).
CREATE TABLE montney.obs_flag (
    series_id integer   NOT NULL REFERENCES montney.series (id) ON DELETE CASCADE,
    ts        timestamp NOT NULL,
    as_of     timestamp NOT NULL,
    flag      text      NOT NULL CHECK (flag ~ '^[a-z0-9_]+$'),
    note      text,
    PRIMARY KEY (series_id, ts, as_of, flag)
);

CREATE TABLE montney.fetch_run (
    id          bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   smallint NOT NULL REFERENCES montney.source (id),
    started     timestamp NOT NULL,
    finished    timestamp,
    ok          boolean,
    n_written   integer  NOT NULL DEFAULT 0,
    n_unchanged integer  NOT NULL DEFAULT 0,
    bytes_in    integer,
    error       text
);
CREATE INDEX fetch_run_source_started_idx ON montney.fetch_run (source_id, started DESC);

CREATE TABLE montney.event (
    id           text     PRIMARY KEY,                -- "<source>:<native id>"
    source_id    smallint NOT NULL REFERENCES montney.source (id),
    kind         text     NOT NULL CHECK (kind IN ('outage', 'bulletin', 'notice', 'alert', 'cargo')),
    title        text     NOT NULL,
    body         text     NOT NULL DEFAULT '',
    url          text     NOT NULL DEFAULT '',
    starts       timestamp,
    ends         timestamp,
    severity     text     NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warn', 'critical')),
    published    timestamp,
    extra        jsonb    NOT NULL DEFAULT '{}',
    first_seen   timestamp NOT NULL,
    updated      timestamp NOT NULL,
    status       text     NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    content_sha1 bytea
);
CREATE INDEX event_source_kind_idx  ON montney.event (source_id, kind, status);
CREATE INDEX event_published_idx    ON montney.event ((coalesce(published, first_seen)) DESC);
CREATE INDEX event_updated_idx      ON montney.event (updated);

CREATE TABLE montney.event_scope (
    event_id  text   NOT NULL REFERENCES montney.event (id) ON DELETE CASCADE,
    ref       text   NOT NULL,                        -- network feature id
    entity_id bigint REFERENCES montney.entity (id),
    PRIMARY KEY (event_id, ref)
);
CREATE INDEX event_scope_ref_idx    ON montney.event_scope (ref);
CREATE INDEX event_scope_entity_idx ON montney.event_scope (entity_id);

-- Weather grid metadata (L-029). Payloads stay files in <home>/grids/.
CREATE TABLE montney.grid_run (
    var     text      NOT NULL,
    run     timestamp NOT NULL,
    fetched timestamp NOT NULL,
    path    text      NOT NULL,
    bytes   bigint    NOT NULL CHECK (bytes >= 0),
    PRIMARY KEY (var, run)
);
