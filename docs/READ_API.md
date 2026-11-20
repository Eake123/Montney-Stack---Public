# Read-only series access (Krasnodar D29)

Other apps and agents read Montney's headline daily metrics through two calls, and nothing else. It's the platform
convention (Krasnodar `docs/APP_CONTRACT.md`, D29); code: `montney/readapi.py` (both front-ends share it).

| Front-end | Where | Who |
|---|---|---|
| HTTP | `GET /api/read/list_series`, `GET /api/read/series?metric=&start=&end=&freq=day` | apps on a granted link (later), curl |
| MCP | `/mcp/read` (stateless Streamable HTTP, JSON): tools `list_series`, `series`, `how_to_use`, all `readOnlyHint: true` | the "Ask everything" chat on kraken |

- `list_series()` → `[{metric, description, unit, first_date, last_date, freqs}]`
- `series(metric, start, end, freq="day")` →
  `{metric, unit, tz: "America/Edmonton", freq, points: [["YYYY-MM-DD", number], ...], source, as_of}`.
  `start`/`end` are inclusive ISO dates; at most 5 years per call. Errors: HTTP 404 (unknown metric) or 400
  (bad dates, a freq the metric doesn't offer, too long a range); on MCP, a tool error with the same message.

## The metrics (14, an allowlist named by Montney's own series ids)

| Metric | Unit | Day value |
|---|---|---|
| `aeso.price.pool` | $/MWh | simple mean of the day's 24 hourly prices (not load-weighted) |
| `aeso.csd.ail` | MW | mean of the day's 5-min AIL samples |
| `aeso.gen.gas_total.net`, `aeso.gen.wind.net`, `aeso.gen.solar.net` | MW | mean of the day's 5-min samples |
| `ngtl.gdsr.field_receipts.flow`, `.intraprovincial.flow`, `.empress.flow`, `.mcneill.flow` | MMcf/d | the gas day's NGTL value |
| `ngtl.gdsr.net_storage.flow` | MMcf/d | the gas day's value; **+ = net withdrawal** |
| `ngtl.gdsr.eod_linepack.flow` | MMcf | end of the gas day |
| `eia.storage.lower48.working_gas` | Bcf | weekly only (`freqs: ["week"]`; `freq=day` is refused) |
| `wx.calgary.tmean.realized`, `wx.calgary.hdd.realized` | degC, degC-d | Calgary's local day, realized (settles ~1 week) |

Left out on purpose: the `region.*` balance estimates, every forecast, and anything at well or facility level. There
are no gas prices (AECO, Station 2) in Montney at all (`docs/research/DECISIONS.md`: daily AECO is licensed).
Adding a metric is one entry in `readapi.METRICS` (a stored series; never a new fetch).

## Rules

- **Complete days only.** A daily mean of 5-min/hourly data needs >= 90 % of the day's samples, so today never
  appears and a day with an outage is left out. Days without a value are left out, never zero-filled.
- **Day boundaries.** The 5-min/hourly means come from the daily rollups (`obs_fast_1d`, `obs_hourly_1d`), which
  bucket at a fixed UTC-6 (L-029). Alberta is UTC-6 all year since 2026-11-01, so that is exactly the Edmonton day;
  before then, a winter (MST) day is cut 1 h early. Documented, not re-cut. NGTL metrics are gas days (09:00 to
  09:00 Alberta time, 15:00 UTC), dated by the day the gas day starts.
- **Revisions.** Each date returns the latest stored vintage; `as_of` is the newest fetch behind the answer. A past
  value can change on a later call (NGTL, EIA, StatCan revise). No "as known on" in v1, though the store keeps every
  vintage.
- **Licensing.** AESO, TC and other data here is for Erik's personal use: inside his own tools, never published.

## Safety and cost

- **Read-only by construction.** It runs in the `map` service: Postgres role `montney_reader`, write secrets
  blanked, read-only root filesystem. No route here writes or queues anything; `POST /api/refresh` is not reachable
  through it.
- **No load.** No collection change, no timers, no polling, no outside requests. One `series` call is one or two
  indexed reads of a daily table or rollup (at most ~1,830 rows).
- **The MCP can't hurt the map.** The `mcp` SDK is pinned (`pyproject.toml` + `uv.lock`) and imported only when the
  app starts, inside try/except; its session manager runs in its own task. If it fails to build, start or later
  dies, `/mcp/read` answers 503 and the map, `/api/status` and `/api/read/*` serve as before
  (`tests/test_readapi.py`).
- **Tailnet only for the MCP.** Krasnodar's LAN view answers only GET/HEAD (405 otherwise), and MCP is POST. The two
  GET routes may show on the LAN view, like `/api/series` already does.
