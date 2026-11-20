"""The biggest gas facilities, AB + BC, as GeoJSON for the production popups (L-075, Erik):

    GET /api/top-producers?province=ab|bc

Erik: "what I really want to see are the biggest wells … only keep the wells that are producing at the top 80
percentile … there shouldn't be more than >100 wells on the map, so group them". The point is a **facility**, which
groups its wells:
- **AB:** a Petrinex reporting facility (gas plant or gathering system). Its masked sales-gas disposition is its gas
  sent to pipelines (`petrinex.<facility>.sales_gas`, gas_petrinex). Petrinex's public volumetrics have no well-level
  sales gas. Storage sites (gas_petrinex.STORAGE) aren't production and are left out.
- **BC:** a BCER gas processing plant. Its gas delivered out of the plant (`bcer.<facility>.plant_outlet`,
  gas_bcer_production). Plant-to-plant transfers make a few of these overlap.

**Selection:** the facilities at or above the 80th percentile of the latest full month (the newest month in the
province total, `production.<ab|bc>.monthly`, which the connectors write only for a month that passed their guards),
capped at MAX_POINTS by rank. Each one has 24 months of history (`null` = no report that month).

**Nothing here is committed** (personal use only; BCER is access-only; never redistributed). The volumes are read from
the store. The names, operators and locations come from a facility registry that the two connectors write on every
fetch into the user's data dir (`<home>/facilities/<ab|bc>.json`). The store doesn't keep SeriesMeta.extra.
Locations are survey-grid arithmetic (montney/survey.py: DLS ±1 km, BC NTS ±1 km). A facility whose location
doesn't parse keeps its rank but gets no point (counted in `unplaced`).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from montney import paths
from montney.survey import locate

PROVINCES = ("ab", "bc")
SERIES = {"ab": "petrinex.{}.sales_gas", "bc": "bcer.{}.plant_outlet"}
TOTAL = {"ab": "production.ab.monthly", "bc": "production.bc.monthly"}
SOURCE = {"ab": "Petrinex public data (AER), plant/gathering-system sales gas; personal, non-commercial use",
          "bc": "BC Energy Regulator facility reporting, gas plant deliveries; personal use, access only"}
PERCENTILE = 0.80
MAX_POINTS = 100
HISTORY_MONTHS = 24
CACHE_SECONDS = 600
_EPOCH = datetime(1970, 1, 1)
_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ registry (written by the connectors)
def registry_path(province: str, home: Path | None = None) -> Path:
    return (home or paths.home()) / "facilities" / f"{province}.json"


def save_registry(province: str, facilities: dict[str, dict], home: Path | None = None) -> Path:
    """{facility id: {name, operator, location, …}} → <home>/facilities/<province>.json, with lat/lon added.

    Written atomically (temp file + replace) so the API never reads half a file.
    """
    out = {}
    for fid, f in facilities.items():
        pt = locate(f.get("location") or "")
        out[fid] = {**f, "lat": round(pt[0], 5) if pt else None, "lon": round(pt[1], 5) if pt else None}
    p = registry_path(province, home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    written = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    tmp.write_text(json.dumps({"province": province, "written": written, "facilities": out}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_registry(province: str, home: Path | None = None) -> dict[str, dict] | None:
    p = registry_path(province, home)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("facilities") or {}
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------ the GeoJSON
def _month(ts: float) -> datetime:
    return _EPOCH + timedelta(seconds=ts)


def _months_back(last: datetime, n: int) -> list[datetime]:
    out, y, m = [], last.year, last.month
    for _ in range(n):
        out.append(datetime(y, m, 1))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _empty(province: str, reason: str, as_of: str | None = None) -> dict:
    return {"type": "FeatureCollection", "province": province, "unit": "MMcf/d", "as_of": as_of, "basis": None,
            "source": SOURCE[province], "features": [], "reason": reason}


def build(store, province: str, home: Path | None = None, use_cache: bool = True) -> dict:
    """The top producers of `province` ('ab' | 'bc') as a GeoJSON FeatureCollection (see the module docstring)."""
    if province not in PROVINCES:
        raise ValueError(f"province must be one of {PROVINCES}")
    key = (id(store), province, str(home))
    if use_cache:
        with _lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    fc = _build(store, province, home)
    with _lock:
        _cache[key] = (time.monotonic(), fc)
    return fc


def _build(store, province: str, home: Path | None) -> dict:
    reg = load_registry(province, home)
    if reg is None:
        conn = "gas_petrinex" if province == "ab" else "gas_bcer_production"
        return _empty(province, f"no facility list yet: it's written when {conn} next runs")
    total = store.series([TOTAL[province]]).get(TOTAL[province], {})
    if not total.get("ts"):
        return _empty(province, f"no {TOTAL[province]} month stored yet")
    latest = _month(max(total["ts"]))
    months = _months_back(latest, HISTORY_MONTHS)
    ids = {SERIES[province].format(fid.lower()): fid for fid in reg}
    data = store.series(list(ids), start=months[0], end=latest)
    rows = []
    for sid, d in data.items():
        by = {_month(t).replace(day=1): v for t, v in zip(d.get("ts", []), d.get("v", []))}
        v = by.get(latest)
        if v is not None and v > 0:
            rows.append((v, ids[sid], by))
    as_of = f"{latest:%Y-%m}"
    if not rows:
        return _empty(province, f"no facility reported gas for {as_of}", as_of)
    rows.sort(key=lambda r: (-r[0], r[1]))
    n_top = max(1, math.ceil(len(rows) * (1 - PERCENTILE)))      # at or above the 80th percentile
    keep = rows[:min(n_top, MAX_POINTS)]
    feats, unplaced = [], 0
    for rank, (v, fid, by) in enumerate(keep, 1):
        f = reg[fid]
        if f.get("lat") is None or f.get("lon") is None:
            unplaced += 1
            continue
        hist = [by.get(m) for m in months]
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]},
                      "properties": {"id": fid, "rank": rank, "name": f.get("name") or fid,
                                     "operator": f.get("operator") or None, "kind": "facility",
                                     "subtype": f.get("subtype") or None, "location": f.get("location") or None,
                                     "series": SERIES[province].format(fid.lower()),
                                     "latest": round(v, 2),
                                     "history": {"t": [f"{m:%Y-%m}" for m in months],
                                                 "v": [None if x is None else round(x, 2) for x in hist]}}})
    kept = sum(r[0] for r in keep)
    alls = sum(r[0] for r in rows)
    basis = (f"{len(keep)} of {len(rows)} reporting facilities: at or above the {PERCENTILE * 100:.0f}th percentile "
             f"of {as_of}"
             + (f", capped at the top {MAX_POINTS}" if n_top > MAX_POINTS else "")
             + f"; {kept / alls:.0%} of their gas")
    return {"type": "FeatureCollection", "province": province, "unit": "MMcf/d", "as_of": as_of, "basis": basis,
            "source": SOURCE[province], "unplaced": unplaced, "features": feats}
