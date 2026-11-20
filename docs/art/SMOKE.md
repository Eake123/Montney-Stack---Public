# Gas-plant activity puff — step 4b / L-058

[Contact sheet](smoke-sample.png): four rising-puff phases, plus a thinner low-output variant,
at native16/32 and integer enlargement. This is an **output/activity symbol**, not measured
smoke, pollution, exhaust composition or emissions volume. The grey plume stays separate
from amber alerts and khaki river low-flow colours.

`web/sprites/smoke.png` / `smoke.json` and `@2x` supply sixteen MapLibre images:
`smoke/{normal,low}-{16,32}-{0,1,2,3}`. Same keys at both densities; exact nearest-neighbour
@2x with `pixelRatio:2`, native index `pixelRatio:1`. Fixed square canvases, one-pixel dark
outlines at native sizes, binary alpha, no smoothing, gradients or partial-alpha fade.
Native16 retains the house style's one-pixel `#7f948a` outer halo; native32 has no halo.
Only `#020504`, `#7f948a`, `#9fb3a9`, `#d3ece0` are opaque. No amber, khaki or new colours.

## Frontend handoff

**PLAN 10b / L-108:** the eight fuel-burning kinds continue to use these unchanged four
phases over their steady glow; the native16 composites in NODES.md's motion sheets show
the puff is sufficient to read as on. The new non-thermal/signed sequences live inside
nodes.png and replace full node images, with no puff offsets. Eligibility remains fresh
positive plant output here; estimated solar/wind/hydro motion uses its own node frames,
never this smoke. See NODES.md for the complete agreed Frontend contract.

**Regions 8b / L-106 extension:** coal, oil and generic gas reuse these unchanged puffs,
under the same fresh-positive-measured-output gate below. NODES.md lists the three new
stack mouths and native offsets. Do not add steam/smoke to battery, biomass, geothermal or
pumped storage from this atlas. Geothermal has only its fixed category silhouette.

Draw as a separate symbol above the actual gas-plant stack, not by shifting the plant's node
or callout. Anchor each puff canvas at bottom centre. The outlet stays at approximately
`(8,14)` in native16 / `(16,28)` in native32; the travelling puff rises above it. Keep anchor,
offset and canvas fixed across all four frames. Place the outlet on the existing stack mouth
by compensating for the outlet being2 source pixels above the bottom anchor at16 (4 at32).
Scale that offset with Frontend's existing CSS-exact sprite handling, not fractional image scaling.
using its native sprite coordinates; do not attach smoke to solar, hydro, wind or gas-processing
facilities just because they have a plant-like shape. This PR does not edit node sprites.

**Hide the entire smoke layer for a plant unless its actual output is fresh and positive.**
No data, stale data, zero/negative MW, historical/estimated input: no smoke, not a frozen puff.
Source freshness comes from existing metadata, never from an art timer. Frontend owns the
fresh-output binding, variant threshold and rate; choose the thinner version only for lower
real output. A shared four-frame clock may encode output within the existing four-updates/sec
budget. No minimum activity when output is missing or zero. Respect reduced motion and pause
when hidden/offscreen; do not imply live activity in those views.

L-058 explicitly authorizes this data-driven plume, superseding STYLE.md's earlier ban on
decorative smoke for this case only. Other decorative motion remains out of scope. Frontend
owns real-map placement/readability/animation proof and keeps alerts and numeric MW legible.

Reproduce: `uv run --with "pillow>=10.1,<14" python docs/art/build_smoke.py`.
Only the existing palette/alpha/@2x checks are run, as requested; no new suite, database work,
server, live-data fetch, runtime wiring or dependency change is included.
