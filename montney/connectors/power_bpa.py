"""BPA (Bonneville Power Administration) balancing authority: load, generation by type and net interchange, hourly
(PLAN 9 item 1, region ``pnw``: BPA is the largest BA in it).

Source (keyless, US DOE public data): https://transmission.bpa.gov/business/operations/wind/baltwg3.txt, the page
"BPA BA Load and Total VER, Hydro, Fossil/Biomass, Nuclear Generation, and Net Interchange, Near-Real-Time". Use
baltwg3, not baltwg: baltwg dropped the Interchange column. Tab-separated: 11 preamble lines, a header, then 5-min SCADA
snapshots (MW, integers) for the last 7 days; slots to the end of today are listed with blank cells (skipped, never 0).
Columns: Load, VER (wind + solar; "Wind" before 2022), Hydro, Fossil/Biomass (thermal, nuclear excluded), Interchange
(net, + = export out of the BA), Nuclear (Columbia Generating Station, ~1,150 MW, the BA's only nuclear plant).
BPA: "total generation (hydro + fossil/biomass + nuclear + VER) will equal load + interchange"; 1,832 rows on
2026-11-13 gave gen - (load + interchange) = -40 MW on average (min -86).

Scope: loads and resources in BPA's BA, including some that aren't BPA's. Not BPA loads served by transfer or scheduled
out of region, nor customers with their own BAs (Seattle, Tacoma, Chelan/Douglas/Grant PUDs, ...).

Time: "Pacific Time" = PREVAILING Pacific (America/Los_Angeles, PST/PDT). Settled from BPA's 2026 year-to-date
workbook (OPITabularReports/WindGenTotalLoadYTD_2026.xlsx), which prints the same clock beside UTC: 2026-03-08 jumps
01:55 -> 03:00, and 2026-11-01 lists 01:00-01:55 twice (08:00Z, then 09:00Z). So the file's stamps run through
``LocalClock("America/Los_Angeles").series`` (a repeated hour takes PDT the first time, PST the second). Each run also
checks the preamble's "last updated <local>" against the HTTP Last-Modified (UTC): if BPA's clock stops matching
America/Los_Angeles (e.g. a switch to fixed PST), the run fails closed rather than shifting every hour (as L-050).

Requests: one ~90 KB file (gzip on the wire) per run, every 15 min = 4 req/h. Conditional GET: If-Modified-Since from
the last 200 (BPA's IIS honours it; the body is kept in <home>/cache/bpa, so a 304 re-reads it). The file changes every
5 min, so most polls get a 200. The 7-day window means one file always covers yesterday too (no second request).
Lag (2026-11-13): newest 5-min row ~4.5 min behind; the newest complete hour ~1 h 5 min behind its start + the poll.

Series (MW, freq 1h, ts = hour start, naive UTC; the mean of the hour's twelve snapshots :00-:55, complete hours only):
  bpa.load                     BA load
  bpa.gen.hydro / .wind_solar / .thermal / .nuclear   Hydro, VER (wind + solar), Fossil/Biomass, Nuclear
  bpa.gen.total                the four generation columns (inside the BA; imports never in it)
  bpa.interchange.net          net IMPORTS into the BA (+ in) = -Interchange (BPA's + is export)
Fail closed (FormatDrift, nothing stored): the header missing or with a column not in COLUMNS (a new one must be
mapped), a stamp that doesn't parse or doesn't step forward, a value that isn't a number or is out of range, BPA's
clock not matching Pacific prevailing time, or no complete hour.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from montney import paths
from montney.connectors.aeso_ets import LocalClock
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://transmission.bpa.gov/business/operations/wind/baltwg3.txt"
TZ = "America/Los_Angeles"
GROUP = "BPA / Bonneville BA load, generation and interchange"
# BPA column -> our key. Order is BPA's; a column not here (or one missing) fails closed.
COLUMNS = {"Load": "load", "VER": "wind_solar", "Hydro": "hydro", "Fossil/Biomass": "thermal",
           "Interchange": "interchange", "Nuclear": "nuclear"}
GEN = ("hydro", "wind_solar", "thermal", "nuclear")
# Sane ranges (MW). BPA's record load is ~11.5 GW, its hydro ~15 GW; a small negative generation is station service.
RANGE = {"load": (0, 25_000), "interchange": (-15_000, 15_000)}
GEN_RANGE = (-500, 25_000)
STEPS = 12                                    # 5-min snapshots per hour
CLOCK_SLACK = timedelta(minutes=10)           # "last updated" vs Last-Modified


def _range(k: str) -> tuple[float, float]:
    return RANGE.get(k, GEN_RANGE)


def parse(text: str) -> dict[datetime, dict[str, float]]:
    """baltwg3.txt -> {5-min UTC stamp: {load, wind_solar, hydro, thermal, interchange, nuclear}} for complete rows."""
    lines = text.lstrip("﻿").splitlines()
    h = next((i for i, ln in enumerate(lines) if ln.startswith("Date/Time")), None)
    if h is None:
        raise FormatDrift("BPA baltwg3: no Date/Time header")
    head = [c.strip() for c in lines[h].split("\t")][1:]
    if head != list(COLUMNS):
        raise FormatDrift(f"BPA baltwg3: columns {head}, expected {list(COLUMNS)} (map a new one in power_bpa.COLUMNS)")
    locals_, vals = [], []
    for ln in lines[h + 1:]:
        cells = [c.strip() for c in ln.split("\t")]
        if not cells[0]:
            continue
        try:
            t = datetime.strptime(cells[0], "%m/%d/%Y %H:%M")
        except ValueError:
            raise FormatDrift(f"BPA baltwg3: stamp {cells[0]!r} doesn't parse") from None
        row = cells[1:] + [""] * (len(COLUMNS) + 1 - len(cells))
        if any(c == "" for c in row[: len(COLUMNS)]):
            continue                                              # a future slot (or a gap): not data, never 0
        try:
            v = {k: float(c) for k, c in zip(COLUMNS.values(), row)}
        except ValueError:
            raise FormatDrift(f"BPA baltwg3 {cells[0]}: a value that isn't a number") from None
        for k, x in v.items():
            lo, hi = _range(k)
            if not lo <= x <= hi:
                raise FormatDrift(f"BPA baltwg3 {cells[0]}: {k} {x:,.0f} MW outside {lo:,}..{hi:,}")
        locals_.append(t)
        vals.append(v)
    stamps = LocalClock(TZ).series(locals_)
    for a, b in zip(stamps, stamps[1:]):
        if b <= a:
            raise FormatDrift(f"BPA baltwg3: stamps don't step forward ({a} -> {b} UTC)")
    return dict(zip(stamps, vals))


def hourly_means(points: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    """{5-min UTC snapshot: row} -> {hour start: mean row + gen total + net imports}, hours with all 12 snapshots."""
    by: dict[datetime, list[dict[str, float]]] = defaultdict(list)
    for ts, row in points.items():
        by[ts.replace(minute=0, second=0)].append(row)
    out = {}
    for hr, rows in by.items():
        if len(rows) != STEPS:
            continue
        m = {k: sum(r[k] for r in rows) / STEPS for k in COLUMNS.values()}
        m["total"] = sum(m[k] for k in GEN)
        m["net_imports"] = -m.pop("interchange")
        out[hr] = m
    return out


def clock_problem(text: str, last_modified: str | None) -> str | None:
    """None if the preamble's 'last updated' (Pacific prevailing) matches the HTTP Last-Modified (UTC), else why not."""
    m = re.search(r"last updated (\d{2}[A-Za-z]{3}\d{4} \d{2}:\d{2}:\d{2})", text[:2000])
    if not m or not last_modified:
        return None                                               # nothing to compare: not a failure
    local = datetime.strptime(m.group(1), "%d%b%Y %H:%M:%S")
    lm = parsedate_to_datetime(last_modified).astimezone(timezone.utc).replace(tzinfo=None)
    if any(abs(u - lm) <= CLOCK_SLACK for u in LocalClock(TZ).candidates(local)):
        return None
    return (f"'last updated {m.group(1)}' is not {TZ} time for Last-Modified {lm:%Y-%m-%d %H:%M} UTC "
            f"(offset {(local - lm).total_seconds() / 3600:+.1f} h): BPA's clock changed")


