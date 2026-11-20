"""NOAA GFS 0.25 deg gridded forecast for the map overlay (temperature, wind, terrain height).

Pulls a server-side crop of each forecast hour from the NOMADS grib-filter, decodes GRIB2 with
`eccodes`, and returns `FetchResult.grids` in the int16 payload format (montney/grids.py):

    t2m  degC  2 m air temperature
    u10  m/s   10 m wind, eastward
    v10  m/s   10 m wind, northward
    z    m     model terrain height (surface geopotential height), static, `times: []`

Plan (docs/research/weather.md section 3.1):
- Steps: every `step_short` h to `short_until` (default 3 h to 120 h), then every `step_long` h
  to `horizon` (default 6 h to 384 h): 85 requests per run.
- Run discovery: newest cycle first. Probing = fetching the last step; a 404 means the run isn't
  complete yet, so try the previous cycle. Stop as soon as we reach the run we already have.
- Pace `pace_s` seconds between requests (NOMADS has no published limit; NWS asks for restraint).
- Any failed step aborts the run (exception), so the previous good run stays on disk.
- Downloaded steps are cached in <home>/cache/gfs/<run>_<extent>/ until the run completes, so a
  retry resumes at the failing step. Retry timing after a failure is the runner's exponential
  per-source backoff (montney/runner.py).
- A run on disk counts as current only if its extent matches the spec, so changing the extent
  forces a re-pull.

Data: NOAA/NWS, public domain (https://www.weather.gov/disclaimer). No key.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from montney import grids, paths
from montney.connectors.base import Connector, Context, FetchResult

FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
MODEL = "gfs_0p25"
NODATA = -32768
SCALE = 0.01

# (shortName, typeOfLevel) -> (var, unit, convert)
FIELDS = {
    ("2t", "heightAboveGround"): ("t2m", "degC", lambda a: a - 273.15),
    ("10u", "heightAboveGround"): ("u10", "m/s", lambda a: a),
    ("10v", "heightAboveGround"): ("v10", "m/s", lambda a: a),
    ("orog", "surface"): ("z", "m", lambda a: a),
}


class RunNotReady(Exception):
    pass


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def split_grib(blob: bytes) -> list[bytes]:
    """Split a concatenated GRIB2 stream into messages (length is in section 0, bytes 8-16)."""
    out, i = [], 0
    while True:
        j = blob.find(b"GRIB", i)
        if j < 0:
            break
        if blob[j + 7] != 2:
            raise ValueError(f"not GRIB edition 2 at byte {j}")
        n = struct.unpack(">Q", blob[j + 8:j + 16])[0]
        msg = blob[j:j + n]
        if len(msg) != n or not msg.endswith(b"7777"):
            raise ValueError(f"truncated GRIB message at byte {j}")
        out.append(msg)
        i = j + n
    return out


def decode(blob: bytes) -> tuple[dict[str, np.ndarray], dict]:
    """GRIB2 bytes -> ({var: 2-D float array, rows S->N, cols W->E}, grid geometry)."""
    import eccodes  # imported lazily so the rest of the app works without it

    fields: dict[str, np.ndarray] = {}
    geom: dict | None = None
    for msg in split_grib(blob):
        h = eccodes.codes_new_from_message(msg)
        try:
            key = (eccodes.codes_get(h, "shortName"), eccodes.codes_get(h, "typeOfLevel"))
            if key not in FIELDS:
                continue
            ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
            eccodes.codes_set(h, "missingValue", 1e20)
            a = eccodes.codes_get_values(h).reshape(nj, ni).astype(np.float64)
            a[a >= 1e19] = np.nan
            if eccodes.codes_get(h, "iScansNegatively"):
                a = a[:, ::-1]
            lat_a = eccodes.codes_get(h, "latitudeOfFirstGridPointInDegrees")
            lat_b = eccodes.codes_get(h, "latitudeOfLastGridPointInDegrees")
            if not eccodes.codes_get(h, "jScansPositively"):  # stored N->S: flip to S->N
                a = a[::-1, :]
            lon_w = min(eccodes.codes_get(h, "longitudeOfFirstGridPointInDegrees"),
                        eccodes.codes_get(h, "longitudeOfLastGridPointInDegrees"))
            g = {"lon0": round((lon_w + 180) % 360 - 180, 6), "lat0": round(min(lat_a, lat_b), 6),
                 "dx": eccodes.codes_get(h, "iDirectionIncrementInDegrees"),
                 "dy": eccodes.codes_get(h, "jDirectionIncrementInDegrees"), "nx": ni, "ny": nj}
            if geom is None:
                geom = g
            elif g != geom:
                raise ValueError(f"grid mismatch between messages: {g} vs {geom}")
            var, _, conv = FIELDS[key]
            fields[var] = conv(a)
        finally:
            eccodes.codes_release(h)
    if geom is None:
        raise ValueError("no usable GRIB messages")
    return fields, geom


def pack(stack: np.ndarray) -> str:
    """float array (NaN = missing) -> base64 little-endian int16 at SCALE."""
    q = np.round(stack / SCALE)
    q = np.where(np.isnan(q), NODATA, np.clip(q, -32767, 32767)).astype("<i2")
    return base64.b64encode(q.tobytes()).decode()


class GFS(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params", {})
        self.lon_min, self.lon_max = float(p.get("lon_min", -140)), float(p.get("lon_max", -95))
        self.lat_min, self.lat_max = float(p.get("lat_min", 44)), float(p.get("lat_max", 65))
        self.step_short, self.short_until = int(p.get("step_short", 3)), int(p.get("short_until", 120))
        self.step_long, self.horizon = int(p.get("step_long", 6)), int(p.get("horizon", 384))
        self.pace = float(p.get("pace_s", 1.0))
        self.min_lag = timedelta(hours=float(p.get("min_lag_h", 3.5)))  # don't probe a run before this
        self.lookback = int(p.get("lookback_cycles", 4))
        # L-080: a second, coarser source (sources/weather_gfs_east.yaml) covers the map east of -95 at 0.5 deg.
        # resolution picks the NOMADS product; var_suffix keeps its grids (t2m_east, ...) and cache apart from the west's.
        self.res = float(p.get("resolution", 0.25))
        if self.res not in (0.25, 0.5):
            raise ValueError(f"resolution {self.res}: GFS grids here are 0.25 or 0.5 deg")
        tag = "0p25" if self.res == 0.25 else "0p50"
        self.model = f"gfs_{tag}"
        self.file_fmt = "gfs.t{hh}z.pgrb2.0p25.f{fh:03d}" if tag == "0p25" else "gfs.t{hh}z.pgrb2full.0p50.f{fh:03d}"
        self.suffix = str(p.get("var_suffix", ""))
        self.url = p.get("url", FILTER if tag == "0p25" else FILTER.replace("0p25", "0p50"))
        self.sleep = time.sleep  # tests replace this
        self._requests = 0

    # ------------------------------------------------------------ planning
    def steps(self) -> list[int]:
        s = list(range(0, self.short_until + 1, self.step_short))
        s += list(range(s[-1] + self.step_long, self.horizon + 1, self.step_long))
        return s

    def cycles(self, now: datetime) -> list[datetime]:
        """Candidate cycles, newest first, that are at least `min_lag` old."""
        now = now.replace(tzinfo=None)
        c = now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)
        out = []
        while len(out) < self.lookback:
            if now - c >= self.min_lag:
                out.append(c)
            c -= timedelta(hours=6)
        return out

    def params(self, run: datetime, fh: int, with_orog: bool) -> dict:
        p = {"dir": f"/gfs.{run:%Y%m%d}/{run:%H}/atmos", "file": self.file_fmt.format(hh=f"{run:%H}", fh=fh),
             "var_TMP": "on", "var_UGRD": "on", "var_VGRD": "on",
             "lev_2_m_above_ground": "on", "lev_10_m_above_ground": "on",
             "subregion": "", "toplat": self.lat_max, "bottomlat": self.lat_min,
             "leftlon": self.lon_min, "rightlon": self.lon_max}
        if with_orog:
            p.update({"var_HGT": "on", "lev_surface": "on"})
        return p

    # ------------------------------------------------------------ download
    def cache_root(self) -> Path:
        d = paths.home() / "cache" / f"gfs{self.suffix}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_dir(self, run: datetime) -> Path:
        ext = f"{self.lon_min:g}_{self.lon_max:g}_{self.lat_min:g}_{self.lat_max:g}"
        return self.cache_root() / f"{run:%Y%m%d%H}_{re.sub(r'[^0-9_.-]', '', ext)}"

    def get_step(self, ctx: Context, run: datetime, fh: int, with_orog: bool = False) -> bytes:
        """One forecast hour, from the partial-run cache if we already have it.

        Downloaded steps are kept until the run completes, so a retry after a mid-run failure
        resumes at the failing step instead of re-pulling the whole run.
        """
        d = self.run_dir(run)
        f = d / f"f{fh:03d}{'_z' if with_orog else ''}.grb2"
        if f.exists():
            return f.read_bytes()
        if self._requests:
            self.sleep(self.pace)
        self._requests += 1
        try:
            r = ctx.get(self.url, params=self.params(run, fh, with_orog), timeout=60, retries=2)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                raise RunNotReady(f"{iso(run)} f{fh:03d} not posted") from e
            raise
        if not r.content.startswith(b"GRIB"):
            raise RunNotReady(f"{iso(run)} f{fh:03d}: response is not GRIB")
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{f.name}.tmp"
        tmp.write_bytes(r.content)
        os.replace(tmp, f)
        return r.content

    def expected_geom(self) -> tuple:
        return (self.lon_min, self.lat_min, round((self.lon_max - self.lon_min) / self.res) + 1,
                round((self.lat_max - self.lat_min) / self.res) + 1)

    def current_run(self) -> str | None:
        """Run on disk, if it was pulled with today's extent (an extent change forces a re-pull)."""
        try:
            for m in grids.catalog():
                if m.get("var") == f"t2m{self.suffix}" and not m.get("prev") and m.get("model") == self.model:
                    if (m["lon0"], m["lat0"], m["nx"], m["ny"]) == self.expected_geom():
                        return m.get("run")
        except Exception:
            pass
        return None

    def _clear_cache(self) -> None:
        for d in self.cache_root().iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    # --------------------------------------------------------------- fetch
    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        self._requests = 0
        now = ctx.now.replace(tzinfo=None)
        have = self.current_run()
        steps = self.steps()
        for run in self.cycles(now):
            if iso(run) == have:
                return res  # newest complete run is already on disk
            try:
                last = self.get_step(ctx, run, steps[-1])  # probe: the last step posts last
            except RunNotReady:
                continue
            # drop partial downloads of other runs; keep this run's (resume)
            for d in self.cache_root().iterdir():
                if d.is_dir() and d != self.run_dir(run):
                    shutil.rmtree(d, ignore_errors=True)
            # A failure raises: the previous run stays, the runner backs off (core, exponential)
            # and the next attempt resumes from the cached steps.
            res.grids = self.pull(ctx, run, steps, last)
            self._clear_cache()
            return res
        res.warnings.append(f"no complete GFS run in the last {self.lookback} cycles")
        return res

    def pull(self, ctx: Context, run: datetime, steps: list[int], last: bytes) -> dict[str, dict]:
        stacks: dict[str, list[np.ndarray]] = {"t2m": [], "u10": [], "v10": []}
        z = geom = None
        for i, fh in enumerate(steps):
            blob = last if fh == steps[-1] else self.get_step(ctx, run, fh, with_orog=(i == 0))
            try:
                fields, g = decode(blob)
            except Exception:
                for f in self.run_dir(run).glob(f"f{fh:03d}*.grb2"):
                    f.unlink(missing_ok=True)  # corrupt: fetch it again next time
                raise
            if geom is None:
                geom = g
            elif g != geom:
                raise ValueError(f"f{fh:03d}: grid changed mid-run")
            if "z" in fields and z is None:
                z = fields["z"]
            for v in stacks:
                if v not in fields:
                    raise ValueError(f"f{fh:03d}: {v} missing from GRIB")
                stacks[v].append(fields[v])
        if z is None:
            raise ValueError("terrain height (orog) missing from first step")
        times = [iso(run + timedelta(hours=fh)) for fh in steps]
        base = {"model": self.model, "run": iso(run), "fetched": iso(ctx.now),
                **geom, "scale": SCALE, "offset": 0.0, "nodata": NODATA}
        units = {v: u for (v, u, _) in FIELDS.values()}
        out = {v + self.suffix: {**base, "var": v + self.suffix, "unit": units[v], "times": times, "data": pack(np.stack(a))}
               for v, a in stacks.items()}
        # Terrain height spans up to ~4,800 m in Canada, beyond int16 at 0.01 m: use 1 m steps.
        q = np.where(np.isnan(z), NODATA, np.round(z)).astype("<i2")
        out["z" + self.suffix] = {**base, "var": "z" + self.suffix, "unit": "m", "times": [], "scale": 1.0,
                    "data": base64.b64encode(q.tobytes()).decode()}
        for p in out.values():
            grids.validate(p)
        return out
