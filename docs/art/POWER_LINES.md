# Power and intertie line art — roadmap step 3, round 2

[Contact sheet](power-lines-sample.png) shows all 12 symbols at native 16/32 px and integer
enlargement. [Raw real-map study at zoom 5.15](power-lines-map-z5.15.png) shows the temporary
overlay on nine existing power/intertie links, at 1280×800 / DPR 1. No production wiring is included.

## Deliverables and palette

`web/sprites/power-lines.png` / `power-lines.json`, plus exact `@2x` PNG/index, supply six kinds
at 16 px and32: `line/{power,intertie}-{tower,joint,casing}-{16,32}`. Standard MapLibre rectangles,
`pixelRatio: 1` at native and 2 in the doubled index; the same image keys at both densities.
They can be loaded/merged alongside nodes and gas flanges, without replacing those files.

Four of the original ten house colours only: `#020504` outline, `#7f948a` shadow,
`#9fb3a9` structure, `#d3ece0` highlight. No storage violet, identity jade/purple, alert colours,
new palette entries or partial alpha. The continuous utilization colours in the base line are
not part of the art palette and must remain untouched (G2). All unused/gutter/clearance pixels
are RGBA zero. Native32 is redrawn with one-pixel detail; @2x is nearest-neighbour only.

## Placement contract for Frontend

Use **symbol layers, never line-pattern**, as with #65. Existing base layer paints, width
expressions, power dashes, area strokes, colours and source values stay unchanged (G1).
Current `power` is 2 px and `power-casing` 4 px; this art does not invent an MW width scale or apply
gas's MMcf/d normalization to power. The preserved data channel remains the source of truth.

- Towers have complete silhouettes and **must be off-line**. In the temporary map study they
  use centre anchor, map rotation, viewport pitch, keep-upright true and offset `[0,-12]` at 16 px,
  `[0,-20]` at 32 px, with unit icon size. The offset rotates with the line; it is not a fixed screen-Y
  offset. This keeps the nearest body edge at least 4 px from a straight local wire tangent.
- Joints/casing are optional centred alternatives. A 6 px horizontal corridor at 16 px, 12 px at 32 px,
  remains entirely transparent through the full image width. Rotation must follow the wire;
  a viewport-upright glyph cannot preserve that corridor on a north–south line.
- All images are static schematic repeat marks, **not surveyed tower locations**, voltage classes,
  utilization states, availability claims, new routes or flow-direction arrows. Power/intertie
  shapes differ by lattice/portal and bracket geometry; no extra identity colour is needed.
- The study cycles tower/joint/casing per feature to display alternatives and uses 160 px screen
  spacing. Frontend recommended 96 px for its eventual cross-arm treatment. No production spacing
  is agreed by this sample: select the final glyph/density and prove crowding before shipping.
- A tower rotates with the line and may read sideways or switch sides on reversed segments.
  Frontend explicitly flagged that for visual review. Skip near sharp bends, crossings, nodes
  and callouts unless the complete icon footprint clears every existing line/label. A local
  tangent corridor is **not** a proof about the next segment or another crossing route.
- Respect narrow-stroke fallback (G4), dimming and missing/zero/stale states (G5). Nothing here
  animates or changes the base line's state. Do not quantize utilization to pick an image (G6).

Frontend owns final layer filters, placement/density, pixel-ratio handling, collision exclusions
and G1–G7 proof. The temporary layer is an art study, not an agreed production implementation.
In particular, it deliberately allows overlaps so the candidates can all be seen; that is not
permission to cover production nodes or labels. Its mixed candidates need a final choice.

## Real-zoom evidence and limits

The supplied raw screenshot was captured from the real app on 127.0.0.1:8061 at zoom 5.15,
centre [-116.6,53.9], DPR 1,1280×800, with the art overlay labelled onscreen. It retains the actual
map, basemap, callouts and existing power dash colours, including stale labels. Five guarded
base layers were serialized before and after installing the overlay and compared unchanged.
The screenshot's small preview banner has an encoding blemish; the PNG/sprite pixels are unchanged.
The later close-view/32px capture was interrupted by a T3 preview restart; it is not claimed as proof.

The art used an already-running Frontend server; it did not start, alter or stop that process.
`power_lines_preview.js` is the reproducible temporary browser study, using a base64 atlas/index
input and `window.__map`; cleanup removes its layers/images/source/banner. It only reads
`/api/network`, never fetches or writes observations. No deployment or product controls are added.

Before production, P4c must compare exact base pixels before/after at the same view/tier and
prove ≥50% data-colour exposure, width monotonicity where width encodes flow, power dash/gap
identity, both directions, curves/crossings, missing/zero/stale, screen spacing, fractional scaling,
and FPS (G3/G7). Erik's GPU/screen check remains separate. Only the overview art study is supplied;
native32/contact-sheet review is not native32 real-map or performance proof.

## Reproduction and verification

Run `uv run --with "pillow>=10.1,<14" python docs/art/build_power_lines.py`, then the same prefix
with `docs/art/verify_power_lines.py --seeded-reds`. The builder validates before writing. The
independent verifier checks required keys, exact neutral palette and alpha, protected corridors,
source margins, rectangle/gutter bounds, unused pixels, and @2x pixels/index. Seeded mutations
stay in memory. No app dependencies, credentials or database-test opt-ins are added.
