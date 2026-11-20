# Selected power-line glyph — Frontend cross-arm contract

This small follow-up supplies Frontend's concrete selection after the broader [round-2 art
study, #129](https://github.com/Eake123/Montney-Stack/pull/129). That approved study stays unchanged;
its off-line towers and mixed alternatives are **not** the production selection for this contract.
[Native/enlarged cross-arm sample](pylons-sample.png) is a schematic raster study, not real-map proof.

`web/sprites/pylons.png` / `pylons.json` contain exactly one key: **`line/pylon`**, 3 px along
the wire × 12 px across. The atlas is16×16 with two clear gutter pixels. The `@2x` files use
the same key, a6×24 glyph,32×32 atlas and `pixelRatio:2`; exact nearest-neighbour pixels.
Rows3–8 of the native glyph are RGBA zero, leaving the4px casing plus1px clearance either side.
Rows0–2 and9–11 contain the accents. Top/bottom and left/right are pixel-identical reflections,
so reversing a line cannot flip the light. The only opaque colours are `#020504`, `#7f948a`,
`#9fb3a9`; binary alpha. No highlight, identity, utilization or severity colour is baked in.

Frontend's agreed layer: symbols on **every power link**, transmission and intertie alike;
`symbol-placement: line`, spacing96 screen px, rotation alignment `map`, pitch alignment
`viewport`, keep-upright, unit icon size and overlap allowed; above `power`, below nodes.
No icon offset. It rotates with the line, so the clear corridor follows any straight wire angle.
The glyph is infrastructure, not a flow/availability signal. Original power widths remain
4px casing/2px dash; no new utilization buckets or data-width expression. No line-pattern.

Frontend loads the atlas CSS-exact like gas flanges and proves G1–G7: centreline±2px pixels
identical before/after at z5.15/7/9 and100/125/150% scaling; dash visible on an intertie; curves,
crossings, nodes/callouts, states, spacing and frame cost. A straight raster strip is not proof
of those map cases. No runtime layer wiring, server, live-data access or observation fetch is
included here. Any later map preview must use an Art-owned server and scratch copy of data,
not Erik's live data directory or another agent's server.

Reproduce and validate: `uv run --with "pillow>=10.1,<14" python docs/art/build_pylons.py --seeded-reds`.
The verifier checks the exact native/@2x index, every pixel's alpha/palette/corridor/gutter and
both symmetries before writing. Seven in-memory corruption probes reject. This adds no app dependency.
