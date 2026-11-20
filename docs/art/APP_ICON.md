# Montney app icon — step 4c / L-060

[Contact sheet](app-icon-sample.png): a folded map with a jade pipeline feeding a small gas
flame. The folded panels carry the map motif; the pipe and flame give the gas/energy cue at
favicon scale. No lettering, alert badge, new palette entry or blur. Transparent outer corners
and a dark outline keep it readable on light and dark browser/desktop backgrounds.

The native16/32/48px drawings retain crisp one-pixel map/flame outlines, with a simplified
pipe route so the folded paper stays visible. Windows256 is an exact **8x nearest-neighbour**
enlargement of the native32px drawing. No interpolated or antialiased pixels are introduced.
Palette: `#020504` casing/outline, `#7f948a` fold shadow, `#9fb3a9` paper midtone,
`#d3ece0` paper/highlight and `#3dffb8` brand pipeline/flame. This is an app brand mark,
not a coloured map feature or a claim about current flow, receipts or alert state.

## Frontend / desktop handoff

Files in `web/sprites/app-icon/`:
- `favicon-16.png`, `favicon-32.png`, `favicon-48.png`: exact-sized RGBA PNGs.
- `favicon.ico`: exact16/32/48 frames for browser favicon selection.
- `montney-desktop.ico`: exact16/32/48/**256** frames, so the Windows shortcut can choose
  its native small size or full256px image without downsampling a single large frame.

Frontend owns linking `/static/sprites/app-icon/favicon.ico` and PNG sizes in `web/index.html`
(replacing the old inline SVG), and selecting the desktop ICO in the shortcut. This PR only
delivers the icon files; it does not modify the page, shortcuts, registry or app data.
ICO frames embed the exact PNG pixel images, not automatically resampled copies.

Reproduce: `uv run --with "pillow>=10.1,<14" python docs/art/build_app_icon.py`.
Checks cover the five-colour palette, binary alpha, exact PNG dimensions, required ICO frame
sizes and byte-identical decoded pixels against each native drawing. No new suite,
database work, server or dependency is added. Actual browser/shortcut wiring belongs to Frontend.
