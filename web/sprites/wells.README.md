# Well / grouped-producer icons

Load `wells.png` + `wells.json`; exact density copies are `wells@2x.png` + `wells@2x.json`.
Keys: `gas-well-16-static`, `gas-well-32-static`, `gas-pad-16-static`, `gas-pad-32-static`.
Use bottom-centre anchors. The pad's three-dot marker means an aggregated entity, not three wells.
Both are static; source period, units, estimates and ranking stay in the popup.

Use existing `loadSprites` default device-exact fit, `icon-size:1`, plus `spriteCss` for geometry.
At DPR2.6, each source pixel becomes3 device pixels: native16 is48 device px /18.4615 CSS px,
native32 is96 device px /36.9231 CSS px. Do not fractionally scale to force16 CSS px, or use
the CSS-exact line-art option. @2x is exact nearest-neighbour, not a smoothed phone resize.

[Contact sheet and detailed handoff](../../docs/art/WELLS.md). Actual phone rendering is Frontend's check.
