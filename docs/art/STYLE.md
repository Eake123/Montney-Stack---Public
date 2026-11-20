# Montney pixel art — approved style

**Approved by Erik, 2026-11-01 · L-028** ([ruling, PR #33](https://github.com/Eake123/Montney-Stack/pull/33)).
[Static sample](style-sample.png): four studies, 16/32 px native and enlarged. Pixel art is the
house style across map features, including pipelines; basemap terrain/water/labels stay unchanged.

## Palette and form

Only these exact opaque colours from [`web/style.js`](../../web/style.js), plus transparent
pixels (alpha 0; no partial alpha). The house palette has eleven colours: the original
style sample shows ten, including legend swatches; the node atlas adds storage violet.

| Hex | Role |
|---|---|
| `#16201b` | Map ground; sample background only |
| `#020504` | One-native-pixel outline, internal gaps |
| `#7f948a` | Shadow faces |
| `#9fb3a9` | Structural midtone; unknown-data mark |
| `#d3ece0` | Upper/left highlights; labels |
| `#b7c98a` | Gas-fired plant identity (existing plant colour) |
| `#3dffb8` | Receipt identity only |
| `#c9a2ff` | Intertie identity only |
| `#b392f0` | Storage identity only |
| `#ffb547` | Warning badge only |
| `#ff4d6d` | Critical badge; wildfire flame identity exception below |

**Step 4c wildfire request (Erik):** rose-red `#ff4d6d` may also identify the animated wildfire
glyph, with a pale core and flame silhouette. It does not assign critical alert severity; keep
severity badges separate. Amber/gas-border orange remain excluded from flame art (WILDFIRE.md).

Front/three-quarter view; **upper-left light / 315°**, matching hillshade. Stepped diagonals,
no smoothing, blur, gradients or decorative smoke. **L-058 exception:** gas-plant activity puffs
may encode real fresh positive output; missing/stale/zero output means no plume (see SMOKE.md).
**L-106 / Regions 8b:** the same activity puff may serve coal and oil (and generic gas),
with the same fresh measured positive-output gate; BA estimates get no smoke. Battery uses
signed charge/discharge states. **L-108 (PLAN 10b) supersedes static-only restrictions:**
every plant kind has output-driven motion, including solar glints and hydro discharge.
The legacy stills remain the off/frozen look. See NODES.md's L-108 table for all sets.
One-pixel dark outlines at both sizes;
light tops/left edges. Simplify at 16×16; detail separately at 32×32. **16 px only:** add a 1 px
`#7f948a` halo outside the dark outline, using the former transparent margin; keep the body
and canvas size unchanged. **32 px:** retain one transparent pixel around the footprint, no halo.
Use a bottom-centre anchor across frames/sizes. @2x exports double every pixel with nearest-neighbour.

## Silhouettes and data

| Study | Recognition | Future animation meaning |
|---|---|---|
| Gas-fired power plant | Turbine hall, twin stacks, sage face | Steady neutral window glow ∝ net MW; never gas flow |
| Coal/oil/gas plant | Fuel-specific hall and stack | Existing rising puff on measured output; steady glow stays independent |
| Wind farm | Staggered towers, three-blade rotors | Blade cycle speed ∝ net MW; not physical RPM/wind speed |
| Hydro / pumped storage | Dam / upper and lower basins | Falling spillway streaks / signed penstock flow |
| Solar | Panel cell grid | A travelling panel glint follows output utilisation |
| Nuclear / unknown | Reactor domes / generic hall | Machinery highlight travels through the hall |
| Biomass / geothermal | Log feed hall / vent | Feed-conveyor teeth / rising vapour |
| Battery | Container cells and signed arrow | Down/in charge, up/out discharge; measured signed MW only |
| Compressor station | Low housing, round compressor, pipes | Pipe ticks follow measured MMcf/d magnitude and signed direction |
| LNG ship | Bow, deckhouse, rounded cargo tanks | Position/heading from timestamped AIS; movement only between valid positions |

For plant/wind, `u = clamp(max(MW, 0) / reference_MW, 0, 1)` with a documented positive
nameplate reference per feature. L-108 uses the existing wind rate, `0.25 + 0.75*u`
cycles/second for eligible nonzero output (1–4 frame changes/s); zero stops motion. Plant glow:
`#020504` off at zero → `#7f948a` → `#9fb3a9` → `#d3ece0` at thirds of the range;
never jade, steady, no pulse. Callouts retain
exact MW and over-range readings. Compressor ticks use the same normalized rule with a
flow reference; missing flow/reference means static. Ships never bob or emit decorative wakes.

Missing/stale inputs freeze motion and replace activity accents with midtone plus static `?`;
measured zero is unlit without `?`. Source metadata defines freshness. Label estimates/lagged
values in callouts. L-108 permits labelled estimated solar/wind/hydro motion only; estimated
dispatched fuels stay at steady glow, signed storage direction requires measured power.
Alerts use shaped badges and text separately
from identity colours.

## Budget and handoff

**Ceiling:** four frames per sequence/type/size, one static base; signed storage has one
four-frame sequence per direction (only one active at a time). Glow is a steady palette
state. Maximum four frame updates/second per visible feature, shared clock, no per-feature
timers. Off-screen, historical and reduced-motion views stay static with numeric values.
AIS follows observation timestamps. P4c measures FPS before/after at identical view/zoom/count
and asks Erik to check his GPU; no performance claim here.

MapLibre symbol layers only; existing particles for rivers; no three.js. Today's `plant` + `fuel`
can select gas-fired art; wind/compressor/ship studies are future types, never inferred from
receipt/border/LNG-terminal nodes. Next: P4b delivers transparent 16/32 px + @2x
atlas/index; P4c owns selection, binding and measured frame cost. Existing callout tiers stay intact.
