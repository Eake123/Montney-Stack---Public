"""USGS streamflow for the Columbia hydro box (L-085, Erik: "glance quickly and see if it's running at regular levels").

Two sites only: The Dalles (the system benchmark, below the Snake) and the International Boundary (what BC releases into
the US). The box shows each as % of normal (weather_flow_normal); the reservoir and runoff-forecast numbers are in
weather_columbia_hydro.

Source: USGS Water Services (NWIS), public domain (US federal), no key. One bulk request per run (`sites=` list; the
hosts send `Cache-Control: max-age=900` and no ETag/Last-Modified, so there's no 304 opt-in):
* `USGSRealtime` (sources/weather_usgs.yaml): instantaneous values (IV), parameter 00060, a 25 h window, hourly.
* `USGSDaily` (sources/weather_usgs_daily.yaml): daily values (DV), statistic 00003 (mean), 1995 -> yesterday, every 30 d:
  the history behind "% of normal".
Units: cfs -> m3/s on ingest (x 0.0283168, AGENTS.md). The no-data sentinel (-999999, e.g. ice or equipment) is dropped,
never stored. Timestamps carry their own offset and are stored as naive UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from montney.connectors.base import Connector, Context, FetchResult
from montney.store import Obs, SeriesMeta

IV = "https://waterservices.usgs.gov/nwis/iv/"
DV = "https://waterservices.usgs.gov/nwis/dv/"
CFS = 0.0283168
NODATA = -999000.0          # USGS sentinel is -999999; anything at or below this is missing

# site: (name, river, lat, lon). Coordinates from the USGS site records (2026-11-03).
SITES: dict[str, tuple[str, str, float, float]] = {
    "14105700": ("Columbia River at The Dalles", "Columbia", 45.6083, -121.1899),
    "12399500": ("Columbia River at International Boundary", "Columbia", 49.0007, -117.6294),
}


def rt_id(site: str) -> str:
    return f"usgs.iv.{site}.discharge"


def parse(j: dict) -> dict[str, dict[datetime, float]]:
    """{site: {naive UTC: m3/s}} from an NWIS JSON response (IV or DV), sentinel values dropped."""
    out: dict[str, dict[datetime, float]] = {}
    for ts in (j.get("value") or {}).get("timeSeries") or []:
        site = ts["sourceInfo"]["siteCode"][0]["value"]
        nod = float(ts["variable"].get("noDataValue") or -999999)
        pts = out.setdefault(site, {})
        for block in ts.get("values") or []:
            for v in block.get("value") or []:
                x = float(v["value"])
                if x <= NODATA or x == nod:
                    continue
                t = datetime.fromisoformat(v["dateTime"])
                t = t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t
                pts[t] = round(x * CFS, 3)
    return out


class USGSRealtime(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(rt_id(s), self.id, f"{name} discharge (USGS {s})", "m3/s", "15min", "Rivers / Columbia hydro")
                for s, (name, _, _, _) in SITES.items()]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        j = ctx.get(IV, params={"format": "json", "sites": ",".join(SITES), "parameterCd": "00060",
                                "period": "PT25H", "siteStatus": "all"}).json()
        data = parse(j)
        for s in SITES:
            res.obs += [Obs(rt_id(s), t, v, t) for t, v in data.get(s, {}).items()]
        missing = [s for s in SITES if not data.get(s)]
        if missing:
            res.warnings.append(f"no current values from USGS for {missing}")
        if len(missing) == len(SITES):
            raise RuntimeError("USGS returned no values for any site")
        return res


class USGSDaily(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.start = str((spec.get("params") or {}).get("start", "1995-01-01"))

    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(f"usgs.dv.{s}.discharge", self.id, f"{n} daily mean discharge (USGS {s})", "m3/s", "D",
                           "Rivers / Columbia hydro") for s, (n, _, _, _) in SITES.items()]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        j = ctx.get(DV, params={"format": "json", "sites": ",".join(SITES), "parameterCd": "00060",
                                "statCd": "00003", "startDT": self.start, "siteStatus": "all"}, timeout=120).json()
        data = parse(j)
        for s in SITES:
            for t, v in data.get(s, {}).items():
                d = datetime(t.year, t.month, t.day)
                res.obs.append(Obs(f"usgs.dv.{s}.discharge", d, v, d))
        if not res.obs:
            raise RuntimeError("USGS daily values: nothing returned")
        return res
