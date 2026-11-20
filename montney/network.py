"""The map's network model: nodes, links and areas for gas and power.

Files: `network/<system>/*.geojson` in the repo (gas agent owns network/gas, power owns
network/power). Each is a GeoJSON FeatureCollection in WGS84 lon/lat. Every feature's
`properties` follow `schemas/network.schema.json`; the essentials:

    id        unique, stable, lowercase  e.g. "gas.empress", "gas.ngtl.egat", "power.intertie.bc"
    kind      node | link | area
    system    gas | power | region (region = generated from regions/*.yaml, docs/REGIONS.md)
    type      free-form subtype (border, receipt, storage, compressor, plant, intertie, pipeline,
              transmission, area, hub...)
    name      display name
    source    citation for the geometry (URL or "schematic: <basis>")
    value     optional series id whose latest value the map shows for this feature
    capacity  optional series id (or capacity_value: number) for utilization
    target    optional series id to compare against, e.g. linepack target ("vs target")
    compare   optional: "1h" | "24h" | "7d" | "prev"  (default: "prev" for daily series, "24h" otherwise)
    unit      optional display unit override
    schematic links only: true when the line is drawn by us, not a surveyed route
    direction links only: +1 if positive flow runs along the coordinate order, -1 against
    panel     optional: {"series": [...ids], "events_ref": "<id>"} for the click panel

Everything else in `properties` is passed through to the frontend untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from montney import paths

NETWORK_DIR = paths.REPO_ROOT / "network"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{1,80}$")
KINDS = {"node": {"Point"}, "link": {"LineString", "MultiLineString"},
         "area": {"Polygon", "MultiPolygon"}}
SYSTEMS = {"gas", "power", "region"}     # region: the generated region outlines + boxes (L-106)
COMPARES = {"1h", "24h", "7d", "prev"}


class NetworkError(ValueError):
    pass


def files(root: Path = NETWORK_DIR) -> list[Path]:
    return sorted(root.glob("*/*.geojson")) if root.exists() else []


def _coords_ok(geom: dict) -> bool:
    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return len(c) >= 2 and -180 <= c[0] <= 180 and -90 <= c[1] <= 90
        return isinstance(c, (list, tuple)) and len(c) > 0 and all(walk(x) for x in c)
    return walk(geom.get("coordinates"))


def validate_feature(f: dict, where: str) -> list[str]:
    errs = []
    p = f.get("properties") or {}
    g = f.get("geometry") or {}
    fid = p.get("id")
    tag = f"{where}:{fid or '?'}"
    if not isinstance(fid, str) or not ID_RE.match(fid):
        errs.append(f"{tag}: id must match {ID_RE.pattern}")
    kind = p.get("kind")
    if kind not in KINDS:
        errs.append(f"{tag}: kind must be one of {sorted(KINDS)}")
    elif g.get("type") not in KINDS[kind]:
        errs.append(f"{tag}: {kind} needs geometry {sorted(KINDS[kind])}, got {g.get('type')}")
    if p.get("system") not in SYSTEMS:
        errs.append(f"{tag}: system must be one of {sorted(SYSTEMS)}")
    for k in ("name", "source", "type"):
        if not isinstance(p.get(k), str) or not p.get(k):
            errs.append(f"{tag}: '{k}' is required")
    if not _coords_ok(g):
        errs.append(f"{tag}: coordinates must be [lon, lat] within WGS84 range")
    if "compare" in p and p["compare"] not in COMPARES:
        errs.append(f"{tag}: compare must be one of {sorted(COMPARES)}")
    if kind == "link":
        if not isinstance(p.get("schematic"), bool):
            errs.append(f"{tag}: links need schematic: true|false")
        if p.get("direction", 1) not in (1, -1):
            errs.append(f"{tag}: direction must be 1 or -1")
    for k in ("value", "capacity", "target"):
        if k in p and not (isinstance(p[k], str) and "." in p[k]):
            errs.append(f"{tag}: {k} must be a series id")
    if "capacity_value" in p and not isinstance(p["capacity_value"], (int, float)):
        errs.append(f"{tag}: capacity_value must be a number")
    return errs


def load(root: Path = NETWORK_DIR, strict: bool = True) -> dict:
    """Merge all network files into one FeatureCollection. Raises NetworkError if strict."""
    feats, errs, seen = [], [], {}
    for f in files(root):
        rel = f.relative_to(root).as_posix()
        try:
            fc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errs.append(f"{rel}: invalid JSON ({e})")
            continue
        if fc.get("type") != "FeatureCollection":
            errs.append(f"{rel}: must be a FeatureCollection")
            continue
        for feat in fc.get("features", []):
            e = validate_feature(feat, rel)
            fid = (feat.get("properties") or {}).get("id")
            if fid in seen:
                e.append(f"{rel}:{fid}: duplicate id (also in {seen[fid]})")
            errs += e
            if not e:
                seen[fid] = rel
                feat = {"type": "Feature", "id": fid, "geometry": feat["geometry"],
                        "properties": feat["properties"]}
                feats.append(feat)
    if errs and strict:
        raise NetworkError("\n".join(errs))
    return {"type": "FeatureCollection", "features": feats, "errors": errs}


def etag(fc: dict) -> str:
    return hashlib.sha1(json.dumps(fc, sort_keys=True).encode()).hexdigest()[:16]


def series_refs(fc: dict) -> dict[str, dict[str, str]]:
    """feature id -> {"value": series, "capacity": series} for features that reference series."""
    out = {}
    for f in fc["features"]:
        p = f["properties"]
        refs = {k: p[k] for k in ("value", "capacity", "target") if isinstance(p.get(k), str)}
        if refs:
            out[f["id"]] = refs
    return out
