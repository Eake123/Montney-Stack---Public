"""Wildfire alerts from CWFIS (Canadian Wildland Fire Information System, NRCan).

Two WFS layers, both GeoJSON, no key, Open Government Licence - Canada:
  public:hotspots_last24hrs    satellite fire detections, last 24 h (several passes a day)
  public:m3_polygons_current   fire perimeters estimated from hotspots (daily)

Both become `Event(kind="alert")`:

- Hotspots are grouped into grid cells (`cell_deg`, default 0.1 deg, ~11 x 7 km). One event per
  cell, id `<source>:hs:<lat>_<lon>`, stays stable while the fire keeps burning in that cell.
  `extra` carries the centroid, count, max FRP and the points ([lon, lat, frp_MW, iso_utc]) so the
  frontend can draw them.
- Perimeters updated in the last `perimeter_days` (default 7) become one event each, id
  `<source>:m3:<wfs id>`. The geometry goes in `extra.geometry`, rounded to ~100 m and capped at
  `max_vertices` per ring to keep the payload small. (The whole season's perimeters for our extent
  are ~4.4 MB; the last 7 days are typically tens of KB.) CWFIS regenerates these polygons, so a
  perimeter can occasionally reappear under a new id; the old one is then withdrawn.

Severity is by distance to gas/power network nodes and links (areas are ignored):
  <= near_km (10 km)  critical,  <= watch_km (30 km)  warn,  otherwise info.
Only nodes and non-schematic links set severity. Schematic links (`schematic: true`: a drawn
line, not the surveyed route) only mark a corridor: within `corridor_km` (25 km) they're added to
`refs` and the title says "near the X corridor (schematic)", with no distance, and severity stays
info unless a node or surveyed link is close. Fires farther than `context_km` (100 km) from every feature are
dropped, and info-level cells carry no `points`, so the ticker and /api/events stay small.
`refs` lists the features within range, nearest first (max 5). A hotspot cell whose
strongest detection is below `pile_frp` (10 MW fire radiative power) is dropped one level: in
autumn these are mostly slash-pile burns, which come in clusters of small detections.
Distances use a local flat-earth approximation (fine at tens of km); for perimeters they're
measured to the polygon's vertices, or 0 when a node or link vertex lies inside it.

Every fetch returns the complete current set, and `event_scopes` withdraws fires that are gone.
Out of season an empty result is normal. Citation required by the licence: "Canadian Forest
Service. Canadian Wildland Fire Information System (CWFIS), Natural Resources Canada".
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

from montney import network
from montney.connectors.base import Connector, Context, FetchResult
from montney.store import Event

WFS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/ows"
SEV = ["info", "warn", "critical"]
CITATION = ("Canadian Forest Service. Canadian Wildland Fire Information System (CWFIS), "
            "Natural Resources Canada")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ geometry (km, local plane)
def _xy(lon: float, lat: float, lat0: float) -> tuple[float, float]:
    return lon * 111.32 * math.cos(math.radians(lat0)), lat * 110.57


def _seg_dist(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _lines(geom: dict) -> list[list[list[float]]]:
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        return [[c]]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    return []


def dist_km(lon: float, lat: float, geom: dict) -> float:
    """Distance from a point to a Point/LineString/MultiLineString."""
    p = _xy(lon, lat, lat)
    best = math.inf
    for line in _lines(geom):
        pts = [_xy(x[0], x[1], lat) for x in line]
        if len(pts) == 1:
            best = min(best, math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]))
        for a, b in zip(pts, pts[1:]):
            best = min(best, _seg_dist(p, a, b))
    return best


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _polys(geom: dict) -> list[list[list[list[float]]]]:
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def poly_dist_km(poly: dict, feat_geom: dict) -> float:
    """Approximate distance between a fire polygon and a network feature (0 if they overlap)."""
    fpts = [pt for line in _lines(feat_geom) for pt in line]
    for p in _polys(poly):
        if any(_in_ring(x, y, p[0]) and not any(_in_ring(x, y, h) for h in p[1:]) for x, y in fpts):
            return 0.0
    return min((dist_km(v[0], v[1], feat_geom) for p in _polys(poly) for ring in p for v in ring),
               default=math.inf)


def simplify(geom: dict, max_vertices: int) -> dict:
    """Round to 3 decimals (~100 m), drop repeats, decimate rings longer than max_vertices."""
    def ring(r):
        r = [[round(x, 3), round(y, 3)] for x, y, *_ in r]
        r = [p for i, p in enumerate(r) if i == 0 or p != r[i - 1]]
        if len(r) > max_vertices:
            step = math.ceil(len(r) / max_vertices)
            r = r[::step] + ([r[-1]] if (len(r) - 1) % step else [])
        if r[0] != r[-1]:
            r.append(r[0])
        return r if len(r) >= 4 else None
    polys = []
    for p in _polys(geom):
        rings = [x for x in (ring(r) for r in p) if x]
        if rings:
            polys.append(rings)
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


# ------------------------------------------------------------------ connector
class CWFISFires(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params", {})
        self.lon_min, self.lon_max = float(p.get("lon_min", -140)), float(p.get("lon_max", -95))
        self.lat_min, self.lat_max = float(p.get("lat_min", 44)), float(p.get("lat_max", 65))
        self.cell = float(p.get("cell_deg", 0.1))
        self.near_km, self.watch_km = float(p.get("near_km", 10)), float(p.get("watch_km", 30))
        self.pile_frp = float(p.get("pile_frp", 10))
        self.perimeters = bool(p.get("perimeters", True))
        self.perimeter_days = float(p.get("perimeter_days", 7))
        self.max_vertices = int(p.get("max_vertices", 300))
        self.max_points = int(p.get("max_points", 100))
        self.corridor_km = float(p.get("corridor_km", 25))  # schematic links: refs + wording only
        self.context_km = float(p.get("context_km", 100))  # fires farther from the network are dropped
        self.url = p.get("url", WFS)

    def load_features(self) -> list[dict]:
        """Network nodes and links (areas would match everything). Tests replace this."""
        fc = network.load(strict=False)
        return [f for f in fc["features"] if f["properties"].get("kind") in ("node", "link")]

    def wfs(self, ctx: Context, layer: str, cql: str) -> list[dict]:
        r = ctx.get(self.url, params={
            "service": "WFS", "version": "1.1.0", "request": "GetFeature", "typeName": layer,
            "outputFormat": "application/json", "srsName": "EPSG:4326", "CQL_FILTER": cql,
        }, timeout=90)
        return r.json().get("features", [])

    def nearest(self, dist_fn, feats: list[dict]) -> list[tuple[float, dict]]:
        out = sorted(((dist_fn(f["geometry"]), f) for f in feats), key=lambda x: x[0])
        return [x for x in out if math.isfinite(x[0])]

    @staticmethod
    def schematic(f: dict) -> bool:
        p = f["properties"]
        return bool(p.get("schematic")) and p.get("kind") == "link"

    def level(self, d: float, f: dict) -> int:
        """0 info, 1 warn, 2 critical. A schematic link is a drawn line, not the surveyed route, so
        distance to it means little: it only marks a corridor (refs + wording), never raises severity."""
        if self.schematic(f):
            return 0
        return 2 if d <= self.near_km else 1 if d <= self.watch_km else 0

    def grade(self, near: list[tuple[float, dict]], weak: bool) -> tuple[str, list[str], dict | None]:
        lvl = max((self.level(d, f) for d, f in near), default=0)
        if weak:
            lvl = max(0, lvl - 1)
        refs = [f["properties"]["id"] for d, f in near
                if d <= (self.corridor_km if self.schematic(f) else self.watch_km)][:5]
        top = None
        if near:
            # name the feature that drives the severity; then one that's in range (so the title can
            # say "near the X corridor"); surveyed features win ties
            def in_range(d, f):
                return d <= (self.corridor_km if self.schematic(f) else self.watch_km)
            d, f = max(near, key=lambda x: (self.level(*x), in_range(*x), not self.schematic(x[1]), -x[0]))
            pr = f["properties"]
            top = {"id": pr["id"], "name": pr.get("name", pr["id"]), "km": round(d, 1),
                   "schematic": self.schematic(f)}
        return SEV[lvl], refs, top

    def where(self, top: dict | None) -> str | None:
        if not top:
            return None
        if top["schematic"]:  # never quote a distance to a drawn line
            return f"near the {top['name']} corridor (schematic)" if top["km"] <= self.corridor_km else None
        return f"{top['km']:.0f} km from {top['name']}" if top["km"] <= self.watch_km else None

    def in_context(self, near: list[tuple[float, dict]]) -> bool:
        return bool(near) and near[0][0] <= self.context_km

    # ---------------------------------------------------------------- fetch
    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        feats = self.load_features()
        res.events += self.hotspot_events(ctx, feats)
        if self.perimeters:
            res.events += self.perimeter_events(ctx, feats)
        # Both lists are complete: fires missing from this fetch are withdrawn.
        res.event_scopes = [(self.id, "alert")]
        return res

    def hotspot_events(self, ctx: Context, feats: list[dict]) -> list[Event]:
        cql = (f"lat BETWEEN {self.lat_min} AND {self.lat_max} "
               f"AND lon BETWEEN {self.lon_min} AND {self.lon_max}")
        cells: dict[tuple[int, int], list[dict]] = defaultdict(list)
        for f in self.wfs(ctx, "public:hotspots_last24hrs", cql):
            p = f["properties"]
            if p.get("lat") is None or p.get("lon") is None:
                continue
            cells[(math.floor(p["lat"] / self.cell), math.floor(p["lon"] / self.cell))].append(p)
        out = []
        for (ci, cj), pts in sorted(cells.items()):
            lat = sum(p["lat"] for p in pts) / len(pts)
            lon = sum(p["lon"] for p in pts) / len(pts)
            frps = [float(p["frp"]) for p in pts if p.get("frp") is not None]
            max_frp = max(frps) if frps else 0.0
            times = sorted(t for t in (parse_ts(p.get("rep_date")) for p in pts) if t)
            near = self.nearest(lambda g: min(dist_km(p["lon"], p["lat"], g) for p in pts), feats)
            if not self.in_context(near):
                continue  # far from anything we track: not worth a ticker line
            sev, refs, top = self.grade(near, weak=max_frp < self.pile_frp)
            where = self.where(top) or f"near {abs(lat):.2f}{'N' if lat >= 0 else 'S'} {abs(lon):.2f}W"
            n = len(pts)
            # points only for warn/critical cells, to keep /api/events small in season
            keep = 0 if sev == "info" else self.max_points
            pts_sorted = sorted(pts, key=lambda p: -(p.get("frp") or 0))[:keep]
            out.append(Event(
                id=f"{self.id}:hs:{ci}_{cj}", source=self.id, kind="alert",
                title=f"Fire hotspots: {n} detection{'s' if n > 1 else ''} {where} (max FRP {max_frp:.0f} MW)",
                body=(f"Satellite fire detections in the last 24 h ({', '.join(sorted({p.get('agency') or '?' for p in pts}))}). "
                      f"Low single detections in autumn are often slash-pile burning. Source: {CITATION}."),
                url="https://cwfis.cfs.nrcan.gc.ca/interactive-map",
                starts=times[0] if times else None,
                ends=(times[-1] + timedelta(hours=24)) if times else None,
                refs=refs, severity=sev, published=times[-1] if times else None,
                extra={"type": "hotspots", "lat": round(lat, 4), "lon": round(lon, 4), "count": n,
                       "max_frp": round(max_frp, 1), "nearest": top,
                       "points": [[round(p["lon"], 4), round(p["lat"], 4), p.get("frp"), p.get("rep_date")]
                                  for p in pts_sorted]},
            ))
        return out

    def perimeter_events(self, ctx: Context, feats: list[dict]) -> list[Event]:
        since = ctx.now.replace(tzinfo=None) - timedelta(days=self.perimeter_days)
        cql = (f"BBOX(geometry,{self.lon_min},{self.lat_min},{self.lon_max},{self.lat_max},'EPSG:4326') "
               f"AND lastdate >= '{iso(since)}'")
        out = []
        for f in self.wfs(ctx, "public:m3_polygons_current", cql):
            g, p = f.get("geometry"), f.get("properties", {})
            if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            geom = simplify(g, self.max_vertices)
            verts = [v for poly in _polys(geom) for v in poly[0]]
            lon = sum(v[0] for v in verts) / len(verts)
            lat = sum(v[1] for v in verts) / len(verts)
            near = self.nearest(lambda fg: poly_dist_km(geom, fg), feats)
            if not self.in_context(near):
                continue
            sev, refs, top = self.grade(near, weak=False)
            area = float(p.get("area") or 0)
            where = f", {w}" if (w := self.where(top)) else ""
            native = str(f.get("id", "")).rsplit(".", 1)[-1] or f"{lat:.2f}_{lon:.2f}"
            out.append(Event(
                id=f"{self.id}:m3:{native}", source=self.id, kind="alert",
                title=f"Fire perimeter: {area:,.0f} ha{where}",
                body=f"Estimated fire perimeter from {p.get('hcount', '?')} hotspots. Source: {CITATION}.",
                url="https://cwfis.cfs.nrcan.gc.ca/interactive-map",
                starts=parse_ts(p.get("firstdate")), refs=refs, severity=sev,
                # ages out if CWFIS stops updating it (the WFS query uses the same window)
                ends=(last + timedelta(days=self.perimeter_days)) if (last := parse_ts(p.get("lastdate"))) else None,
                published=last,
                extra={"type": "perimeter", "lat": round(lat, 4), "lon": round(lon, 4), "area_ha": area,
                       "hcount": p.get("hcount"), "lastdate": p.get("lastdate"), "nearest": top,
                       "geometry": geom},
            ))
        return out
