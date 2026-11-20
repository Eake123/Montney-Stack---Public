# Map config reference (`web/map.json`)

The old draggable board and its widgets are gone. Montney Stack v1 is a single live map
([`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §6; design notes in
[`docs/research/frontend.md`](research/frontend.md)). What the map shows is configured in
**`web/map.json`**, served as a static file, so editing it and reloading the page is all it takes.

## `view`

```json
"view": {"center": [-116.6, 53.9], "zoom": 5.15, "minZoom": 3.2, "maxZoom": 13,
         "maxBounds": [[-175, 8], [-25, 75]]}
```

PLAN 10a (Erik, 2026-11-14: "increase the zoom" and "zoom over all of America and northern Mexico ... right now I cannot
look at Texas"):
- **Bounds:** generous, with open ocean at the edges. The contiguous US, northern Mexico to 20° N and the Canada we show are
  all inside. They're wide enough that minZoom, not the bounds, stops a 1920 px screen (`placement.js floorZoom`).
- **The floor, `minZoom` 3.2:** on 1920x1080 all 13 regions' territory and boxes are on screen at once, clear of the side
  panels; 1600x900 still fits. A phone's floor is lower still (NARROW_DZ), where the bounds' height stops it.
- **The deep end, `maxZoom` 13:** nothing breaks. The terrain (DEM maxzoom 10) overzooms and our style draws no roads, so past
  ~z 11 the extra zoom only separates dense plant and facility clusters.
- **The weather field** covers only its grids (lon −140..−66.5, lat 25.5..65). Beyond them it fades out over its 1° edge
  feather and the plain basemap goes on. Widening the grids is Weather's call.
- `tests/test_web.py` checks the bounds and that the 13 regions fit at the floor on 1920x1080.

## `layers`

```json
{"id": "temp", "label": "Temperature", "group": "field", "on": true}
```

| id | what |
|---|---|
| `temp`, `windspeed` | the colour field (`group: "field"` makes them radio buttons, plus "No field") |
| `wind` | animated particles |
| `gas`, `power` | network links (schematic in v1) and their nodes |
| `areas` | network areas (e.g. NGTL DOP areas), shaded by capability utilization |
| `events` | outage/alert pins on their referenced features |
| `callouts` | the callout boxes below |
| `terrain` | hillshade **and** the lapse-rate downscale of temperature |

## Callouts

Compact value boxes with a leader line to an anchor. Click a row to open the drill-down.

**Most callouts come from the network itself.** Any feature in `network/**.geojson` with
`callout: {"label": "...", "priority": 1-3}` gets a box showing its `value` (with "vs tgt" if it has a
`target`, and a `cap` tag if `value_kind: "capacity"`). Its optional `callouts_extra: [{label, series, unit,
priority}]` adds rows. A `type: "region"` node, or a feature with `region_value: true`, is drawn as a labelled
dashed ring with a dashed leader. Region names that share an anchor (BC production and Westcoast T-North at Fort St
John) stack one line apart under one ring. New features show up without code changes.

`web/map.json` adds what the network can't express:

```json
"callouts": [
  {"id": "c.ngtl", "tier": 1, "anchor": {"at": [-113.9, 52.7], "label": "NGTL SYSTEM"}, "prefer": ["W", "SW", "NW"],
   "rows": [{"series": "ngtl.csr.linepack", "label": "Linepack", "ref": "ngtl.csr.linepack_target",
             "ref_label": "vs tgt", "unless_feature": "gas.ngtl.linepack"},
            {"series": "ngtl.csr.total_export", "label": "NGTL export"}]}],
"overrides": {
  "gas.empress": {"prefer": ["E", "NE", "SE"]},
  "power.region.ab": {"rows_before": [{"series": "ngtl.csr.intraprovincial", "label": "AB demand (gas)"}]}}
```

| key | meaning |
|---|---|
| `callouts[].tier` | zoom level: 0 = overview, shown from the furthest zoom-out; 1 = from zoom 6 (the breakdown); 2 = from 6.75; 3 = from 7.5. `minzoom` overrides the start; `maxzoom` hides the box from that zoom on (an overview box handing off to its breakdown). Higher tiers are dropped first when space runs out |
| `callouts[].anchor` | a network feature id (node point, link midpoint, area `label_point`), or a region `{"at": [lon, lat], "label": "..."}` |
| `callouts[].prefer` | ordered compass directions (`N NE E SE S SW W NW`), each tried at leader lengths 34, 64 and 100 px. Used for a box's first placement only; after that the box keeps its slot (see Stability) |
| `unless_feature` | on a callout or a row: skip it once that feature exists (a fallback until the network provides it) |
| `rows[]` | **either** `series` (fetched via `/api/snapshot?extra=`) **or** `feature`; plus `label`, `unit`, `decimals`, `priority` (rows with priority 2 or 3 appear from zoom 6.75 or 7.5) |
| `rows[].ref`, `ref_label`, `vs_target` | "vs" chips: a `ref` series, or the snapshot's feature-level `target` |
| `rows[].band` | an estimate: shows `≈` and `±band` |
| feature `band` / `estimate` | a feature's own `band` (e.g. 0.10) marks its value an estimate in its box and its panel headline (`≈ v ±abs`). An `estimate` block `{lo, hi, basis}` (lo/hi series ids) makes the panel headline `est v [lo–hi]` with the basis below. An estimated feature never shows a bare number |
| `overrides[feature id]` | `prefer`, `tier`, `minzoom`, `maxzoom`, `label`, `hide`, `rank`, `rows_before` (rows prepended to that feature's box), `sprite` (a node's atlas kind, e.g. `plant-coal-to-gas`, for an exception its `fuel_primary`/`fuel` can't express) |

**Stale and zero values (PLAN 4b, L-058).** A stale, missing or zero value gets no box, so you don't see solar at
night, an idle unit or a dead feed:
- A feature's box goes when its own value does. A map.json box goes when its first row (its own value) does, so a
  part row never stands in for the whole.
- Other rows go one by one, and a box with no rows left goes.
- "Zero" means the value shows as 0 at the row's precision.
- A NO-RESULT window mean (a thin hour) stays, because it says why there's no number.

The exception is a box marked `always_show: true`: a network feature property (Gas and Power set it on field
receipts, storage, LNG export, AESO load, AB/BC production and the Fort McMurray box), or the same key on a map.json
callout (the production, demand and storage overview boxes). Those boxes always show, stale values included, and keep the
STALE chip.

**Fully zoomed out (step 5, L-060).** The overview (tier 0) is Erik's four boxes:
- total AB + BC production at the Montney area (`gas.production.total`: NGTL, Alliance, Alberta, BC, each with its share);
- AB gas demand (`gas.ab_demand`): population vs industry, with the industry parts (oil sands, Heartland, power, other) as
  indented "· " sub-rows from zoom 6.75;
- LNG export at Kitimat (`gas.kitimat`, the full total);
- the AESO load box by fuel (`power.region.aeso_load`).

The export borders join them. The four are ranked first in `map.json`, so they're placed before anything else. Storage
follows, and the Montney Area box shows from zoom 5.6 until it hands off at 6. A `callouts_extra` label starting "· " is
a part of the row above and renders indented.

**Alberta demand box (step 6, Erik: "the gas and the power should be in the same box").** A map.json override `merge_into`
appends one feature's box to another's as a section. The target's `title` and `section`, and the merged feature's
`section`, become head rows. With `merge_until`, boxes merge only below that zoom. `power.region.aeso_load` merges into
`gas.ab_demand` below zoom 6.75, giving one "Alberta demand" box zoomed out:
- a gas section (MMcf/d, shares of AB gas demand): intra-AB, population, industry and its parts;
- a power section (MW, shares of AB load): AESO load, then generation by fuel and net imports.

Rows, units, shares and signs are unchanged. Demand reads negative; the fuel rows are the supply that meets the load. The
merged box has a neutral border, since it holds both systems. From 6.75, where the gas parts appear, the two boxes split again so
each has room to place.

**Montney Area overview (PLAN 4b, Gas #209; Erik's name).** Zoomed out (tier 0, up to zoom 6), the `c.montney` box
("MONTNEY AREA") shows:
- Montney Area receipts (today est., ±6 %);
- BC: into NGTL, T-North and CGL direct (±9 %);
- AB: into NGTL (±6 %);
- Alliance Zone 1 (BC + NW AB), on the same gas day as the total (`montney.zone1.in_total_est`, Gas #215).

The BC Montney box has `rank: 0`, so it wins its spot at zoom 6.

The three parts add up to the total, with Zone 1 counted once. From zoom 6 it hands off to Gas's BC and AB Montney boxes,
which split each side by destination.

**Signs (PLAN 4c, L-060).** Production reads positive and demand negative, for gas and power. This covers values, change
chips, window means, totals and the panel (headline and every chart line). A share stays a size (+5.2 % of AB demand).
- `flow: "demand"` or `"supply"` on a feature or a row decides.
- Otherwise a demand-like series id reads negative: …demand…, intraprovincial, deliver…, OSDA, `.city.`, AESO `.ail`, load,
  gas_burn.
- Everything else reads as stored. That includes receipts, production, generation, interties (already signed import/export)
  and exports. Gas or Power set `flow` where that default is wrong.

**Click to open (PLAN 4c).** Clicking a running asset (a node with a value) whose box isn't on screen opens the box it
would have when shown, pinned at its point. It shows the value, band and share plus any live extra rows, and it's placed
first. Its ✕ closes it. An asset whose box is already showing opens its panel as before.

**Borders by system (PLAN 4b; step 7).** A box's frame keyline, plus a 1 px line just inside it, is coloured by its
system: gas orange (#ff6b1a), power blue (#3d9bff), a 2 px band at any display scaling. The gas orange is a red-orange,
clear of the alert amber #ffb547. A box's system is its feature's `system`, or
`system` on a map.json callout. The tints are SVG colour-matrix filters in `index.html`, applied to Art's frame, so the
pixel art stays exact and crisp.

**Shares (PLAN 4b, 4c).** A feature with `share: {of: "<basis series>", label: "% of AESO generation"}` shows its share as
a small number on its value line, with a tiny basis cue so it can't read as utilisation: "2,803 MMcf/d · 22 % prod",
"1,372 MW · 13 % gen", "· 5 % dem", "· 22 % exp".
- **Colour:** a gradient by size, from muted (#6e8d7f) for small shares to strong (#e8f5ee) from 25 %. It's a neutral
  ramp, never an identity or alert colour.
- **Basis:** named in the share's tooltip ("22 % of AB production").
- **Sign:** none; a share is a size (a city is +5.2 % of AB demand).
- **When either side is an estimate:** it reads "· ≈4.2 ±0.8 %", with ± in percentage points. The relative bands combine
  in quadrature, √(a² + b²): the feature's `band` (or half its `estimate` lo–hi range) and the basis's `band` or the
  share's `of_band`.
- **No share at all** when either value is missing or stale, the basis is zero, or the two units differ.
- **A row's own share:** a `callouts_extra` row can carry `share` too, e.g. each fuel's "% of AB load" in the AESO load box
  (step 5).
- **Sign:** a share is the part as stored over the size of the whole. A city is +5.2 % of AB demand (stored positive, only
  displayed negative), but a net export or storage charging (stored negative) reads as a negative share, never a positive one.

**Zoom levels.** Zoomed all the way out the map shows only the overview: three region boxes (PRODUCTION,
AB DEMAND, STORAGE) plus the major export points (Empress, McNeill, AB-BC border, Willow Valley → CGL).
From zoom 6 the overview boxes hand off (`maxzoom: 6`) to the per-point and per-area boxes that carry the same
values, and more detail appears at 6.75 and 7.5. A feature's dot follows its box's tier, so minor dots don't
crowd the overview; alert pins show only `critical` ones there. A test (`test_overview_zoom_levels`) keeps the
overview to that set and checks every handoff has a breakdown box.

**Stability** (PLAN P4a; logic in `web/placement.js`, tested under node in `tests/test_web.py`):
- **Sticky slots.** A box keeps its side and leader length while you pan and zoom. It moves only on a hard
  collision: the screen edge, a panel, a region label, a box placed before it or, for tier 2-3 boxes, a node
  dot. When it must move it tries its old side at another leader length first.
- **Glide and fade.** A move glides over 200 ms (ease-out). A box that appears fades in at its slot, and one
  that disappears fades out where it is. `prefers-reduced-motion` turns both off.
- **Hysteresis.** Every show/hide decision (boxes, rows, node dots, alert pins) uses a *level zoom* with a 0.15
  dead band around each boundary (tier starts, row tiers, any `minzoom`/`maxzoom`). Zooming in, a level switches
  at boundary + 0.075; zooming out, at boundary - 0.075. A pinch that wobbles across a boundary doesn't flicker.
- **In-place handoff.** An overview box that hands off at `maxzoom` and the tier-0/1 box carrying the same value
  are partners (`map.js` pairs them). Crossing the boundary, the new box starts exactly where its partner was,
  the partner disappears, and the new box glides to the free slot nearest that spot. It works both ways.

Placement: tier first, then system and region boxes, then individual features. Boxes never overlap panels,
other boxes or region labels. They avoid node dots too, but a tier-0/1 box may cover a minor dot as a last
resort. The change chip follows the snapshot's `compare` (`prev` reads "d/d" for daily series, otherwise
`1h`, `24h` or `7d`). Stale values are dimmed and tagged STALE. Utilization uses `cap_current` (today's
capability) when the snapshot has it.

**Window means (`agg`, #151).** A feature property `agg: "1h"` or `"24h"`, or an `?extra=<series>@1h|@24h` row, adds
`agg: {window, start, end, mean, n, n_min, reason}` to that snapshot entry: the mean of the series' latest-vintage points
in `(now − window, now]`. Below the n floor (75 % of the points the series' `freq` implies, at least 2), `mean` is null and
`reason` says why. Show the reason, never a thin mean dressed as a window. A daily series, or one whose `freq` isn't finer
than the window, has no window mean. A series without `freq` in its spec is treated as hourly, so declare `freq`.
On the map (`web/callouts.js` `shown()` / `rowHtml()`):
- **A row asks for a window** with a `<series>@1h` series (Power's burn rows, #153) or `agg: "1h"` on the row.
  `extraIds()` sends that token, and the box reads the same key.
- **With a mean,** the box shows `agg.mean`, tagged with its window and n ("1 h mean · n 12"). It has no change chip,
  because the snapshot's compare is point-to-point, not window-to-window.
- **With `mean` null,** the box shows `--` and a grey **NO-RESULT** chip, with `agg.reason` on hover. It never falls
  back to the latest point `.v`.
- **The panel** shows the same value, plus a "Window" row (the window, n, its minimum and the time span, or NO-RESULT and
  the reason) and the latest point. Its history chart uses the bare series id.

## Node sprites

Nodes are pixel-art symbols from Art's atlas (`web/sprites/nodes*`, rules in `docs/art/NODES.md`, LOG L-028). The
logic lives in `web/sprites.js`:
- **Kind:** chosen by `type`, and plants by `fuel_primary` (Power's main fuel for compound plants), else `fuel`.
  A compound or unknown fuel with no `fuel_primary` shows `plant-unknown`; it's never guessed. `overrides.<id>.sprite`
  is only for exceptions the fuel can't express.
- **Glow:** thermal plants glow with output ÷ capacity: zero → `glow0`, up to ⅓ → `glow1`, up to ⅔ → `glow2`, above → `glow3`.
- **Badge:** the `unknown` badge marks stale data, and plants with no measured output or no capacity.
- **Size and visibility:** sprites are 16 px, and 32 px from level zoom 8. Like the dots before them, they appear at
  their callout tier (both through the hysteretic level zoom).
- **Crispness:** images are cut from the @1x atlas, scaled by a whole number with nearest-neighbour, and added at the
  map's own pixel ratio. MapLibre then draws them 1:1 with no resampling, at 100 %, 125 % and 150 % display scaling.
  A plain sprite URL would blur them at 125 % and 150 %.
- **Regions** keep their dashed ring. A click on a sprite opens the node, and the selection ring circles the sprite.

## Motion

Wind farms and compressor stations animate from data (docs/art/STYLE.md, LOG L-005):
- **Wind:** a producing farm's rotor turns at u = 0.25 + 0.75 × clamp(MW / capacity, 0, 1) cycles per second, which is
  1 frame/s at the lowest output up to 4 frames/s at full (PLAN 4b). Zero or negative output stops it. Pure utilisation
  left a typical low-wind farm (2-15 %) changing frame every few seconds, so it read as frozen.
- **Compressor:** the ticks run at the same normalized rate on |MMcf/d| / its flow reference, backwards for reverse flow.
- **Frozen states:** stale or missing data stays static and shows the `?` badge.
- **Gas-plant smoke (PLAN 4b, Art's `docs/art/SMOKE.md`):** a gas-fired plant shows a rising puff on its stack only when
  its output is fresh, measured and positive.
  - **Rate:** the wind's, 1 frame/s at the lowest output up to 4 frames/s at full.
  - **Variant:** the thinner "low" puff below a third of the plant's reference output.
  - **No puff at all** (not a frozen one) for zero, negative, stale, missing or estimated output (`estimate`/`band` on
    the feature), and for non-gas kinds.
  - **Gating:** off "now", under reduced motion or in a hidden tab, the smoke layers draw nothing.
  - **Drawing:** the puff draws below every data line and below the plant sprite, so a line is never covered and the
    puff rises from behind the stack (`SMOKE_OFFSET` in `web/sprites.js`, measured from the atlas).
- **When motion stops:** there's one shared 4 Hz clock (`ANIM_TICK_MS`, at most 4 frame updates/s per feature). It
  only runs while a moving sprite is on screen, the time slider is at "now", the tab is visible and the system isn't
  set to reduced motion. Otherwise everything is drawn static. On Windows, "Animation effects" off (Settings →
  Accessibility → Visual effects) makes the browser report reduced motion, which stops every animation on the map.
- **How it draws:** each tick changes only the paint of four frame layers (`nodes-anim-0..3`), never the layout.
  Logic is in `web/sprites.js` (`animSpeed`, `frameAt`), with the expression twin `animFrame` in `web/style.js`.

**Every producing plant kind (PLAN 10b, L-108; Art's frames in #538, `docs/art/NODES.md`).** Erik: "animations for all
powerplant sprites when they're turned on". One rule decides it, `sprites.js plantMotion(kind, v, util, stale,
estimated, off, tracked)`, which returns `{state, set, u, badge}`. `state` is the still image, `set` the timed set,
`u` the cycles/s, and `badge` the `?`.
- **Measured** (fresh, above zero, with a positive reference): it moves at the wind rate, 0.25 + 0.75 × utilisation
  (1–4 frame changes/s). Hydro, solar, nuclear, biomass, geothermal and unknown use `frame0..3`, which replace the
  still. Thermal kinds keep glow plus smoke. Zero is still. Missing or stale is frozen with the `?`.
- **Estimated:**
  - solar, wind and hydro move at the estimate's rate and keep "est.";
  - gas, coal, oil, biomass, geothermal and other keep a steady glow, with no frames and no smoke;
  - nuclear stays still (the US plants are measured since #537, from NRC's daily level).
- **Battery and pumped storage:** direction comes only from a measured signed value. Above 0 runs `discharge` /
  `generate`, below 0 runs `charge` / `pump`. The set already runs the right way, so the rate stays positive. On an
  estimate they stay still. Today every US battery and pumped-storage plant is an estimate, so none moves yet.
- **Capability 0** (PLAN 10c): still, glow0 for a glowing kind, and no badge.
- **No value source at all** (`tracked` false, e.g. 551 OpenStreetMap plants): still, with no `?`.
- **Drawing:** the frame layers concat a per-feature `spr_set` (`<kind>-<16|32>-<set><k>`). It's the same four
  layers and the same shared clock as before.

**Frame budget (PLAN 10b).** The frame and smoke layers hold only movers whose sprite is drawn at this level
(`style.js animFilter`, `zmin <= level`). The filter is set again only when that set changes. The clock counts only
those movers on screen. Measured at 1920x1080 on live data (headless Edge, CPU busy over 8 s):

| view | movers on screen | CPU with motion | CPU under reduced motion |
|---|---|---|---|
| floor (z 3.2) | 0 plants (only the fire flicker runs) | 4.7 % | 0.7 % |
| Texas (z 7.2) | 19 | 7.1 % | 0.8 % |
| California (z 7.2) | 3 | 8.3 % | 1.0 % |
| Alberta (z 7.6) | 14 | 5.0 % | 0.7 % |

The clock runs at 4 Hz: 32 ticks in 8 s, and none under reduced motion. Inventory plants are drawn from zoom 6.75
(tier 2), so motion starts where sprites are shown individually.

## Wildfire

CWFIS fire alerts (`fire_cwfis` events) are drawn with Art's wildfire sprites (`docs/art/WILDFIRE.md`); `fireFeatures` is
in `web/sprites.js`.
- **Active hotspot cell** (newest detection under 24 h old): a small flame at the cell's centroid.
- **Perimeter:** its real outline (dashed rose-red) and "fire · last <date>" from zoom 7, below the data lines. While CWFIS
  has updated it in the last 48 h, a large flame sits at its centroid. An older perimeter keeps only the outline and
  date: no stale or historical flames.
- **Motion:** a fixed flicker (2 frames/s) on the shared clock. It never encodes size, intensity or spread.
- **Off motion:** under reduced motion or a hidden tab, a still frame. Off "now", nothing, because a past view must not
  show today's fires.
- **Severity** stays on the event pins. A flame is red because it's a fire, not because the alert is critical. Click a
  flame for its event.

## Pipelines

Gas pipes carry pixel-art flanges every 64 screen px (`gas-flanges`, a symbol overlay; `docs/research/frontend.md`
"Pixel pipelines"). They appear only where flow is measured, fresh and non-zero. The flange height follows the pipe
width. The pipe's colour (utilization) and width (flow) are unchanged. Transmission and intertie lines carry small pixel joints every 96 px (`power-joints`), drawn under the lines so the
dashes, utilization colour and every crossing line stay on top.

## River gauges

WSC river gauges (`gauges` layer, toggle "River gauges") are drawn at their WSC station coordinates
(`web/gauges.geojson`, built by `docs/research/frontend-proto/build_gauges.py` from api.weather.gc.ca, Open Government
Licence – Canada). Each marker has a blue ring, and is coloured from `/api/snapshot` → `gauges` (`web/gauges.js`):
- **state ok:** filled by flow vs the 1995-2024 normal for the time of year. Very low (< p10) brown, low (< p25) khaki,
  normal (p25-75) pale, high (≤ p90) light blue, very high blue.
- **seasonal:** a hollow ring ("seasonal, no data"; not an error).
- **stale:** dark and dimmed (no real-time reading for 3 h).
- **no-result:** dark (data too thin for a percentile; the reason is in the panel).

Click a gauge for its panel: discharge (m³/s) with "· N % of normal", "percentile NN (band)" and n, or the state's reason, plus the series' history.

## River flow

The `river-flow` layer (toggle "River flow") draws particles moving down the Peace River and, from step 5, the AB hydro rivers
(Bow, Kananaskis, North Saskatchewan, Brazeau, Red Deer, Oldman, St. Mary) and the Columbia and its tributaries
(Illecillewaet, Kootenay, Duncan, Slocan), from Weather's reach specs (`docs/research/v2/weather-rivers-*.json`, built
with `build_rivers.py --spec`). For the step-5 rivers:
- a reach takes its flow from its upstream anchor: a WSC gauge, an AB Rivers outflow series, or nothing (a reservoir level
  or a dam with no live release, which gets no particles at all);
- a gauge moves on state `ok`, or `no-result` with a value;
- a seasonal gauge falls back to the release above where the spec says so, and is otherwise frozen and flagged.

For the Peace: The
centreline is `web/rivers.geojson`: © OpenStreetMap contributors (ODbL), from the OpenMapTiles `waterway` layer of the
OpenFreeMap tiles, built by `docs/research/frontend-proto/build_rivers.py`. It runs from W.A.C. Bennett Dam to the
Fort Vermilion gauge and is split into reaches at each dam and main-stem WSC gauge. Each reach takes its discharge from
its upstream end:
- **Dam reaches** (W.A.C. Bennett, Peace Canyon, Site C) use the dam's `power.hydro.<plant>.flow`: the **24 h mean
  outflow** (Weather's connector), so their speed changes slowly. They move only when that series is fresh and positive.
- **Gauge reaches** use the gauge's snapshot `value`. They move only when its state is `ok`.
- **Speed and density** are both ∝ √(Q / 1500 m³/s), capped at 2.5× (square root since step 5, so a 30 m³/s river still
  visibly moves and the bigger river still moves faster). The speed is in screen px/s, so it's the same at
  every zoom.
- **Frozen:** stale, no-result, seasonal, zero or missing discharge gives dim, still dots. Motion is never invented.
  Reduced motion, a hidden tab, or a time other than "now" also stop the motion.
- **Drawing:** particles are 2-device-px squares snapped to the pixel grid. They draw below every network layer, so
  data lines cross over them (L-028 G1). The layer repaints at most ~30 times a second, and only while a moving reach
  is in view.

## River flow vs normal

A box row showing a dam outflow (`abrivers.<stn>.outflow`) or a WSC discharge (`wsc.realtime.<stn>.discharge`) adds
"· 84 % of normal" to its value line (step 7, Erik). The value is Weather's sibling series `<series>_pct_normal` (#266):
today's 24 h mean ÷ the median for the same dates (±7 days), all years. It is tinted khaki below 75 % and blue above
125 %, the gauge colours for low and high. A gauge's panel shows the same figure after its discharge. With no such
series, or a stale one (too little history), nothing is shown, never 0.

**Gauge boxes (L-067).** map.json `gauge_boxes` gives every gauge in its `basins` (Columbia, Kootenay) a box at the gauge,
showing its discharge and % of normal: tier 3 (from z7.5), rank 3 so the dam boxes place first, Birchbank tier 2. A
click opens the gauge panel. The panel names the band position "percentile NN", so it isn't a second "normal" figure.

## Top producers popup

From the AB and BC production panels (L-075, Erik). map.json `producers` maps a series id to a province, and a panel
showing that series offers "Top producers in <province> ›" (`spec.action`). It opens an in-page modal (`web/producers.js`):
- **Map and list:** a small map of its own, fitted to the province's points, plus a list sortable by rank, name, operator
  or MMcf/d (a header click; again to reverse).
- **Detail:** a click on a point or a row shows "a bit of it": name, kind and operator, latest MMcf/d, rank, the
  optional fields as label/value, and a monthly history chart.
- **Data:** Gas's `GET /api/top-producers?province=ab|bc`, a GeoJSON FeatureCollection with `features[].properties
  {id, rank, name, operator, kind, latest, history {t, v}}`, plus `unit`, `as_of`, `basis` and `source` (shown under the
  list). With no features it shows the response's `reason`; with no endpoint, that it isn't available yet.
- **Points and layout:** each point is Art's sprite (#284, docs/art/WELLS.md): a pad for a facility, a well for a
  well, 16 px (32 px from zoom 7.5), bottom-centre and pixel-exact. A faint orange circle under it is sized by volume,
  and the top 10 are numbered. If the sprite sheet can't load, the circle is drawn solid. It closes with ✕, Esc or the backdrop. On a narrow screen it fills the screen, the map above the list, and
  the popup map's credits start folded to the ⓘ.

## Search

One box for everything with a name (L-076, Erik), in `web/search.js`. map.js hands it the items from what is loaded, so
new features are found without a list:
- **What's indexed:** every network feature with a `name` (grouped by system and `type`, e.g. "Gas border", "Power plant";
  also found by id, operator and fuel), the river gauges, the rivers, the map.json boxes, and the top producers (Gas's
  `/api/top-producers`, both provinces, fetched once on first use).
- **Matching:** prefix, then word prefix, then all words in any order, then substring, then the letters in order (fuzzy).
  Other keys count a little less. At most 20 results, grouped by type, each with a small icon (a system dot; Art's pad
  or well sprite for producers).
- **Picking** flies there and opens what a click would: a node's pinned box and panel, a gauge's panel, a box opened in
  place. A well or facility opens the top producers popup with it selected (its detail and history), zoomed to it.
- **Desktop:** an input in the top bar; ↑↓ to move, Enter to pick, Esc to clear or close.
- **Small screens:** a 🔍 beside ☰ opens a full-width bar with a 16 px input (no browser zoom) and 44 px result rows.
  The keyboard's Search key picks the first result, and ✕ or Back closes the bar.

## East of AB/BC (L-077, L-078)

- **Bounds and placement:** `view.maxBounds` reaches 75° W, past the TC Mainline's end at North Bay Junction. The opening
  view stays on AB/BC. Gas's TC boxes (Saskatchewan, Manitoba, NW Ontario areas; Prairies Line, Emerson and EDA export
  spots) are tier 1, from zoom 6, so the overview isn't crowded. Areas place first (rank 1). Power's SK/MB/ON
  generation boxes are tier 1 by their priority. Every box folds to its headline with its parts behind ▸.
- **Monthly series** (`freq` M, e.g. StatCan SK/MB generation, 2–3 months behind by design): the value line names the
  month, "Aug 2026 · monthly", and the change chip reads "m/m". The panel says "As of Aug 2026 (monthly mean)" and
  "monthly mean, published with a lag (not live)".
- **Daily fallback:** a live-only series with no value (IESO's hourly MW, until it runs) falls back to its daily sum
  when the feature's own panel lists `<series>.daily` (or `<x>.daily` for an `<x>.hourly` series). The row is labelled "· daily total" in its own unit, and its
  share basis falls back the same way. A click on the headline opens the feature's panel.
- **One unit per axis:** a panel with two units shows the sub-daily series on 7D and the daily/monthly ones on 30D and
  longer, and says so in a note. A second unit left over gets its own axis on the right.

## AB + BC tie-out check

(L-079, Erik.) A panel showing a map.json `tieouts` trigger series (the NGTL net storage box or node) adds the tie-out
view (`web/tieout.js`). Every term comes from config (Gas #296/#297). Each is read at the balance's gas day (the latest
day of `day_of`) from `/api/series`, so the bars add up even when a term has a newer day.
- **Waterfall:** AB + BC production − the demand off NGTL (carried remainders) − exports (pipelines, LNG) − AB demand
  on NGTL = left over.
- **Actual storage:** NGTL and Aitken Creek, each shown (+ = withdrawal), and their sum.
- **Tie-out error:** left over + actual storage. It ties out (✓) when the error is within its band. The error before the
  carried demand is shown for context, and is not a tie-out.
- **Preliminary days (Gas #328):** TC revises a GDSR gas day for up to ~47 h. A gas day younger than the tie-out's
  `preliminary_hours` (48) since its start (map.json `gas_day.start_hour_utc`) gets a "preliminary" chip in the
  waterfall and breakdown headers. The chart shades those days.
- **What the error is (L-080 item 4, Gas #313):** clicking that error opens `raw.breakdown`. Each part is shown at the
  same gas day, with its band (`band` ±absolute, `band_rel` × |value|, `band_series`, or `measured`) and what it
  assumes. A part's own `parts` are folded under it. The sum is shown too (the parts sum to the error), along with a
  monthly destination chart (`history`). Its series are fetched only on the first open.
- **Chart:** implied vs actual storage, with the error, over up to 30 days (filling daily from 2026-11-01).

## A plant's fuel (PLAN 10d)

Erik: "for the powerplants, make the fuel type obvious in this tab". A plant's panel names its fuel three times, before
any sentence: the header ("OIL PLANT · POWER.PLANT.EIA_540"), a chip with the plant's 16 px map sprite beside its name,
and a "Fuel" row at the top of the facts.
- **Words:** the region page's fuel words (montney/regions.py `FUELS`: Gas, Coal, …, Pumped storage, Other). A gas plant's
  technology follows in the row ("Gas · combined cycle", an AESO compound plant's "cogeneration + simple cycle").
- **Sources** (`sprites.js plantFuel`): the inventories' `fuel_group` first (EIA-860M, OpenStreetMap, Ontario). An AESO
  plant's `fuel_primary` or `fuel` maps to it (combined / simple cycle, cogeneration, gas-fired steam → Gas).
- **Unknown:** nothing known reads "fuel unknown" (header "POWER PLANT · FUEL UNKNOWN"), never a guess.

**Out of service, idle, and readings (PLAN 10c):**
- **Out of service:** a plant whose capability series reads exactly 0 and that isn't producing (IESO's Portlands and
  Pickering since 2026-11-07) says "Out of service (IESO capability 0)" under the headline (`sprites.js outOfService`).
  It has no capacity, capability or utilisation rows, and its headline reads "0", not "0.00". Its sprite is still
  (glow0 or static) with no `?` badge: known to be off, not unknown. Stale data says nothing.
- **0 MW with capacity above 0** depends on what the capacity series is (`capacity_words` `live`):
  - **Live availability** (IESO capability, published hour by hour): "Idle, available (IESO capability 466 MW)".
  - **A static rating** (AESO max capability, one value for months; NRC net summer capacity): "Not producing (0 of
    N MW AESO max capability)". NRC uses its own words (`zero`): "Not producing (0 % on NRC's daily report)". A static
    rating can't say the plant is available (Reviewer on #539: Palisades read 0 of 768.5 MW for 30 days, restarting).
- **Words:** each source's own word for its capacity series comes from map.json `capacity_words`, where the longest
  series-id prefix wins: IESO capability, AESO max capability, net summer capacity (NRC). Unmatched: "capacity", not
  live.
- **A feature `basis`** (Power #537: the 15 US nuclear plants' NRC daily power level) shows under the headline, where an
  estimate's sentence would. The headline stays plain (it's a reading, not an estimate), and Status says "a daily
  reading, not metered MW".

## Panel history

- **Empty range:** a panel chart with no points in the selected range (e.g. CER key points that end months ago) widens to
  1Y, and says so.
- **Dated series:** a series whose newest point is over a week old is labelled "(to <date>)", and a note calls it dated
  context, not today's flow.
- **No live value** (a CER-only link): the panel shows no "As of" or "live" status.

## Status dots

The header has one dot per enabled source (`web/status.js`, from `/api/status`):
- **Green:** the last fetch was ok.
- **Red circle:** the last fetch failed.
- **Amber:** the source's region is over 80 % of its storage budget (`budget: warn`).
- **Red square:** the budget is full and writes are stopped for that source (`budget: blocked`).
- **Precedence:** blocked > fetch error > warn > ok. Hover a dot for the details.

On Postgres the header also shows the runner's heartbeat (`runner`), but only when something is wrong: **RUNNER BUSY**
(amber, no heartbeat for over 60 s, data may lag) or **RUNNER DOWN** (red, over 5 min: nothing is fetched or stored). In
DuckDB mode there's no budget or runner, and the dots show fetch health only.

## Weather regions (L-080, Weather #303)

The weather field reads every region in `/api/grids`: the main `t2m`/`u10`/`v10`/`z`, plus any `t2m<suffix>` set, today
`_east` (0.5°, −95..−75).
- An extra region loads only once the view comes within 5° of it (the east grid isn't fetched for an AB/BC view), and
  stays loaded after that.
- Each region gets its own field layer (`field<suffix>`), terrain lapse (its own DEM mosaic) and time step: the
  regions' forecast steps differ, so each is set from the slider time with `stepAt` on its own times.
- The wind particles and the hover readout take the first region that covers a point.
- Neighbouring regions share their seam column and are drawn between cell centres, so they meet without overlap. The
  grid-edge fade is switched off on a shared edge (`u_seam`: west, east, south, north; `weather.js seamsOf`), so there's
  no dark band at −95° or at 44° N (L-090, the south grid). An edge counts as shared only where the two regions overlap
  along it, and only along that stretch (per segment, Weather #370): the south grid's east edge is shared with the
  east grid for 41–44° N and still fades below 41° N. Neighbours on one edge join into one stretch.

## Battery (L-080)

`web/power.js` has one power state, and every animation takes its pace from it:

| state | when | wind fps | river particles fps | sprite clock |
|---|---|---|---|---|
| active | touched in the last 30 s | 30 | 30 | 4 Hz |
| idle | 30 s without input | 15 | 15 | 1 Hz |
| battery saver | automatic on a phone-sized touch screen or on battery (Battery API); the Layers toggle overrides it | 10 | 10 | 1 Hz (spin still means output) |
| hidden | the tab is hidden | stopped | stopped | stopped |

- **Wind field:** motion and fade scale with the frame time, so it looks the same at any rate. It's drawn at a pixel
  ratio of at most 2 (1 in saver), and its particle count follows the screen area (a phone draws about a third of a
  desktop's, half that in saver). With the layer off it doesn't run at all.
- **Pacing:** below 60 fps a loop sleeps on a timer until its next frame, so it wakes once per frame it draws.
- **River repaints (L-102):** river particles sit on whole device pixels, so the next repaint waits at least until the
  fastest moving particle in view has crossed one device pixel (`rivers.js riverFrameMs`). At zoom 6–7.5 the visible
  rivers move about 4 px/s, so they repaint about 5 times a second instead of 15–30, with the same image.
- **Polling:** nothing while the tab is hidden; a missed refresh runs as soon as it's visible again.
- **Requests (L-081, Database #312):**
  - **ETag:** snapshot, status, events and network send their ETag back (`If-None-Match`); a 304 keeps what's drawn.
  - **Cadence:** the snapshot polls when the server's `X-Next-Update` says new data is due (15–300 s), never sooner than
    30 s, and twice as long in saver.
  - **One at a time:** one request per endpoint is in flight (`api(path, {key})`): the same path shares it, and a new
    scope aborts it.
  - **Status:** the runner badge comes with the snapshot, so `/api/status` (source dots) polls every 5 minutes. The
    header clock ticks on its own each minute.
  - **Saver cap:** saver stretches a wait to at most 5 minutes.
- **Snapshot scope (L-081 PR 2, Database #317):**
  - **Tier:** `/api/snapshot?z=<tier>`, where the tier is that of the level zoom `S.ze` (map.json `tiers`, the one table
    the server also reads).
  - **What it holds:** boxes shown at the tier come in full (`values`). Every other drawn feature comes as
    `lite: [v, stale, cap]`, which paints the same.
  - **Extras:** `extra=` asks only for the rows of boxes shown at that tier. Rivers, gauges and estimate ranges are
    always asked for.
  - **Tier change:** the map refetches at once and keeps what's drawn until the new scope arrives.
  - **Clicks:** a dot clicked open (its box), a panel, or a pinned box whose feature is only lite gets its full entry
    from `?features=<id>`.
- **No continuous repaints:** the style has no paint transitions and the map no symbol fade (`fadeDuration: 0`). Both
  turned each sprite-clock tick into hundreds of milliseconds of repaints, so the map re-rendered at 60 fps for good.
- **Playback:** the forecast-playback loop runs only while playing.

## Gas day

`"gas_day": {"start_hour_utc": 15}`: the NGTL gas day D starts 08:00 MST, which is a fixed 15:00 UTC
(TC bulletin 3374725064). The header shows `date(now_utc − 15 h)`.

## Time

Alberta times are formatted with `/api/status` → `tz.offset_minutes` (bundled tzdata), never the
browser's zone data. The time slider moves **weather only**. Network values are live, and when the
slider is more than 1.5 h from now the network dims, callouts get a LIVE tag, and the time bar says
"NETWORK: LIVE hh:mm".

## Phone

The map works on a phone (L-068, Erik's Pixel over Tailscale). This is a minimal port; box layout is unchanged.
- **Viewport:** the page never zooms (`user-scalable=no`), so pinch zooms the map.
- **Narrow (≤ 900 px) or short (≤ 500 px):** the layers list and legend fold behind a 44 px ☰ button and scroll if they
  don't fit (side by side in landscape). The panel is a bottom sheet in portrait, and sits below the button in landscape.
  The credits fold to the ⓘ at once.
- **Touch:** a tap takes features within 12 px, and close buttons are at least 40 px (28 px on a pinned box).

## Folded boxes and compact boxes

**Box header (L-072).** Every box has a bold header: its title head (Alberta demand), else its first row's label in
the same style. The header is also what folds an open box.

**Fold / expand, every screen (L-070, Erik).** A box with a breakdown starts folded. It shows its lead rows (its title,
its first row, and the first row after each `section` head) with their daily change chip, and "+N ▾" for the rows that
wait. A click on a folded box opens it in place. On an open box, its header folds it again: the frame, a head row, the
"▴" strip, or a headline row's label (the text shown on top when folded). A row's value, and every breakdown row, open
that row's panel. The open state is kept per box while panning and zooming. An open box places
first, may cover labels and dots, and scrolls on a small screen if taller than the screen. A merged box keeps each
`section`'s total when folded (Alberta demand: gas and power). A plain merged part (a facility under a total) folds
away. One code path: the rules read only rows, section heads, tier, rank and always_show, never a feature id.

**Exports box (L-070, L-071; Erik: "exports to be a single box when fully zoomed out").** The map.json callout
`c.exports` (anchor EXPORTS, near Empress) shows Gas's `export.pipeline.total_est`: gas leaving the AB / NE BC production
area by pipeline, without LNG, which keeps its own box. Its "· " parts sum to it on the same gas day: Empress, McNeill,
AB-BC, other NGTL borders (GDSR daily), Alliance, and T-South (Westcoast Station 2 South, scheduled). It folds to the total
with its daily change. At zoom 6 it hands off (`maxzoom: 6`) to the facility boxes (overrides `minzoom: 6`), which show
their live values. Huntingdon isn't part of it (L-071) and has its own box at every zoom (L-072). All config; no JS
knows these ids.

**Nested parts (L-073, Erik: "nested split out boxes").** A box's rows form a tree (`callouts.js` `rowTree`). Rows stay
one flat list in pre-order, each parent followed by its parts:
- **Depth:** a row's `depth` sets its level: 1 = a part of the headline, 2 = a part of the nearest depth-1 row above,
  and so on (Gas's contract). Without `depth`, a "· " label is one level below the plain row above it, and a row is never
  more than one level deeper than the row before.
- **Opening and closing:** an open box shows its depth-1 rows. A row with parts shows ▸ before its label; a click on
  its label opens them (▾) or closes them, which hides the whole subtree. A click on its number opens its graph. Leaf
  rows open their graph as before.
- **Persistence and zoom:** opened rows are kept per box while panning and zooming. A part the user opened shows
  whatever its row priority.
- **Sums and size:** every level's parts sum to their parent (Gas), and shares and ± show at every level. An open box
  is capped to the screen height and scrolls, with its ▴ kept in view.

**Compact (a small map, L-069).** Narrower than 600 px or shorter than 500 px (`callouts.js` `COMPACT`), boxes are also
narrower (labels truncate; the full text is in the tooltip and the panel) and hide an estimate's ± text while folded.
- **Budget:** at most one box per 50,000 px² of map (at least 3), in the usual tier and rank order; always_show, pinned
  and open boxes don't count. Features without a box stay as dots you can tap.
- **Opening view:** narrower than 1280 px, the opening zoom and the zoom-out limit drop by log2(1280 / width), so a phone
  opens on the extent a 1280 px screen sees. Wider screens are unchanged.
- The ⓘ credits stay above the bottom sheet and boxes.

## Developing without data

`http://127.0.0.1:8050/?dev=1` answers every `/api` call from `web/dev/` (fixtures plus a
synthetic GFS-shaped grid) and shows a DEV banner. Real mode never loads those files
(`tests/test_web.py` enforces it).

## PacNW and the Columbia (L-085)

- **Bounds:** south to 41.5° N (Malin, 42.0° N); the opening view is unchanged. The weather grids end at 44° N, so the
  field fades out below that.
- **Gas boxes (Gas #336):** Lower Mainland demand, Sumas, Jackson Prairie, Stanfield and Malin show from zoom 6 (tier 1).
  Fully zoomed out, Huntingdon's tier-0 box folds Sumas and the Lower Mainland under it.
- **Columbia hydro (Weather #334):** one summary box at The Dalles, at every zoom (tier 0, `always_show`). Its rows:
  - The Dalles flow;
  - US reservoir storage (% full, with the kaf total under it);
  - the Jan–Jul runoff forecast (% of average).
- **Per-row % of normal:** `normal: "<series>"` names a row's own % of normal, and `"self"` means the value already is
  one. `cue: {above, below}` words it as above / near / below normal. There's no bullish/bearish word.
- **Stale rows:** a stale % of normal shows only on a row that's stale itself.
- **Lead rows:** `lead: true` keeps a row in the folded box.

## The outermost level (L-087)

> **Superseded by L-106 8a:** the region boxes are the overview now (see *Region boxes* below). The outermost-level boxes are
> retired, and nothing in map.json uses `"outer"` any more. The `floorZoom` / `"outer"` machinery stays for future use.

Erik: "when max zoomed [out] ... combine Alberta demand, production, export, and storage. Then make Sask have demand
and Prairies, then make MB have demand and Emerson lateral and Manitoba into the NOL."

- **Where:** the last `view.outer_span` (0.6) zoom levels before the map can't zoom out further. That's maxBounds or
  minZoom, whichever stops it first on this screen (`placement.js floorZoom`), so a phone reaches it too. A phone opens
  there, since it opens fully zoomed out. Map.json writes `"outer"` where it means that zoom; map.js resolves it at
  load and again on resize.
- **Alberta:** `merge_into` / `merge_until: "outer"` (now also on map.json callouts, in `merge_order`) folds production,
  exports and storage into the Alberta demand box, which carries its gas and power sections. Its `outer_title`
  "Alberta" names it. Each section keeps its headline folded, and its breakdown opens underneath.
- **Saskatchewan and Manitoba:** `c.sk_outer` is SK demand plus the Prairies Line. `c.mb_outer` is MB demand, the
  Emerson lateral and Manitoba into the NOL (`tcm.area.mb.outflow_est`). They're shown only at this level, with every
  row `lead`.
- **BC:** Huntingdon folds under Kitimat, as "BC".
- **Waiting until you zoom in:** the TC overview boxes (`box_minzoom`: the box waits, the dot doesn't), Columbia hydro
  and Birchbank.
- **PacNW (L-086, Gas #341):** one box at Yakima, at tier 0 and at the outermost level. Imports is the box, titled
  "PacNW"; demand and storage (Bcf) merge into it as sections. The Columbia hydro box places first (rank 1, to the
  south-west), and PacNW goes north-east into the open interior, so the two don't crowd each other.
- **The Dalles in PacNW (L-089):** `c.pnw_dalles` is one map.json row, The Dalles flow with its % of normal and cue. It
  always merges into the PacNW box as a section, at every zoom. PacNW places first (rank −1), since it's a row taller.
- **BC demand at the outermost level (L-089, Gas #345):** `c.bc_demand_outer` (`demand.bc.implied_est`, BC demand off
  T-South, est. ±110) folds into the "BC" box below the outermost zoom and is hidden above it.

## California (L-090)

- **Bounds:** south to 32.5° N, the south weather grid's edge. The south grid (−140..−95 × 32.5..44) loads lazily like the
  east one, and meets the west grid at 44° N without a band (#352).
- **The box (Gas #351, Power #350):** one California box, at tier 0 and at the outermost level. Gas demand is the box,
  titled "California". Power (CAISO), gas storage and gas imports merge in as sections, in that order. The gas heads say
  PG&E + SoCalGas, since direct-connects and SW Gas aren't included. It places first (rank −2, to the north-east), since it's
  the tallest box near the map's southern edge.
- **Import points:** Topock, Ehrenberg, Kramer and Wheeler Ridge show from zoom 6 (tier 1).
- **Daily fallback:** it checks a feature's own value in the snapshot's `values` (it used to look in `series`), so a live
  hourly value, like CAISO's MW, no longer shows its daily MWh/d sum.

## Ontario and Québec (L-091)

- **Bounds:** east to −69.5°, past Québec City and the Saguenay. The east weather grid ends at −75°, so the field fades
  out beyond it until Weather widens that grid.
- **Ontario box (Gas #358):** gas demand is the box, titled "Ontario", at tier 0 and the outermost level. Two sections
  merge in: Imports / exports (`c.ont_flows`: Niagara in, Iroquois at Waddington out) and Power (the IESO box). IESO's
  hourly isn't live yet, so Power shows its daily total, labelled as such.
- **Québec box:** `gas.qc.demand`, titled "Québec", at tier 0 and the outermost level. Hydro-Québec's demand and mix
  (`power.region.qc`, Power #361) merge in as its Power section. HQ's generation runs ~3 h behind its demand.
- **Ontario plants (Power #361):** nuclear plants from zoom 6.75 and gas plants from 7.5, by their callout priority.
  Nuclear uses the static `plant-nuclear` sprite; gas plants use their fuel's sprite and glow.
- **Hydro summary nodes (Weather #362):** `type: hydro_summary` uses the static `plant-hydro` sprite. Each is a box
  at tier 0, not at the outermost level (like Columbia hydro): Manitoba (Lake Winnipeg) and Québec (inflow proxy). An
  override's `cue: {above, below}` words each "% of normal" row of the box above / near / below normal.
- **TC points:** Parkway, Niagara and Waddington show from zoom 6 (tier 1).
- **Nuclear plants:** `fuel: nuclear` uses Art's `plant-nuclear` sprite (#359), static.
- **`depth: 0` rows (Gas #354):** such a row is a headline of its own, not a part of the one above. It's a root in the row
  tree, its own parts nest under it, and it shows when the box is folded at any zoom. A row's priority zoom gate applies to
  parts only.

## BC exports and the Alberta order (L-092, Erik)

- **BC exports:** at moderate zoom (the overview, above the outermost level and below zoom 6), `c.bc_exports` is one box
  anchored at Huntingdon, with three rows: Huntingdon (scheduled), Sumas → NWP, and Lower Mainland demand. From zoom 6
  they split into their own boxes, the way the Exports box hands off. Huntingdon's own box waits for zoom 6
  (`box_minzoom`); its dot doesn't.
- **Section order:** a merged section with a negative `merge_order` goes above the target's own rows. The outermost
  Alberta box leads with production, then demand (gas, then power), exports and storage.

## The ☰ menu (L-097, Erik)

"A small setting with a hamburger button in the UI ... I just would like to monitor it"; "remove these dots for the
sources and instead make that a tab in the hamburger button".

- **The button:** a ☰ at the start of the header opens a compact panel (`web/sysmenu.js`). It closes with ✕, the button
  again, or Escape. There's one code path for every screen; on a phone the panel fills the width. The phone's layers toggle wears a
  stacked-layers icon, so the ☰ means only this menu.
- **Sources tab:** the header's old dot row. Each enabled source shows its `sourceDot` colour (fetch health raised by its
  budget state), name, age and title, worst first. Only a row's ↻ button POSTs `/api/refresh/<id>` to queue a fetch, and only once until the list
  redraws: each refresh is an upstream request, so a stray tap shouldn't cost one. Opening the tab
  asks for a fresh `/api/status` (ETag: a 304 is free). The header keeps the title, gas day, clock and runner badge.
- **System tab:** `GET /api/system` (orchestrator #384). It shows:
  - machine CPU and RAM bars;
  - map server, runner and Postgres memory and CPU (% of the whole machine), and their total against RAM;
  - storage per region against budget, and req/h per region against cap. Bars are amber from 80 %, red over. These two
    sections don't show on DuckDB.

  It polls every 10 s only while the tab is open and the page visible (CPU % is since the previous call), and stops when
  closed.

## Midwest (L-101)

- **One Midwest box at the overview and the outermost level (Erik: "When I'm mid zoom I don't see anything"):**
  `gas.midwest.chicago` is the box, titled "Midwest". Bakken supply and MISO Midwest load (forecast) merge in as
  sections below zoom 6 (`merge_until`), and split back to their own boxes, at North Dakota and Wisconsin, from zoom 6.
  The merged boxes are tier 0 too, so their rows are asked for at the overview. The box places first (rank −1).
- **Point boxes:** Dawn, Farwell, Wisconsin and Michigan show from zoom 6 (tier 1). Ventura, Marshfield and Muttonville
  stay at their priority tiers.

## Region boxes (L-106 8a, docs/REGIONS.md §5)

Fully zoomed out (tier 0, below zoom 6) the map shows the region boxes, one per region with data, and the region outlines. Nothing else is boxed there. From zoom 6 the region boxes hand off to each region's detail. The hand-built boxes (Alberta, PacNW, California, Ontario, Québec, Midwest, MB/QC hydro, the Columbia summary, Birchbank) are tier-1 boxes. The overview-only boxes are retired: the TC overview (`hide`), Exports, Storage, BC exports, and the SK/MB/BC outermost-level boxes. The rest of this section is one code path with no region ids in the JS.

- **Boxes.** `montney/regions.py` generates `network/region/boxes.geojson`, one `system: region` anchor per region.
  - Every line is a row in `callouts_extra`, with `section` (gas | power | oil), `depth`, `always_show`, and `share` on fuel rows.
  - Because the extras carry the anchor's own `value` line, the box draws no separate headline row.
  - The title row is the region name, and a click on it (›) opens the region panel.
- **Zoom hand-off: `systems`.** map.json `systems.<system>` is a default override for every feature of that system, and an `overrides` entry still wins. `systems.region.maxzoom: 6` makes the region boxes hand off at zoom 6. `overrides["region.wcan"].rank` places Erik's home region first.
- **Section heads: `sections`.** map.json `sections.<section>.head` is the head row ("Gas · MMcf/d", "Power · MW", "Oil · bbl/d").
  - A folded region box keeps its title, the section heads, and each section's `lead` lines (standard lines, REGIONS.md §4).
  - Its other depth-0 lines (storage, inventory, interchange) get `fold: true` and wait for a click.
  - A head marked `lead: true` stays when folded.
- **Thin regions.** A row with `always_show` shows while its series has any reading; a stale reading shows its STALE chip. A line with no data at all is left out, so a thin region's box stays short. A region with no lines gets no box, only its outline and a faint name at its anchor.
- **Share bar.** `share: "<series>"` on a row (a fuel's share of generation) draws its percentage as a 28 px bar plus the %.
- **Outlines and colours** (Erik: "regional borders around the regions ... give each region a colour"). `network/region/outlines.geojson` (`type: region_outline`) carries each region's `color` (`regions/<id>.yaml`, #452). Everything reads `["get", "color"]`; no hue is in the JS.
  - **Border:** a solid line in that colour, ~1.8 px (softer above zoom 6), on a thin dark casing so it reads on the temperature field. It sits under every data line and above the power joints, and areas don't style it.
  - **Tint:** a fill at 10 % (`REGION_TINT`). `applyField` drops it to 0 while a weather field is drawn, because it would shift the field's colours; the borders stay.
  - **Box:** the region box's left edge and title take the same colour (`--region`, from the box feature's `color`).
- **Region panel:** replaced by the region page (PLAN 9 part 3, below); `web/regionpanel.js` is gone.

## Region box placement (PLAN 9 part 1, Erik 2026-11-13)

Erik: "at any zoom level it should keep the boxes over their designated geographic region. Like the California box should be over California." Region boxes don't use the generic slot placement. `web/regionlayout.js` lays them out, a pure function with no imports, tested under node. `callouts.js place()` runs it first, and every other box keeps clear of the result.

- **Home:** a box is centred on its region's `anchor`, with no leader, whenever it fits.
- **Fit:** the box's form must be at most `FIT[form]` × the region's on-screen extent (the bbox of its outline's largest polygon) in each dimension. That's 1.0 for the full box, 1.2 for compact and 1.6 for a chip.
- **Forms:** the largest form that fits is used.
  - **full:** the box as configured.
  - **compact** (`data-form="compact"`): the title plus the two headline rows (map.js marks `compact` on the first lead line of the first two sections).
  - **chip:** the title only.
- **No overlaps:** while two centred boxes would overlap, the smaller region's box steps down a form (chip → outside), instead of drifting.
- **Outside:** a region too small for even a chip gets its box just beyond its edge.
  - It goes in the direction its region file names (`box.dir`: N … NW; E when unset). If that side is taken, it uses the nearest free side.
  - A short leader and dot in the region's colour point at the anchor. The box never overlaps another box, and the leader never crosses one.
  - With no room anywhere, the box hides; the outline stays.
- **A box never hides a smaller region's box** (PLAN 10a): at the new floor, Québec's full box covered New England's anchor,
  so no leader could start there. When a region gets no spot and a centred box covers its anchor, that box steps down a
  form and the layout runs again.
- **Hysteresis:** a form bigger than the last one must fit with 15 % to spare and keep 14 px from other boxes (6 px otherwise). Pinching never flips a form back and forth.
- **Pan invariance:** everything is in relative screen px at the current zoom and nothing reads the screen edges, so a pan never changes a form or a place. A box near the edge simply runs off it.
- **Colour:** the whole border and the title are in the region's `color`, as its outline is.
- **Clicks:** the title opens the region. A compact or chip box doesn't unfold in place, because it would no longer fit.

## The region box, v2 (PLAN 9 part 2)

Every region box has the same six rows, from the region feature's `box_rows` (montney/regions.py; docs/REGIONS.md §5). `map.js boxRowsV2` builds them, and today's rows remain only as a fallback when `box_rows` is absent.

- **Rows:**
  - **Gas · MMcf/d:** Production, Demand, Imports, Exports, Storage implied.
  - **Power · MW:** Generation.
  - Section heads come from map.json `sections`. Nothing folds.
- **Signs:** each row's `sign` is its display sign (sources +, sinks −, carried as the row's `flow`), so the four gas rows add up to Storage implied.
- **Missing values:** a row without a value shows "—", dimmed, with its `why` on hover ("no value yet" when there's none), never "--", a blank or a zero. `zero: true` shows 0.
- **Tags** (est., scope, seasonal…) stay quiet: small and muted after the label, and ellipsised in the narrow box (230 px max), with the full text on hover.
- **Compact form:** the first row of each section, Production and Generation.
- **Data:** the snapshot asks for the `box_rows` series.

## The region page (PLAN 9 part 3)

A click on a region box's title opens the region page (`web/regionpage.js`). It's built from one call, `GET /api/region/<id>` (montney/regions.py; docs/REGIONS.md §5b). It covers most of the screen on a desktop and the whole screen on a phone (Esc or ✕ closes it). No region is named in the JS.

- **Tabs:** Gas, Power and Oil. A tab with no lines or groups is disabled; the first one with content opens.
- **Balance:** the tab's lines (label, quiet tags, value, unit) beside a history chart (7D / 30D / 90D / 1Y).
  - Lines take the box's display sign for the same line (sources +, sinks −); a line the box doesn't carry is − when it's demand, load or exports.
  - A line without a value shows "—", dimmed, with its `why` on hover.
  - A monthly, quarterly or annual value names its period as its first tag ("Aug 2026", "Q3 2026", "2026"), so a months-old value never reads as current. This applies to lines and group rows.
  - The chart plots the tab's box lines. A click on any line or group row charts that series instead; ← goes back.
- **Detail groups:** cards in an auto-fill grid (min 300 px), so two groups still look finished and twenty stay readable.
  - Groups are free tables, not partitions: rows overlap, nest and mix units. A `%` appears only in an `of` group (`groupBasis`), in one of two modes:
    - **share:** every row is on the line's day or hour and has its sign, so each row is its part of the line.
    - **gross:** only for a group whose payload opts in with `gross` (the server sets it on `generation_by_fuel` alone, `"fuels' total"`; interchange by tie never goes gross). The card's header then says `· % of fuels' total`, because the % isn't of the line above. The rows share one timestamp among themselves (an hourly line is stamped an hour or two before its fuel rows), and rows of the line's sign carry at least 80 % of the absolute total. Those rows are parts of their own sum (gross generation); "of which" rows stay out of the sum. A row running the other way (a battery or pumped storage charging at midday) shows its value with no %, a bar the other way from the zero line, and a "charging" tag when it's battery or pumped storage.
    - Anything else is a free table: rows on different days among themselves, or signs mixed with no dominant side (interchange by tie).
  - `rest` shows as "Other / not itemised" only in share mode, and only when it's at least 0.5 % of the line.
  - With no `of`, there's no % and no "Other". The bar is drawn lighter, against the group's largest row in the group's unit, and a row in another unit gets none. When a free table's rows have both signs, the bars grow from a zero line in the middle: negatives left, positives right.
  - Static facts (`value`, `as_of`, `source`) are plain rows without a bar.
  - A tab with lines but no groups yet says so in one line.
- **Display rules from the live pass (PLAN 9g):**
  - **Labels:** group-row labels wrap and are never cut with "…".
  - **Group notes:** a group's `note` shows under its title, clamped to two lines; a click unfolds it.
  - **"Of which" rows:** indented.
  - **Static facts:** the value stays on the row, and the date and source wrap under the label.
  - **Weekly rows:** they name their week by its last day ("wk to Nov 6"; the EIA weeklies stamp the week-ending Friday).
  - **Balance caveats:** the balance says "Finished gas days, through yesterday" when `through` is yesterday, and "published ~N behind" when `lag_s` is 6 h or more.
  - **Empty tabs:** a tab with no group and no line that has a value says plainly that there's nothing for it yet.
  - **Groups with off-day rows:** a group whose rows are for another day than its `of` line is drawn as a free table.
  - **Map box:** a row showing "—" or a known zero gets no STALE chip; a known zero reads "0".
- **Weather:** a degree-day table (HDD and CDD: today, normal, realized, days 1–7, days 8–15), matched by series name, plus each city's mean temperature.
- **Watch list:** the region's `watch` items.

## Late rows (L-106, #428/#433)

Some lines are late by design but not stale. For example, AB · BC · SK power waits for BC Hydro's weekday load; the region's `lag:` keeps the stale flag off. For every callout row, and for the region panel's fuel mix, `lib.js lateAge` applies one rule:
- **When it applies:** the newest point is older than 6 h for a sub-daily series, 2 days for a daily one, or 10 days for a weekly one.
- **What changes:** the value is muted and says its age after the unit ("· 3 d"), with the newest point's time on hover.
- **What it skips:** stale rows (their STALE chip says it), monthly rows (they name their month) and windowed rows.
