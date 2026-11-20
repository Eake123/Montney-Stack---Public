# Gas flange symbols — Art / Frontend contract, L-028 G1–G7

Frontend agreed this concrete method in its queued contract to Art (2026-11-01): **separately
placed gas flange symbols**, never `line-pattern`. This follow-up supplies five reference symbols;
Frontend owns integration and the proof below. [Rendered sample](flanges-sample.png) is a static
art board, not evidence from the map. It uses synthetic flow/utilization examples.

## Files and orientation

`web/sprites/flanges.png` + `flanges.json` are a small MapLibre atlas with keys
`line/flange-6`, `line/flange-8`, `line/flange-10`, `line/flange-12`, `line/flange-14`.
Each image is **3 px along the line × H px across it**. The `@2x` PNG/JSON have the same keys,
6×2H rectangles and `pixelRatio: 2`; exact nearest-neighbour copies at identical logical size.
Only top two and bottom two rows are opaque. The outermost row is the collar extending one
pixel beyond the casing; the inner row lies on the casing edge. Every centre pixel is RGBA zero.
Opaque colours are exactly `#020504` / `#7f948a`; no utilization, identity or severity colours.
Two clear atlas gutter pixels surround every image. Key names are ready for merging/loading
alongside the node atlas; this PR does not replace its sprite URL or edit its index.

## Integration agreed with Frontend

- Add `gas-flanges`, a symbol layer on gas links, above `gas` and below nodes.
- `symbol-placement: line`, `symbol-spacing: 64` screen px; rotation alignment `map` so width3
  follows the line and heightH spans it. `icon-allow-overlap` and `icon-ignore-placement`: true.
- `icon-size: 1` at both densities; select an image rather than fractionally scale pixels.
- Preserve `flowW(4,11)` casing width exactly. Height = casing width rounded **up to even**,
  then +2: casing≤4 → H6; 4<width≤6 → H8; 6<width≤8 → H10; 8<width≤10 → H12;
  10<width≤11 → H14. Frontend derives selection from the same |flow| expression, not zoom or util.
  Its `step` boundaries must implement these intervals, including exact boundary readings.
- **Power and interties: no line accents in round 1.** Preserve dashes and gaps pixel-for-pixel,
  including the dark casing under gaps. Pylons need a later proven placement and new art request.

The existing gas/gas-casing/power/power-casing/areas-line paint expressions remain unchanged
(G1); symbols are neutral art only, with no amber/red (G2). Frontend must show at least half the
base stroke remains visible at every tier, monotonic width in |flow| (G3), and plain-line fallback
where stroke is below about 3 px (G4). These drawings have no timed frames or tick animation;
missing/zero/stale distinctions and suppression follow existing metadata and G5, not a new
minimum activity speed. The sample suppresses the zero-flow collar. Continuous base utilization
colour is never bucketed (G6); choosing geometric flange heights is only crisp pixel sizing.

## Required P4c proof (Frontend)

Before shipping, record the contract in `docs/research/frontend.md`. At the same view before/after,
zoom 5.15 and 7: show a 4× crop where power dash/gap pixels are identical, measure collar height
against casing width at low/mid/max flow, and retain correct colour on low/mid/high-util links.
Show the same 64 px screen spacing at z5 and z8 and FPS before/after. Exercise narrow-stroke fallback,
exact height boundaries, zero/missing/stale, negative flow and fractional display scaling. P4c
owns real-map evidence and Erik's GPU check; the integer-raster sample is not a substitute (G7).

Reproduce: `uv run --with "pillow>=10.1,<14" python docs/art/build_flanges.py`.
Add `--seeded-reds` to exercise eight in-memory corruptions without writing invalid art.
The builder validates keys, native dimensions, neutral collar pixels, RGBA-zero centres,
gutters/bounds and @2x index/pixels before writing. No runtime or dependency change is included.
