"""Build network/gas/plants.geojson: gas plants linked to NGTL receipt stations by a **volume fingerprint** (PLAN 5a).

    uv run python network/gas/build_links.py      # fetches TC, Petrinex and AER (~25 MB), writes the GeoJSON

Only the **volume-matched high tier** is drawn (DECISIONS P5a / L-044): the links whose precision is calibrated at
~95 %. The other 112 high-tier links and the whole medium tier stay off the map until calibrated (Q7, no answers).

**The rule**, for the latest month both TC and Petrinex publish (the P2 method, gas-matching.md §2, restricted to the shown
set):
1. **Fingerprint:** a plant's masked sales gas (Petrinex, ≥ 5 MMcf/d) is within ±3 % of an AB NGTL receipt station's
   receipts (TC Station % Utilization), the station is one of the plant's 6 nearest, and it's < 30 km away.
2. **Unique on both sides:** the plant has only that station within ±3 %, and the station only that plant. Ambiguous
   fingerprints aren't drawn.
3. **Score ≥ 4** with P2's proximity/name points:
   - distance < 1 km: 3, < 3 km: 2, < 8 km: 1, < 15 km: 0, else −1;
   - +2 for a shared place name;
   - +1 for the only NGTL station within 8 km;
   - +1 for the nearest station;
   - +1 for a plant ≥ 20 MMcf/d.

   (P2's high tier is ≥ 8, including +4 for the volume match.)
- **Calibration** (P2 null test): a ±3 % hit occurs on 1.17 % of plant-station pairs 40-80 km apart, against 22.9 %
  within 15 km. So ~5 % of fingerprint links are chance.
- **Stability:** the build also re-runs the rule on the month before and reports how many links persist.

**Locations** are AER ST102 points for both the plant and the station (P2 used uncalibrated LSD arithmetic for the
plants, so the set can differ slightly from P2's 97; the build prints the overlap when the P2 list is given).

Output: plant nodes (type `gas_processing`, value = `petrinex.<id>.sales_gas`) plus one link per pair. A link runs
plant → station (schematic, direction +1, value = the plant's sales gas) and carries:
- `volume_match_pct` (plant ÷ station − 1);
- `km`;
- `score`;
- `month`;
- `confidence` 0.95 and `method`.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from montney.connectors import gas_petrinex as gp  # noqa: E402
from montney.connectors import ngtl_station_util as su  # noqa: E402

_spec = importlib.util.spec_from_file_location("build_stations", Path(__file__).with_name("build_stations.py"))
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)

OUT = Path(__file__).with_name("plants.geojson")
STATIONS = Path(__file__).with_name("stations.geojson")
TOL, MIN_PLANT, MAX_KM, NEAREST, MIN_SCORE = 0.03, 5.0, 30.0, 6, 4
CONFIDENCE = 0.95
PLANT_NOTE = ("last-month context, not live: Petrinex sales gas, ~2 months behind; "
              f"±{gp.PLANT_BAND * 100:.0f} % as a guide to now (P90, Jan-Sep 2026)")
GENERIC = {"NORTH", "SOUTH", "EAST", "WEST", "CREEK", "LAKE", "RIVER", "HILL", "HILLS", "DEEP", "PLANT", "SWEET", "SOUR",
           "SALES", "SUMMARY", "COMPRESSOR", "GATHERING", "SYSTEM", "FACILITY", "PROCESSING", "ABGP", "ABGS", "CANADA",
           "ENERGY", "RESOURCES", "PARTNERSHIP", "MIDSTREAM", "PEMBINA", "KEYERA", "TOURMALINE", "CNRL", "CENTRAL",
           "BATTERY", "PIPELINE", "CORP", "LIMITED", "STATION", "MAIN"}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def place_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[A-Z]{4,}", s.upper()) if t not in GENERIC}


def _norm(s: str) -> str:
    s = re.sub(r"\b(APN|APS|SUMMARY|SALES|METER|STATION|GAS|PLANT|GP|GS|NO)\b", " ", s.upper())
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", s).split())


def name_hit(a: str, b: str) -> bool:
    return bool(place_tokens(a) & place_tokens(b)) or difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio() >= 0.6


def dist_pts(d: float) -> int:
    return 3 if d < 1 else 2 if d < 3 else 1 if d < 8 else 0 if d < 15 else -1


def match(plants: dict[str, dict], stations: dict[int, dict]) -> list[dict]:
    """plants {fac: {mmcfd, name, ll}}, stations {n: {mmcfd, name, ll}} → the shown links (rule in the module doc)."""
    st_items = [(n, s) for n, s in stations.items() if s["mmcfd"] > 0]
    edges = []
    for fac, p in plants.items():
        if p["mmcfd"] < MIN_PLANT:
            continue
        near = sorted((km(p["ll"], s["ll"]), n) for n, s in st_items)[:NEAREST]
        ngtl_8 = sum(1 for d, _ in near if d < 8)
        for rank, (d, n) in enumerate(near):
            s = stations[n]
            if d >= MAX_KM or abs(p["mmcfd"] / s["mmcfd"] - 1) > TOL:
                continue
            nh = name_hit(s["name"], p["name"])
            score = (dist_pts(d) + 2 * nh + (1 if ngtl_8 == 1 and d < 8 else 0) + (1 if rank == 0 else 0)
                     + (1 if p["mmcfd"] >= 20 else 0))
            edges.append(dict(fac=fac, station=n, km=round(d, 2), rank=rank, name_hit=nh, score=score,
                              plant_mmcfd=p["mmcfd"], station_mmcfd=s["mmcfd"],
                              volume_match_pct=round(100 * (p["mmcfd"] / s["mmcfd"] - 1), 2)))
    by_fac, by_st = defaultdict(int), defaultdict(int)
    for e in edges:
        by_fac[e["fac"]] += 1
        by_st[e["station"]] += 1
    return [e for e in edges if by_fac[e["fac"]] == 1 and by_st[e["station"]] == 1 and e["score"] >= MIN_SCORE]


# ------------------------------------------------------------------ inputs
def station_month(client: httpx.Client, month: date) -> dict[int, float]:
    links = su.receipt_links(client.get(su.PAGE).text, su.PAGE)
    url = links[month.year]
    rows, as_of = su.parse_receipts(su.read_sheet(url, client.get(url).content), month.year)
    return {x["station"]: su.to_mmcfd(x["flow_e3m3"], x["month"]) for x in rows if x["month"] == month}


def petrinex_month(client: httpx.Client, month: date) -> dict[str, float] | None:
    r = client.get(f"{gp.API}/Vol/{month:%Y-%m}/CSV", timeout=300)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    sales = gp.sales_by_facility(gp.inner_csv(r.content))
    sales.pop(gp.MASKED_KEY, None)
    return {f: gp.mmcfd(v, month) for f, v in sales.items()}


def prev_month(m: date) -> date:
    return date(m.year - (m.month == 1), (m.month - 2) % 12 + 1, 1)


def build(client: httpx.Client) -> tuple[dict, dict]:
    stations_fc = json.loads(STATIONS.read_text(encoding="utf-8"))["features"]
    st_ll = {f["properties"]["tc_station"]: (f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0], f)
             for f in stations_fc if f["properties"]["facility_id"].startswith("ABMS")}       # AB only: Petrinex is AB
    m = date.today().replace(day=1)
    pet = None
    for _ in range(4):                       # the latest month Petrinex has published (TC lags less)
        m = prev_month(m)
        pet = petrinex_month(client, m)
        if pet:
            break
    if not pet:
        raise SystemExit("no Petrinex month in the last 4")
    aer = bs.st102(client, prefixes=("ABGP", "ABGS", "ABIF", "ABBT"))
    names = gp.facility_list(gp.inner_csv(client.get(f"{gp.API}/Infra/Facility%20Infrastructure/CSV", timeout=300).content),
                             set(pet))

    def run(month: date, sales: dict[str, float]) -> list[dict]:
        rec = station_month(client, month)
        stations = {n: {"mmcfd": rec.get(n, 0.0), "name": ll[2]["properties"]["name"], "ll": ll[:2]}
                    for n, ll in st_ll.items()}
        plants = {f: {"mmcfd": v, "name": names.get(f, {}).get("name", f), "ll": (aer[f]["lat"], aer[f]["lon"])}
                  for f, v in sales.items() if f in aer}
        return match(plants, stations)

    links = run(m, pet)
    pm = prev_month(m)
    prev = run(pm, petrinex_month(client, pm) or {})
    persist = {(e["fac"], e["station"]) for e in prev}
    feats = []
    for e in sorted(links, key=lambda e: -e["plant_mmcfd"]):
        f, s = e["fac"], st_ll[e["station"]]
        info = names.get(f, {})
        p_ll = (aer[f]["lon"], aer[f]["lat"])
        src = bs.AER_CITE.format(fac=f, src=aer[f]["LOC_SOURCE"])
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(p_ll[0], 5), round(p_ll[1], 5)]},
                      "properties": {"id": f"gas.plant.{f.lower()}", "kind": "node", "system": "gas",
                                     "type": "gas_processing", "name": (info.get("name") or f).title(),
                                     "source": src, "facility_id": f, "operator": info.get("operator", ""),
                                     "subtype": info.get("subtype", ""), "value": gp.series_id(f), "unit": "MMcf/d",
                                     "compare": "prev", "panel": {"series": [gp.series_id(f)]},
                                     "band": gp.PLANT_BAND,
                                     "note": PLANT_NOTE}})
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": [[round(p_ll[0], 5), round(p_ll[1], 5)],
                                                                         s[2]["geometry"]["coordinates"]]},
                      "properties": {"id": f"gas.plant_link.{f.lower()}.{e['station']}", "kind": "link", "system": "gas",
                                     "type": "plant_link", "name": f"{(info.get('name') or f).title()} → {s[2]['properties']['name']}",
                                     "schematic": True, "direction": 1,
                                     "source": ("schematic: inferred link, a straight line plant → NGTL station (not a "
                                                "surveyed lateral); endpoints from AER ST102"),
                                     "value": gp.series_id(f), "unit": "MMcf/d", "from": f"gas.plant.{f.lower()}",
                                     "to": s[2]["properties"]["id"], "tc_station": e["station"], "facility_id": f,
                                     "volume_match_pct": e["volume_match_pct"], "km": e["km"], "score": e["score"],
                                     "month": m.isoformat(), "confidence": CONFIDENCE, "persists_prev_month": (f, e["station"]) in persist,
                                     "method": "volume fingerprint ±3 %, unique both ways, < 30 km, score ≥ 4 (gas-matching.md §2)"}})
    vol = sum(e["plant_mmcfd"] for e in links)
    total = sum(v for n, v in station_month(client, m).items() if n in st_ll)
    stats = {"month": m.isoformat(), "links": len(links), "linked_mmcfd": round(vol, 1),
             "ab_station_receipts_mmcfd": round(total, 1), "share_pct": round(100 * vol / total, 1) if total else None,
             "prev_month": pm.isoformat(), "links_prev_month": len(prev),
             "persisting": sum((e["fac"], e["station"]) in persist for e in links)}
    return {"type": "FeatureCollection", "features": feats}, stats


def main() -> None:
    with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": "montney-stack (personal use)"}) as c:
        fc, stats = build(c)
    OUT.write_text(json.dumps(fc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
