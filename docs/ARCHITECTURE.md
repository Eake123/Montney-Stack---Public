# Architecture and contracts (v1 map)

Montney Stack v1 is a Ventusky-style live map of gas and power for Alberta + NE BC. It runs
locally. The backend fetches and caches; the frontend is a single map page.

```
sources/*.yaml ──► connectors ──► DuckDB: observations + events      ──► /api/series, /api/snapshot, /api/events
                              └─► grid files (current + prev run)     ──► /api/grid/{var}
network/{gas,power}/*.geojson ─────────────────────────────────────── ──► /api/network
web/ (MapLibre, no build step) ◄──────────────────────── all of the above
```

What to build is decided in [`docs/research/DECISIONS.md`](research/DECISIONS.md).
Who owns what is in [`agents/roster.json`](../agents/roster.json).

## 1. Series (unchanged)

Long table `observations(series_id, ts, value, as_of, fetched_at)`. ts is naive UTC; daily series
are the calendar date at 00:00 with `freq: D`. Units: gas flows MMcf/d, volumes MMcf, storage
Bcf, power MW, price $/MWh. Forecasts use `as_of: fetch`. Spec reference:
[SOURCES.md](SOURCES.md).

New in Phase 0:
- **`derive.weights`**: `{series_id: factor}`, summed alongside `sum`/`minus` (e.g. implied gas burn).
- **POST bodies**: `request.method: POST` with `request.json: {...}` or `request.form: {...}` (templated).
- **Time zones:** Alberta is permanent UTC-6 from 2026-11-01 (BC permanent UTC-7). The app resolves
  zones from the bundled `tzdata` package only (`montney/__init__.py`), never the OS copy.
  `/api/status` returns `tz: {name, abbr, offset_minutes, tzdata}`; the frontend formats Alberta
  times with that offset, because browsers may carry stale zone data. Never hard-code -07:00/MST.
- **Named time zones** already work: `ts: {field: x, tz: America/Edmonton}` handles DST. Prefer this
  over fixed offsets unless the source is documented as fixed.

## 2. Network model: `network/<system>/*.geojson`

FeatureCollections in WGS84 `[lon, lat]`. Validated by `montney/network.py` (CI fails on errors).
Required `properties`:

| key | meaning |
|---|---|
| `id` | stable, lowercase, `[a-z0-9_.-]`, unique across all files. Convention `<system>.<thing>`, e.g. `gas.empress`, `gas.ngtl.egat`, `power.intertie.bc`, `power.plant.shepard` |
| `kind` | `node` (Point), `link` (LineString/MultiLineString), `area` (Polygon/MultiPolygon) |
| `system` | `gas` or `power` |
| `type` | subtype: border, receipt, storage, compressor, hub, plant, intertie, pipeline, transmission, area… |
| `name` | display name |
| `source` | citation for the geometry: URL, dataset name, or `schematic: <basis>` |

Optional: `value` (series id shown on the map), `capacity` (series id) or `capacity_value`
(number), `target` (series id for "vs target", e.g. linepack target), `compare`
(`prev` \| `1h` \| `24h` \| `7d`), `unit` (display override), `panel`
(`{"series": [...], "events_ref": "<id>"}`), and for links: `schematic` (required bool),
`direction` (+1 if positive flow follows coordinate order, -1 otherwise), `operator`.
Unknown keys pass through to the frontend.

v1 lines are **schematic**, drawn by us through cited public coordinates (borders, compressor
stations, plants), with `schematic: true`. Surveyed routes come later.

## 3. API

