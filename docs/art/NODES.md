# Node atlas handoff — P4b / L-028

[Contact sheet](nodes-sample.png) shows 33 silhouettes at native 16/32 px and integer enlargement,
then plant glow, wind and compressor state strips at native16/native32/4x16. Art only: Frontend
integrates symbol layers and supplies real-map readability, before/after FPS and Erik's GPU check.

## Files and loading

`web/sprites/nodes.png` + `nodes.json` are one MapLibre sprite atlas with both native sizes.
`nodes@2x.png` + `nodes@2x.json` contain the **same keys**, exact doubled pixels/rectangles,
and `pixelRatio: 2`. `nodes-manifest.json` maps kind → anchor/states/sizes → image keys; it is
not a MapLibre sprite index. Both atlas densities are transparent RGBA with binary alpha,
two clear gutter pixels around each rectangle, and only the colours listed below.

Frontend may set the style's sprite URL to the absolute URL ending `/static/sprites/nodes`
(no suffix); MapLibre chooses `.json`/`.png` or `@2x`. This requires a Frontend change and
is not wired by this PR. Image key example: `plant-combined-cycle-16-glow2`; use `icon-size: 1`
at either density to display 16 logical pixels. Choose the 32 key explicitly for 32 logical px.
Use manifest `anchor` (`bottom` for structures, `center` for hub/region/system/badges).
Keep the canvas/anchor fixed across frames. Tier visibility, overlap, event offset, selection,
historical dimming, symbol filtering and nearest-neighbour rendering belong to Frontend.

## Kind selection (explicit mapping, never inferred geography)

| Network input | Atlas kind |
|---|---|
| `type: plant`, fuel `combined_cycle` | `plant-combined-cycle` |
| fuel `simple_cycle` | `plant-simple-cycle` |
| fuel `cogeneration` / `cogen` | `plant-cogen` |
| fuel `gas_fired_steam` | `plant-gas-fired-steam` |
| Explicit coal-to-gas conversion metadata | `plant-coal-to-gas` |
| fuel `hydro` / `wind` / `solar` | `plant-hydro` / `plant-wind` / `plant-solar` |
| fuel `nuclear` (L-091; Frontend wires selection) | `plant-nuclear` |
| Inventory fuel `gas` without a documented subtype | `plant-gas` (do not guess CC/SC) |
| fuel `coal` / `oil` / `biomass` / `geothermal` | `plant-coal` / `plant-oil` / `plant-biomass` / `plant-geothermal` |
| fuel `battery` / `pumped_storage` | `plant-battery` / `plant-pumped-storage` |
| Explicit `other` fuel | Existing `plant-unknown` |
| Missing, unrecognized or compound fuel | `plant-unknown` until a feature override selects its known type |
| `receipt`, `hub`, `storage`, `border`, `compressor` | Same kind name |
| `interconnect`, `intertie` | Same kind name (distinct neutral / purple arrow) |
| `lng` (current gas node = terminal) / explicit terminal | `lng-terminal` |
| Explicit AIS ship entity | `lng-ship` (never substitute for a terminal) |
| Gas processing facility / meter station | `gas-processing` / `meter` |
| `region` / `system` | `region` / `system` |
| Warning / critical event | `warn` / `critical` as separate badges |
| Missing/stale data | `unknown` badge alongside the appropriate static/off type |

Normalize fuel spaces/hyphens to underscores only for the single-fuel aliases above. Current
compound strings `combined_cycle + gas-fired steam` and `cogeneration + simple_cycle` need a
documented feature override or `plant-unknown`; do not silently imply one fuel. Shapes are
schematic categories, not literal depictions of the named facilities. No new network data is added.

## Identity colours and outline

All opaque colours are from the approved [house palette](STYLE.md), originally drawn from
`web/style.js` (some old dot identity literals have since been removed there).
`#020504` outlines/off, `#7f948a` shadow/halo,
`#9fb3a9` structure, `#d3ece0` highlight; `#b7c98a` thermal plant face; `#3dffb8` receipts only;
`#c9a2ff` interties only; **additional identity `#b392f0` storage only**. `#ffb547` warning triangle,
`#ff4d6d` critical octagon only. Border gates are neutral to keep the badge colours unambiguous.
The review board background `#16201b` is not baked into the atlas.

