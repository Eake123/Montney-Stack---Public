# Gas network (v1, schematic)

`nodes.geojson`, `areas.geojson` and `links.geojson` follow `docs/ARCHITECTURE.md` §2. Rebuild them
with `uv run python network/gas/build.py`. Don't hand-edit the GeoJSON; edit `build.py`.

## Where the coordinates come from

Every feature cites its source in `properties.source`.

| Source | Used for | Licence |
|---|---|---|
| CER Pipeline Profiles key points (`key_points.csv`, github.com/mbradds/pipeline-profiles) | Kingsgate, Monchy, Huntingdon, Kingsvale, Sunset Creek, Saturn, West Gate, East Gate, Upstream of James River, Calgary, North and East, OSDA Liege, Alliance Zone 2 and Border | CER open data, OGL-Canada (repo MIT) |
| AER ST102 Facility List (static.aer.ca; personal non-commercial use with attribution) | Gordondale meter, the AB station layer | AER |
| BCER open data facility locations | Wilde Lake (CGL compressor station) | BCER Open Data Licence |
| Petrinex BC facility list (DLS/NTS → lat/lon) | the BC station layer (schematic) | Petrinex |
| Canadian Geographical Names Database (geogratis.gc.ca, by feature id) | Empress, McNeill, Groundbirch, Chetwynd, Fort St. John, Kitimat, Suffield, and the towns lines pass through | NRCan, OGL-Canada |
| TC Customer Express `/areas` | NGTL DOP area outlines and centres (USJR, WGAT, EGAT, OSDA, NEDA, FHBC, FHSK) | TC Energy; used under the personal-use ruling (`docs/research/DECISIONS.md`) |

Station 2 has no published coordinate. The CER describes it as "just west of Chetwynd", so it's
placed about 7 km west of Chetwynd and labelled schematic. Town-anchored nodes (Empress, McNeill,
Groundbirch) sit on the named place, not on the meter station. Gordondale sits on its meter (AER ST102 ABMS0003886),
and Wilde Lake / Willow Valley on the CGL compressor station (BCER facility 00018222) (PLAN 5a).

## Station layer (`stations.geojson`, PLAN 5a)

`build_stations.py` writes one node per active NGTL receipt station with a cited location. Each shows the station's
latest monthly receipts (`ngtl.station.<n>.receipt`, MMcf/d), with firm utilization in its panel.
- **Alberta:** AER ST102 points.
- **BC:** the Petrinex BC facility list's DLS/NTS location, converted and flagged `schematic`. DLS is calibrated to AER
  points (p90 0.74 km); NTS is unverified.
- **Not drawn:** stations without a location.
- **Callouts:** only stations ≥ 200 MMcf/d, at priority 3.
- **Rebuild:** `uv run python network/gas/build_stations.py`. It fetches TC, AER and Petrinex in memory and prints coverage.

## Notes for the frontend

- `callout: {label, priority}`: priority is the zoom level the box (and dot) appears at: 1 from zoom 6, 2 from
  6.75, 3 from 7.5. The overview (tier 0) is set in `web/map.json` overrides, not here (docs/WIDGETS.md).
- `value_kind: "capacity"` on `gas.station2` and `gas.huntingdon`: the value is published capacity,
  not flow (Westcoast posts no public daily flow). On `gas.cgl`, `value_kind: "ngtl_share"` means
  the value is NGTL's delivery into CGL at Willow Valley. Producers also deliver into CGL directly,
  and there is no public number for that part.
- `gas.montney_receipts` shows NGTL **total** field receipts (all of AB + NE BC). It is anchored in
  the Montney only for placement and flagged `region_value: true`, so show it as a system/region
  figure, not a Montney number. The Montney-specific receipts are `gas.gordondale` and
  `gas.groundbirch_east`.
- Areas carry `label_point` ([lon, lat], TC's own area centre) as the callout and pin anchor.
- Events attach through `panel.events_ref`. Outages and bulletins use the area ids
  (`gas.ngtl.egat`, ...); Westcoast notices use `gas.station2`, `gas.huntingdon`,
  `gas.westcoast.t_north` and `gas.westcoast.t_south`.
- `gas.ngtl.linepack` is a system-wide value, anchored at the Upstream of James River key point.
  It sets `target: ngtl.csr.linepack_target` and `compare: "1h"`. Other system-wide numbers, such
  as total export, fit best as region-anchored callouts through
  `/api/snapshot?extra=ngtl.csr.total_export`.