| endpoint | returns |
|---|---|
| `GET /api/regions`, `GET /api/region/<id>` | the regions, and one region's page: its six box rows, and per tab (gas / power / oil) the balance `lines` and the `detail:` `groups` with latest values (`docs/REGIONS.md` §5b). A row's history is `/api/series?ids=` |
| `GET /api/network` | merged FeatureCollection (+ `errors` list, empty when valid). ETag; send `If-None-Match` for a 304 |
| `GET /api/snapshot[?extra=a,b]` | `{as_of, values: {feature_id: {series, v, ts, prev, prev_ts, next, next_ts, cap, cap_ts, cap_current, cap_current_ts, cap_next, cap_next_ts, target, target_ts, unit, freq, day, compare, stale, agg?}}, series: {id or id@1h/@24h: {...}}, gauges: {station: {...}}}`. `agg` = `{window, start, end, mean, n, n_min, reason}` window mean (#151; `docs/WIDGETS.md`); `gauges` = WSC flow vs normal (#142). ts are epoch seconds. Poll every 60 s |
| `GET /api/series?ids=&start=&end=&as_of_lag=` | history for click panels (unchanged) |
| `GET /api/events?kind=outage,bulletin&ref=gas.ngtl.egat&state=current&since=ISO&limit=` | events, newest first. `state`: `ongoing` (started, not ended), `upcoming`, `current` (= both), `past`. An event without `ends` ends 12 h (alert) / 48 h (bulletin) / 72 h (notice) after it was published; outages need real times and stay open otherwise. `since` is a change feed (only real content changes and withdrawals bump `updated`) and also returns withdrawn items |
| `GET /api/grids` | available grid vars with run/fetched metadata (current + prev) |
| `GET /api/grid/{var}[?prev=true]` | gzipped grid payload (see §5) |
| `GET /api/catalog`, `/api/status`, `POST /api/refresh/{source}` | unchanged |

Compare rules: `prev` = previous point (default for `freq: D`), `1h`, `24h` (default otherwise), `7d`.
"Current" is the newest point at or before now (intraday). For daily series it depends on the
series' **day convention**: gas-day series (spec `day: gas` on the block or field, or
`SeriesMeta(day="gas")`, e.g. NGTL GDSR/DOP and Westcoast capacity) use the current NGTL gas day,
which starts 08:00 MST = a fixed 15:00 UTC (09:00 Alberta time from 2026-11-01) per TC bulletin
3374725064. All other daily series (weather, AESO outages) use today's Alberta calendar date.
Snapshot entries carry `day`: `gas` or `calendar` for daily series, `""` for intraday. Forward series (DOP capability, Westcoast D+1 capacity)
therefore report today's value; the first forward point is returned as `next` / `cap_next`.
`cap` is taken at the value's timestamp (for utilization); `cap_current` is the capacity for the
current gas day even when the value (e.g. DOP flow) lags a day behind. `cap` is taken at or before the value's timestamp.
`stale` = newest point older than max(3 × freq, 1 h) intraday, or more than 4 days for daily.

## 4. Events

`montney.store.Event`, returned by connectors in `FetchResult.events` and upserted by stable `id`
(`"<source>:<native id>"`). Fields: `kind` (`outage` \| `bulletin` \| `notice` \| `alert`),
`title`, `body`, `url`, `starts`, `ends`, `refs` (network feature ids), `severity`
(`info` \| `warn` \| `critical`), `published`, `extra` (dict). Times are naive UTC. Use it for NGTL
DOP outages, TC bulletins, Westcoast notices, the AESO event log, and later fire and weather
alerts.

Retraction: set `FetchResult.event_scopes = [(source, kind)]` when `events` is the complete
current set for that scope (e.g. the full DOP outage list). Active events of that scope that are
missing get `status: withdrawn` and drop out of normal queries. `updated` changes only when the
content changes.

## 5. Grids (weather overlay)

Connectors return `FetchResult.grids = {var: payload}`. Files live in `<home>/grids/`: one file per
run, plus `<var>.ptr.json` pointing at the current and previous run. Run files are never replaced
in place (safe on Windows while being served), and older runs are deleted. Payload (validated in `montney/grids.py`):

```
var, unit, model, run (ISO UTC cycle), fetched, lon0, lat0 (first cell centre, SW corner),
dx, dy (deg), nx (cols W→E), ny (rows S→N), times [ISO UTC valid times; [] for static z],
scale, offset, nodata, data = base64 little-endian int16 [t][row S→N][col W→E]
```

v1 vars: `t2m` (degC), `u10`, `v10` (m/s), `z` (model terrain height, m; for the lapse-rate
downscale). Extent at least lon -135..-100, lat 45..63.

## 6. Frontend

- `web/` is a single map page (MapLibre, vendored, no build). The old board, `tabs/` and
  `/api/tabs` are gone.
- Map config (view, layers, callouts and their anchors) lives in `web/map.json`, served statically.
- Callouts: compact boxes with leader lines to their node (or a region anchor for AB-wide values),
  click to drill down. No top headline strip.
- Weather overlay: translucent field, hillshade multiplied on top, water/borders/labels above,
  lapse-rate downscale with the DEM, subtle wind particles. Attribution must include every
  basemap/terrain/data source.
