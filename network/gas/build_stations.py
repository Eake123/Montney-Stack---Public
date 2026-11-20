"""Build network/gas/stations.geojson: NGTL receipt meter stations with a cited location (PLAN 5a).

    uv run python network/gas/build_stations.py            # fetches TC, AER and Petrinex (~15 MB), writes the GeoJSON

Each node shows its station's latest monthly receipts (`ngtl.station.<n>.receipt`, MMcf/d) and has the firm
utilization (`ngtl.station.<n>.firm_pct`) in its panel. Both come from sources/ngtl_station_util.yaml.

**Which stations:** every TC receipt station with receipts in the last 12 published months, **and** a cited location.
A station without a location gets no node (Wellsite: no guessed geometry).

**Locations**, by the TC station number (P2: TC no. NNNN = Petrinex/AER meter station id ..MS000NNNN, with names
agreeing on all 523 overlaps; docs/research/v2/gas-matching.md §1):
1. **Alberta:** AER ST102 Facility List shapefile (`ST102_Facility_GCS_NAD83`, points at the LSD or section centre),
   matched by `ABMS000NNNN`, else by a unique normalised name among ABMS facilities. The AER allows personal or
   non-commercial reproduction with attribution (https://www.aer.ca/copyright-disclaimer).
2. **BC:** Petrinex BC facility list (`bbreportsBC/PRAFacilityIds.csv`), matched by `BCMS000NNNN`. Its DLS or NTS
   location is converted to lat/lon by survey-grid arithmetic, so these nodes carry `schematic: true` and
   `location_quality: "dls_approx"` / `"nts_approx"`.
   - DLS is calibrated against AER points: p90 0.74 km.
   - NTS is unverified: the grid arithmetic is exact to the quarter-unit (~0.5 × 0.7 km), but there was no reference
     point to check it against.

**Excluded:** stations that already have a v1 node (`EXISTING`). They're drawn once, by that node.

**Callouts:** only stations ≥ CALLOUT_MMCFD in the latest month, at priority 3 (zoom ≥ 7.5); the rest are dots with a
panel. The label carries the caveat that these are TC's allocated volumes, ~2.7 % below measured.

The raw files are read in memory and never saved; only the derived station points are committed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import struct
import sys
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from montney.connectors import ngtl_station_util as su  # noqa: E402
from montney.connectors.ngtl_split import BC_STATIONS, TOTAL_BAND  # noqa: E402

OUT = Path(__file__).with_name("stations.geojson")
ST102_ZIP = "https://static.aer.ca/prd/data/codes/ST102-SHP.zip"
ST102_BASE = "ST102_Facility_GCS_NAD83"
PETRINEX_BC = "https://www.petrinex.gov.ab.ca/bbreportsBC/PRAFacilityIds.csv"
CALLOUT_MMCFD = 200.0
# Last-month context, never live (DECISIONS P5a): the value is last month's TC allocated receipts. The band is P2's station
# back-test (last month's value as a guide to this month: P90 19.8 %, gas-stations.md §4).
BAND = 0.25
# PLAN 4b / L-058: the box shows today's estimate (ngtl_split: today's GDSR field receipts × k × last month's share),
# with last month's actual and firm % in the panel.
NOTE = ("today's estimate: NGTL field receipts today (GDSR, rescaled to measured) × this station's share of the latest "
        "TC month's receipts; ±25 % (monthly back-test; a single day can swing more)")


def share(st: int) -> dict:
    """PLAN 4b share row: the station's estimate as a % of its province's production today (ngtl_split)."""
    pv = "bc" if st in BC_STATIONS else "ab"
    return {"of": f"production.{pv}.daily_est", "label": f"% of {pv.upper()} production", "of_band": TOTAL_BAND[pv]}


EXISTING = {3886: "gas.gordondale", 5120: "gas.groundbirch_east", 1958: "gas.empress"}
AER_CITE = ("AER ST102 Facility List (static.aer.ca/prd/data/codes/ST102-SHP.zip, {fac}, {src}); "
            "AER, reproduced for personal non-commercial use with attribution")
BC_CITE = ("Petrinex BC facility list (petrinex.gov.ab.ca/bbreportsBC/PRAFacilityIds.csv, {fac}, {loc}); "
           "{kind} location converted to lat/lon by survey-grid arithmetic (DLS calibrated to AER points, p90 0.7 km; "
           "NTS unverified); schematic")


# ------------------------------------------------------------------ AER ST102 shapefile (stdlib reader)
def read_dbf(data: bytes, want: set[str]) -> list[dict]:
    n, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields, off = [], 32
    while data[off] != 0x0D:
        name = data[off:off + 11].split(b"\0")[0].decode("ascii")
        fields.append((name, data[off + 16]))
        off += 32
    out, pos = [], hlen
    for _ in range(n):
        rec, p, row = data[pos:pos + rlen], 1, {}
        for name, length in fields:
            if name in want:
                row[name] = rec[p:p + length].decode("utf-8", "replace").strip()
            p += length
        out.append(row)
        pos += rlen
    return out


def read_shp_points(data: bytes) -> list[tuple[float, float] | None]:
    pts, pos = [], 100
    while pos < len(data):
        clen = struct.unpack(">i", data[pos + 4:pos + 8])[0] * 2
        stype = struct.unpack("<i", data[pos + 8:pos + 12])[0]
        pts.append(struct.unpack("<dd", data[pos + 12:pos + 28]) if stype == 1 else None)
        pos += 8 + clen
    return pts


def st102(client: httpx.Client, prefixes: tuple[str, ...] = ("ABMS",)) -> dict[str, dict]:
    r = client.get(ST102_ZIP)
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        raise SystemExit(f"ST102: expected a zip, got HTTP {r.status_code} {r.headers.get('content-type')!r} "
                         "(AER may refuse requests without a User-Agent)")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = {Path(n).name: n for n in z.namelist()}
    dbf = read_dbf(z.read(names[ST102_BASE + ".dbf"]), {"FAC_ID", "FAC_NAME", "OPERATOR", "FAC_STATUS", "LOC_SOURCE"})
    pts = read_shp_points(z.read(names[ST102_BASE + ".shp"]))
    if len(dbf) != len(pts):
        raise SystemExit(f"ST102: {len(dbf)} records vs {len(pts)} shapes")
    return {r["FAC_ID"]: {**r, "lon": p[0], "lat": p[1]} for r, p in zip(dbf, pts)
            if p and r["FAC_ID"].startswith(prefixes)}


# ------------------------------------------------------------------ BC survey grids (DLS / NTS → lat/lon, approx)
# montney/survey.py (calibration notes there); re-exported for tests/test_gas_stations.py
from montney.survey import DLS_LAT_KM_PER_TWP, DLS_LON_KM, dls, nts  # noqa: E402,F401


def petrinex_bc(client: httpx.Client) -> dict[str, dict]:
    text = client.get(PETRINEX_BC).content.decode("latin-1")
    return {r["FacilityCode"]: r for r in csv.DictReader(io.StringIO(text)) if r["FacilityCode"].startswith("BCMS")}


# ------------------------------------------------------------------ TC stations
def active_stations(client: httpx.Client) -> tuple[dict[int, dict], date]:
    """{station: {name, segment, latest_mmcfd}} for stations with receipts in the last 12 published months."""
    links = su.receipt_links(client.get(su.PAGE).text, su.PAGE)
    recs = []
    for year in sorted(links)[-2:]:
        rows, as_of = su.parse_receipts(su.read_sheet(links[year], client.get(links[year]).content), year)
        recs += [x for x in rows if su._month_end(x["month"]) <= as_of]
    latest = max(x["month"] for x in recs)
    first = date(latest.year - 1, latest.month, 1) if latest.month == 12 else date(latest.year - 1, latest.month + 1, 1)
    by: dict[int, dict] = defaultdict(lambda: {"flow": 0.0, "latest": 0.0})
    for x in sorted(recs, key=lambda x: x["month"]):
        if x["month"] < first:
            continue
        d = by[x["station"]]
        d["name"], d["segment"] = x["name"], x["segment"] or d.get("segment", "")
        d["flow"] += x["flow_e3m3"]
        if x["month"] == latest:
            d["latest"] = su.to_mmcfd(x["flow_e3m3"], x["month"])
    return {st: d for st, d in by.items() if d["flow"] > 0}, latest


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", re.sub(r"\bNO\.?\s*", "NO", s.upper()))


def locate(st: int, name: str, aer: dict, aer_by_name: dict, bc: dict) -> dict | None:
    fac = f"ABMS{st:07d}"
    if fac in aer:
        a = aer[fac]
        return dict(lon=a["lon"], lat=a["lat"], fac=fac, how="id", quality=a["LOC_SOURCE"].lower().replace(" ", "_"),
                    schematic=False, source=AER_CITE.format(fac=fac, src=a["LOC_SOURCE"]))
    fac = f"BCMS{st:07d}"
    if fac in bc:
        loc = bc[fac]["Location"].strip()
        ll, kind = (dls(loc), "DLS") if dls(loc) else (nts(loc), "NTS")
        if ll:
            return dict(lon=ll[1], lat=ll[0], fac=fac, how="id", quality=f"{kind.lower()}_approx", schematic=True,
                        source=BC_CITE.format(fac=fac, loc=loc, kind=kind))
    bc_hits = [r for r in bc.values() if norm(r["FacilityName"]) == norm(name) and "NOVA" in r["OperatorName"].upper()]
    if len(bc_hits) == 1:
        r = bc_hits[0]
        loc = r["Location"].strip()
        ll, kind = (dls(loc), "DLS") if dls(loc) else (nts(loc), "NTS")
        if ll:
            return dict(lon=ll[1], lat=ll[0], fac=r["FacilityCode"], how="name", quality=f"{kind.lower()}_approx",
                        schematic=True, source=BC_CITE.format(fac=r["FacilityCode"], loc=loc, kind=kind)
                        + "; matched by station name")
    hits = aer_by_name.get(norm(name), [])
    if len(hits) == 1:
        a = hits[0]
        return dict(lon=a["lon"], lat=a["lat"], fac=a["FAC_ID"], how="name",
                    quality=a["LOC_SOURCE"].lower().replace(" ", "_"), schematic=False,
                    source=AER_CITE.format(fac=a["FAC_ID"], src=a["LOC_SOURCE"]) + "; matched by station name")
    return None


def feature(st: int, d: dict, loc: dict, latest: date) -> dict:
    name = d["name"].title()
    props = {"id": f"gas.ngtl.station.{st}", "kind": "node", "system": "gas", "type": "meter",
             "name": f"{name} ({st})", "source": loc["source"], "tc_station": st, "segment": d["segment"],
             "facility_id": loc["fac"], "matched_by": loc["how"], "location_quality": loc["quality"],
             "value": su.series_id(st, "receipt_est"), "unit": "MMcf/d", "compare": "prev",
             "note": NOTE, "band": BAND, "share": share(st),
             "panel": {"series": [su.series_id(st, "receipt_est"), su.series_id(st), su.series_id(st, "firm_pct")]}}
    if loc["schematic"]:
        props["schematic"] = True
    if d["latest"] >= CALLOUT_MMCFD:
        props["callout"] = {"label": f"{name} (today est., ±{BAND * 100:.0f} %)", "priority": 3}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(loc["lon"], 5), round(loc["lat"], 5)]},
            "properties": props}


def build(client: httpx.Client) -> tuple[dict, dict]:
    stations, latest = active_stations(client)
    aer, bc = st102(client), petrinex_bc(client)
    aer_by_name: dict[str, list] = defaultdict(list)
    for a in aer.values():
        aer_by_name[norm(a["FAC_NAME"])].append(a)
    feats, stats, unplaced, placed_vol = [], defaultdict(int), [], 0.0
    for st, d in sorted(stations.items()):
        if st in EXISTING:
            stats["existing_node"] += 1
            continue
        loc = locate(st, d["name"], aer, aer_by_name, bc)
        if not loc:
            stats["no_location"] += 1
            unplaced.append((d["latest"], st, d["name"], d["segment"]))
            continue
        placed_vol += d["latest"]
        stats[f"{loc['how']}:{loc['quality']}"] += 1
        feats.append(feature(st, d, loc, latest))
    stats["nodes"], stats["active"], stats["latest_month"] = len(feats), len(stations), latest.isoformat()
    total = sum(d["latest"] for st, d in stations.items() if st not in EXISTING)
    stats["latest_month_volume_placed_pct"] = round(100 * placed_vol / total, 1) if total else None
    stats["largest_unplaced"] = [f"{st} {nm} ({seg}) {v:.0f} MMcf/d" for v, st, nm, seg in sorted(unplaced, reverse=True)[:12]]
    return {"type": "FeatureCollection", "features": feats}, dict(stats)


def main() -> None:
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "montney-stack (personal use)"}) as c:
        fc, stats = build(c)
    OUT.write_text(json.dumps(fc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