class BpaBalancingAuthority(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "tz": "America/Los_Angeles (BPA 'Pacific Time', prevailing)",
              "lag": "rows ~5 min; an hour lands ~5 min after it ends (+ the 15-min poll)", "region": "pnw"}
        names = {"bpa.load": "BPA BA load",
                 "bpa.gen.hydro": "BPA BA hydro generation",
                 "bpa.gen.wind_solar": "BPA BA wind + solar generation (VER)",
                 "bpa.gen.thermal": "BPA BA fossil/biomass generation (no nuclear)",
                 "bpa.gen.nuclear": "BPA BA nuclear generation (Columbia Generating Station)",
                 "bpa.gen.total": "BPA BA total generation (hydro + VER + fossil/biomass + nuclear)",
                 "bpa.interchange.net": "BPA BA net interchange (+ = net imports into the BA)"}
        return [SeriesMeta(sid, self.id, f"{n}, hourly mean", "MW", "1h", GROUP, extra=ex) for sid, n in names.items()]

    def _text(self, ctx: Context, res: FetchResult) -> str:
        """Today's file, with If-Modified-Since from the last 200; a 304 re-reads the kept body."""
        cache = paths.home() / "cache" / "bpa"
        body, stamp = cache / "baltwg3.txt", cache / "baltwg3.last_modified"
        headers = {}
        if body.exists() and stamp.exists():
            headers["If-Modified-Since"] = stamp.read_text(encoding="utf-8").strip()
        r = ctx.get(URL, headers=headers, allow=(304,))
        if r.status_code == 304:
            return body.read_text(encoding="utf-8")
        text = r.text
        lm = r.headers.get("last-modified")
        bad = clock_problem(text, lm)
        if bad:
            raise FormatDrift(f"BPA baltwg3: {bad}")
        if lm is None:
            res.warnings.append("BPA baltwg3: no Last-Modified header; BPA's clock not checked this run")
        parse(text)                                               # never keep a body that doesn't parse
        if lm:
            cache.mkdir(parents=True, exist_ok=True)
            body.write_text(text, encoding="utf-8")
            stamp.write_text(lm, encoding="utf-8")
        return text

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        hours = hourly_means(parse(self._text(ctx, res)))
        if not hours:
            raise FormatDrift("BPA baltwg3: no complete hour (NO-RESULT)")
        sid = {"load": "bpa.load", "total": "bpa.gen.total", "net_imports": "bpa.interchange.net",
               **{k: f"bpa.gen.{k}" for k in GEN}}
        for hr, row in sorted(hours.items()):
            for k, s in sid.items():
                res.obs.append(Obs(s, hr, round(row[k], 1), hr))
        return res