At16, validate the **body bbox inside [1,15)** before adding a one-pixel four-neighbour
(plus-shaped) `#7f948a` halo. It spends the margin without resizing/recolouring body pixels.
The wind rotors are spaced diagonally; no diagonal dilation joins their blades into a square.
At32 there is no halo and at least one transparent pixel surrounds each body. Native32 outlines
remain one pixel, with more detailed surfaces; @2x doubles every native pixel exactly.

## Data states and frame selection

**L-091 nuclear:** `plant-nuclear-16-static` and `plant-nuclear-32-static`, `anchor: bottom`.
Paired reactor domes and a turbine hall are a schematic category shared by Bruce, Darlington
and Pickering, not facility-specific architecture. Only outline/shadow/structure/highlight neutrals;
no sage gas identity. L-108 adds turbine-hall motion below. The [before/after art sheet](nuclear-before-after.png)
compares today's unknown-fuel fallback with the new silhouette. Frontend owns `fuel: nuclear`
selection in `web/sprites.js` and actual-map proof; this PR adds no network/data/runtime wiring.
The kind is appended to the atlas, preserving all 96 previous rectangles, keys and sprite pixels.

The five gas-fired/conversion kinds plus generic gas, coal and oil have `glow0..3`
(four steady palette states, no timed glow loop): measured zero → `glow0` (`#020504`),
positive normalized output up to 1/3 → `glow1` (`#7f948a`), up to 2/3 → `glow2` (`#9fb3a9`),
above 2/3 → `glow3` (`#d3ece0`). Clamp at nameplate; retain exact/over-range MW in callouts.
Reference capacity must be explicit and positive. Never use jade for plant output.

Wind/compressor have `static` plus **four distinct** `frame0..3` images per native size.
Wind phase advances at `clamp(max(MW,0)/reference_MW,0,1)` visual cycles/second; each cycle is
120° for three equal blades, not physical RPM. Compressor ticks advance at normalized measured
|MMcf/d|, reverse frame order for signed reverse flow. Max four frame changes/second, shared
clock, visible current features only. Zero flow/output → static, never a fake minimum speed.

Missing/stale/invalid reference → frozen static/off plus `unknown` badge, neutral activity,
numeric label explaining why. Source metadata supplies freshness. Historical/reduced-motion
views freeze motion while retaining values. L-108 below defines which estimates may move;
the earlier static-only restrictions on power plants are superseded. AIS ship position/heading
follows real timestamped fixes, with no bobbing/wake animation.

## Regions 8b / L-106 fuel additions

[All plant kinds side by side](plants-sample.png): native16/native32/3x32, plus the new glow
and battery state strips. The seven kinds are appended; all 98 previous keys, rectangles,
sprite pixels at both densities and 26 previous manifest entries are preserved.
All use `anchor: bottom`, native 16/32 and exact @2x. No new colours: the new glyphs use
`#020504`, `#7f948a`, `#9fb3a9`, `#d3ece0`; generic gas alone also uses gas sage `#b7c98a`.
Receipt jade, intertie purple, storage violet, amber and red remain reserved.

| Kind | Silhouette | States per size | Motion / output |
|---|---|---|---|
| `plant-coal` | Tall stack, angular coal pile, boiler house | `glow0..3` | Utilisation glow; shared four-frame smoke overlay |
| `plant-oil` | Horizontal tank, narrow stack, small hall | `glow0..3` | Utilisation glow; shared four-frame smoke overlay |
| `plant-gas` | Single stack, pitched sage turbine hall | `glow0..3` | Utilisation glow; shared four-frame smoke overlay |
| `plant-battery` | Two rows of container cells, direction marker | Legacy `static`, `charge`, `discharge`; L-108 adds signed frames below | Charge down/in, discharge up/out |
| `plant-biomass` | Sawtooth fuel hall, round log ends | `static`, `frame0..3`, `glow0..3` | Fuel feed moves only on measured output; estimates glow |
| `plant-geothermal` | Ground well/vent with steam silhouette | `static`, `frame0..3`, `glow0..3` | Vapour moves only on measured output; estimates glow |
| `plant-pumped-storage` | Upper/lower basins linked by penstock | `static`, `pump0..3`, `generate0..3` | Direction only from measured signed output |

