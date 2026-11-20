"""Build network/region/outlines.geojson + boxes.geojson from regions/*.yaml (L-106, docs/REGIONS.md).

    uv run --with shapely python scripts/build_regions.py [path/to/ne_50m_admin_1_states_provinces.geojson]

Outlines: each region = the union of its members' Natural Earth admin-1 polygons (1:50m, public domain,
https://github.com/nvkelso/natural-earth-vector v5.1.2), minus the clips it hands to a neighbour, plus the clips it
receives; simplified (~3 km) and rounded to 3 decimals so /api/network stays small. Boxes: montney/regions.py
(no shapely needed; tests/test_regions.py checks the committed file is current).

Without a path, the Natural Earth file is downloaded once into the system temp dir (2.3 MB).
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shapely.geometry import Polygon, mapping, shape  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from montney import regions as R  # noqa: E402

NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/"
          "ne_50m_admin_1_states_provinces.geojson")
SIMPLIFY_DEG = 0.02
SOURCE = ("Natural Earth 1:50m admin-1 states/provinces v5.1.2 (public domain), " + NE_URL +
          "; merged and clipped by scripts/build_regions.py from regions/*.yaml")


def _ne(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    cache = Path(tempfile.gettempdir()) / "ne_50m_admin_1_v5.1.2.geojson"
    if not cache.exists():
        urllib.request.urlretrieve(NE_URL, cache)
    return json.loads(cache.read_text(encoding="utf-8"))


def _round(geom: dict) -> dict:
    def r(c):
        return [round(c[0], 3), round(c[1], 3)] if isinstance(c[0], (int, float)) else [r(x) for x in c]
    return {"type": geom["type"], "coordinates": r(geom["coordinates"])}


def build(ne: dict, regions: dict[str, R.Region]) -> dict:
    admin = {f["properties"]["iso_3166_2"]: shape(f["geometry"]) for f in ne["features"]
             if f["properties"].get("adm0_a3") in ("USA", "CAN", "MEX")}
    geoms = {}
    for reg in regions.values():
        missing = [m for m in reg.members if m not in admin]
        if missing:
            raise SystemExit(f"{reg.id}: no Natural Earth polygon for {missing}")
        geoms[reg.id] = unary_union([admin[m] for m in reg.members])
    for reg in regions.values():                        # clips: carve from the giver, add to the receiver
        for c in reg.clips:
            piece = admin[c["member"]].intersection(Polygon(c["polygon"]))
            geoms[reg.id] = geoms[reg.id].difference(piece)
            geoms[c["to"]] = unary_union([geoms[c["to"]], piece])
    countries = {rid: {m.split("-", 1)[0] for m in reg.members} for rid, reg in regions.items()}
    held = {rid: set(reg.members) for rid, reg in regions.items()}     # members + clips received (region_of member=)
    for reg in regions.values():
        for c in reg.clips:
            countries[c["to"]].add(c["member"].split("-", 1)[0])
            held[c["to"]].add(c["member"])
    # Natural Earth's provinces run to the international line in the Great Lakes and border rivers: cut every region
    # back from the other countries' land, so no outline covers a foreign shore (Reviewer on #408).
    land = {cc: unary_union([g for code, g in admin.items() if code.startswith(cc + "-")]) for cc in ("CA", "US", "MX")}
    feats = []
    for rid, g in geoms.items():
        foreign = [land[cc] for cc in land if cc not in countries[rid]]
        if foreign:
            g = g.difference(unary_union(foreign))
        g = g.simplify(SIMPLIFY_DEG, preserve_topology=True).buffer(0)
        feats.append({"type": "Feature", "geometry": _round(mapping(g)), "properties": {
            "id": f"region.{rid}.outline", "kind": "area", "system": "region", "type": "region_outline",
            "name": regions[rid].name, "region": rid, "countries": sorted(countries[rid]), "members": sorted(held[rid]), "color": regions[rid].color,
            "source": SOURCE}})
    return {"type": "FeatureCollection", "features": feats}


def main() -> None:
    regions = R.load()
    fc = build(_ne(sys.argv[1] if len(sys.argv) > 1 else None), regions)
    R.OUTLINES.parent.mkdir(parents=True, exist_ok=True)
    R.OUTLINES.write_text(json.dumps(fc, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    R.write_boxes(regions)
    print(f"{R.OUTLINES.relative_to(ROOT)}: {len(fc['features'])} regions, {R.OUTLINES.stat().st_size // 1024} KB; "
          f"{R.BOXES.relative_to(ROOT)} written")


if __name__ == "__main__":
    main()
