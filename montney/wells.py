"""Every reporting gas facility, AB + BC, as map nodes for the main map (L-106 8b, Erik: "that petrinex well data on AB
+ BC, make it so it's not a separate tab, and instead on the main screen").

    GET /api/wells          lazy-loaded by the frontend from zoom tier 2

The points are the same facilities as /api/top-producers (montney/top_producers.py: AB Petrinex reporting facilities'
masked sales gas, BC BCER gas plants' outlet gas; storage sites left out), but **all** of them that reported gas in the
latest month, as `/api/network`-style node features, so the map draws them with the other nodes and a click opens the
facility's monthly history (`panel.series`, read through /api/series).

- **Runtime only, never committed** (Petrinex / BCER: personal use, never redistributed). Built from the facility
  registry the connectors write into the user's data dir (`<home>/facilities/<ab|bc>.json`) plus the store, and kept
  out of `/api/network` so the always-loaded network payload doesn't carry licensed data or change with every fetch.
- **Value inline:** each feature carries its latest month's value (`v`, `ts` = the month's first day as epoch seconds,
  `stale`). The snapshot's zoom scope never lists them (700 lite entries per poll would undo L-081), and the endpoint's
  ETag changes when the data does. A click still resolves: /api/snapshot?features= falls back to this collection.
- **Tier from rank:** the top TOP_N per province by the latest month (the popup's set) at tier 2, the rest at tier 3,
  so ~650 Alberta dots don't bury the pipelines at zoom 6.75.
- **No duplicates:** a facility already placed by hand in network/gas (a node whose series is its petrinex.<fid>.* /
  bcer.<px>.*) is left out.
- `licence: "personal"` on every feature: Frontend keeps them out of committed proof images.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

from montney import httpcache
from montney.connectors.base import utcnow
from montney.top_producers import (MAX_POINTS, PROVINCES, SERIES, SOURCE, TOTAL, _EPOCH, _month, load_registry,
                                   registry_path)

TOP_N = MAX_POINTS              # tier 2: the popup's set (top 100 per province); the rest tier 3
STALE_DAYS = 130                # a monthly report older than this is stale (like the snapshot's monthly sources)
CACHE_SECONDS = 600
_cache: dict = {}
_lock = threading.Lock()
_FACILITY_REF = re.compile(r"^(?:petrinex\.(ab[a-z]{2}\d{7})\.sales_gas|bcer\.(bc[a-z]{2}\d+)\.plant_outlet)$")


def placed_by_hand(network_fc: dict) -> set[str]:
    """Facility ids (upper case) that already have a hand-placed node: any series ref in network/gas naming them."""
    out: set[str] = set()
    for f in network_fc.get("features", []):
        p = f.get("properties", {})
        refs = [p.get(k) for k in ("value", "capacity", "target")] + list((p.get("panel") or {}).get("series") or [])
        for r in refs:
            m = _FACILITY_REF.match(r) if isinstance(r, str) else None
            if m:
                out.add((m.group(1) or m.group(2)).upper())
    return out


def _province(store, province: str, skip: set[str], home: Path | None) -> tuple[list[dict], dict]:
    reg = load_registry(province, home)
    info = {"facilities": 0, "unplaced": 0, "as_of": None}
    if not reg:
        info["reason"] = "no facility list yet"
        return [], info
    total = store.series([TOTAL[province]]).get(TOTAL[province], {})
    if not total.get("ts"):
        info["reason"] = f"no {TOTAL[province]} month stored yet"
        return [], info
    latest_ts = max(total["ts"])
    latest = _month(latest_ts)
    prov_total = dict(zip(total["ts"], total.get("v", []))).get(latest_ts)
    ids = {SERIES[province].format(fid.lower()): fid for fid in reg if fid.upper() not in skip}
    data = store.series(list(ids), start=latest, end=latest)
    rows = []
    for sid, d in data.items():
        by = {_month(t).replace(day=1): v for t, v in zip(d.get("ts", []), d.get("v", []))}
        v = by.get(latest)
        if v is not None and v > 0:
            rows.append((v, ids[sid], sid))
    rows.sort(key=lambda r: (-r[0], r[1]))
    month_ts = (latest.replace(day=1) - _EPOCH).total_seconds()
    stale = utcnow() - latest > timedelta(days=STALE_DAYS)
    feats = []
    for rank, (v, fid, sid) in enumerate(rows, 1):
        f = reg[fid]
        if f.get("lat") is None or f.get("lon") is None:
            info["unplaced"] += 1
            continue
        feats.append({"type": "Feature", "id": f"gas.well.{province}.{fid.lower()}",
                      "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]},
                      "properties": {"id": f"gas.well.{province}.{fid.lower()}", "kind": "node", "system": "gas",
                                     "type": "facility", "name": f.get("name") or fid,
                                     "operator": f.get("operator") or None, "province": province,
                                     "facility_id": fid, "subtype": f.get("subtype") or None,
                                     "value": sid, "unit": "MMcf/d", "freq": "M", "agg": "month",
                                     "panel": {"series": [sid]}, "rank": rank,
                                     "share": round(v / prov_total, 5) if prov_total else None,
                                     "tier": 2 if rank <= TOP_N else 3, "licence": "personal",
                                     "v": round(v, 3), "ts": month_ts, "stale": stale}})
    info.update(facilities=len(feats), as_of=f"{latest:%Y-%m}")
    return feats, info


def collection(store, network_fc: dict, home: Path | None = None) -> dict:
    """The FeatureCollection (cached CACHE_SECONDS, rebuilt sooner when a registry file is rewritten)."""
    key = (id(store), str(home), tuple(_mtime(registry_path(p, home)) for p in PROVINCES))
    with _lock:
        hit = _cache.get("fc")
    if hit and hit[0] == key and time.monotonic() - hit[1] < CACHE_SECONDS:
        return hit[2]
    skip = placed_by_hand(network_fc)
    feats, meta = [], {}
    for p in PROVINCES:
        f, meta[p] = _province(store, p, skip, home)
        feats += f
    fc = {"type": "FeatureCollection", "licence": "personal", "unit": "MMcf/d",
          "source": {p: SOURCE[p] for p in PROVINCES}, "provinces": meta, "features": feats}
    with _lock:
        _cache["fc"] = (key, time.monotonic(), fc)
    return fc


def body(store, network_fc: dict, home: Path | None = None) -> httpcache.Body:
    """The gzipped body + a content ETag (so a client revalidates to a 304 until the facilities or values change)."""
    fc = collection(store, network_fc, home)
    with _lock:
        hit = _cache.get("body")
    if hit and hit[0] is fc:                        # the same cached collection: the same bytes
        return hit[1]
    raw = httpcache.encode(fc)
    b = httpcache.Body(gzip.compress(raw, httpcache.GZIP_LEVEL), 'W/"' + hashlib.sha1(raw).hexdigest()[:20] + '"')
    with _lock:
        _cache["body"] = (fc, b)
    return b


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None
