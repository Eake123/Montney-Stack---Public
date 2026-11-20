# Source spec reference

A source is one YAML file in `sources/` (shared, in the repo) or in your home `sources/`
folder (private; a spec there with the same `id` overrides the repo one).

## Common keys

| Key | Meaning |
|---|---|
| `id` | unique id, `[a-z0-9_]` |
| `name` | display name |
| `type` | `http` (declarative, default) or `python` |
| `mode` | `cached` (default; stored in DuckDB, history accumulates) or `live` (kept in memory, replaced each fetch) |
| `interval` | how often to fetch: `30s`, `15m`, `2h`, `1d` |
| `stale_after` | optional, `90m` / `4h` / `2d`: for a source that publishes late by design, how old its newest sub-daily value may be before the map calls it stale (default: 3 × the series' frequency, at least 1 h). Daily series have their own rule (stale after 4 days) |
| `enabled` | `false` to switch off without deleting |
| `region` | budget region (Postgres `region` table: `ab_nebc` default, `rest`, `pnw`, `cal`, `ont`, `qc`, `rockies`, `midwest`, `upne`, `nyc`, `bakken`, `sunbelt`, `tx`; Ontario is `ont` because YAML reads a bare `on` as true); the storage and request budgets (`montney du`, `sources/BUDGET.yaml`) count the source there |
| `tz` | timezone for `{today}` templates (default `America/Edmonton`) |
| `derive` | computed series, see below |

## `type: http`

```yaml
request:
  url: https://host/path
  params:  {startDate: "{today-3d}", endDate: "{today+1d}"}
  headers: {API-KEY: "{secret:AESO_API_KEY}"}
  iterate: {var: date, days_back: 5, days_forward: -1}   # optional: one request per day -> {date}
  timeout: 30
  retries: 3          # on 429/5xx/network errors, with backoff (daily-quota 429s fail fast)
  pause: 0.3          # seconds between iterated requests
extract:
  - path: data              # dotted path into the JSON. A list = rows, an object = one row.
    ts:    {field: lastUpdated, tz: "-07:00"}
    as_of: ts
    id: "ngtl.csr.{key}"
    label: "NGTL {label}"
    group: NGTL / Current System
    freq: 30min
    from: e3m3/d
    unit: MMcf/d
    fields:
      total_receipts: {field: totalReceipts, label: total receipts}
      linepack:       {field: currentLinepack, from: e3m3, unit: MMcf}
      empress:        {field: empressBorder, abs: true}
```

### Templates

Usable in `url`, `params` and `headers`:

- `{secret:NAME}`: from the OS credential store (`montney secrets set NAME`) or the env var `NAME`
- `{today}`, `{today-3d}`, `{today+1d}`: local date, ISO format
- `{date}` (or your `iterate.var`): the iterated date

Usable in `id`, `label` and `group`: `{key}` (field key), `{label}` (field label),
`{row}` / `{row_name}` (the `key_field` value, slugged or raw). Filters: `|slug`, `|title`, `|upper`.

### Timestamps: `ts`

| Form | Meaning |
|---|---|
| `{field: name}` | read from each row |
| `{root: a.b.c}` | read once from the response root (e.g. a report date) |
| `tz: UTC \| "-07:00" \| America/Edmonton` | zone of naive timestamps (stored as UTC) |
| `format: "%Y-%m-%d %H:%M"` | strptime format if not ISO-like |
| `epoch: s \| ms` | numeric epoch timestamps |
| `date_only: true` | daily data: store the calendar date (use with `freq: D`) |
| `offset_days: 1` | shift (e.g. next-day nominations) |

### Vintages: `as_of`

- `ts` (default): the value's own timestamp. Use this for actuals.
- `fetch`: the fetch time. Use this for forecasts, so every run is kept and you can compare runs.
- `{root: ..., tz: ...}` or `{field: ...}`: a publish time from the payload.

Queries return the latest vintage. Widgets can ask for an older one, e.g. the weather strip's
"vs 24h ago".

### Fields

`fields` maps a series key to a source field, given as a name or as an object:

| Key | Meaning |
|---|---|
| `field` | source field (dotted path allowed). Defaults to the key. |
| `label` | human label |
| `id` | override the block's id template |
| `from` / `unit` | convert from source unit to stored unit (gas: `cf Mcf MMcf Bcf Tcf m3 e3m3 e6m3`, optional `/d`) |
| `abs` | store absolute value |
| `scale` | multiply |
| `freq` | override block freq |
| `day` | daily series only: `gas` if the series is keyed by NGTL gas day (rolls at 15:00 UTC); omit for calendar days. Can also be set on the block |

### One series per row value: `key_field`

```yaml
  - path: return.generation_data_list
    key_field: fuel_type             # COGENERATION, COMBINED CYCLE, ...
    id: "aeso.gen.{row}.{key}"       # aeso.gen.combined_cycle.net
    label: "AESO {row_name|title} {label}"
    fields:
      net: {field: aggregated_net_generation, label: net gen}
```

YAML anchors (`&name` / `<<: *name`) are handy for repeating a block with small changes.
See `sources/ngtl_gdsr.yaml`.

## `derive`

Sum or difference of series at the same timestamp. A point is only produced when every
input is present.

```yaml
derive:
  - id: aeso.gen.gas_total.net
    label: AESO gas-fired generation
    sum: [aeso.gen.cogeneration.net, aeso.gen.combined_cycle.net]
    minus: []
    unit: MW
    freq: 5min
```

## `type: python`

For anything a spec can't express (scraping, PDFs, computed fields):

```yaml
id: weather
type: python
module: montney.connectors.open_meteo     # or: file: plugins/my_thing.py (must live in plugins/)
class: OpenMeteo
params: {...}                             # read by your class from self.spec["params"]
```

```python
from montney.connectors.base import Connector, Context, FetchResult
from montney.store import Obs, SeriesMeta

class MyThing(Connector):
    def declared_series(self):            # optional: shows in the catalog before first fetch
        return [SeriesMeta("my.series", self.id, "My series", "MMcf/d", "D", "My group")]

    def fetch(self, ctx: Context) -> FetchResult:
        j = ctx.get("https://...", params={...}).json()   # retries/backoff built in
        obs = [Obs("my.series", ts, value, as_of) for ...]  # ts/as_of: naive UTC datetimes
        return FetchResult(obs=obs, meta=self.declared_series())
```

A file polled more often than it changes (a daily report checked hourly): `r = ctx.get_if_changed(url)`. It sends
the ETag / Last-Modified the URL last answered with and returns `None` on HTTP 304: nothing new, what the file says
is already stored. The validators are kept only after a run whose observations were written, so a failed run
downloads the file again. They live in memory, per source: the first pull after a runner restart is a full download, and another source
reading the same URL gets the file. Use it only where the output depends on the file alone; a connector that also
depends on the clock or on a second file keeps its own parsed copy.

Imported tab bundles never install `file:` plugins.

## Series id conventions

`<source-family>.<report>.<item>[.<measure>]`, lowercase with underscores, for example
`ngtl.gdsr.empress.flow`, `aeso.load.ail_forecast`, `wx.calgary.hdd`.
Keep ids stable: tabs and history reference them.
