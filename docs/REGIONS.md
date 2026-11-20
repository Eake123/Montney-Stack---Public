# Regions: the building block (L-106)

Erik, 2026-11-07: "I want everything to be broken down into regions… each region should balance in terms of power and
natural gas, or should try to." His map: `D:\montney\Division_Map.png` (13 regions). This file is the contract every
agent builds against. **One code path for every region:** a region is a YAML file, never hand-written JS or a
hand-merged box.

## 1. The 13 regions

| id | Box title (Erik's label) | Members (by ISO 3166-2) | Budget region | Detail |
|---|---|---|---|---|
| `wcan` | AB · BC · SK | CA-AB, CA-SK, CA-BC **minus the Lower Mainland / south coast** (→ `pnw`) | `ab_nebc` | full |
| `mbnwo` | Manitoba & NW Ontario | CA-MB + Ontario **north and west** of the Sault Ste. Marie – Moosonee line | `ont` | standard |
| `sont` | Toronto (S. Ontario) | Ontario south and east of that line | `ont` | standard |
| `qc` | Québec | CA-QC | `qc` | standard |
| `upne` | Upper NE (New England) | US-ME, US-NH, US-VT, US-MA, US-RI, US-CT (ISO-NE) | `upne` | standard |
| `nyc` | NYC (New York) | US-NY (NYISO) | `nyc` | standard |
| `umw` | Upper Midwest | US-MN, US-WI, US-MI | `midwest` | standard |
| `pnw` | PacNW | US-WA, US-OR, US-ID + BC's Lower Mainland / south coast | `pnw` | standard |
| `bakken` | Bakken | US-MT, US-ND | `bakken` | standard |
| `mtnw` | Mountain West | US-WY, US-UT, US-CO | `rockies` | standard |
| `cal` | California | US-CA | `cal` | standard |
| `sunbelt` | Upper Sunbelt | US-NV, US-AZ, US-NM | `sunbelt` | standard |
| `tx` | Texas | US-TX | `tx` | standard |

Everything else (the Plains, the Southeast, the Mid-Atlantic, the Maritimes, Alaska, Mexico) is **outside** every region
for now. Erik: "I will fill the rest after this is done." Points out there (Chicago, Dawn's neighbours, Henry Hub) stay
as ordinary features.

Reading of Erik's lines where the drawing is ambiguous (in `docs/QUESTIONS.md` Q-R1..R3; the files are easy to change):
- `upne` / `nyc` follow the ISO footprints (ISO-NE / NYISO), because that's how the power data comes.
- `mtnw` is WY + UT + CO. His line crosses western SD/NE; those stay outside until he says otherwise.
- "MT" in "MT & Western Ont" is read as **Manitoba** (it sits on Manitoba in his map). The split from `sont` follows his
  curve from Lake Superior to James Bay, which is close to IESO's Northwest + Northeast zones.
- The BC coast cut follows his PacNW line: Vancouver Island south of ~50.5°N and the Lower Mainland / Fraser Valley go
  to `pnw`. Kitimat and Prince Rupert stay in `wcan`.

**Membership rules (so nobody argues case by case):**
1. A **point feature** (plant, station, storage field, city) belongs to the region of its own country whose polygon
   contains it. It's computed by `montney.regions.region_of(lon, lat, country=…, member=…)`, never typed by hand.
   `country` is required; pass `member` (its state/province, ISO 3166-2) whenever it's known, so a point near a
   border can't fall into the neighbour's simplified outline (Reviewer on #408 / #411).
2. A **balancing-authority or province-wide number** (BC Hydro load, IESO total, EIA state data) belongs to the region
   that holds most of it. When a region holds only part of a BA, the row says so with `covers` (§3). It is never
   silently counted twice.
3. A **flow between two regions** is an export of one and an import of the other, measured at the border point. Flows
   *inside* a region (AB→SK on NGTL, AB↔BC on the intertie) are not imports or exports of that region.

## 2. Files

| Path | What | Owner |
|---|---|---|
| `regions/<id>.yaml` | one file per region: members, clips, anchor, balance parts, weather, watch list (§3) | orchestrator owns the schema and the file; Gas / Power / Weather fill their own sections by PR |
| `regions/README.md` | how to add or fill a region (short) | orchestrator |
| `montney/regions.py` | load and validate the region files; point-in-region lookups; the generated map features | orchestrator |
| `scripts/build_regions.py` | builds `network/region/regions.geojson` from Natural Earth admin-1 (public domain) + the clips; run with `uv run --with shapely python scripts/build_regions.py` | orchestrator |
| `network/region/regions.geojson` | generated: one `area` (outline) + one `node` (box anchor) per region. **Never edit by hand.** | generated |
| `montney/connectors/region_balance.py` + `sources/region_balance.yaml` | the one engine that turns every region's parts into the standard series (§4). Zero requests: reads the store. | orchestrator |

## 3. A region file

```yaml
id: wcan
name: AB · BC · SK              # the box title
members: [CA-AB, CA-SK, CA-BC]
clips:                          # optional: a piece of a member handed to a neighbour (polygon in lon/lat)
  - {member: CA-BC, to: pnw, note: "Lower Mainland + south Vancouver Island", polygon: [[-129, 48], ...]}
anchor: [-112.0, 55.0]          # where the box sits
tz: America/Edmonton
budget_region: ab_nebc          # montney.region code for storage + request budgets
coverage: full                  # full | standard (how much zoom-in detail we promise)
box: {dir: E}                   # optional: where the box goes when the region is too small on screen to hold it (§5)
detail:                         # the region page's groups, per tab (§5b). Gas owns gas: and oil:, Power owns power:
  gas:
    - id: demand_by_sector
      title: Demand by sector
      of: demand
      rows:
        - {label: Power burn, series: gas.wcan.power_burn}
        - {label: Storage capacity, value: 550, unit: Bcf, as_of: "2026-06", source: "EIA-191"}

gas:                            # MMcf/d (storage inventory Bcf). Every line is optional.
  production: {parts: [production.ab.daily_est, production.bc.daily_est], covers: [CA-AB, CA-BC], quality: estimate}
  imports:    {parts: [...]}
  exports:    {parts: [...]}
  demand:     {parts: [...]}
  storage:    {parts: [ngtl.csr.net_storage]}                 # measured net INJECTION (+ in, − out)
  inventory:  {parts: [...], unit: Bcf}
power:                          # MW, hourly
  load:        {parts: [aeso.csd.ail], covers: [CA-AB]}
  generation:  {parts: [...]}
  interchange: {parts: [...]}   # net IMPORTS into the region (+ in, − out)
  fuels:                        # generation by fuel; keys from FUELS in montney/regions.py
    gas: {parts: [aeso.gen.gas_total.net]}
    wind: {parts: [aeso.gen.wind.net]}
oil:                            # bbl/d
  production: {parts: [...]}
weather:
  cities: [wx.calgary.temp, ...]  # optional; Weather owns
watch:                          # what to watch daily (research, plain words; shown in the panel)
  - "NGTL linepack vs target: low linepack precedes AECO spikes"
```

**A part** is a series id, `-series` (subtract), or a mapping `{series, sign: -1, scale: 1.0, fill: month}`:
- `fill: month` holds a monthly value (as a daily rate) across every day of its month. Used for EIA / StatCan monthly
  data in a daily balance. It marks the line `estimate`.
- `fill: latest` does the same, and also holds the **newest** month forward into the months not published yet (EIA's
  state data runs 2-3 months behind), for at most 150 days past that month's end. Also `estimate`. Use it when a region
  line would otherwise go blank for recent days; the panel shows which month the value is from.
- `fill: seasonal`: an unpublished month takes the **same calendar month a year earlier**. For demand-type flows
  (consumption) where a current estimate matters: the season stays right. The box tags it "seasonal"; `latest` is
  tagged "held".
- **Which fill where** (Reviewer on #434/#435; enforced by `regions.LINE_FILLS`): holding August's storage injection
  into November flips its sign.
  - `storage` (a flow): `fill: month` only, the real month. Add a section `lag:` so it shows with its age.
  - `inventory` (a stock): `month` or `latest`.
  - Demand: `seasonal` for monthly parts.
  - Production and oil: `latest`.
  - No `storage_implied` / `residual` is written when any production / import / export / demand part is a held or
    seasonal stand-in.
- **Oil** is crude + condensate everywhere (EIA's state "crude oil" already includes lease condensate; Canadian lines
  add field condensate to match).
- `scale` converts units on the fly. Prefer converting on ingest (AGENTS.md); `scale` is for re-using a series that's
  legitimately in another unit (e.g. a Bcf/d feed).

**`covers`** lists the members a line actually measures, when that's not all of them. An empty `covers` means the
region's own `members`. A region that **receives a clip** (pnw holds BC's Lower Mainland, mbnwo holds NW Ontario) must
list the clipped member in `covers` for a line to count as the whole region; otherwise the box tags it ("(WA, OR, ID)"). The box shows a short "(AB, BC)"
after the label, so a partial number never looks like the whole region.

**`scope`**: a short label (≤ 16 characters) for a line whose footprint isn't the region and that `covers` can't
express, e.g. `scope: CAISO` (California's CAISO is ~80 % of the state) or `scope: all ON` (Ontario's IESO total shown
in `sont`). The box shows it after the label: "Load (CAISO)".

**`derived: true`** marks a line computed from the region's other balance lines (PacNW's demand is imports − exits +
storage withdrawal). The engine then writes no `storage_implied` / `residual` for the region, because they would equal
the measured storage by construction. Derived box rows (net imports, storage implied) inherit every `scope`, partial
`covers` and est. of the lines they're built from.

**`lag:`** (on a line, or on a section as every line's default: `gas:` / `power:` / `oil:`, e.g. `lag: 5d`) is how late the section's slowest part publishes.
BC Hydro posts load on weekdays, up to ~4 days late. Put it on the line when only that line is late (a monthly storage
line next to daily imports), so a dead daily feed still reads stale (Reviewer on #434). The section's region series then read stale only after `lag` + 3 h
(`regions.stale_after`, used by the snapshot), and the box shows the row's age instead. A composite that's days old is
never presented as live. The engine's power window reaches back 7 days so such lines still get written.

**`quality`**: `measured` (default when every part is a measured feed), `scheduled` (pipeline nominations) or
`estimate`. The box marks estimates "est.".

**`gas: through: yesterday`** is for a region whose gas lines come from pipeline bulletin boards. The boards carry the
current gas day at its latest intraday cycle, and each pipe is at a different one, so today's sum is not a balance
(New England read −550 MMcf/d mid-cycle against +16 for the finished day). With this key, every gas line of the
region (and its storage implied) is written only for gas days that have ended on the NAESB clock (09:00 to 09:00
Central). The box then shows yesterday's gas day; today's postings stay visible in the detail groups.

**`gas: storage: {none: true}`** says the region has no underground storage (New England, Manitoba). What the four
lines leave over is then the balance's **closure** (linepack, timing, measurement), not a storage flow: the same
`storage_implied` series, labelled "Closure (no storage)" in the box and on the page, with no residual line.

**`held: true`** (with `quality: estimate`) marks a line with a part that is its source's latest value held forward
by its connector (SaskPower publishes one daily mean ~2 days late; `saskpower.*_best` holds it to the current hour).
The row is tagged "held", like a `fill: latest` part, and like one it never feeds storage implied.

**`why:`** in a section (`power: {why: {generation: "MISO publishes no generation by region"}}`) gives the reason a
line the region does *not* have is missing. The box row and the page show it instead of "no source yet". It's only
for a line with no source.

**`power: fuels_note:`** is the note under the region page's automatic "Generation by fuel" group (what "other"
includes, which fuels are a nowcast).

**`unproven: true`** (with `quality: estimate`) marks a line that is a model whose **daily** error hasn't been measured
yet (the regional demand model's band is a monthly p90; Reviewer on #471). The line is written and shown, but the
engine writes no `storage_implied` / `residual` from it, and the box says why. It comes off per region, with the
numbers in the PR, once a daily back-test against measured data shows the error is small next to the storage signal.
A demand line that is mostly measured deliveries, with a model only for the remainder, isn't unproven.

## 4. The standard series (written by `region_balance`)

For a region `<r>`. Only lines whose parts all exist are written, and a timestamp is written only when **every** part
has a value there. A missing part never counts as zero.

| Series | Unit | Freq | Meaning |
|---|---|---|---|
| `region.<r>.gas.production` | MMcf/d | D (gas day) | sum of parts |
| `region.<r>.gas.imports` / `.exports` | MMcf/d | D | sums of parts, both positive |
| `region.<r>.gas.net_imports` | MMcf/d | D | imports − exports |
| `region.<r>.gas.demand` | MMcf/d | D | sum of parts |
| `region.<r>.gas.storage_implied` | MMcf/d | D | **production + net imports − demand**: what must have gone into (+) or out of (−) storage, linepack and the error |
| `region.<r>.gas.storage` | MMcf/d | D | measured net injection (+ in, − out), where a feed exists |
| `region.<r>.gas.residual` | MMcf/d | D | storage_implied − storage: the unexplained part (only where both exist) |
| `region.<r>.gas.inventory` | Bcf | D | storage inventory, where a feed exists |
| `region.<r>.power.load` / `.generation` | MW | H | sums of parts (hourly means) |
| `region.<r>.power.interchange` | MW | H | net imports (+ in, − out) |
| `region.<r>.power.gen.<fuel>` | MW | H | generation by fuel |
| `region.<r>.power.residual` | MW | H | generation + interchange − load (losses, behind-the-meter, missing pieces) |
| `region.<r>.oil.production` | bbl/d | D or M | sum of parts |

Buckets: a gas part's value for a gas day is the mean of its points in that day (or its month's value with
`fill: month`). A power part's value for an hour is the mean of its points in that hour. The engine re-writes a trailing
window each run (gas 21 days, power 7 days, oil 400 days). The store is change-only, so unchanged values cost nothing.
After a source back-fills history older than the window, rebuild once by hand:
`MONTNEY_REGION_REBUILD_DAYS=<n>` in the environment of one `region_balance` run (1-400 days; gas and power).

`FUELS` (the only fuel keys, and the sprite kinds they map to): `gas`, `coal`, `nuclear`, `hydro`, `pumped_storage`,
`wind`, `solar`, `battery`, `oil`, `biomass`, `geothermal`, `other`.

## 5. On the map: the box (v2, Erik 2026-11-13, L-107)

Erik: "there should be one main box, then two subboxes within the main one. The first one is gas: total production,
total demand, imports, exports. Then that residual should give the implied storage number. The second box is just the
generation number. And just that."

- **One box per region, the same six rows everywhere**, built by `montney/regions.py` (`box_v2`) into the anchor
  feature's `box_rows`. No region is special-cased in JS.
  - **Gas · MMcf/d:** Production · Demand · Imports · Exports · Storage implied.
    Sources read **+** and sinks **−** (each row carries `sign`), so the first four add up to the fifth:
    storage implied = production + imports − exports − demand (+ = injection).
  - **Power · MW:** Generation. Only that.
- **Every row is always there.** A row is one of: a value (`have: true`); a known zero (`zero: true`, from
  `none: true`: California exports nothing by pipe); or missing with the reason in `why` ("no source yet", "demand is a
  monthly stand-in, not daily data"). A missing row reads "—" on the map. It is never a blank, never a zero.
- **Storage implied needs real daily numbers.** It isn't written when production, imports, exports or demand is a held
  or seasonal monthly stand-in (Reviewer on #434/#435: that's noise, not a balance). A region with no production says
  `production: {none: true}`. Getting every region to four daily lines is Gas's job (PLAN 9).
- Oil, load, interchange, the fuel rows, measured storage and inventory are on the region page (§5b), not in the box.

**Where a box sits** (Erik: "it should keep the boxes over their designated geographic region… the California box should
be over California"; "it's anarchy right now"). The rules, implemented by Frontend:
1. A box's home is its region. Whenever it fits, it is drawn centred on the region's `anchor`, with no leader, and it
   stays there while panning and zooming.
2. Boxes never overlap. When they would, a box steps down in size instead of drifting away: full → compact (title +
   the headline numbers) → chip (title only, in the region's colour). The form depends only on zoom and screen size,
   with hysteresis, so nothing flips during a pan or a pinch (the locked "callouts must not jump" rule).
3. A region too small on screen for even a chip gets its box just outside, on the side named in its file
   (`box: {dir: E}`; E when absent), with a short leader to the anchor. A leader never crosses another region's box.
4. The box's border and title use the region's `color`, the same as its outline.
5. Per-region hints live in the region file (`anchor`, `box`), never in JS.

- **The region outline** is drawn faintly at every zoom, with the title at low zoom.
- **Zooming in (≥ 6)** hides the region box and shows the region's detail: plants with fuel sprites, pipelines,
  compressors, storage, hubs, wells.

## 5b. The region page (click the box title)

Erik: "when you click the region's header, it should open a tab-like thing with much more detail… this is where the
granularity really should be, split between gas, power & oil. I should be able to see for example what the total data
center production is."

One call builds it: **`GET /api/region/<id>`** (`montney/regionpage.py`; `GET /api/regions` lists the regions). Tabs
**Gas / Power / Oil**, each with:
- **`lines`**: the tab's balance lines in page order, each `{line, label, series, unit, v, ts, prev, prev_ts, stale,
  freq, tags, have, quality, note, parts}` (+ `zero` or `why` when there's no value).
  Gas: production, demand, imports, exports, net imports, storage implied, storage measured, unexplained, inventory.
  Power: generation, load, net interchange, unexplained. Oil: production.
- **`groups`**: the region's `detail:` groups, each `{id, title, unit, rows, of?, rest?, note?}`.
  - A **row** is a stored series (`{label, series, unit, v, ts, prev, stale, …}`) or a **static fact**
    (`{label, value, unit, as_of, source}`) for something with no feed, like an installed capacity or a published
    estimate. A fact without a date and a source is rejected. Derived numbers must be written as real series by a
    connector, so everything on the page can be charted (`/api/series?ids=`) and, later, served to agents by an API.
  - **`of`** names the balance line the rows break down (`of: demand`). The page then gets **`rest`** = the line minus
    the rows' sum: what the breakdown doesn't explain. It's only computed when the line and every series row have a
    fresh value in the same unit for the same reading (same timestamp). A missing or late row never counts as zero.
  - Power's first group is always **`generation_by_fuel`**, built from `power.fuels` (don't repeat it in `detail:`).
- Also on the page: `box` (the six rows with values), `weather` (the region's weather section with latest values) and
  `watch`.

**Group ids are shared across regions** (snake_case), so the same table means the same thing everywhere:

| Tab | Group ids |
|---|---|
| gas | `production_by_area`, `imports_by_point`, `exports_by_point`, `demand_by_sector` (power burn, residential + commercial, industrial, LNG feedgas, pipeline fuel), `storage` (fields, inventory, the EIA weekly region), `lng`, `pipelines` (flow vs capacity on the big pipes) |
| power | `generation_by_fuel` (automatic), `load_by_zone`, `interchange_by_tie`, `plants` (the largest, live output where published), `capacity_by_fuel`, `nuclear_units`, `outages`, `data_centres`, `miso_fuel_mix` (umw only: MISO publishes its fuel mix for the whole footprint, not by region; labelled "all of MISO") |
| oil | `production_by_area`, `refinery_runs`, `pipelines`, `exports`, `stocks`, `rigs` (not built: the rig-count providers' terms don't allow automated use) |

A region adds a new id when it has something the list doesn't cover; say so in the PR so the list stays one list.

## 6. Requests (v2, Erik 2026-11-13, L-107)

Erik: "I am running a VPN on kraken, so we can make more requests than we are doing right now… the request limits I
imposed before are too strict, so use your discretion to increase them quite substantially. Of course don't be DDoSing
people, but we can do much more than we're doing." And: "90% of things are stale, or monthly data. We should try to
find at least daily data for most of this stuff."

**The aim is fresh data, not few requests.** A daily number should be on the map within hours of being published; a
feed an operator updates every 5 minutes may be read every 5 minutes.

What binds now is **politeness per host**, not a small total:
- **The client paces every host:** request starts to one host are at least 0.5 s apart across all sources (at most 2 a
  second; `MONTNEY_HOST_GAP_S`, `montney/connectors/base.py`), and at most 2 are in flight. A spec can't go faster.
- **A small operator's site** (a pipeline's bulletin board, a utility's page): at most ~60 requests an hour, sustained.
  Big public APIs built for polling (EIA, an ISO's real-time feed, NOMADS) may take more, within their published limits.
- **Follow the site's own rules:** its stated rate limits, its terms, its API key quotas. A 429 means slow down for
  good, not retry harder.
- **Never download the same thing twice.** Use `If-Modified-Since` / `ETag` where the site supports them, ask only for
  the days you don't have, and cache files that never change (past years, plant lists) once.
- **Poll around the publish time.** A file that appears "by 10:00" is checked every 15-60 minutes from a little before
  until it lands, then left alone until the next cycle. Pipeline boards are read every 1-2 hours for the intraday
  cycles. This is what turns a 39-hour-old daily number into a 3-hour-old one.
- **Probe from kraken first.** The runner goes out through a VPN and some sites block VPN exits. Check a new host from
  the server before building on it: `docker exec -i montney-runner-1 python - < probe.py` (httpx inside
  the runner honours its proxy, so the request leaves the way the runner's do; Gas, 2026-11-13).
- **Multi-region sources** (one pipeline family's boards covering several regions, EIA for every state) declare
  `region: rest`.
- **Sources must be public and free**, as before. Nothing licensed (no AECO or Station 2 prices), no logins.

Region caps (`sources/BUDGET.yaml`, a warning, never a block): 120 requests/hour for every region outside `ab_nebc`
(was 3-16), 400 for `ab_nebc`, 120 for sources that cover many regions at once. Each source still declares `region:`
and adds its measured or estimated rate to the file.

## 7. Adding detail to a region (the per-region checklist)

Each region gets the same pass, in this order. One PR per agent per region.
1. **Research** (`docs/research/regions/<id>.md`, Gas + Power + Weather sections): the grid (BAs, interties, big plants,
   what moves prices), the gas system (production basins, pipelines in/out, storage, LNG, hubs), and **what to watch
   daily**. Sources must be public and free. List the requests each would cost.
2. **Gas:** fill `gas:` (and `oil:`), with border-point features for the imports and exports.
3. **Power:** fill `power:`. Add every public generator with a fuel sprite (from the plant inventory, §8), plus live
   output where the operator publishes it.
4. **Weather:** cities and heating/cooling degree days for the region's demand; GFS grid coverage if the region is
   outside the current grids.
5. **Frontend:** check the box and the zoom-in detail render right. Screenshots for Erik.

Since L-107 the bar is higher (PLAN 9): four daily gas lines per region so storage implied computes, a live generation
number, and the region page's detail groups (§5b) filled per tab.

## 8. Shared building blocks (done once, used by every region)

- **Plant inventory (Power):** one public inventory of every generator ≥ 1 MW across the US and Canada, with location,
  fuel, capacity and status (NACEI / EIA-860 / the Canadian lists; Power's research picks). It's built by a script into
  `network/power/plants_<region>.geojson`, assigned to regions by rule 1, and sprites come from fuel by `FUELS`.
  Live MW where the operator publishes it (AESO, IESO today). Elsewhere capacity, with a fuel-level utilisation from the
  BA's hourly fuel mix (marked est.) so wind farms still spin with the wind.
- **US hourly grid (Power):** EIA-930 (demand, net generation by fuel, interchange for every balancing authority), as few
  requests as possible. BAs sum into regions.
- **US state gas and oil (Gas):** EIA monthly production / consumption by state and weekly storage by EIA region, in as
  few page loads as possible.
- **Sprites (Art):** every `FUELS` kind needs a sprite. New: coal, battery, oil, biomass, geothermal, pumped storage.
- **Weather (Weather):** cities for every region, and GFS coverage for Texas and the south.
- **Wells on the main map (Gas + Frontend):** the Petrinex (AB) and BCER (BC) facilities are drawn on the main map
  when zoomed in, not only in the producers popup.