Image keys follow `{kind}-{16|32}-{state}`. `FUELS` from REGIONS.md is fully covered:
gas/coal/nuclear/hydro/pumped_storage/wind/solar/battery/oil/biomass/geothermal, with `other`
using the existing unknown-fuel plant. Existing subtype kinds stay available for explicit metadata.
Frontend owns all mapping/clock/state selection changes; `web/sprites.js` is untouched here.

**Battery proposal:** positive **net generation/export** MW → `discharge` (up/out arrow);
negative → `charge` (down/in arrow); measured zero → `static` (neutral rest mark).
Normalize the source sign in the connector/feature contract first. Missing/stale/unsigned power,
capacity-only inventory or a BA-level fuel estimate cannot determine an individual battery's
direction: use `static` plus the existing unknown indication where appropriate. These states
never indicate state of charge, available MWh or charge efficiency. Under L-108 a valid measured
signed value selects the corresponding four-frame sequence; its rate follows utilisation.
Retain the three legacy still states for frozen/reduced-motion views, with numbers/sign/units.

**Glow:** the new coal/oil/gas kinds use the existing L-028 utilisation thirds and exact neutral
glow steps above. Regions §8 permits explicitly labelled **BA fuel-level utilisation estimates**;
those can choose a steady glow step with `est.` and the source period visible. They must not
be presented as measured plant MW. Capacity alone/absent reference is unknown, not zero.

**Smoke:** reuse `web/sprites/smoke.png/json` + `smoke@2x.png/json`, unchanged;
`smoke/{low,normal}-{16|32}-{0..3}`. One four-phase overlay, no coal/oil node-frame duplication.
Extend existing fresh-positive-output smoke eligibility to coal/oil/generic gas as an activity
symbol, not measured emissions. Missing/stale/zero/negative/historical/estimated values hide
smoke entirely; BA estimates may drive steady glow, never these puffs. Use the existing
low-output threshold (one third), shared clock and maximum four updates/second. Reduced
motion/offscreen hides it. L-108 adds measured geothermal motion inside the node atlas;
do not attach this gas-stack puff to a geothermal well.

| Kind | Native16 stack mouth `(x,y)` | Native32 stack mouth | Smoke canvas offset from bottom anchor, 16 / 32 |
|---|---|---|---|
| coal | `(11,2)` | `(22,4)` | `[3,-12]` / `[6,-24]` |
| oil | `(12,2)` | `(24,4)` | `[4,-12]` / `[8,-24]` |
| generic gas | `(4,2)` | `(8,4)` | `[-4,-12]` / `[-8,-24]` |

Offsets account for the puff outlet being 2/4 native pixels above its bottom anchor (SMOKE.md).
Frontend scales them with existing device-exact geometry and proves actual-map placement,
freshness/state binding, DPR crispness and frame cost. This sheet proves the assets only.

## PLAN 10b / L-108: every producing kind moves

**Contract confirmed by Frontend, 2026-11-14:** append full-node frames to the existing
nodes atlas; no new runtime atlas/layers. All 134 old images, coordinates and keys at both
densities remain identical. Existing states/sizes maps keep their references; new states
extend them. The only metadata changes are appended states/size entries. Every plant
anchor remains `bottom`, with fixed 16/32 canvases and exact nearest-neighbour @2x.
The atlas grows to 512×504 (1024×1008 @2x), 238 indexed images, 33 kinds.

[All phases](plant-motion-sheet.png), review pages [1](plant-motion-1.png),
[2](plant-motion-2.png), [3](plant-motion-3.png), and [animated art preview](plant-motion.gif).
Each sheet cell shows native16, native32, 3x16 and 3x32. The GIF runs at 4 Hz as an art study,
not evidence of live-map placement/FPS. Thermal composites show the unchanged puff on glow2;
at16 it rises over the stack distinctly, so **no additional thermal node frames are needed**.
One smoke overlay works with every steady glow level; zero/unknown output never shows a puff.

