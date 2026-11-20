# Regions

One YAML file per region. The contract and the rules are in [`docs/REGIONS.md`](../docs/REGIONS.md); read it first.

- **Fill a line:** add its `parts` (series ids; `-id` subtracts; `{series, sign, scale, fill: month}`), plus `covers`
  when only some members are measured, and `quality` when it's scheduled or an estimate. Gas owns `gas:` and `oil:`,
  Power owns `power:`, Weather owns `weather:`. Plain-words daily `watch` items come from each region's research note.
- **Fill the region page:** add groups under `detail: gas: | power: | oil:` (REGIONS.md §5b): `{id, title, of?, rows}`
  with rows `{label, series}` or a static fact `{label, value, unit, as_of, source}`. Use the shared group ids. Gas
  owns `detail: gas:` and `detail: oil:`, Power owns `detail: power:`.
- **After any edit:** `uv run python -c "from montney import regions; regions.write_boxes()"` regenerates
  `network/region/boxes.geojson` (tests/test_regions.py fails until you do). Changing `members`, `clips` or `anchor`
  needs the outlines rebuilt: `uv run --with shapely python scripts/build_regions.py`.
- **Check:** `uv run pytest tests/test_regions.py`. The engine (`sources/region_balance.yaml`) writes a line only where
  every part has a value, so a new part that isn't stored yet just leaves that line empty. It never shows a zero.
