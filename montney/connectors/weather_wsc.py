"""Water Survey of Canada (ECCC) hydrometric gauges: real-time 5-min and approved daily means (PLAN P5d).

Source: the OGC API at api.weather.gc.ca (ECCC Data Servers End-use Licence; attribution "Data Source: Environment and
Climate Change Canada", many BC gauges are BC Hydro-contributed). Research and gauge choice:
docs/research/v2/weather.md (P2) and docs/research/v2/weather-p5d.md §3.1.

Two connectors share one gauge table (GAUGES):

* `WSCRealtime` (sources/weather_wsc.yaml): `hydrometric-realtime`, one request per gauge with a `lookback` window
  (overlap is harmless: same (series, ts, as_of) overwrites, unchanged values write nothing). `DATETIME` is UTC; the
  fixed-offset `DATETIME_LST` is never parsed (it won't follow Alberta's 2026-11-01 move to UTC-6).
    wsc.realtime.<stn>.discharge   m3/s
    wsc.realtime.<stn>.elevation   m, CGVD2013, where HYDAT has a conversion factor (DECISIONS P5d)
    wsc.realtime.<stn>.level       m, the gauge's own (assumed) datum, everywhere else
* `WSCDaily` (sources/weather_wsc_daily.yaml): `hydrometric-daily-mean` (HYDAT, approved), discharge only, from
  `start` (1995-01-01: the percentile reference window starts there) to the latest approved day. Every run re-reads
  the whole range, so the first run is the backfill and later runs pick up each quarterly HYDAT release (and any
  revision) for free.
    wsc.daily.<stn>.discharge      m3/s, daily (the station's LST day), date_only

All points are actuals: as_of = ts. A gauge that fails is a warning, not a failed source; if every gauge fails, the
fetch raises (fail closed).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.store import Obs, SeriesMeta

API = "https://api.weather.gc.ca/collections"
PAGE = 10000           # the server caps `limit` at 10,000 rows per page
MAX_PAGES = 10

# id: (name, river group, has live discharge, CGVD2013 factor or None). The 30 WSC gauges of weather-p5d.md §3.1,
# plus the AB hydro rivers of PLAN step 5 (Bow mouth, Red Deer, Oldman; docs/research/v2/weather-rivers-ab.json).
# Factors: HYDAT 2026-10-19 STN_DATUM_CONVERSION, ASSUMED -> CGVD2013 (epoch 2010), verified by the Reviewer (#117).
GAUGES: dict[str, tuple[str, str, bool, float | None]] = {
    "07EF002": ("Williston Lake at Lost Cabin Creek", "Peace", False, 630.018),
    "07EF003": ("Williston Lake near Schooler Creek", "Peace", False, 630.060),
    "07FA004": ("Peace River above Pine River", "Peace", True, 405.891),
    "07FD002": ("Peace River near Taylor", "Peace", True, 400.278),
    "07FD010": ("Peace River above Alces River", "Peace", True, None),
    "07FD003": ("Peace River at Dunvegan Bridge", "Peace", True, None),
    "07HA001": ("Peace River at Peace River", "Peace", True, None),
    "07HF001": ("Peace River at Fort Vermilion", "Peace", True, None),
    "07FA006": ("Halfway River near Farrell Creek", "Peace", True, None),
    "07FB008": ("Moberly River near Fort St. John", "Peace", True, None),
    "07FB001": ("Pine River at East Pine", "Peace", True, None),
    "07FC001": ("Beatton River near Fort St. John", "Peace", True, None),
    "07FD001": ("Kiskatinaw River near Farmington", "Peace", True, None),
    "07FD007": ("Pouce Coupe River below Henderson Creek", "Peace", True, None),
    "07GE001": ("Wapiti River near Grande Prairie", "Peace", True, None),
    "07GJ001": ("Smoky River at Watino", "Peace", True, None),
    "07GH002": ("Little Smoky River near Guy", "Peace", True, None),
    "07HC001": ("Notikewin River at Manning", "Peace", True, None),
    "05BB001": ("Bow River at Banff", "Bow", True, None),
    "05BC001": ("Spray River at Banff", "Bow", True, None),
    "05BH005": ("Bow River near Cochrane", "Bow", True, None),
    "05BH004": ("Bow River at Calgary", "Bow", True, None),
    "05BJ010": ("Elbow River at Sarcee Bridge", "Bow", True, None),
    "05BJ001": ("Elbow River below Glenmore Dam", "Bow", True, None),
    "05BM002": ("Bow River below Carseland Dam", "Bow", True, None),
    "05DA009": ("North Saskatchewan River at Whirlpool Point", "N. Saskatchewan", True, None),
    "05DD007": ("Brazeau River below Cardinal River", "N. Saskatchewan", True, None),
    "05DC001": ("North Saskatchewan River near Rocky Mountain House", "N. Saskatchewan", True, None),
    "05DE010": ("North Saskatchewan River at Highway No. 759", "N. Saskatchewan", True, None),
    "05DF001": ("North Saskatchewan River at Edmonton", "N. Saskatchewan", True, None),
    # PLAN step 5, AB rivers with hydro (2026-11-02): the main stems below the hydro plants, all live with Q
    "05BN012": ("Bow River near the Mouth", "Bow", True, None),
    "05CA009": ("Red Deer River below Burnt Timber Creek", "Red Deer", True, None),     # above Dickson Dam
    "05CB007": ("Dickson Dam Tunnel Outlet", "Red Deer", True, None),                   # = the dam release
    "05CC002": ("Red Deer River at Red Deer", "Red Deer", True, None),
    "05CK004": ("Red Deer River near Bindloss", "Red Deer", True, None),
    "05AA035": ("Oldman River at Range Road No. 13A", "Oldman", True, None),           # above the Oldman Reservoir
    "05AA024": ("Oldman River near Brocket", "Oldman", True, None),                     # ~ the Oldman Dam release
    "05AE006": ("St. Mary River near Lethbridge", "Oldman", True, None),
    "05AD007": ("Oldman River near Lethbridge", "Oldman", True, None),
    "05AG006": ("Oldman River near the Mouth", "Oldman", True, None),
    # PLAN step 5, Columbia (2026-11-02; docs/research/v2/weather-rivers-columbia.json). No live outflow is published
    # below Mica, Revelstoke or Keenleyside (08ND025/08NE126 are RT=0), so those reaches have levels/heads, not flows.
    "08NA002": ("Columbia River at Nicholson", "Columbia", True, None),
    "08NB005": ("Columbia River at Donald", "Columbia", True, None),                  # into Kinbasket (Mica)
    "08NB017": ("Kinbasket Lake below Garrett Creek", "Columbia", False, None),      # Mica forebay, mid-lake; GSC geodetic
    "08ND026": ("Revelstoke Reservoir at Martha Creek", "Columbia", False, 546.214),   # HYDAT ASSUMED -> CGVD2013 (#117)
    "08ND013": ("Illecillewaet River at Greeley", "Columbia", True, None),            # enters below Revelstoke Dam
    "08NE104": ("Arrow Reservoir at Nakusp", "Columbia", False, 0.317),               # HYDAT GSC (1970 adj.) -> CGVD2013
    "08NE102": ("Arrow Reservoir at Fauquier", "Columbia", False, None),              # Keenleyside forebay side; GSC
    "08NE049": ("Columbia River at Birchbank", "Columbia", True, None),               # below Keenleyside + the Kootenay
    "08NG065": ("Kootenay River at Fort Steele", "Kootenay", True, None),             # above Koocanusa (Libby, US)
    "08NH127": ("Duncan Reservoir at Duncan Dam", "Kootenay", False, None),           # GSC (local 1945 adj.)
    "08NH118": ("Duncan River below Lardeau River", "Kootenay", True, None),          # minus 08NH007 = Duncan Dam release
    "08NH007": ("Lardeau River at Marblehead", "Kootenay", True, None),               # live although metadata says RT=0
    "08NH064": ("Kootenay Lake at Queens Bay", "Kootenay", False, 0.319),             # HYDAT GSC (1928 adj.) -> CGVD2013
    "08NJ013": ("Slocan River near Crescent Valley", "Kootenay", True, None),         # joins the Kootenay above Brilliant
}


# Gauges WSC runs only in open water. Window = (latest season start, earliest season end) seen in HYDAT 2019-2024, so a
# day inside it with no data is a real gap ("stale"), and a day outside it is "seasonal, no data". 05BM002 and 07HF001
# reported through 2024 and on 2026-11-01, so they're year-round.
SEASONAL: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "05BH005": ((3, 1), (10, 31)),
    "05DD007": ((5, 1), (10, 31)),
    "05DE010": ((5, 1), (10, 31)),
}


def in_season(stn: str, d) -> bool:
    """True if `stn` is year-round, or `d` (a date) falls inside its open-water window."""
    if stn not in SEASONAL:
        return True
    (m0, d0), (m1, d1) = SEASONAL[stn]
    return (m0, d0) <= (d.month, d.day) <= (m1, d1)


# Daily-history-only WSC stations (not real-time): the like-for-like release points of the AB Rivers TransAlta
# outflows, for their "% of normal" (weather_flow_normal, L-066). WSC station -> (name, river, the AB Rivers station
# whose release it measured). Horseshoe (TAU-004) and Upper Kananaskis (05BF005) have no like-for-like WSC history.
HISTORY_ONLY: dict[str, tuple[str, str, str]] = {
    "05BD004": ("Cascade Power Diversion near Banff", "Bow", "TAU-002"),
    "05BE999": ("Ghost Tailrace", "Bow", "05BE005"),
    "05BH008": ("Bow River below Bearspaw Dam", "Bow", "05BH010"),
    "05BF025": ("Kananaskis River below Barrier Dam", "Bow", "05BF024"),
    "05BF003": ("Kananaskis River above Pocaterra Creek", "Bow", "05BF009"),      # below the Pocaterra outlet
    "05DC010": ("North Saskatchewan River below Bighorn Plant", "N. Saskatchewan", "TAU-001"),
    "05DD005": ("Brazeau River below Brazeau Plant", "N. Saskatchewan", "05DD006"),
}


def _stations(spec: dict, need_q: bool = False) -> list[str]:
    ids = (spec.get("params") or {}).get("stations") or list(GAUGES)
    unknown = [s for s in ids if s not in GAUGES]
    if unknown:
        raise ValueError(f"unknown WSC station(s) {unknown}: add them to GAUGES with their datum factor")
    return [s for s in ids if GAUGES[s][2] or not need_q]


def level_measure(stn: str) -> str:
    return "elevation" if GAUGES[stn][3] is not None else "level"


def _num(v) -> float | None:
    return None if v is None else round(float(v), 3)   # daily means carry float32 noise (6.986999988555908)


def _pages(ctx: Context, collection: str, params: dict) -> list[dict]:
    """All features of a query, following offset pages. Raises if the page cap is hit (never silently truncate)."""
    out: list[dict] = []
    for page in range(MAX_PAGES):
        j = ctx.get(f"{API}/{collection}/items", params={**params, "limit": PAGE, "offset": page * PAGE}).json()
        feats = j.get("features") or []
        out += feats
        if len(feats) < PAGE:
            return out
    raise ValueError(f"{collection} {params.get('STATION_NUMBER')}: more than {MAX_PAGES * PAGE} rows; narrow the range")


class _Base(Connector):
    def _run(self, ctx: Context, stations: list[str], one) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        failed = []
        for stn in stations:
            try:
                res.obs += one(ctx, stn)
            except Exception as e:  # one bad gauge must not drop the other 29
                failed.append(stn)
                res.warnings.append(f"{stn}: {type(e).__name__}: {e}")
        if stations and len(failed) == len(stations):
            raise RuntimeError(f"every WSC gauge failed: {'; '.join(res.warnings)[:300]}")
        return res


# L-081: three regional bboxes (W, S, E, N) instead of one request per gauge: 3 requests an hour, not 108. Together they
# contain all 54 gauges (Peace incl. Williston; AB rivers; Columbia/Kootenay). Overlaps are harmless: rows are keyed by
# (series, ts). Measured 2026-11-03 for a 3 h window: 1,193 + 2,662 + 2,596 rows, ~1.15 MB, each well under one page.
REGIONS = ((-124.5, 54.9, -115.5, 58.6), (-117.0, 48.9, -110.0, 53.8), (-119.5, 48.9, -114.5, 52.6))


class WSCRealtime(_Base):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params") or {}
        self.stations = _stations(spec)
        self.lookback = timedelta(hours=float(p.get("lookback_h", 3)))
        self.regions = [tuple(map(float, r)) for r in p.get("regions") or REGIONS]

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for s in self.stations:
            name, river, has_q, factor = GAUGES[s]
            g = f"Rivers / {river}"
            if has_q:
                out.append(SeriesMeta(f"wsc.realtime.{s.lower()}.discharge", self.id, f"{name} discharge (WSC {s})",
                                      "m3/s", "5min", g))
            lbl = "water elevation, CGVD2013" if factor is not None else "water level (gauge datum)"
            out.append(SeriesMeta(f"wsc.realtime.{s.lower()}.{level_measure(s)}", self.id, f"{name} {lbl} (WSC {s})",
                                  "m", "5min", g))
        return out

    def _obs(self, stn: str, p: dict) -> list[Obs]:
        _, _, has_q, factor = GAUGES[stn]
        ts = datetime.fromisoformat(p["DATETIME"].replace("Z", "+00:00")).replace(tzinfo=None)
        q, lv = _num(p.get("DISCHARGE")), _num(p.get("LEVEL"))
        out = []
        if has_q and q is not None:
            out.append(Obs(f"wsc.realtime.{stn.lower()}.discharge", ts, q, ts))
        if lv is not None:
            out.append(Obs(f"wsc.realtime.{stn.lower()}.{level_measure(stn)}", ts,
                           round(lv + factor, 3) if factor is not None else lv, ts))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        start = (ctx.now - self.lookback).strftime("%Y-%m-%dT%H:%M:%SZ")
        want, seen, failed = set(self.stations), {}, 0
        for r in self.regions:
            try:
                feats = _pages(ctx, "hydrometric-realtime", {
                    "f": "json", "bbox": ",".join(f"{x:g}" for x in r), "datetime": f"{start}/..",
                    "properties": "STATION_NUMBER,DATETIME,LEVEL,DISCHARGE", "skipGeometry": "true"})
            except Exception as e:  # one region failing must not drop the others
                failed += 1
                res.warnings.append(f"bbox {r}: {type(e).__name__}: {e}")
                continue
            for f in feats:
                p = f["properties"]
                stn = p.get("STATION_NUMBER")
                if stn in want:
                    for o in self._obs(stn, p):
                        seen[(o.series_id, o.ts)] = o        # overlapping regions: one row per (series, ts)
        if self.regions and failed == len(self.regions):
            raise RuntimeError(f"every WSC region failed: {'; '.join(res.warnings)[:300]}")
        res.obs = list(seen.values())
        return res


class WSCDaily(_Base):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params") or {}
        self.stations = _stations(spec, need_q=True)
        if not p.get("stations"):                       # the shipped spec also loads the history-only stations
            self.stations += list(HISTORY_ONLY)
        self.start = str(p.get("start", "1995-01-01"))

    def declared_series(self) -> list[SeriesMeta]:
        name = lambda s: GAUGES[s][0] if s in GAUGES else HISTORY_ONLY[s][0]
        grp = lambda s: GAUGES[s][1] if s in GAUGES else HISTORY_ONLY[s][1]
        return [SeriesMeta(f"wsc.daily.{s.lower()}.discharge", self.id,
                           f"{name(s)} daily mean discharge, approved (WSC {s})", "m3/s", "D",
                           f"Rivers / {grp(s)}") for s in self.stations]

    def _one(self, ctx: Context, stn: str) -> list[Obs]:
        feats = _pages(ctx, "hydrometric-daily-mean", {
            "f": "json", "STATION_NUMBER": stn, "datetime": f"{self.start}/..", "sortby": "DATE",
            "properties": "STATION_NUMBER,DATE,DISCHARGE", "skipGeometry": "true"})
        sid = f"wsc.daily.{stn.lower()}.discharge"
        out = []
        for f in feats:
            p = f["properties"]
            q = _num(p.get("DISCHARGE"))
            if p.get("STATION_NUMBER") != stn or q is None:   # HYDAT pads the end of a period with nulls
                continue
            d = datetime.fromisoformat(p["DATE"][:10])
            out.append(Obs(sid, d, q, d))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        return self._run(ctx, self.stations, self._one)
