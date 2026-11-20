"""Alberta Rivers (rivers.alberta.ca) TransAlta reservoirs: hourly dam outflow, level and live storage (PLAN P5d).

The only near-real-time source for TransAlta's dam releases (WSC doesn't publish them live). Ten reservoirs on the
Bow chain and the North Saskatchewan + Brazeau (docs/research/v2/weather-p5d.md §3.1), including Ghost, which the
Ghost → Bearspaw reach needs in winter when WSC's 05BH005 is off for the season.

Endpoint: the site's static 5-day JSON per station, `.../stationdata/L_HG_<station>_table.json` (no key, CORS *, regenerated
about hourly). Columns are mapped by NAME (`columnarray`), never by position: TAU, EPA and Spray files differ.
Terms: alberta.ca disclaimer, non-commercial use (personal use, L-041). Research: docs/research/v2/weather-notes-alberta.md.

Series (L-033 ids: hyphens become underscores, so TAU-001 -> tau_001):
    abrivers.<stn>.outflow   m3/s   hourly mean over the hour ENDING at ts
    abrivers.<stn>.level     m      reservoir level as published (AB Rivers' datum)
    abrivers.<stn>.storage   dam3   live storage ("Capacity"); negative values (Horseshoe) are dropped

Timestamps (L-034 and power.md §A.1):
* WISKI stamps are local wall clock on **Alberta's pre-2026 rules** (DST each summer). On 2026-11-01 the site fell back
  to UTC-7 like any other year, instead of staying on Alberta's new permanent UTC-6. The evidence: the Download feed
  repeats 01:00 on 2026-11-01, and the file clock (`filecreate_timestamp` vs the HTTP `Last-Modified`) reads UTC-7.
  So stamps are read on WISKI_CLOCK (America/Denver, which has exactly those rules), not on America/Edmonton. This is a
  deliberate exception to AGENTS.md's "use America/Edmonton".
* A value is the mean of the hour ending at the stamp. Stamps in DST run 1 h late, so subtract 1 h (L-034). Standard-time
  stamps are the hour-end as they stand. Store the hour-end as naive UTC; it joins AESO HE directly.
* At the fall repeat of 01:00, the first occurrence is DST (fold 0) and the second is standard. A first 01:00 that only
  copies 00:00 (the 2024 pattern) is dropped; this year's isn't a copy, so it's kept. Empty values are dropped.
* **After 2026-11-01 the convention is NO-RESULT under L-034 until P5 re-checks it on our CSD data.** We store with the
  rule above and VERIFY it on every fetch, failing closed per file:
  - the file clock offset must equal WISKI_CLOCK's offset (±15 min), else that file writes nothing (a WISKI fix to
    UTC-6 would otherwise shift every stamp by 1 h);
  - no converted hour-end may lie in the future, else that file writes nothing.
  Post-switch outflows aren't to be used in MW fits until the P5 check (L-034); the series labels say so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.store import Obs, SeriesMeta

URL = "https://rivers.alberta.ca/apps/Basins/data/figures/river/abrivers/stationdata/L_HG_{stn}_table.json"
WISKI_CLOCK = ZoneInfo("America/Denver")   # = Alberta's pre-2026 rules; see the module docstring
CLOCK_TOL = timedelta(minutes=15)
FUTURE_TOL = timedelta(minutes=15)
CONVENTION = ("hour-ending mean; WISKI clock on Alberta's pre-2026 DST rules, DST stamps -1 h (L-034); "
              "after 2026-11-01 inferred from the site clock, P5 check pending (no MW fits yet)")

# station: (name, river group, has outflow, has storage)
STATIONS: dict[str, tuple[str, str, bool, bool]] = {
    "TAU-002": ("Cascade Reservoir (Lake Minnewanka)", "Bow", True, True),
    "05BF005": ("Upper Kananaskis Lake", "Bow", True, True),
    "05BF009": ("Lower Kananaskis Lake", "Bow", True, True),
    "05BF024": ("Barrier Lake", "Bow", True, True),
    "TAU-004": ("Horseshoe Forebay", "Bow", True, False),          # its "Capacity" is negative: not a storage
    "05BE005": ("Ghost Reservoir", "Bow", True, True),
    "05BH010": ("Bearspaw Reservoir", "Bow", True, True),
    "05BC006": ("Spray Reservoir (Three Sisters Dam)", "Bow", False, True),   # no outflow column
    "TAU-001": ("Bighorn Reservoir (Abraham Lake)", "N. Saskatchewan", True, True),
    "05DD006": ("Brazeau Reservoir", "N. Saskatchewan", True, True),
}
# column name -> (measure, the unit symbol AB Rivers must publish, our unit)
COLUMNS = {"Level": ("level", "m", "m"), "Outflow": ("outflow", "m³/s", "m3/s"), "Capacity": ("storage", "dam³", "dam3")}


def series_key(stn: str) -> str:
    return stn.lower().replace("-", "_")


def hour_end_utc(stamp: datetime, fold: int = 0) -> datetime:
    """A WISKI stamp (local wall clock) -> naive UTC end of the hour it averages (L-034)."""
    aware = stamp.replace(tzinfo=WISKI_CLOCK, fold=fold)
    utc = aware.astimezone(timezone.utc).replace(tzinfo=None)
    return utc - timedelta(hours=1) if aware.dst() else utc


def clock_problem(filecreate: str | None, last_modified: str | None) -> str | None:
    """None if the site's file clock follows WISKI_CLOCK; else why not (fail closed: missing data is a problem)."""
    if not filecreate or not last_modified:
        return "no filecreate_timestamp / Last-Modified to check the site clock"
    lm = parsedate_to_datetime(last_modified).astimezone(timezone.utc).replace(tzinfo=None)
    seen = datetime.fromisoformat(filecreate) - lm
    want = lm.replace(tzinfo=timezone.utc).astimezone(WISKI_CLOCK).utcoffset()
    if abs(seen - want) > CLOCK_TOL:
        return (f"site clock is UTC{seen.total_seconds() / 3600:+.2f} h but the rule expects "
                f"UTC{want.total_seconds() / 3600:+.0f} h: timestamps NO-RESULT")
    return None


