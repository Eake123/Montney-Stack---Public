"""New York (NYISO) real-time generation by fuel, load by zone and external-tie flows, hourly (PLAN 9 item 1).

Source: NYISO MIS public CSVs, keyless, plain HTTP, gzip, ``Last-Modified`` (no ETag); one file per Eastern day:
  http://mis.nyiso.com/public/csv/rtfuelmix/<YYYYMMDD>rtfuelmix.csv                    generation by fuel (5 min)
  http://mis.nyiso.com/public/csv/pal/<YYYYMMDD>pal.csv                                actual load by zone (5 min)
  http://mis.nyiso.com/public/csv/ExternalLimitsFlows/<YYYYMMDD>ExternalLimitsFlows.csv interface flows (5 min)
Daily files stay up for ~5-10 days (ExternalLimitsFlows 5).

Requests: today's 3 files every run (15 min) = 12 req/h. The server honours ``If-Modified-Since`` (304, 0 bytes,
probed 2026-11-13), so each file is fetched conditionally against the copy in ``<home>/cache/nyiso``; a 304 re-reads
that copy and re-emits it (change-only writes drop the repeats, and a write that failed after a 200 is redone - the
L-081 rule that validators count only once the data is stored). Today's files post every 5 min, so 304s are rare.
Yesterday's 3 files are re-read until a check at or after 01:00 Eastern the next day (they then hold every row and are
kept as final), so the hours from yesterday's start on are written every run: ~15 extra requests a day. Requests
are spaced ``PAUSE_S`` (1 s) apart.

Time: prevailing Eastern (America/New_York). Every stamp is treated as the END of its 5-min interval, so an hour
``h`` is the instants in (h:00, h+1:00]: rtfuelmix runs 00:05 .. next day 00:00 (interval-ending); pal and
ExternalLimitsFlows run 00:00 .. 23:55, and their 00:00 row closes the previous day's last hour.
* rtfuelmix and pal carry a ``Time Zone`` column (EDT/EST): used as given, and checked against the zone's rules.
* ExternalLimitsFlows has no zone column: on the fall-back day the 01:xx stamps appear twice, in two blocks (first
  EDT, then EST). Rows are read in file order and once the clock steps BACK into the repeated hour, its stamps are EST.
  (RTD re-runs add off-grid stamps such as 09:37, and sometimes the same minute twice in adjacent rows; a repeated
  instant keeps its last row.)

Hourly means (freq 1h, ts = hour start, naive UTC): an hour is written once a later instant exists and it has at least
``MIN_N`` (10) of its ~12 instants for that series. A total (gen, load, net) counts an instant only when every one of
its parts is present there. Blank cells are skipped, never read as 0.

Series (MW):
  nyiso.gen.<fuel>            gas (Natural Gas), dual_fuel (gas/oil units, mostly burning gas), nuclear, hydro, wind,
                              other_fossil (oil; no coal since 2020), other_renewables (grid-scale solar, biomass,
                              landfill gas, ...). Behind-the-meter solar isn't in NYISO's mix.
  nyiso.gen.total             the sum of the 7 fuels = generation inside NYISO; imports are never in it
  nyiso.load, nyiso.load.<z>  PAL actual load, the sum of the 11 zones (west, genese, centrl, north, mhk_vl, capitl,
                              hud_vl, millwd, dunwod, nyc, longil)
  nyiso.tie.<tie>.flow        each external tie, + = flow INTO New York (NYISO's own sign)
  nyiso.interchange.<n>       net imports per neighbour: hq, isone, ieso, pjm (+ = into New York)
  nyiso.interchange.net       net imports, the sum of the 11 ties (+ = into New York). ``SCH - HQ_IMPORT_EXPORT``
                              repeats ``SCH - HQ - NY`` (the same number), so it isn't added.
Generation + net imports ~= load: within ~100 MW at 5-min stamps (research, 2026-11-13); hourly figures in the tests.
The 8 internal interfaces (CENTRAL EAST, TOTAL EAST, ...) aren't stored.

Fail closed (FormatDrift, nothing stored): a header that changed, an unknown fuel, zone or external (``SCH -``) tie, a
known one missing from the whole file, a stamp or zone label that doesn't parse, a value out of range, or no complete
hour at all.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "http://mis.nyiso.com/public/csv"
TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
FILES = {"fuel": ("rtfuelmix", "rtfuelmix"), "load": ("pal", "pal"),
         "flows": ("ExternalLimitsFlows", "ExternalLimitsFlows")}
GROUP = "NYISO / New York generation, load and interchange"
PAUSE_S = 1.0                    # between requests to mis.nyiso.com (brief: ~1 req/s per host)
MIN_N = 10                       # of an hour's ~12 five-minute instants
STEP = timedelta(minutes=5)
FINAL_AFTER = timedelta(hours=1)  # a day's files are final once checked 1 h after its Eastern midnight
GEN_RANGE = (-1_000, 40_000)     # NYISO's record load is ~33.9 GW
TIE_RANGE = (-5_000, 5_000)      # the largest tie limit is 2,400 MW

FUELS = {"Natural Gas": "gas", "Dual Fuel": "dual_fuel", "Nuclear": "nuclear", "Hydro": "hydro", "Wind": "wind",
         "Other Fossil Fuels": "other_fossil", "Other Renewables": "other_renewables"}
FUEL_NAMES = {"gas": "natural gas", "dual_fuel": "dual fuel (gas/oil units, mostly gas)", "nuclear": "nuclear",
              "hydro": "hydro", "wind": "wind", "other_fossil": "other fossil fuels (oil)",
              "other_renewables": "other renewables (grid solar, biomass, landfill gas)"}
ZONES = {"WEST": "west", "GENESE": "genese", "CENTRL": "centrl", "NORTH": "north", "MHK VL": "mhk_vl",
         "CAPITL": "capitl", "HUD VL": "hud_vl", "MILLWD": "millwd", "DUNWOD": "dunwod", "N.Y.C.": "nyc",
         "LONGIL": "longil"}
ZONE_NAMES = {"west": "West (A)", "genese": "Genesee (B)", "centrl": "Central (C)", "north": "North (D)",
              "mhk_vl": "Mohawk Valley (E)", "capitl": "Capital (F)", "hud_vl": "Hudson Valley (G)",
              "millwd": "Millwood (H)", "dunwod": "Dunwoodie (I)", "nyc": "New York City (J)", "longil": "Long Island (K)"}
# external tie name in the file -> (series key, neighbour, label)
TIES = {"SCH - HQ - NY": ("hq_ny", "hq", "Hydro-Quebec - NY (Chateauguay)"),
        "SCH - HQ_CEDARS": ("hq_cedars", "hq", "Hydro-Quebec Cedars"),
        "SCH - HQ_CHPE": ("hq_chpe", "hq", "Hydro-Quebec CHPE (HVDC into NYC)"),
        "SCH - NE - NY": ("ne_ny", "isone", "ISO-NE - NY (AC ties)"),
        "SCH - NPX_CSC": ("ne_csc", "isone", "ISO-NE Cross-Sound Cable"),
        "SCH - NPX_1385": ("ne_1385", "isone", "ISO-NE Northport-Norwalk (1385)"),
        "SCH - OH - NY": ("oh_ny", "ieso", "Ontario (IESO) - NY"),
        "SCH - PJ - NY": ("pj_ny", "pjm", "PJM - NY (AC ties)"),
        "SCH - PJM_HTP": ("pjm_htp", "pjm", "PJM Hudson Transmission (HTP)"),
        "SCH - PJM_NEPTUNE": ("pjm_neptune", "pjm", "PJM Neptune (HVDC into Long Island)"),
        "SCH - PJM_VFT": ("pjm_vft", "pjm", "PJM Linden VFT")}
DUPLICATE_TIES = {"SCH - HQ_IMPORT_EXPORT"}     # = SCH - HQ - NY: never added
NEIGHBOURS = {"hq": "Hydro-Quebec", "isone": "ISO-NE", "ieso": "Ontario (IESO)", "pjm": "PJM"}

Points = dict[datetime, dict[str, float]]      # instant (naive UTC, interval end) -> {key: MW}


# ---------------------------------------------------------------- time
def eastern_date(now_utc: datetime) -> date:
    return now_utc.replace(tzinfo=UTC).astimezone(TZ).date()


def day_start_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)


def _local(s: str, what: str) -> datetime:
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise FormatDrift(f"NYISO {what}: a stamp that doesn't parse: {s!r}")


def _utc_from_zone_col(local: datetime, zone: str, what: str) -> datetime:
    """Local wall clock + its EDT/EST label -> naive UTC, checked against America/New_York's rules."""
    off = {"EDT": -4, "EST": -5}.get(zone.strip())
    if off is None:
        raise FormatDrift(f"NYISO {what}: time zone {zone!r} (expected EDT or EST)")
    u = local - timedelta(hours=off)
    if u.replace(tzinfo=UTC).astimezone(TZ).replace(tzinfo=None) != local:
        raise FormatDrift(f"NYISO {what}: {local} {zone} isn't a valid Eastern clock reading")
    return u


