# Gas well / grouped producer markers — L-075

[Contact sheet](wells-sample.png) shows a Christmas-tree gas wellhead and a grouped pad/facility
variant, with native16/32 images, review enlargement and a DPR2.6 device-pixel study.
The wellhead has three valve handwheels and a flanged vertical tree; it is not an oil pumpjack.
The group has a wider pad and a neutral three-dot marker. Those dots mean aggregation, not a
literal count of three wells. Use the group only when the source entity is actually a facility/pad
group; do not turn a facility production number into an individual-well claim.

## Usage / files for Frontend

`web/sprites/wells.png` + `wells.json`, and `wells@2x.png` + `wells@2x.json`, expose:
- `gas-well-16-static`, `gas-well-32-static`
- `gas-pad-16-static`, `gas-pad-32-static`

MapLibre index rectangles are native16/32 at `pixelRatio:1`; @2x has the same keys, doubled
rectangles/pixels and `pixelRatio:2`. All use bottom-centre anchors and fixed square canvases.
The two variants fit the existing node image naming convention. Keep existing sprites intact;
load this small atlas alongside them. Only neutral house colours `#020504`, `#7f948a`,
`#9fb3a9`, `#d3ece0`; one-native-pixel dark outlines, native16 halo, no smoothing/partial alpha.

For map markers use existing `loadSprites` **device-exact/default fit**, `icon-size:1`, not a
fractional icon-size or CSS-fit line-art path. At DPR2.6 the existing loader chooses `n=3`,
upscales each source pixel to3×3 device pixels with nearest-neighbour, and registers
`pixelRatio:2.6`. A native16 image is48 device pixels /18.4615 CSS px; native32 is96 device
pixels /36.9231 CSS px. Size rings/offsets/keep-outs with existing `spriteCss`, not a fixed16 CSS px.
Exact16 CSS px at DPR2.6 would be41.6 device px and cannot preserve an integer pixel grid.
For HTML popup rows, retain the same integer-device scale or use explicit native images with
pixelated rendering; Frontend owns the real phone/browser check. The sheet is not browser proof.

These sprites are deliberately **static**: monthly production rankings or estimates must not
acquire an invented live flare/shimmer. Ranking labels, AB/BC source period, units, estimates,
entity links and any map/popup selection remain with Gas/Frontend. The art adds no well data.

Reproduce: `uv run --with "pillow>=10.1,<14" python docs/art/build_wells.py`.
Only the existing palette/alpha/exact @2x checks are run. No new suite, data fetch, server,
runtime wiring, infrastructure or dependency change is included.
