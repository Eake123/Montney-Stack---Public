"""New England (ISO-NE) generation by fuel, load (system + 8 zones) and interchange per external tie, hourly
(PLAN 9 item 1: the upne box's generation under 2 h old; EIA-930 runs ~33 h late).

Source: ISO Express CSVs, keyless, one file per Eastern day, ``https://www.iso-ne.com/transform/csv/<report>
?start=YYYYMMDD&end=YYYYMMDD`` (without ``end`` most reports answer 500):

  genfuelmix                    generation by fuel category, irregular snapshots (gaps ~1.5-11 min), lag ~3 min
  fiveminutesystemload          Total Load, Native Load, Asset Related Load (+ "with estimated solar"), lag ~1 min
  fiveminuteestimatedzonalload  Estimated Native Load for the 8 load zones, lag ~12-15 min
  fiveminuteinterchange         Actual Interchange per external node (7), lag ~6 min

Session handshake (the fragile part): ``/transform/csv`` answers 403 (a 47-byte S3 "fof" page) without the
``isox_token`` cookie, which any ISO Express page sets. The runner keeps one httpx client (and its cookie jar) for the
process, so a token from an earlier run is reused; on a 403 or a body that isn't the CSV grammar the connector drops the
iso-ne.com cookies, loads the warm-up page once (~93 KB) and retries. A warm-up that fails or sets no token, or a CSV
that is still refused after it, fails closed (FormatDrift), never a partial store.

Files: ``"C"`` comment rows, the ``"H"`` header (names, then units), ``"D"`` data rows and a ``"T","<n> lines"``
trailer whose n is the number of D rows (checked: a truncated download fails closed).

Time: prevailing Eastern (America/New_York), no zone marker. On the fall-back day the 01:xx stamps appear twice in
file order (EDT first, then EST): a stamp earlier than the one before it switches the rest of the ambiguous hour to its
second occurrence (``_utc``). The zonal file omits both 01:xx hours that day (those hours aren't written); the
interchange file marks the repeat ``02X`` on one row only, so file order is what counts. The 5-min stamps are interval
beginnings (``00:00`` is in HE01), so an hour is the 12 stamps ``HH:00 .. HH:55``.

Hourly means (freq 1h, ts = hour start, naive UTC), complete hours only:
* 5-min reports: the mean of the hour's 12 rows; a key with fewer rows (a missing or blank row) isn't written.
* genfuelmix: snapshots at irregular times, so the mean is TIME-WEIGHTED: the integral of the linearly interpolated
  snapshots over the hour / 1 h (a plain mean of the readings would over-weight bursts of close snapshots). An hour is
  complete when a snapshot at or after its end exists (so the next day's first snapshot completes the last hour) and
  no gap between snapshots touching it exceeds ``MAX_GAP``. Before the first snapshot read, its value is held back to
  the hour start only if it is within ``EDGE`` of it. Each snapshot is the full dispatch list: a category missing from
  a snapshot (Solar at night, Coal, Oil off) generates 0 in it; a blank or non-numeric Gen Mw cell fails closed.

Requests (L-081/L-107): ``mode: cached``, every 15 min. Each run needs today's and yesterday's files (the last 24-48 h).
The CSVs carry no ETag/Last-Modified, so a file is re-read only when it can hold something new: a cached file
(``<home>/cache/isone``) is reused when it already covers the newest ended hour (yesterday: its last hour, or it was
read >= 1 h after the day ended), and today's 5-min files aren't read before today's first hour ends. About 8 requests
per hour in steady state (+ the warm-up when the cookie is lost, + yesterday's four once a day), at most ~1/s.

Series (MW, freq 1h):
  isone.gen.<fuel>         gas, nuclear, hydro, oil, coal, battery, solar (grid-connected), wind, wood, refuse,
                           landfill_gas, other (ISO-NE's Fuel Category; the Rollup column is ignored)
  isone.gen.total          the sum of the fuel categories = ISO-NE's own generation (imports are never in it)
  isone.load               Total Load = generation + net imports, incl. Asset Related Load (pumping, storage charging)
  isone.load.native        Native Load;  isone.load.asset_related  Asset Related Load
  isone.load.<zone>        Estimated Native Load per zone: maine, new_hampshire, vermont, connecticut, rhode_island,
                           sema, wcma, nema_boston
  isone.tie.<tie>.flow     Actual Interchange AS PUBLISHED: + = export out of New England, - = import into it.
                           ny_ac (Roseton, the NY AC ties), ny_csc (Shoreham, Cross-Sound Cable), ny_1385
                           (Northport-Norwalk), hq_phase2 (Phase II DC), hq_highgate, hq_necec (Merrill Road), nb
                           (Salisbury, New Brunswick)
  isone.interchange.net    NET IMPORTS into New England (+ in, - out) = -(sum of the seven ties); written only for an
                           hour with all seven complete
Checked on live data (2026-11-01 incl. the fall-back hour, 2026-11-12/13; 57 hours): gen.total + interchange.net vs
isone.load within -0.27 / +0.44 % (the test's tolerance is 1 %); the 8 zones vs Native Load within 0.25 %.

Fail closed (FormatDrift, nothing stored): the token handshake, an HTML/403 body, a header that isn't the known one, an
unknown fuel category / zone / interface (map it first), a known zone or tie missing from a whole file, a stamp that
doesn't parse, a non-numeric cell, a value out of range, a trailer that doesn't match, or a report with no complete
hour.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, drop_cookies
from montney.store import Obs, SeriesMeta

HOST = "iso-ne.com"
BASE = "https://www.iso-ne.com/transform/csv"
WARM_URL = "https://www.iso-ne.com/isoexpress/web/reports/operations/-/tree/gen-fuel-mix"
TOKEN = "isox_token"
TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
HOUR = timedelta(hours=1)
FUEL, LOAD, ZONAL, TIES_R = ("genfuelmix", "fiveminutesystemload", "fiveminuteestimatedzonalload",
                             "fiveminuteinterchange")
REPORTS = (FUEL, LOAD, ZONAL, TIES_R)
HEADERS = {
    FUEL: ["Date", "Time", "Fuel Category", "Fuel Category Rollup", "Gen Mw", "Marginal Flag"],
    LOAD: ["Date/Time", "Total Load", "Native Load", "Asset Related Load", "Total Load With Estimated Solar",
           "Native Load With Estimated Solar"],
    ZONAL: ["Date/Time", "Load Zone ID", "Load Zone Name", "Estimated Native Load",
            "Estimated Behind-the-Meter Solar"],
    TIES_R: ["Interface Name", "Date", "Time", "Hour Ending", "Actual Interchange", "Purchases", "Sales"],
}
FUELS = {"Natural Gas": "gas", "Nuclear": "nuclear", "Hydro": "hydro", "Oil": "oil", "Coal": "coal",
         "Batteries": "battery", "Solar": "solar", "Wind": "wind", "Wood": "wood", "Refuse": "refuse",
         "Landfill Gas": "landfill_gas", "Other": "other"}
FUEL_NAMES = {"gas": "natural gas", "nuclear": "nuclear", "hydro": "hydro", "oil": "oil", "coal": "coal",
              "battery": "batteries", "solar": "solar (grid-connected; behind-the-meter isn't in it)", "wind": "wind",
              "wood": "wood", "refuse": "refuse (municipal solid waste)", "landfill_gas": "landfill gas",
              "other": "other"}
LOADS = {"Total Load": "load", "Native Load": "load.native", "Asset Related Load": "load.asset_related"}
LOAD_NAMES = {"load": "Total Load (= generation + net imports, incl. pumping and storage charging)",
              "load.native": "Native Load", "load.asset_related": "Asset Related Load (pumping, storage charging)"}
ZONES = {".Z.MAINE": "maine", ".Z.NEWHAMPSHIRE": "new_hampshire", ".Z.VERMONT": "vermont",
         ".Z.CONNECTICUT": "connecticut", ".Z.RHODEISLAND": "rhode_island", ".Z.SEMASS": "sema",
         ".Z.WCMASS": "wcma", ".Z.NEMASSBOST": "nema_boston"}
ZONE_NAMES = {"maine": "Maine", "new_hampshire": "New Hampshire", "vermont": "Vermont", "connecticut": "Connecticut",
              "rhode_island": "Rhode Island", "sema": "SE Massachusetts", "wcma": "W/Central Massachusetts",
              "nema_boston": "NE Massachusetts + Boston"}
TIES = {".I.ROSETON 345 1": "ny_ac", ".I.SHOREHAM138 99": "ny_csc", ".I.NRTHPORT138 5": "ny_1385",
        ".I.HQ_P1_P2345 5": "hq_phase2", ".I.HQHIGATE120 2": "hq_highgate", ".I.HQMRL_RD345 1": "hq_necec",
        ".I.SALBRYNB345 1": "nb"}
TIE_NAMES = {"ny_ac": "New York AC ties (Roseton)", "ny_csc": "Cross-Sound Cable (Shoreham, NY)",
             "ny_1385": "Northport-Norwalk 1385 (NY)", "hq_phase2": "Hydro-Quebec Phase II DC",
             "hq_highgate": "Highgate (Quebec)", "hq_necec": "NECEC (Merrill Road, Quebec)",
             "nb": "New Brunswick (Salisbury)"}
NEIGHBOURS = {"ny": ("ny_ac", "ny_csc", "ny_1385"), "hq": ("hq_phase2", "hq_highgate", "hq_necec"), "nb": ("nb",)}
NEIGHBOUR_NAMES = {"ny": "New York", "hq": "Quebec", "nb": "New Brunswick"}
GROUP = "ISO-NE / New England generation, load and interchange"
MAX_MW = 40_000                 # ISO-NE's record load is ~28 GW: anything above is a parse error
MIN_FUEL_MW = -5_000            # a fuel row may dip below 0 (station service, storage); far below is a parse error
MAX_TIE_MW = 5_000              # the biggest single tie (Phase II) is ~2 GW
MAX_GAP = timedelta(minutes=20)  # genfuelmix gaps seen: 1.5-11 min
EDGE = timedelta(minutes=10)     # hold the first snapshot back to the hour start only this far
FINAL_AFTER = timedelta(hours=1)  # a past day read this long after its end is final (lags are <= 15 min)
PAUSE_S = 1.0                    # at most ~1 request/s to www.iso-ne.com
LAGS = {FUEL: "~3 min (hour complete at the next snapshot)", LOAD: "~1 min", ZONAL: "~12-15 min", TIES_R: "~6 min"}
TZ_NOTE = "America/New_York (prevailing Eastern)"


# ------------------------------------------------------------------ parsing
def _rows(text: str, report: str) -> list[list[str]]:
    """The D rows of one ISO Express CSV (without the leading "D"), header and trailer checked."""
    body = text.lstrip("﻿").lstrip()
    if not body.startswith('"C"') and not body.startswith('"H"'):
        kind = "HTML" if body[:1] == "<" else f"{body[:40]!r}"
        raise FormatDrift(f"ISO-NE {report}: not the CSV ({kind}); the isox_token session or the URL changed")
    head, data, trailer = None, [], None
    for r in csv.reader(io.StringIO(body)):
        if not r:
            continue
        if r[0] == "H" and head is None:
            head = [c.strip() for c in r[1:]]
        elif r[0] == "D":
            data.append(r[1:])
        elif r[0] == "T":
            trailer = r[1:]
    if head != HEADERS[report]:
        raise FormatDrift(f"ISO-NE {report}: header {head} isn't {HEADERS[report]} (a column was added, renamed or "
                          f"dropped: map it first)")
    n = int(trailer[0].split()[0]) if trailer and trailer[0].split() and trailer[0].split()[0].isdigit() else None
    if n != len(data):
        raise FormatDrift(f"ISO-NE {report}: trailer {trailer} but {len(data)} data rows (a truncated file?)")
    return data


def _local(s: str, report: str) -> datetime:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y %H:%M:%S")
    except ValueError:
        raise FormatDrift(f"ISO-NE {report}: a stamp that doesn't parse: {s!r}") from None


def _utc(locals_: list[datetime]) -> list[datetime]:
    """Prevailing-Eastern stamps in file order -> naive UTC. On the fall-back day the ambiguous 01:xx stamps come twice
    (EDT, then EST): once the clock steps back (strictly; many rows share a stamp), later ones take the second."""
    out, prev, second = [], None, False
    for t in locals_:
        if prev is not None and t < prev:
            second = True
        out.append(t.replace(tzinfo=TZ, fold=1 if second else 0).astimezone(UTC).replace(tzinfo=None))
        prev = t
    return out


def _num(s: str, report: str, what: str, lo: float, hi: float) -> float | None:
    """A cell as a float; blank is None (never 0); non-numeric or out of [lo, hi] fails closed."""
    s = (s or "").strip()
    if s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        raise FormatDrift(f"ISO-NE {report}: {what} {s!r} isn't a number") from None
    if not lo <= v <= hi:
        raise FormatDrift(f"ISO-NE {report}: {what} {v:,.1f} MW outside [{lo:,}, {hi:,}]")
    return v


def parse_fuel(text: str) -> dict[datetime, dict[str, float]]:
    """genfuelmix -> {UTC snapshot: {fuel: MW}} with every fuel key (0 where the snapshot doesn't list it)."""
    data = _rows(text, FUEL)
    stamps = _utc([_local(f"{r[0]} {r[1]}", FUEL) for r in data])
    snaps: dict[datetime, dict[str, float]] = {}
    for ts, r in zip(stamps, data):
        cat = r[2].strip()
        if cat not in FUELS:
            raise FormatDrift(f"ISO-NE genfuelmix: unknown fuel category {cat!r} (map it in power_isone.FUELS first)")
        v = _num(r[4], FUEL, f"{ts} {cat}", MIN_FUEL_MW, MAX_MW)
        if v is None:
            raise FormatDrift(f"ISO-NE genfuelmix: {ts} {cat}: blank Gen Mw")
        row = snaps.setdefault(ts, dict.fromkeys(FUELS.values(), 0.0))
        row[FUELS[cat]] = v
    return snaps


def parse_load(text: str) -> dict[datetime, dict[str, float]]:
    data = _rows(text, LOAD)
    ix = {k: HEADERS[LOAD].index(c) for c, k in LOADS.items()}
    out: dict[datetime, dict[str, float]] = {}
    for ts, r in zip(_utc([_local(r[0], LOAD) for r in data]), data):
        vals = {k: _num(r[i] if i < len(r) else "", LOAD, f"{ts} {k}", -MAX_MW if "asset" in k else 0, MAX_MW)
                for k, i in ix.items()}
        if all(v is not None for v in vals.values()):
            out[ts] = vals                                        # a row with a blank cell isn't data
    return out


def parse_zonal(text: str) -> dict[datetime, dict[str, float]]:
    data = _rows(text, ZONAL)
    out: dict[datetime, dict[str, float]] = defaultdict(dict)
    seen = set()
    for ts, r in zip(_utc([_local(r[0], ZONAL) for r in data]), data):
        name = r[2].strip()
        if name not in ZONES:
            raise FormatDrift(f"ISO-NE zonal load: unknown zone {name!r} (map it in power_isone.ZONES first)")
        seen.add(name)
        v = _num(r[3], ZONAL, f"{ts} {name}", 0, MAX_MW)
        if v is not None:
            out[ts][ZONES[name]] = v
    if data and seen != set(ZONES):
        raise FormatDrift(f"ISO-NE zonal load: zones missing from the file: {sorted(set(ZONES) - seen)}")
    return dict(out)


def parse_ties(text: str) -> dict[datetime, dict[str, float]]:
    data = _rows(text, TIES_R)
    out: dict[datetime, dict[str, float]] = defaultdict(dict)
    seen = set()
    for ts, r in zip(_utc([_local(f"{r[1]} {r[2]}", TIES_R) for r in data]), data):
        name = r[0].strip()
        if name not in TIES:
            raise FormatDrift(f"ISO-NE interchange: unknown interface {name!r} (map it in power_isone.TIES first)")
        seen.add(name)
        v = _num(r[4], TIES_R, f"{ts} {name}", -MAX_TIE_MW, MAX_TIE_MW)
        if v is not None:
            out[ts][TIES[name]] = v
    if data and seen != set(TIES):
        raise FormatDrift(f"ISO-NE interchange: interfaces missing from the file: {sorted(set(TIES) - seen)}")
    return dict(out)


PARSERS = {FUEL: parse_fuel, LOAD: parse_load, ZONAL: parse_zonal, TIES_R: parse_ties}


# ------------------------------------------------------------------ hourly means
def five_min_hours(points: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    """{5-min stamp: {key: MW}} -> {hour start: {key: mean}} for keys with all 12 stamps of the hour."""
    acc: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for ts, row in points.items():
        for k, v in row.items():
            acc[ts.replace(minute=0, second=0)][k].append(v)
    out = {}
    for h, keys in acc.items():
        full = {k: sum(vs) / len(vs) for k, vs in keys.items() if len(vs) == 12}
        if full:
            out[h] = full
    return out


def _interp(t: datetime, t0: datetime, v0: dict, t1: datetime, v1: dict) -> dict[str, float]:
    w = (t - t0) / (t1 - t0)
    return {k: v0[k] + (v1[k] - v0[k]) * w for k in v0}


def snapshot_hours(snaps: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    """Irregular snapshots -> time-weighted hourly means (trapezoid over the linear interpolation), complete hours only:
    a snapshot at or after the hour end, no gap > MAX_GAP, and the start covered (a snapshot at or before it, or the
    first snapshot within EDGE of it)."""
    ts = sorted(snaps)
    if not ts:
        return {}
    out: dict[datetime, dict[str, float]] = {}
    h = ts[0].replace(minute=0, second=0)
    while h + HOUR <= ts[-1]:
        a, b = h, h + HOUR
        h = b
        k = bisect.bisect_left(ts, b)                             # first snapshot >= b (exists: b <= ts[-1])
        j = bisect.bisect_right(ts, a) - 1                        # last snapshot <= a, or -1
        if j < 0:
            if ts[0] - a > EDGE:
                continue
            real = ts[:k + 1]
            pts = [(a, snaps[ts[0]])]
        else:
            real = ts[j:k + 1]
            pts = [(a, snaps[ts[j]] if ts[j] == a else _interp(a, ts[j], snaps[ts[j]], ts[j + 1], snaps[ts[j + 1]]))]
        if any(t2 - t1 > MAX_GAP for t1, t2 in zip(real, real[1:])):
            continue
        pts += [(t, snaps[t]) for t in real if a < t < b]
        pts.append((b, snaps[ts[k]] if ts[k] == b else _interp(b, ts[k - 1], snaps[ts[k - 1]], ts[k], snaps[ts[k]])))
        acc = dict.fromkeys(pts[0][1], 0.0)
        for (t1, v1), (t2, v2) in zip(pts, pts[1:]):
            dt = (t2 - t1).total_seconds()
            for f in acc:
                acc[f] += dt * (v1[f] + v2[f]) / 2
        out[a] = {f: v / 3600 for f, v in acc.items()}
    return out


def covered_until(report: str, points: dict[datetime, dict[str, float]]) -> datetime | None:
    """How far a file's data reaches, for the re-read decision: the newest snapshot (genfuelmix), else the end of the
    newest complete hour."""
    if report == FUEL:
        return max(points, default=None)
    hours = five_min_hours(points)
    want = {ZONAL: len(ZONES), TIES_R: len(TIES), LOAD: len(LOADS)}[report]
    full = [h for h, row in hours.items() if len(row) == want]
    return max(full) + HOUR if full else None


def eastern_date(now_utc: datetime) -> date:
    return now_utc.replace(tzinfo=UTC).astimezone(TZ).date()


def day_bounds(d: date) -> tuple[datetime, datetime]:
    """The Eastern date's [start, end) in naive UTC (23, 24 or 25 h)."""
    s = datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)
    n = d + timedelta(days=1)
    return s, datetime(n.year, n.month, n.day, tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ connector
class IsoNewEngland(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        def meta(sid: str, label: str, report: str) -> SeriesMeta:
            return SeriesMeta(sid, self.id, f"ISO-NE {label}, hourly mean", "MW", "1h", GROUP,
                              extra={"tier": "hourly", "tz": TZ_NOTE, "lag": LAGS[report]})
        out = [meta(f"isone.gen.{k}", f"generation, {FUEL_NAMES[k]}", FUEL) for k in FUELS.values()]
        out.append(meta("isone.gen.total", "generation, total (sum of the fuel categories; no imports)", FUEL))
        out += [meta(f"isone.{k}", LOAD_NAMES[k], LOAD) for k in LOADS.values()]
        out += [meta(f"isone.load.{z}", f"{ZONE_NAMES[z]} zone estimated native load", ZONAL) for z in ZONES.values()]
        out += [meta(f"isone.tie.{t}.flow", f"actual interchange, {TIE_NAMES[t]} (as published: + export, - import)",
                     TIES_R) for t in TIES.values()]
        out += [meta(f"isone.interchange.{n}", f"net imports from {NEIGHBOUR_NAMES[n]} (+ = into New England; minus its "
                     f"ties' sum)", TIES_R) for n in NEIGHBOURS]
        out.append(meta("isone.interchange.net", "net interchange (+ = net imports into New England)", TIES_R))
        return out

    # -------------------------------------------------------------- HTTP
    def _pause(self) -> None:
        if self._last is not None:
            wait = PAUSE_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()

    @staticmethod
    def _has_token(ctx: Context) -> bool:
        return any(c.name == TOKEN and c.domain.lstrip(".").endswith(HOST) for c in ctx.client.cookies.jar)

    def _warm(self, ctx: Context) -> None:
        drop_cookies(ctx, HOST)
        self._pause()
        try:
            ctx.get(WARM_URL, timeout=30, retries=1)
        except httpx.HTTPError as e:
            raise FormatDrift(f"ISO-NE: the ISO Express warm-up page failed ({e}); without its isox_token cookie the "
                              f"CSVs answer 403") from e
        self._warmed = True
        if not self._has_token(ctx):
            raise FormatDrift(f"ISO-NE: the ISO Express warm-up page ({WARM_URL}) set no isox_token cookie (the "
                              f"session handshake changed); the CSVs would answer 403")

    def _get_csv(self, ctx: Context, report: str, d: date) -> str:
        url = f"{BASE}/{report}?start={d:%Y%m%d}&end={d:%Y%m%d}"
        if not self._has_token(ctx):
            self._warm(ctx)
        while True:
            self._pause()
            r = ctx.get(url, timeout=60, allow=(403,))
            body = r.text.lstrip("﻿").lstrip()
            if r.status_code == 200 and body.startswith('"C"'):
                return r.text
            if self._warmed:
                kind = "HTML" if body[:1] == "<" else f"{len(r.content)} bytes"
                raise FormatDrift(f"ISO-NE {report} {d}: HTTP {r.status_code} ({kind}) instead of the CSV even with a "
                                  f"fresh isox_token (the ISO Express session handshake changed?)")
            self._warm(ctx)                                       # the token expired: once per run

    # -------------------------------------------------------------- cache
    @staticmethod
    def _cache_dir():
        d = paths.home() / "cache" / "isone"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _file(self, ctx: Context, report: str, d: date, state: dict) -> dict[datetime, dict[str, float]] | None:
        """One report-day's points: from the cache when it can't hold anything new, else read (and cached)."""
        start, end = day_bounds(d)
        hour_now = ctx.now.replace(minute=0, second=0, microsecond=0)
        need = min(hour_now, end)                                 # the end of the newest ended hour in that day
        key = f"{report}_{d:%Y%m%d}"
        path = self._cache_dir() / f"{key}.csv"
        info = state.get(key)
        if info and path.exists():
            points = PARSERS[report](path.read_text(encoding="utf-8"))
            cov = covered_until(report, points)
            if info.get("final") or (cov is not None and cov >= need):
                return points
        if report != FUEL and need <= start:
            return None                                           # no hour of today has ended: nothing to read yet
        text = self._get_csv(ctx, report, d)
        points = PARSERS[report](text)                            # never cache a file that doesn't parse
        path.write_text(text, encoding="utf-8")
        state[key] = {"fetched": ctx.now.isoformat(), "final": ctx.now >= end + FINAL_AFTER}
        return points

    # -------------------------------------------------------------- fetch
    def fetch(self, ctx: Context) -> FetchResult:
        self._last, self._warmed = None, False
        res = FetchResult(meta=self.declared_series())
        today = eastern_date(ctx.now)
        days = [today - timedelta(days=1), today]
        cache = self._cache_dir()
        state_path = cache / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            state = {}
        keep = {f"{r}_{d:%Y%m%d}" for r in REPORTS for d in days}
        state = {k: v for k, v in state.items() if k in keep}
        merged: dict[str, dict[datetime, dict[str, float]]] = {r: {} for r in REPORTS}
        for d in days:
            for report in REPORTS:
                pts = self._file(ctx, report, d, state)
                if pts:
                    merged[report].update(pts)
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        for f in cache.glob("*.csv"):
            if f.stem not in keep:
                f.unlink(missing_ok=True)

        hours = {FUEL: snapshot_hours(merged[FUEL]), LOAD: five_min_hours(merged[LOAD]),
                 ZONAL: five_min_hours(merged[ZONAL]), TIES_R: five_min_hours(merged[TIES_R])}
        for report, rows in hours.items():
            if not rows:
                raise FormatDrift(f"ISO-NE {report}: no complete hour (NO-RESULT)")

        def put(sid: str, h: datetime, v: float) -> None:
            res.obs.append(Obs(sid, h, round(v, 1), h))

        for h, row in sorted(hours[FUEL].items()):
            for k, v in row.items():
                put(f"isone.gen.{k}", h, v)
            put("isone.gen.total", h, sum(row.values()))
        for h, row in sorted(hours[LOAD].items()):
            for k, v in row.items():
                put(f"isone.{k}", h, v)
        for h, row in sorted(hours[ZONAL].items()):
            for z, v in row.items():
                put(f"isone.load.{z}", h, v)
        for h, row in sorted(hours[TIES_R].items()):
            for t, v in row.items():
                put(f"isone.tie.{t}.flow", h, v)
            for n, ties in NEIGHBOURS.items():
                if all(x in row for x in ties):
                    put(f"isone.interchange.{n}", h, -sum(row[x] for x in ties))
            if len(row) == len(TIES):
                put("isone.interchange.net", h, -sum(row.values()))
        return res