def parse(stn: str, doc: dict, last_modified: str | None, now: datetime) -> tuple[list[Obs], list[str]]:
    """Observations from one 5-day JSON document, or ([], [reason]) if it fails a check."""
    if doc.get("station_no") != stn:
        return [], [f"{stn}: file is for {doc.get('station_no')!r}"]
    bad = clock_problem(doc.get("filecreate_timestamp"), last_modified)
    if bad:
        return [], [f"{stn}: {bad}"]
    cols = doc.get("columnarray") or []
    units = [None] + list(doc.get("ts_unitsymbols") or [])      # units align with the value columns
    _, _, has_q, has_s = STATIONS[stn]
    want = {"level"} | ({"outflow"} if has_q else set()) | ({"storage"} if has_s else set())
    idx, warns = {}, []
    for i, c in enumerate(cols):
        if c in COLUMNS and COLUMNS[c][0] in want:
            measure, unit_src, _ = COLUMNS[c]
            if i >= len(units) or units[i] != unit_src:
                warns.append(f"{stn}: column {c!r} unit {units[i] if i < len(units) else None!r}, expected {unit_src!r}: skipped")
                continue
            idx[measure] = i
    missing = want - set(idx)
    if missing:
        warns.append(f"{stn}: no {sorted(missing)} column")
    key, seen, out, prev = series_key(stn), set(), [], {}
    for row in doc.get("data") or []:
        stamp = datetime.fromisoformat(row[0])
        fold = 1 if stamp in seen else 0
        seen.add(stamp)
        ts = hour_end_utc(stamp, fold)
        if ts > now + FUTURE_TOL:
            return [], [f"{stn}: hour-end {ts:%Y-%m-%d %H:%M}Z is in the future: timestamp rule broken, NO-RESULT"]
        for measure, i in idx.items():
            v = row[i] if i < len(row) else None
            if v is None or (measure == "storage" and v < 0):
                continue
            copy = (fold == 0 and stamp.hour == 1 and _is_fall_repeat(stamp)
                    and prev.get(measure) == (stamp - timedelta(hours=1), v))
            prev[measure] = (stamp, v)
            if copy:                       # 2024 pattern: the DST 01:00 only repeats 00:00
                continue
            out.append(Obs(f"abrivers.{key}.{measure}", ts, round(float(v), 3), ts))
    return out, warns


def _is_fall_repeat(stamp: datetime) -> bool:
    """True for the local hour that WISKI_CLOCK repeats in the fall (01:00 on the DST end day)."""
    a, b = stamp.replace(tzinfo=WISKI_CLOCK, fold=0), stamp.replace(tzinfo=WISKI_CLOCK, fold=1)
    return a.utcoffset() != b.utcoffset()


class ABRiversReservoirs(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        ids = (spec.get("params") or {}).get("stations") or list(STATIONS)
        unknown = [s for s in ids if s not in STATIONS]
        if unknown:
            raise ValueError(f"unknown Alberta Rivers station(s) {unknown}")
        self.stations = ids

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for s in self.stations:
            name, river, has_q, has_s = STATIONS[s]
            k, g, ex = series_key(s), f"Rivers / {river}", {"native_id": s, "ts_convention": CONVENTION}
            if has_q:
                out.append(SeriesMeta(f"abrivers.{k}.outflow", self.id, f"{name} dam outflow, hour-ending (AB Rivers {s})",
                                      "m3/s", "1h", g, ex))
            out.append(SeriesMeta(f"abrivers.{k}.level", self.id, f"{name} level (AB Rivers {s})", "m", "1h", g, ex))
            if has_s:
                out.append(SeriesMeta(f"abrivers.{k}.storage", self.id, f"{name} live storage (AB Rivers {s})",
                                      "dam3", "1h", g, ex))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        failed = 0
        for s in self.stations:
            try:
                r = ctx.get(URL.format(stn=s))
                body = r.json()
                doc = body[0] if isinstance(body, list) and body else {}
                obs, warns = parse(s, doc, r.headers.get("last-modified"), ctx.now)
            except Exception as e:  # one bad file must not drop the other nine
                obs, warns = [], [f"{s}: {type(e).__name__}: {e}"]
            res.obs += obs
            res.warnings += warns
            failed += not obs
        if self.stations and failed == len(self.stations):
            raise RuntimeError(f"no Alberta Rivers reservoir gave data: {'; '.join(res.warnings)[:300]}")
        return res