| Kind(s) | Timed set / four images | What moves |
|---|---|---|
| combined-cycle, simple-cycle, cogen, gas-fired-steam, coal-to-gas, gas, coal, oil | Existing `smoke/{normal,low}-{16|32}-{0..3}` over existing `glow0..3` | Rising stack puff; no glow/frame multiplication |
| `plant-wind` | Existing `frame0..3` | Rotor blades (unchanged) |
| `plant-hydro` | New `frame0..3` | Streaks descend spillway channels |
| `plant-solar` | New `frame0..3` | Glint crosses cell columns on the panel |
| `plant-nuclear` | New `frame0..3` | Turbine-hall machinery highlights, not steam from a reactor dome |
| `plant-biomass` | New `frame0..3` | Feed-conveyor teeth travel above stored logs |
| `plant-geothermal` | New `frame0..3` | Vapour rises from the well vent |
| `plant-unknown` | New `frame0..3` | Machinery highlights in the generic hall; no invented fuel |
| `plant-battery` | New `charge0..3` / `discharge0..3` | Charge travels down into cells / discharge travels up out; signed arrow remains visible |
| `plant-pumped-storage` | New `pump0..3` / `generate0..3` | Water slug climbs / descends penstock; signed arrow distinguishes directions even in a still |

New keys are exactly `{kind}-{16|32}-{set}{phase}`; phases **0 → 1 → 2 → 3 → 0**.
Signed sets already encode their direction: **do not reverse phase order again** for a negative
value. Positive net generation/export MW selects battery `discharge` or pumped-storage
`generate`; negative selects `charge` or `pump`. Source sign must be normalized first.
Both sizes list every new state in manifest `states` and `sizes.16` / `sizes.32`.
No offsets for full-node sets: replace the still node image on the same bottom anchor.
Thermal puff offsets remain the existing SMOKE_OFFSET values in sprites.js and the coal/oil/gas
table above; the review builder uses those actual native mouth coordinates. Smoke assets are unchanged.

Biomass, geothermal and unknown additionally gain **steady** `glow0..3`, using the exact
L-028 neutral ramp. They are for estimated dispatched plants, not a second timed loop.
Frames are the measured-on look, replacing the entire still/glow node; do not overlay them
on glow. Existing nuclear/hydro/solar/static and signed battery stills remain available.

**Eligibility (L-108, supersedes prior static-only/estimate statements):**
- Fresh measured positive output may animate every plant kind. Battery/pumped storage
  also animate on negative **measured signed** power in their pumping/charging sets.
- Estimated solar, wind and hydro may animate at the estimate's rate, with `est.` and the
  source period retained. Estimated gas/coal/oil/biomass/geothermal/other stay at steady glow.
  Nuclear moves only when its input is classified measured; daily NRC-derived output must
  keep its basis visible and must not be described as metered MW. Power/Frontend own that classification.
- Measured zero or known capability 0: legacy `static`, or `glow0` for kinds that glow;
  no motion, no puff. Capability 0 is known out of service, not missing data.
- Missing/stale value or invalid utilisation reference: still/static/off plus existing `?`.
  No value does not mean zero; capacity alone never implies activity or signed direction.

Use the existing wind rate: for a valid nonzero eligible reading,
`cycles/s = 0.25 + 0.75 * clamp(abs(MW)/positive_reference_MW, 0, 1)`; this is
1–4 frame changes/s. An eligible estimated solar/wind/hydro uses its explicit utilisation.
At zero/ineligible/invalid: rate 0. One shared 250 ms clock, max four phase changes/s,
no per-feature timers. Freeze offscreen/hidden, in history, or under reduced motion;
freeze with a still base rather than an active puff. Motion denotes output/activity, never
measured emissions, photon speed, fluid velocity, turbine RPM or battery state of charge.
Frontend owns real-map animation, scaling, signed-data binding and FPS proof after this PR.

## Reproduction and checks

Run `uv run --with "pillow>=10.1,<14" python docs/art/build_nodes.py`, then
`uv run --with "pillow>=10.1,<14" python docs/art/verify_nodes.py --seeded-reds`.
Then `uv run --with "pillow>=10.1,<14" python docs/art/build_plant_motion.py` for sheets/GIF.
The latter independently checks shipped rectangles, gutters, both densities, required kinds,
palette, alpha and four distinct frames. Its seeded corruptions stay in memory. The builder
checks native body margins before halo and validates all images before writing outputs.
Pillow is an optional authoring tool; no app dependency or build step is added.
