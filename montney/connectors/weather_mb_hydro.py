"""Manitoba Hydro water at a glance (L-091, Erik: "once a day kinda thing"): is the system wet/full or dry/low vs normal?
Numbers only, no market call.

Source: Water Survey of Canada (ECCC) OGC API, as weather_wsc (ECCC Data Servers End-use Licence). ONE request a day:
`hydrometric-realtime` for 8 stations at once (`STATION_NUMBER=A|B|...`, the pipe filter), the last 24 h. Normals come
from HYDAT approved daily means (`hydrometric-daily-mean`, 1995 -> 30 days ago), cached on disk and re-read every 30
days (one paged query per station, ~16 requests).

What the box shows (and why these gauges):
* **Lake Winnipeg**, Manitoba Hydro's big storage: the mean of the 4 WSC gauges the province uses for its official
  lake level (Berens River 05RD005, Gimli 05SB006, George Island 05RE003, Mission Point 05SG001), so wind set-up
  cancels. WSC's "Lake Winnipeg datum (1986)" metres match the Hydrologic Forecast Centre's feet (checked 2026-11-02 to
  0.05 ft), so the licence operating range 711-715 ft (216.713-217.932 m) applies directly:
    mb.hydro.lake_winnipeg.level            m, 24 h mean of the 4-gauge mean
    mb.hydro.lake_winnipeg.pct_range        % of the 711-715 ft range (0 = 711 ft, 100 = 715 ft; can go outside 0-100)
    mb.hydro.lake_winnipeg.pct_range_normal the same for the median level for the date (+/-7 d, 1995+)
    mb.hydro.lake_winnipeg.anomaly          m, level - that median
* **Southern Indian Lake** at Missi Falls (06EC006), the Churchill diversion's storage: level + anomaly (m).
* **Inflow index** (a proxy, labelled as such): Saskatchewan River at The Pas (05KJ001) + Churchill River above
  Granville Falls (06EA006), the two big gauged inflows from upstream basins, as % of their summed normal:
    mb.hydro.inflow.pct_normal
  plus each one's discharge and % of normal, and the Burntwood near Thompson (05TG001: the Churchill diversion into the
  Nelson; regulated by Manitoba Hydro at Notigi, so it reflects operations as much as water).
Not available, stated: Nelson River and Winnipeg River discharge (WSC lists them real-time but serves no rows; Manitoba
Hydro posts them only on an HTML page).

Current = the 24 h mean of the provisional 5-min data; normal = the median of approved daily means. NO-RESULT (no row)
under 10 years of history or under 12 readings in the 24 h.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import date, datetime, time as dtime, timedelta

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.weather_flow_normal import alberta_date
from montney.connectors.weather_percentile import window_keys
from montney.connectors.weather_wsc import API, _num, _pages
from montney.store import Obs, SeriesMeta

LW_GAUGES = ("05RD005", "05SB006", "05RE003", "05SG001")
SIL = "06EC006"
INFLOW = ("05KJ001", "06EA006")
DIVERSION = "05TG001"
# station: (name, measure)
STATIONS: dict[str, tuple[str, str]] = {
    "05RD005": ("Lake Winnipeg at Berens River", "LEVEL"),
    "05SB006": ("Lake Winnipeg at Gimli", "LEVEL"),
    "05RE003": ("Lake Winnipeg at George Island", "LEVEL"),
    "05SG001": ("Lake Winnipeg at Mission Point", "LEVEL"),
    "06EC006": ("Southern Indian Lake at Missi Falls", "LEVEL"),
    "05KJ001": ("Saskatchewan River at The Pas", "DISCHARGE"),
    "06EA006": ("Churchill River above Granville Falls", "DISCHARGE"),
    "05TG001": ("Burntwood River near Thompson (Churchill diversion)", "DISCHARGE"),
}
FT = 0.3048
RANGE_M = (711 * FT, 715 * FT)      # Lake Winnipeg licence operating range, 711-715 ft ASL
HIST_START = "1995-01-01"
HIST_MAX_AGE = 30 * 86400
MIN_YEARS = 10
MIN_POINTS = 12
G = "Rivers / Manitoba Hydro"


def pct_range(level_m: float) -> float:
    lo, hi = RANGE_M
    return 100.0 * (level_m - lo) / (hi - lo)


def median_for(hist: dict[date, float], day: date) -> float | None:
    """Median of the daily values within day +/- 7 d (month-day, all years), None under MIN_YEARS years."""
    keys = window_keys(day)
    vals = [(d.year, v) for d, v in hist.items()
            if ((2, 28) if (d.month, d.day) == (2, 29) else (d.month, d.day)) in keys]
    if len({y for y, _ in vals}) < MIN_YEARS:
        return None
    return statistics.median(v for _, v in vals)


def mean24(rows: list[tuple[datetime, float]], end: datetime) -> float | None:
    v = [x for t, x in rows if end - timedelta(hours=24) < t <= end]
    return sum(v) / len(v) if len(v) >= MIN_POINTS else None


class ManitobaHydro(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        lw = "mb.hydro.lake_winnipeg"
        m = [SeriesMeta(f"{lw}.level", self.id, "Lake Winnipeg level, 4-gauge mean (WSC, Lake Winnipeg datum 1986)",
                        "m", "D", G),
             SeriesMeta(f"{lw}.pct_range", self.id, "Lake Winnipeg level, % of the 711-715 ft operating range", "%", "D", G),
             SeriesMeta(f"{lw}.pct_range_normal", self.id,
                        "Lake Winnipeg normal level for the date (median +/-7 d, 1995+), % of the 711-715 ft range",
                        "%", "D", G),
             SeriesMeta(f"{lw}.anomaly", self.id, "Lake Winnipeg level minus its normal for the date", "m", "D", G),
             SeriesMeta("mb.hydro.southern_indian_lake.level", self.id,
                        "Southern Indian Lake level at Missi Falls (WSC 06EC006)", "m", "D", G),
             SeriesMeta("mb.hydro.southern_indian_lake.anomaly", self.id,
                        "Southern Indian Lake level minus its normal for the date (median +/-7 d, 1995+)", "m", "D", G),
             SeriesMeta("mb.hydro.inflow.pct_normal", self.id,
                        "Manitoba inflow index (proxy): Saskatchewan at The Pas + Churchill above Granville Falls, "
                        "% of normal for the date", "%", "D", G)]
        for s in INFLOW + (DIVERSION,):
            m += [SeriesMeta(f"mb.hydro.{s.lower()}.discharge", self.id, f"{STATIONS[s][0]} discharge, 24 h mean (WSC {s})",
                             "m3/s", "D", G),
                  SeriesMeta(f"mb.hydro.{s.lower()}.discharge_pct_normal", self.id,
                             f"{STATIONS[s][0]} discharge, % of normal for the date (median +/-7 d, 1995+)", "%", "D", G)]
        return m

    # ------------------------------------------------------------ history (normals)
    def history(self, ctx: Context, today: date) -> dict[str, dict[date, float]]:
        """{station: {date: value}} of the approved daily means, from a 30-day disk cache or one paged bulk query."""
        cf = paths.home() / "cache" / "wsc_mb_hydro_history.json"
        if cf.exists() and time.time() - cf.stat().st_mtime < HIST_MAX_AGE:
            raw = json.loads(cf.read_text())
            if set(raw) == set(STATIONS):
                return {s: {date.fromisoformat(d): v for d, v in h.items()} for s, h in raw.items()}
        end = (today - timedelta(days=30)).isoformat()   # the last 30 days are never part of their own baseline
        out: dict[str, dict[date, float]] = {s: {} for s in STATIONS}
        for s, (_, measure) in STATIONS.items():          # per station: ~11k rows each, 2 pages (8 together ~ the cap)
            for f in _pages(ctx, "hydrometric-daily-mean", {
                    "f": "json", "STATION_NUMBER": s, "datetime": f"{HIST_START}/{end}", "sortby": "DATE",
                    "properties": "STATION_NUMBER,DATE,LEVEL,DISCHARGE", "skipGeometry": "true"}):
                p = f["properties"]
                v = _num(p.get(measure))
                if p.get("STATION_NUMBER") == s and v is not None:
                    out[s][date.fromisoformat(p["DATE"][:10])] = v
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps({s: {d.isoformat(): v for d, v in h.items()} for s, h in out.items()}))
        return out

    # ------------------------------------------------------------ fetch
    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        end = ctx.now.replace(second=0, microsecond=0)
        feats = _pages(ctx, "hydrometric-realtime", {
            "f": "json", "STATION_NUMBER": "|".join(STATIONS),
            "datetime": f"{(end - timedelta(hours=25)).strftime('%Y-%m-%dT%H:%M:%SZ')}/..",
            "properties": "STATION_NUMBER,DATETIME,LEVEL,DISCHARGE", "skipGeometry": "true"})
        rows: dict[str, list[tuple[datetime, float]]] = {s: [] for s in STATIONS}
        for f in feats:
            p = f["properties"]
            s = p.get("STATION_NUMBER")
            if s in STATIONS:
                v = _num(p.get(STATIONS[s][1]))
                if v is not None:
                    rows[s].append((datetime.fromisoformat(p["DATETIME"].replace("Z", "+00:00")).replace(tzinfo=None), v))
        cur = {s: mean24(r, end) for s, r in rows.items()}
        latest = max((t for r in rows.values() for t, _ in r), default=None)
        if latest is None:
            raise RuntimeError("WSC returned no readings for any Manitoba station")
        # freq D: dated at 00:00 of the Alberta calendar day of the latest reading, as every daily series (the snapshot
        # cuts daily series at 00:00 of today's Alberta date, so a reading-hour stamp would read as a future point).
        day = alberta_date(latest)
        ts = datetime.combine(day, dtime())
        hist = self.history(ctx, day)
        norm = {s: median_for(hist.get(s, {}), day) for s in STATIONS}
        thin = [s for s in STATIONS if cur[s] is None]
        if thin:
            res.warnings.append(f"under {MIN_POINTS} readings in 24 h (NO-RESULT where needed): {thin}")
        put = lambda sid, v, nd=3: res.obs.append(Obs(sid, ts, round(v, nd), ts))

        lw = "mb.hydro.lake_winnipeg"
        if all(cur[s] is not None for s in LW_GAUGES):          # the official mean needs all 4 gauges
            lvl = sum(cur[s] for s in LW_GAUGES) / 4
            put(f"{lw}.level", lvl)
            put(f"{lw}.pct_range", pct_range(lvl), 1)
            if all(norm[s] is not None for s in LW_GAUGES):
                nl = sum(norm[s] for s in LW_GAUGES) / 4
                put(f"{lw}.pct_range_normal", pct_range(nl), 1)
                put(f"{lw}.anomaly", lvl - nl)
        if cur[SIL] is not None:
            put("mb.hydro.southern_indian_lake.level", cur[SIL])
            if norm[SIL] is not None:
                put("mb.hydro.southern_indian_lake.anomaly", cur[SIL] - norm[SIL])
        for s in INFLOW + (DIVERSION,):
            if cur[s] is not None:
                put(f"mb.hydro.{s.lower()}.discharge", cur[s], 1)
                if norm[s]:
                    put(f"mb.hydro.{s.lower()}.discharge_pct_normal", 100.0 * cur[s] / norm[s], 1)
        if all(cur[s] is not None and norm[s] for s in INFLOW):
            put("mb.hydro.inflow.pct_normal", 100.0 * sum(cur[s] for s in INFLOW) / sum(norm[s] for s in INFLOW), 1)
        if not res.obs:
            raise RuntimeError(f"Manitoba hydro: nothing computable: {'; '.join(res.warnings)[:300]}")
        return res