def _ambiguous(local: datetime) -> bool:
    return local.replace(tzinfo=TZ, fold=0).utcoffset() != local.replace(tzinfo=TZ, fold=1).utcoffset()


def _to_utc(local: datetime, fold: int) -> datetime:
    return local.replace(tzinfo=TZ, fold=fold).astimezone(UTC).replace(tzinfo=None)


def _num(s: str, what: str, lo: float, hi: float) -> float | None:
    s = s.strip()
    if not s:
        return None                                              # blank: skipped, never 0
    try:
        v = float(s)
    except ValueError:
        raise FormatDrift(f"NYISO {what}: a value that isn't a number: {s!r}") from None
    if not lo <= v <= hi:
        raise FormatDrift(f"NYISO {what}: {v:,.0f} MW outside {lo:,} .. {hi:,}")
    return v


def _rows(text: str, header: list[str], what: str) -> list[list[str]]:
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if r]
    if not rows or [c.strip() for c in rows[0]] != header:
        raise FormatDrift(f"NYISO {what}: header changed: {rows[0] if rows else 'empty file'}")
    return rows[1:]


# ---------------------------------------------------------------- parsing
def parse_fuel(text: str) -> Points:
    out: Points = defaultdict(dict)
    seen = set()
    for r in _rows(text, ["Time Stamp", "Time Zone", "Fuel Category", "Gen MW"], "rtfuelmix"):
        if len(r) < 4:
            raise FormatDrift(f"NYISO rtfuelmix: short row {r}")
        key = FUELS.get(r[2].strip())
        if key is None:
            raise FormatDrift(f"NYISO rtfuelmix: unknown fuel {r[2]!r} (map it in FUELS)")
        seen.add(key)
        t = _utc_from_zone_col(_local(r[0], "rtfuelmix"), r[1], "rtfuelmix")
        v = _num(r[3], f"rtfuelmix {r[0]} {r[2]}", *GEN_RANGE)
        if v is not None:
            out[t][key] = v
    missing = set(FUELS.values()) - seen
    if out and missing:
        raise FormatDrift(f"NYISO rtfuelmix: fuels missing from the file: {sorted(missing)}")
    for p in out.values():
        if len(p) == len(FUELS):
            p["total"] = sum(p.values())
    return dict(out)


