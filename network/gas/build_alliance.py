"""Build network/gas/alliance.geojson: Alliance Pipeline receipt points (PLAN 4b item 3, L-058).

    uv run python network/gas/build_alliance.py

Source: Pembina's Alliance receipt point listing (POST …/api/ReceiptPointListings/search, no auth). It gives 59 points
(50 AB, 9 BC), each with a legal location:
- AB DLS: "02-13-063-01 W6";
- BC NTS: "D 44-L / 094 A 13";
- BC DLS quarter section: "NW 35-80-16W6".

These are converted with the station layer's arithmetic: DLS calibrated to AER points (p90 0.74 km); NTS is the exact
quarter-unit arithmetic. Every point is `schematic: true`.

Alliance publishes no per-point volumes, so the points are dots with their operator. They carry no value box. The
daily volumes are by zone, on `gas.alliance.receipts` and the Alliance lines (network/gas/build.py).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx

OUT = Path(__file__).with_name("alliance.geojson")
API = "https://content.alliancepipeline.pembina.com/api/ReceiptPointListings/search"
_spec = importlib.util.spec_from_file_location("build_stations", Path(__file__).with_name("build_stations.py"))
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)
QUARTER_LSDS = {"NE": (9, 10, 15, 16), "NW": (11, 12, 13, 14), "SW": (3, 4, 5, 6), "SE": (1, 2, 7, 8)}


def locate(loc: str) -> tuple[float, float, str] | None:
    """Legal location → (lat, lon, kind)."""
    s = " ".join(loc.split())
    s = re.sub(r"(W\d)M$", lambda m: m.group(1), s)  # "W6M" → "W6"
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{2,3})-(\d{1,2})[ -]?W(\d)$", s)               # LSD DLS
    if m:
        ll = bs.dls(f"{int(m[1]):02d}-{int(m[2]):02d}-{int(m[3]):03d}-{int(m[4]):02d}W{m[5]}")
        return (*ll, "dls_approx") if ll else None
    m = re.match(r"^(NE|NW|SE|SW) (\d{1,2})-(\d{1,3})-(\d{1,2})[ -]?W(\d)$", s)             # quarter section
    if m:
        pts = [bs.dls(f"{q:02d}-{int(m[2]):02d}-{int(m[3]):03d}-{int(m[4]):02d}W{m[5]}") for q in QUARTER_LSDS[m[1]]]
        return sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4, "dls_approx"
    m = re.match(r"^([A-D]) (\d{1,3})-([A-L]) / (\d{3}) ([A-P]) (\d{1,2})$", s)               # BC NTS
    if m:
        ll = bs.nts(f"{m[1]}-{int(m[2]):03d}-{m[3]}/{m[4]}-{m[5]}-{int(m[6]):02d}")
        return (*ll, "nts_approx") if ll else None
    return None


def build(client: httpx.Client) -> tuple[dict, dict]:
    body = {"sortFields": [], "filterFields": [], "page": 0, "pageSize": 500, "isFirstSearch": False}
    items = client.post(API, json=body).json()["items"]
    feats, missed = [], []
    for it in sorted(items, key=lambda x: x["locId"]):
        ll = locate(it["location"])
        if not ll:
            missed.append(f"{it['locId']} {it['locNm']} {it['location']!r}")
            continue
        lat, lon, kind = ll
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
                      "properties": {"id": f"gas.alliance.rp.{it['locId'].lower()}", "kind": "node", "system": "gas",
                                     "type": "receipt", "name": f"Alliance {it['locNm'].title()} ({it['locId']})",
                                     "source": ("Alliance receipt point listing (content.alliancepipeline.pembina.com "
                                                f"ReceiptPointListings), legal location {it['location']}; converted "
                                                "by survey-grid arithmetic (DLS calibrated to AER points, p90 0.7 km; "
                                                "NTS unverified); schematic"),
                                     "schematic": True, "location_quality": kind, "alliance_point": it["locId"],
                                     "province": it["locId"][:2], "operator": (it.get("facilityOperator") or "").strip(),
                                     "context": True}})
    return {"type": "FeatureCollection", "features": feats}, {"points": len(items), "placed": len(feats), "missed": missed}


def main() -> None:
    with httpx.Client(timeout=60, headers={"User-Agent": "montney-stack (personal use)"}) as c:
        fc, stats = build(c)
    OUT.write_text(json.dumps(fc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
