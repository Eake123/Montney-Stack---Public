# Animated wildfire art — step 4c

[Contact sheet](wildfire-sample.png) shows four flicker frames, a smaller single-hotspot glyph
and a larger active-fire/perimeter glyph, at native16/32 and integer enlargement.

`web/sprites/wildfire.png/json` + `wildfire@2x.png/json` supply sixteen MapLibre images:
`wildfire/{hotspot,perimeter}-{16,32}-{0,1,2,3}`. Same keys at both densities, native
`pixelRatio:1`, exact nearest-neighbour @2x with `pixelRatio:2`. Fixed canvases and bottom-centre
anchor across frames. Both sizes have one-pixel dark outlines; native16 retains the house halo.
No smoothing or partial-alpha fade.

The flame is existing **rose-red `#ff4d6d`**, with `#d3ece0` pale core and `#020504` outline;
`#7f948a` halo/base. `#9fb3a9` is allowed neutral shading. No amber `#ffb547`, gas-border orange,
khaki low-flow, jade, new colour or interpolation is baked into the sprites. The sheet compares
the flame with amber and the current gas-border keyline (`#d49c7e`, computed by
the `tint-gas` filter from the frame's `#9fb3a9`; main `web/index.html`).

This new wildfire request explicitly reuses the house rose-red as a **fire entity identity**,
not a critical-severity classification. A flame silhouette with pale core is distinct from a
triangle/octagonal alert badge; Frontend keeps actual alert severity separate. No event becomes
critical merely because its fire sprite is red. STYLE.md records this limited exception.

Frontend owns source validity/freshness, placement and animation. Animate actual reported active
fires/hotspots only. A historic/inactive perimeter or absent/stale fire data does not establish
an active flame. Prefer the smaller glyph for a single hotspot and larger for an active perimeter
or source-classified large fire; do not infer acreage, temperature, intensity or spread speed from
these two art sizes. Keep real perimeter geometry and attribution intact, with source dates visible.
Place the larger glyph at the agreed feature anchor, not repeated along a perimeter as a fake fire front.

The overlay is static art frames only; no browser animation, layer wiring or data fetch is included.
Use the shared existing frame clock (max four changes/sec), respecting hidden/reduced-motion views.
The contact sheet proves appearance, not live-map source binding or animation behaviour.

Reproduce: `uv run --with "pillow>=10.1,<14" python docs/art/build_wildfire.py`.
Only existing palette/alpha/@2x checks are run, as requested; no new suite, database work,
server, network source, dependency or broader process is introduced.