def parse_load(text: str) -> Points:
    out: Points = defaultdict(dict)
    seen = set()
    for r in _rows(text, ["Time Stamp", "Time Zone", "Name", "PTID", "Load"], "pal"):
        if len(r) < 5:
            raise FormatDrift(f"NYISO pal: short row {r}")
        key = ZONES.get(r[2].strip())
        if key is None:
            raise FormatDrift(f"NYISO pal: unknown zone {r[2]!r} (map it in ZONES)")
        seen.add(key)
        t = _utc_from_zone_col(_local(r[0], "pal"), r[1], "pal")
        v = _num(r[4], f"pal {r[0]} {r[2]}", *GEN_RANGE)
        if v is not None:
            out[t][key] = v
    missing = set(ZONES.values()) - seen
    if out and missing:
        raise FormatDrift(f"NYISO pal: zones missing from the file: {sorted(missing)}")
    for p in out.values():
        if len(p) == len(ZONES):
            p["total"] = sum(p.values())
    return dict(out)


def parse_flows(text: str) -> Points:
    """External ties, + = into New York. No zone column: the fall-back day's repeated 01:xx block is told apart by
    file order (the clock stepping back into the ambiguous hour starts the EST block)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = [c.strip() for c in rows[0]] if rows else []
    if head[:2] != ["Timestamp", "Interface Name"] or "Flow (MWH)" not in head:
        raise FormatDrift(f"NYISO ExternalLimitsFlows: header changed: {head or 'empty file'}")
    fi = head.index("Flow (MWH)")
    out: Points = defaultdict(dict)
    seen = set()
    prev: datetime | None = None
    second: set[date] = set()                    # local dates whose clock has stepped back (EST block started)
    for r in rows[1:]:
        if not r:
            continue
        if len(r) <= fi:
            raise FormatDrift(f"NYISO ExternalLimitsFlows: short row {r}")
        name = r[1].strip()
        local = _local(r[0], "ExternalLimitsFlows")
        if prev is not None and local < prev:
            if not _ambiguous(local):
                raise FormatDrift(f"NYISO ExternalLimitsFlows: the clock steps back to {local} outside a fall-back hour")
            second.add(local.date())
        prev = local
        if not name.startswith("SCH - "):
            continue                                              # an internal interface: not stored
        if name in DUPLICATE_TIES:
            seen.add(name)
            continue
        tie = TIES.get(name)
        if tie is None:
            raise FormatDrift(f"NYISO ExternalLimitsFlows: unknown external tie {name!r} (map it in TIES)")
        seen.add(name)
        fold = 1 if _ambiguous(local) and local.date() in second else 0
        v = _num(r[fi], f"ExternalLimitsFlows {r[0]} {name}", *TIE_RANGE)
        if v is not None:
            out[_to_utc(local, fold)][tie[0]] = v                 # a repeated instant keeps its last row
    missing = set(TIES) - seen
    if out and missing:
        raise FormatDrift(f"NYISO ExternalLimitsFlows: ties missing from the file: {sorted(missing)}")
    keys = {n: [k for k, nb, _ in TIES.values() if nb == n] for n in NEIGHBOURS}
    for p in out.values():
        ties = dict(p)
        for n, ks in keys.items():
            if all(k in ties for k in ks):
                p[f"nb_{n}"] = sum(ties[k] for k in ks)
        if len(ties) == len(TIES):
            p["net"] = sum(ties.values())
    return dict(out)


PARSERS = {"fuel": parse_fuel, "load": parse_load, "flows": parse_flows}


def hour_of(t: datetime) -> datetime:
    """The hour an interval-ending instant belongs to: (h:00, h+1:00] -> h."""
    return (t - timedelta(microseconds=1)).replace(minute=0, second=0, microsecond=0)


def hourly_means(points: Points) -> dict[datetime, dict[str, float]]:
    """{hour start: {key: mean}} for hours that a later instant has passed, with >= MIN_N instants for that key."""
    if not points:
        return {}
    newest = max(points)
    by: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t, p in points.items():
        for k, v in p.items():
            by[hour_of(t)][k].append(v)
    out = {}
    for h, keys in by.items():
        if newest < h + timedelta(hours=1):
            continue                                              # the hour in progress
        row = {k: sum(v) / len(v) for k, v in keys.items() if len(v) >= MIN_N}
        if row:
            out[h] = row
    return out


# ---------------------------------------------------------------- series
def _sid(kind: str, key: str) -> str | None:
    if kind == "fuel":
        return f"nyiso.gen.{key}"
    if kind == "load":
        return "nyiso.load" if key == "total" else f"nyiso.load.{key}"
    if key == "net":
        return "nyiso.interchange.net"
    if key.startswith("nb_"):
        return f"nyiso.interchange.{key[3:]}"
    return f"nyiso.tie.{key}.flow"


class NyisoRealtime(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "tz": "America/New_York (prevailing Eastern)"}
        gen = {**ex, "lag": "~20 min after the hour (5-min file posts ~15 min late)"}
        load = {**ex, "lag": "~15 min after the hour"}
        flow = {**ex, "lag": "~5 min after the hour (stamps run at or just ahead of now)"}
        m = [SeriesMeta(f"nyiso.gen.{k}", self.id, f"NYISO {n} generation, hourly mean", "MW", "1h", GROUP, extra=gen)
             for k, n in FUEL_NAMES.items()]
        m.append(SeriesMeta("nyiso.gen.total", self.id, "NYISO generation, all fuels (inside NYISO; no imports; "
                            "excl. behind-the-meter solar), hourly mean", "MW", "1h", GROUP, extra=gen))
        m.append(SeriesMeta("nyiso.load", self.id, "NYISO actual load (sum of the 11 zones), hourly mean", "MW", "1h",
                            GROUP, extra=load))
        m += [SeriesMeta(f"nyiso.load.{k}", self.id, f"NYISO {n} zone load, hourly mean", "MW", "1h", GROUP,
                         extra=load) for k, n in ZONE_NAMES.items()]
        m += [SeriesMeta(f"nyiso.tie.{k}.flow", self.id, f"NYISO tie {lab} flow (+ into New York), hourly mean",
                         "MW", "1h", GROUP, extra=flow) for k, _, lab in TIES.values()]
        m += [SeriesMeta(f"nyiso.interchange.{n}", self.id, f"NYISO net imports from {lab} (+ into New York), "
                         "hourly mean", "MW", "1h", GROUP, extra=flow) for n, lab in NEIGHBOURS.items()]
        m.append(SeriesMeta("nyiso.interchange.net", self.id, "NYISO net imports, all 11 external ties (+ into New "
                            "York), hourly mean", "MW", "1h", GROUP, extra=flow))
        return m

    @staticmethod
    def _cache():
        d = paths.home() / "cache" / "nyiso"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _pause(self) -> None:
        if self._sent and PAUSE_S:
            time.sleep(PAUSE_S)
        self._sent += 1

    def _read(self, ctx: Context, d: date, kind: str, *, today: date) -> Points | None:
        """One day's file: from the cache if final, else a conditional GET (a 304 re-reads the cached copy).
        None when it isn't published (404). Parsed before it's cached, so a bad file is never kept."""
        folder, name = FILES[kind]
        fname = f"{d:%Y%m%d}{name}.csv"
        body, meta = self._cache() / fname, self._cache() / f"{fname}.json"
        m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() and body.exists() else {}
        if m.get("final"):
            return PARSERS[kind](body.read_text(encoding="utf-8"))
        headers = {"If-Modified-Since": m["last_modified"]} if m.get("last_modified") else None
        self._pause()
        r = ctx.get(f"{BASE}/{folder}/{fname}", headers=headers, allow=(304, 404))
        if r.status_code == 404:
            return None
        if r.status_code == 304:
            self.not_modified += 1
            text, lm = body.read_text(encoding="utf-8"), m["last_modified"]
        else:
            text, lm = r.text, r.headers.get("last-modified")
        pts = PARSERS[kind](text)
        final = d < today and ctx.now >= day_start_utc(d + timedelta(days=1)) + FINAL_AFTER
        if r.status_code != 304:
            body.write_text(text, encoding="utf-8")
        meta.write_text(json.dumps({"last_modified": lm, "final": final}), encoding="utf-8")
        return pts

    def _prune(self, keep: set[date]) -> None:
        names = {f"{d:%Y%m%d}" for d in keep}
        for f in self._cache().iterdir():
            if f.name[:8] not in names:
                f.unlink(missing_ok=True)

    def fetch(self, ctx: Context) -> FetchResult:
        self._sent, self.not_modified = 0, 0
        res = FetchResult(meta=self.declared_series())
        today = eastern_date(ctx.now)
        days = (today - timedelta(days=1), today)
        self._prune(set(days))
        for kind in FILES:
            pts: Points = {}
            for d in days:
                got = self._read(ctx, d, kind, today=today)
                if got is None:
                    res.warnings.append(f"NYISO {FILES[kind][1]} {d}: not published (404)")
                    continue
                pts.update(got)
            for h, row in sorted(hourly_means(pts).items()):
                for k, v in row.items():
                    res.obs.append(Obs(_sid(kind, k), h, round(v, 1), h))
        if not res.obs:
            raise FormatDrift("NYISO: no complete hour in any file (NO-RESULT)")
        return res
