# Montney Stack

A live map of the North American natural gas and power system. It started with the Montney
(Alberta and northeast BC) and now covers 13 regions across Western Canada, Ontario, Québec and
the US. It collects about 125 public feeds on a schedule, normalises them into one time-series
store, and draws pipelines, flows, plants and regional supply/demand balances on a MapLibre map.

```
sources/*.yaml ─► connectors ─► observations (DuckDB / Postgres) ─► FastAPI ─► web/ (MapLibre, no build)
network/{gas,power}/*.geojson ─────────────────────────────────────┘
```

## What it covers

**Gas supply and transport**
- NGTL (Nova Gas Transmission): 30-minute receipts, border deliveries, linepack vs target, gas-day
  summaries, area capability and maintenance from the Daily Operating Plan, and a per-station
  receipt/delivery split (`montney/connectors/ngtl_split.py`) that reconciles station data to
  system totals.
- TC Mainline capability and flows, Westcoast (Station 2, Huntingdon), Alliance, and about 20 US
  interstate pipelines' electronic bulletin boards (scheduled quantities at key points).
- Production (Petrinex, BCER), EIA weekly storage and state data, CER key-point throughput vs
  capacity, LNG feedgas (LNG Canada, Gulf Coast).
- A supply/demand balance per region (`montney/region_balance.py`), with every row stating its basis.

**Power**
- Load, generation by fuel, interties and outages from AESO, IESO, BC Hydro, Hydro-Québec, CAISO,
  ERCOT, MISO, NYISO, ISO-NE, SPP, BPA, SaskPower and EIA-930; the AESO pool price.
- 5,485 plants drawn by fuel. Output is measured where an operator publishes it per unit (Alberta,
  Ontario, US nuclear from the NRC) and estimated from the balancing area's fuel utilisation elsewhere,
  always labelled as such.
- Gas burn for power and NGTL deliveries to gas-fired plants.

**Weather and water:** GFS/Open-Meteo forecasts as a map overlay, river flows and reservoirs
(USGS, Water Survey of Canada), wildfires.

## Engineering

- **Declarative sources.** Most feeds are a YAML spec (`sources/`, reference in `docs/SOURCES.md`):
  request, templated dates, extraction paths, units, time zones, gas-day handling, derived series.
  A Python connector is written only when the spec can't express the source.
- **Units and time.** Gas in MMcf/d and Bcf, converted on ingest; timestamps stored as naive UTC with
  explicit per-source rules (gas day, fixed-offset publishers, Alberta's 2026 clock change).
- **Storage.** A long `observations(series_id, ts, value, as_of, fetched_at)` table keeps every
  forecast run; Postgres with tiered retention and rollups (`migrations/`, `docs/db/`).
- **Map.** Plain JavaScript and MapLibre, no build step. Callout boxes, drill-down panels, region
  pages, animated plant sprites whose motion encodes live output (`web/`, `docs/WIDGETS.md`).
- **Tests.** About 140 test files with mocked payloads (`tests/`).

Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Regions: [`docs/REGIONS.md`](docs/REGIONS.md).

## Not in this repository

This is the code. It contains no collected data. The network GeoJSON files (pipeline stations,
plant inventories) and some test fixtures are left out because several sources (TC Energy, AESO,
BCER, Petrinex, BC Hydro) allow personal use but not redistribution; the build scripts that produce
them from the public sources are included. Some tests need those fixtures and are expected to skip
or fail without them. The source APIs that need a key read it from a local secret store
(`{secret:NAME}`); no keys are committed.

## Run

```bash
uv sync --extra dev
uv run montney try <source-id>     # fetch one source and print what it parsed
uv run pytest
```

MIT licensed code. Data belongs to its publishers and is subject to their terms.
