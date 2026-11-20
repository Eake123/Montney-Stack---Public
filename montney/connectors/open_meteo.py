"""Open-Meteo weather: 16-day daily forecast plus HDD/CDD and a 10-year normal.

This is a `type: python` connector, the escape hatch for anything the declarative
HTTP connector can't express (here: computing degree days, climatological normals
and weighted composites). Free, no API key: https://open-meteo.com

Series per location (daily, ts = local calendar date):
  wx.<loc>.tmax / tmin / tmean   degC
  wx.<loc>.hdd / cdd             degC-days, base `hdd_base` (default 18)
  wx.<loc>.precip                mm
  wx.<loc>.wind                  km/h (max)
  wx.<loc>.hdd_normal / tmean_normal   mean of the same calendar day over `normal_years`
The last `past_days` (default 7) come back with every fetch, so charts show recent "actuals"
(model analysis) next to the forecast.

Realized daily actuals (L-039), for locations and composites:
  wx.<loc>.tmean.realized / hdd.realized   the same values for days that have ENDED (local day before the
                                           API's "today"), stamped as_of = ts so they're actual history
                                           (`daily` tier, kept forever). The plain series are as_of = fetch
                                           hour (`forecast` tier: thinned after 14 d, dropped after 1 y).
realized(D) is the value from the LATEST fetch in which D is still within `past_days` (L-039): each fetch
re-emits its ended days with as_of = ts, so the store overwrites (last write wins), and D settles about
`past_days` days after it ends. Day windows are each location's `tz` (the Etc/GMT+6/+7 city windows), not
L-029's Alberta day. Today and forecast days are never realized.

Composites (weighted averages, e.g. population-weighted Alberta) get the daily series above plus
HDD outlook sums, one point per issue date (ts = local "today"):
  wx.<comp>.hdd_1_7 / hdd_8_15                 sum of forecast HDD, days 1-7 / 8-15 ahead
  wx.<comp>.hdd_1_7_normal / hdd_8_15_normal   the same window from the normals
  wx.<comp>.cdd_1_7 / cdd_8_15 (+ _normal)     the same for cooling degree days (L-106: the summer-peaking regions)
Comparing today's point with yesterday's gives the forecast revision ("+34 HDD vs yesterday").
Each forecast fetch is stored as a new vintage, so you can diff against older runs.

L-106 (regions, docs/REGIONS.md):
* CDD gets the same treatment as HDD: wx.<loc>.cdd_normal and wx.<loc>.cdd.realized (and on composites).
* A composite id may hold dots: `region.<r>` gives the region series wx.region.<r>.* (population-weighted HDD / CDD
  for Gas and Power's demand estimates), one per region file.
* `batch: true`: ONE request per run for all the spec's locations (Open-Meteo takes comma-separated latitude /
  longitude / timezone and answers a list in order), and one archive request for the normals. Each answer's grid point
  must lie within 0.5 deg of its own location, or the fetch fails (never one city's weather on another).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from montney import paths

from montney.connectors.base import Connector, Context, FetchResult, slug
from montney.store import Obs, SeriesMeta

FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

VARS = {
    "tmax": ("temperature_2m_max", "degC", "High"),
    "tmin": ("temperature_2m_min", "degC", "Low"),
    "tmean": ("temperature_2m_mean", "degC", "Mean temp"),
    "precip": ("precipitation_sum", "mm", "Precip"),
    "wind": ("wind_speed_10m_max", "km/h", "Max wind"),
}


def check_offset(j: dict, tz: str, year: int) -> None:
    """For a fixed-offset zone (e.g. Etc/GMT+6), the response must be on exactly that offset.

    Guards against the provider silently bucketing days in another zone. Zones with DST have
    no single expected offset, so they aren't checked.
    """
    z = ZoneInfo(tz)
    jan, jul = (datetime(year, 1, 15, tzinfo=z).utcoffset(), datetime(year, 7, 15, tzinfo=z).utcoffset())
    if jan != jul or "utc_offset_seconds" not in j:
        return
    want = int(jan.total_seconds())
    if int(j["utc_offset_seconds"]) != want:
        raise ValueError(f"Open-Meteo answered utc_offset_seconds={j['utc_offset_seconds']} "
                         f"for timezone={tz} (expected {want})")


# Daily keys a composite carries (weighted over its locations).
COMPOSITE_KEYS = ("tmean", "hdd", "cdd", "hdd_normal", "cdd_normal", "tmean_normal", "tmean.realized", "hdd.realized",
                  "cdd.realized")

class OpenMeteo(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params", {})
        self.locations = [
            {**loc, "id": loc.get("id") or slug(loc["name"])} for loc in p.get("locations", [])
        ]
        self.composites = p.get("composites", [])
        self.days = int(p.get("forecast_days", 16))
        self.base = float(p.get("hdd_base", 18.0))
        self.normal_years = int(p.get("normal_years", 10))
        self.past_days = int(p.get("past_days", 7))
        # Local day boundaries for daily values. A fixed-offset zone (e.g. "Etc/GMT+6" = UTC-6)
        # doesn't depend on the provider's time-zone database being current.
        self.tz = p.get("tz", "America/Edmonton")
        # HDD outlook windows, in days ahead of today: [[1, 7], [8, 15]] -> hdd_1_7, hdd_8_15
        self.windows = [tuple(int(x) for x in w) for w in p.get("hdd_windows", [[1, 7], [8, 15]])]
        self.model = p.get("model")  # e.g. "gfs_seamless", "ecmwf_ifs025"; default = best match
        self.batch = bool(p.get("batch", False))  # L-106: one request for all locations (forecast and normals)
        self._normals: dict[str, dict[tuple[int, int], dict[str, float]]] = {}
        self._normals_day: date | None = None

    # ---------------------------------------------------------------- meta
    def _meta_for(self, loc_id: str, name: str) -> list[SeriesMeta]:
        g = f"Weather / {name}"
        m = [SeriesMeta(f"wx.{loc_id}.{k}", self.id, f"{name} {lbl}", u, "D", g)
             for k, (_, u, lbl) in VARS.items()]
        m += [
            SeriesMeta(f"wx.{loc_id}.hdd", self.id, f"{name} HDD", "degC-d", "D", g),
            SeriesMeta(f"wx.{loc_id}.cdd", self.id, f"{name} CDD", "degC-d", "D", g),
            SeriesMeta(f"wx.{loc_id}.hdd_normal", self.id, f"{name} HDD normal", "degC-d", "D", g),
            SeriesMeta(f"wx.{loc_id}.cdd_normal", self.id, f"{name} CDD normal", "degC-d", "D", g),
            SeriesMeta(f"wx.{loc_id}.tmean_normal", self.id, f"{name} mean temp normal", "degC", "D", g),
            SeriesMeta(f"wx.{loc_id}.tmean.realized", self.id, f"{name} mean temp (realized)", "degC", "D", g),
            SeriesMeta(f"wx.{loc_id}.hdd.realized", self.id, f"{name} HDD (realized)", "degC-d", "D", g),
            SeriesMeta(f"wx.{loc_id}.cdd.realized", self.id, f"{name} CDD (realized)", "degC-d", "D", g),
        ]
        return m

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for loc in self.locations:
            out += self._meta_for(loc["id"], loc["name"])
        for c in self.composites:
            pre = f"wx.{c['id']}."            # a composite id may hold dots (L-106: "region.tx" -> wx.region.tx.*)
            out += [m for m in self._meta_for(c["id"], c["name"]) if m.series_id[len(pre):] in COMPOSITE_KEYS]
            g = f"Weather / {c['name']}"
            for a, b in self.windows:
                for dd in ("hdd", "cdd"):          # cdd windows: L-106 (tx, sunbelt, cal peak in summer; #413 review)
                    out += [
                        SeriesMeta(f"wx.{c['id']}.{dd}_{a}_{b}", self.id, f"{c['name']} {dd.upper()} days {a}-{b}",
                                   "degC-d", "D", g),
                        SeriesMeta(f"wx.{c['id']}.{dd}_{a}_{b}_normal", self.id,
                                   f"{c['name']} {dd.upper()} days {a}-{b} normal", "degC-d", "D", g),
                    ]
        return out

    # --------------------------------------------------------------- fetch
    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0)
        per_loc: dict[str, dict[str, dict[datetime, float]]] = {}
        ended: dict[str, set[datetime]] = {}  # per location: days before the API's local "today"
        issue_day: datetime | None = None  # local "today" per the API (first non-past day)

        today = ctx.now.date()
        if self._normals_day != today and self.normal_years > 0:
            try:
                self._load_normals(ctx)
                self._normals_day = today
            except Exception as e:  # normals are nice-to-have
                res.warnings.append(f"normals: {e}")

        params0 = {"daily": ",".join(v[0] for v in VARS.values()), "forecast_days": self.days,
                   "past_days": self.past_days}
        if self.model:
            params0["models"] = self.model
        pairs = [(loc, loc.get("tz", self.tz)) for loc in self.locations]
        if self.batch:          # L-106: one request for every location of the spec
            js = self._get_many(ctx, FORECAST, params0, pairs)
        else:
            js = [ctx.get(FORECAST, params={**params0, "latitude": loc["lat"], "longitude": loc["lon"],
                                            "timezone": tz}).json() for loc, tz in pairs]
        for (loc, tz), j in zip(pairs, js):
            check_offset(j, tz, ctx.now.year)
            daily = j["daily"]
            loc_today = (datetime.fromisoformat(daily["time"][self.past_days])
                         if len(daily["time"]) > self.past_days else None)
            if issue_day is None:
                issue_day = loc_today
            series: dict[str, dict[datetime, float]] = defaultdict(dict)
            for i, d in enumerate(daily["time"]):
                ts = datetime.fromisoformat(d)
                for k, (api, _, _) in VARS.items():
                    v = daily.get(api, [None] * (i + 1))[i]
                    if v is not None:
                        series[k][ts] = float(v)
                tm = series["tmean"].get(ts)
                if tm is None and ts in series["tmax"] and ts in series["tmin"]:
                    tm = (series["tmax"][ts] + series["tmin"][ts]) / 2
                    series["tmean"][ts] = tm
                if tm is not None:
                    series["hdd"][ts] = max(0.0, self.base - tm)
                    series["cdd"][ts] = max(0.0, tm - self.base)
                nm = self._normals.get(loc["id"], {}).get((ts.month, ts.day))
                if nm:
                    series["hdd_normal"][ts] = nm["hdd"]
                    series["tmean_normal"][ts] = nm["tmean"]
                    if "cdd" in nm:
                        series["cdd_normal"][ts] = nm["cdd"]
            per_loc[loc["id"]] = series
            ended[loc["id"]] = {ts for ts in series["tmean"] if loc_today is not None and self._ended(ts, loc_today)}
            for k in ("tmean", "hdd", "cdd"):
                series[f"{k}.realized"] = {ts: series[k][ts] for ts in ended[loc["id"]] if ts in series[k]}

        for c in self.composites:
            w = c["weights"]
            tot = sum(w.values())
            comp: dict[str, dict[datetime, float]] = defaultdict(dict)
            for k in COMPOSITE_KEYS:
                dates = set.intersection(*[set(per_loc.get(l, {}).get(k, {})) for l in w]) if w else set()
                for ts in dates:
                    comp[k][ts] = sum(per_loc[l][k][ts] * wt for l, wt in w.items()) / tot
            per_loc[c["id"]] = comp
            if issue_day is not None:
                for a, b in self.windows:
                    days = [issue_day + timedelta(days=i) for i in range(a, b + 1)]
                    for src, name in [(f"{dd}{n}", f"{dd}_{a}_{b}{n}") for dd in ("hdd", "cdd") for n in ("", "_normal")]:
                        if all(d in comp[src] for d in days):  # never sum a partial window
                            comp[name][issue_day] = sum(comp[src][d] for d in days)

        for loc_id, series in per_loc.items():
            for k, pts in series.items():
                realized = k.endswith(".realized")
                for ts, v in pts.items():
                    res.obs.append(Obs(f"wx.{loc_id}.{k}", ts, v, ts if realized else as_of))
        return res

    def _get_many(self, ctx: Context, url: str, params: dict, pairs: list, timeout: float = 60) -> list[dict]:
        """One Open-Meteo request for many locations (L-106): comma-separated latitude / longitude / timezone, answered
        as a list in the same order. Each answer's grid point must sit near its own location (so a reordered or short
        answer fails the fetch instead of putting one city's weather on another)."""
        q = {**params, "latitude": ",".join(str(loc["lat"]) for loc, _ in pairs),
             "longitude": ",".join(str(loc["lon"]) for loc, _ in pairs), "timezone": ",".join(tz for _, tz in pairs)}
        j = ctx.get(url, params=q, timeout=timeout).json()
        js = j if isinstance(j, list) else [j]
        if len(js) != len(pairs):
            raise ValueError(f"Open-Meteo answered {len(js)} locations for {len(pairs)}")
        for i, ((loc, _), x) in enumerate(zip(pairs, js)):
            # Exact order: Open-Meteo numbers each answer with its request position (`location_id`, left out for the
            # first). The distance check alone can't tell close pairs apart (Hartford / New Haven, SLC / Ogden; #407 review).
            if int(x.get("location_id", 0)) != i:
                raise ValueError(f"Open-Meteo answer for {loc['name']} carries location_id {x.get('location_id', 0)}, not {i}")
            if abs(float(x["latitude"]) - loc["lat"]) > 0.5 or abs(float(x["longitude"]) - loc["lon"]) > 0.5:
                raise ValueError(f"Open-Meteo answer for {loc['name']} is at {x['latitude']}, {x['longitude']}")
        return js

    # Seeded red (tests/test_weather_realized.py): "le" would let the API's "today" (a forecast) into the realized series.
    _REALIZED_CMP = "lt"

    def _ended(self, ts: datetime, loc_today: datetime) -> bool:
        return ts < loc_today if self._REALIZED_CMP == "lt" else ts <= loc_today

    NORMALS_MAX_AGE = 30 * 86400  # climatology barely moves; don't re-pull 10 years daily

    def _load_normals(self, ctx: Context) -> None:
        end = date.today() - timedelta(days=7)  # archive lags ~5 days
        start = date(end.year - self.normal_years, end.month, end.day)
        cache_dir = paths.home() / "cache"
        cache_dir.mkdir(exist_ok=True)
        todo = []
        for loc in self.locations:
            tz = loc.get("tz", self.tz)
            # _v2: the cached normals carry cdd too (L-106); older files without it are re-pulled once
            cf = cache_dir / (f"normals_{loc['id']}_{loc['lat']}_{loc['lon']}_{self.normal_years}y_b{self.base}"
                              f"_{slug(tz)}_v2.json")
            if cf.exists() and time.time() - cf.stat().st_mtime < self.NORMALS_MAX_AGE:
                raw = json.loads(cf.read_text())
                self._normals[loc["id"]] = {tuple(map(int, k.split("-"))): v for k, v in raw.items()}
            else:
                todo.append((loc, tz, cf))
        if not todo:
            return
        base = {"start_date": start.isoformat(), "end_date": end.isoformat(), "daily": "temperature_2m_mean"}
        if self.batch:          # L-106: one archive request for every location that needs its normals
            js = self._get_many(ctx, ARCHIVE, base, [(loc, tz) for loc, tz, _ in todo], timeout=90)
        else:
            js = [ctx.get(ARCHIVE, params={**base, "latitude": loc["lat"], "longitude": loc["lon"], "timezone": tz},
                          timeout=60).json() for loc, tz, _ in todo]
        for (loc, tz, cf), j in zip(todo, js):
            check_offset(j, tz, ctx.now.year)
            acc: dict[tuple[int, int], list[float]] = defaultdict(list)
            for d, t in zip(j["daily"]["time"], j["daily"]["temperature_2m_mean"]):
                if t is None:
                    continue
                dd = date.fromisoformat(d)
                acc[(dd.month, dd.day)].append(float(t))
            norms = {}
            for md, temps in acc.items():
                # +/-3 day window smooths a 10-sample mean into something usable
                window = []
                for off in range(-3, 4):
                    x = date(2001, md[0], min(md[1], 28) if md[0] == 2 else md[1]) + timedelta(days=off)
                    window += acc.get((x.month, x.day), [])
                window = window or temps
                tmean = sum(window) / len(window)
                hdd = sum(max(0.0, self.base - t) for t in window) / len(window)
                cdd = sum(max(0.0, t - self.base) for t in window) / len(window)
                norms[md] = {"tmean": tmean, "hdd": hdd, "cdd": cdd}
            self._normals[loc["id"]] = norms
            cf.write_text(json.dumps({f"{m}-{d}": v for (m, d), v in norms.items()}))
