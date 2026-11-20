# Pixel callout frame and leader — production handoff

**Frame approved: L-041 Q10, 2026-11-01.** Production copies are
`web/sprites/callout-frame.png` (16×16, slice 4/4/4/4) and `callout-frame@2x.png` (32×32),
byte-identical to the source frames below. Erik chose the existing translucent background;
the solid backing in the historical sample was **not** selected. Frontend draws leaders in
SVG from this spec (1 px `#7f948a`, 3 px `#020504` casing, and a 4×4 px endcap with
a 2×2 px `#d3ece0` centre inside a 1 px `#020504` outline),
preserving region dashes; no leader PNG assets are needed in production.

[Rendered sample](callout-proposal/sample.png) uses **synthetic values**, not a live map.
For Erik's review: a neutral stepped frame and a thin pixel leader in the L-028 house style.
No frontend code, production sprite atlas or font changes are included. Integration needs Erik's
approval (now granted for the frame) and a Frontend PR with actual-map proof; these assets alone
do not alter the UI.

The transparent 16×16 `frame.png` is a nine-slice source with **four-pixel fixed corners**,
eight-pixel stretchable centre bands and a transparent centre. `slices.json` gives the contract.
The sample assembles it at 156×56 and 156×100, with an independent solid `#020504` backing.
That backing is a sample option only: **retain the current callout background treatment unless
Erik explicitly chooses the solid backing.**
Insets are image slicing coordinates, not permission to change box padding or hit targets.
Keep P4a's measured outer box dimensions and content padding; if four-pixel border geometry
does not fit existing padding, use the frame as a noninteractive overlay rather than enlarge boxes.

`leader-horizontal/vertical/elbow/diagonal/cap.png` are reference pieces. The line is one pixel
`#7f948a` with three-pixel `#020504` casing. Endcaps match the approved 4×4 tile:
2×2 `#d3ece0` centre, surrounded by a one-pixel `#020504` outline. Fixed corners/caps never
stretch; straight sections stretch only along their axis. Diagonal steps repeat at 1:1 or are
redrawn to the actual endpoint; never stretch a diagonal texture into arbitrary angles.
Every component has an exact nearest-neighbour @2x export with the same logical dimensions.

Palette: `#020504` outline/backing, `#7f948a` shadow/leader, `#9fb3a9` upper-left keyline,
`#d3ece0` sample text/caps. Board-only `#16201b` ground. No severity or receipt/intertie colours
in the frame. Existing event badges/text retain severity; frames do not pulse or imply freshness.
Leaders show association, never flow direction. No animation is proposed.

**No pixel font is supplied.** Keep existing JetBrains Mono, numeric precision, units, minus signs,
estimated-value marks, stale labels and accessibility text. Sample typography is illustrative
Pillow raster text, not a font proposal or screenshot of browser text rendering.

Frontend must preserve P4a sticky placement, glide/fade, hysteresis and handoff.
Draw leaders from the existing anchor and box edge; raster rounding must not move the box or
geographic anchor. Preserve regional dash semantics (dashed leader stays dashed), selection,
row click targets and reduced-motion behaviour. Fixed-slice rendering must not trigger layout
or change callout tiers/collision footprints. Compare before/after at the same zoom and viewport,
including narrow boxes, negative values, long names, multirow estimates and missing/stale states.
P4c must also prove crisp fixed corners and leaders in the actual renderer at fractional zoom,
fractional CSS sizes and 125%/150% display scaling. The Python corner check proves only the
reference assembly; it does not establish browser rendering quality.

Reproduce: `uv run --with "pillow>=10.1,<14" python docs/art/build_callout_proposal.py`.
Validation checks alpha, the neutral palette, a transparent frame centre and unchanged corners
at four card sizes before writing. Authoring uses optional Pillow; app dependencies stay intact.
