"""CAISO OASIS: actual hourly load of every Western EIM (WEIM) balancing area, including BC Hydro (PLAN 9, wcan).

Source (keyless): https://oasis.caiso.com/oasisapi/SingleZip?queryname=SLD_FCST&market_run_id=ACTUAL&version=1&
resultformat=6&startdatetime=<YYYYMMDDTHH:MM-0000>&enddatetime=<...>
One request per run reads the last 48 hours for all ~37 WEIM / CAISO TAC areas at once (~4 KB zip holding one CSV).
OASIS asks for no more than 1 request per 5 s: this source makes one request per run (every 15 min, 4/h) and never
retries inside a run (the shared client's retry would come back after 2 s); the next run re-reads the whole window.
An OASIS error is also a zip (an ``INVALID_REQUEST.xml`` with ERR_CODE / ERR_DESC), so the zip's content is checked,
not only the HTTP status.

Columns: INTERVALSTARTTIME_GMT, INTERVALENDTIME_GMT, LOAD_TYPE, OPR_DT, OPR_HR, OPR_INTERVAL, MARKET_RUN_ID,
TAC_AREA_NAME, LABEL ("Total Actual Hourly Integrated Load"), XML_DATA_ITEM (SYS_FCST_ACT_MW), POS, MW, EXECUTION_TYPE,
GROUP. Each row is one area's **hourly integrated** load over [start, end), both explicit GMT ISO stamps, so there is no
local-time handling at all (OPR_DT / OPR_HR are the Pacific-prevailing trading day: HE25 on the US fall-back day).
``ts`` = INTERVALSTARTTIME_GMT as naive UTC. OASIS publishes an hour only once it is complete; an hour whose end is
after the fetch is never written anyway.

Lag: the hour ending HH:00 UTC is there ~30-40 min later (2026-11-13: 14:00-15:00 at 15:38, 15:00-16:00 by 16:40).

BC Hydro (``BCHA``) is why this exists: its own load file posts weekdays, 1-4.5 days late (power_bch_load). OASIS's BCHA
matches that file within 0.8 % MAE (-0.5 % to +1.8 %, 192 hours of 2026-11-05..11-08) on the same UTC hour, once the BC
file is read in BC local time (power_bch.py). wcan's BC generation = weim.bcha.load + BC's net exports (bch.intertie.*).
WEIM publishes no per-area generation, and its transfers aren't a BA's whole interchange, so for the other areas this is
load only (their generation stays on EIA-930).

Series (MW, freq 1h): weim.<area>.load, <area> = the TAC_AREA_NAME lowercased with non-alphanumerics as "_" (``CA ISO-TAC``
-> ``ca_iso_tac``, ``PGE-TAC`` (PG&E) -> ``pge_tac``, ``PGE`` (Portland General) -> ``pge``). Sub-areas are stored too
(BANC's members, WALC's two parts, CAISO's TACs; CA ISO-TAC = PGE-TAC + SCE-TAC + SDGE-TAC + VEA-TAC + MWD-TAC). AVRN
(Avangrid Renewables) and GWA (NaturEner) are generation-only areas whose load is always 0: not stored.
Fail closed (FormatDrift): a missing column, a data item other than SYS_FCST_ACT_MW, an interval that isn't one hour or
doesn't parse, a non-numeric MW, a value out of range, conflicting duplicates, no BCHA row, or no complete hour at all.
An area that isn't in AREAS (a new WEIM entrant) is a warning, not a failure, so a new member can't blank BC's load; map it
here. A blank MW is skipped, never written as 0.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://oasis.caiso.com/oasisapi/SingleZip"
WINDOW_H = 48
MAX_BACKFILL = timedelta(days=30)          # OASIS answers up to ~31 days in one query
GROUP = "CAISO OASIS / WEIM actual load"
MAX_MW = 80_000
COLUMNS = ("INTERVALSTARTTIME_GMT", "INTERVALENDTIME_GMT", "TAC_AREA_NAME", "XML_DATA_ITEM", "MW")
ITEM = "SYS_FCST_ACT_MW"
REQUIRED = "BCHA"

# TAC area -> (region file id for the series meta, a readable name). "rest" = no single region.
AREAS: dict[str, tuple[str, str]] = {
    "BCHA": ("wcan", "BC Hydro (BC incl. Lower Mainland)"),
    "BPAT": ("pnw", "Bonneville Power Administration"),
    "PACW": ("pnw", "PacifiCorp West"),
    "PSEI": ("pnw", "Puget Sound Energy"),
    "SCL": ("pnw", "Seattle City Light"),
    "TPWR": ("pnw", "Tacoma Power"),
    "PGE": ("pnw", "Portland General Electric"),
    "AVA": ("pnw", "Avista"),
    "IPCO": ("pnw", "Idaho Power"),
    "NWMT": ("bakken", "NorthWestern Energy (Montana)"),
    "PACE": ("mtnw", "PacifiCorp East"),
    "BHBA": ("rest", "Black Hills"),
    "NEVP": ("sunbelt", "NV Energy"),
    "AZPS": ("sunbelt", "Arizona Public Service"),
    "SRP": ("sunbelt", "Salt River Project"),
    "TEPC": ("sunbelt", "Tucson Electric Power"),
    "PNM": ("sunbelt", "PNM (New Mexico)"),
    "EPE": ("sunbelt", "El Paso Electric"),
    "WALC": ("sunbelt", "WAPA Desert Southwest"),
    "WALCAEPCO": ("sunbelt", "WAPA Desert Southwest: AEPCO part"),
    "WALCDSW": ("sunbelt", "WAPA Desert Southwest: DSW part"),
    "CA ISO-TAC": ("cal", "CAISO (all TACs)"),
    "PGE-TAC": ("cal", "CAISO PG&E TAC"),
    "SCE-TAC": ("cal", "CAISO SCE TAC"),
    "SDGE-TAC": ("cal", "CAISO SDG&E TAC"),
    "VEA-TAC": ("cal", "CAISO Valley Electric TAC"),
    "MWD-TAC": ("cal", "CAISO Metropolitan Water District TAC"),
    "LADWP": ("cal", "Los Angeles Dept. of Water and Power"),
    "TIDC": ("cal", "Turlock Irrigation District"),
    "BANC": ("cal", "Balancing Authority of Northern California"),
    "BANCMID": ("cal", "BANC: Modesto Irrigation District"),
    "BANCRDNG": ("cal", "BANC: Redding"),
    "BANCRSVL": ("cal", "BANC: Roseville"),
    "BANCSMUD": ("cal", "BANC: SMUD"),
    "BANCWASN": ("cal", "BANC: WAPA Sierra Nevada"),
}
GEN_ONLY = {"AVRN", "GWA"}                         # generation-only areas: load is always 0, not stored


def series_id(area: str) -> str:
    return f"weim.{re.sub(r'[^a-z0-9]+', '_', area.lower()).strip('_')}.load"


def _stamp(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H:%M-0000")


def _gmt(s: str, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.strip())
    except ValueError:
        raise FormatDrift(f"OASIS WEIM load: {what} {s!r} doesn't parse") from None
    if t.utcoffset() is None:
        raise FormatDrift(f"OASIS WEIM load: {what} {s!r} has no offset")
    return (t - t.utcoffset()).replace(tzinfo=None)


def _csv_text(content: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        names = z.namelist()
        if not names:
            raise FormatDrift("OASIS WEIM load: empty zip")
        body = z.read(names[0]).decode("utf-8-sig")
    except zipfile.BadZipFile as e:
        raise FormatDrift(f"OASIS WEIM load: not a zip ({e})") from e
    if names[0].lower().endswith(".xml") or body.lstrip().startswith("<"):
        code = re.search(r"<(?:m:)?ERR_CODE>([^<]*)<", body)
        desc = re.search(r"<(?:m:)?ERR_DESC>([^<]*)<", body)
        raise FormatDrift(f"OASIS WEIM load: error reply {code.group(1) if code else '?'} "
                          f"({desc.group(1).strip() if desc else names[0]}) (NO-RESULT)")
    return body


def parse(content: bytes, now: datetime) -> tuple[dict[str, dict[datetime, float]], list[str]]:
    """The OASIS zip -> ({TAC area: {hour start UTC: MW}}, warnings). Only hours ending at or before `now`."""
    rd = csv.DictReader(io.StringIO(_csv_text(content)))
    missing = [c for c in COLUMNS if c not in (rd.fieldnames or [])]
    if missing:
        raise FormatDrift(f"OASIS WEIM load: missing columns {missing}")
    out: dict[str, dict[datetime, float]] = {}
    unknown: set[str] = set()
    for r in rd:
        area = (r["TAC_AREA_NAME"] or "").strip()
        if r["XML_DATA_ITEM"].strip() != ITEM:
            raise FormatDrift(f"OASIS WEIM load: data item {r['XML_DATA_ITEM']!r}, expected {ITEM}")
        start, end = _gmt(r["INTERVALSTARTTIME_GMT"], "interval start"), _gmt(r["INTERVALENDTIME_GMT"], "interval end")
        if end - start != timedelta(hours=1) or start.minute or start.second:
            raise FormatDrift(f"OASIS WEIM load {area}: interval {start} - {end} isn't one clock hour")
        if area in GEN_ONLY:
            continue
        if area not in AREAS:
            unknown.add(area)
            continue
        raw = (r["MW"] or "").strip()
        if raw == "":
            continue                                              # blank: no value, never a 0
        try:
            mw = float(raw)
        except ValueError:
            raise FormatDrift(f"OASIS WEIM load {area} {start}: MW {raw!r} isn't a number") from None
        if not 0 <= mw <= MAX_MW:
            raise FormatDrift(f"OASIS WEIM load {area} {start}: {mw:,.0f} MW out of range")
        if end > now:
            continue                                              # not a complete hour yet
        prev = out.setdefault(area, {}).get(start)
        if prev is not None and prev != mw:
            raise FormatDrift(f"OASIS WEIM load {area} {start}: two values ({prev:g}, {mw:g})")
        out[area][start] = mw
    if not out:
        raise FormatDrift("OASIS WEIM load: no complete hour (NO-RESULT)")
    if not out.get(REQUIRED):
        raise FormatDrift(f"OASIS WEIM load: no {REQUIRED} row (BC Hydro left the WEIM, or the query changed)")
    warnings = [f"OASIS WEIM load: unmapped TAC area(s) {sorted(unknown)} skipped: add them to "
                "power_caiso_weim.AREAS"] if unknown else []
    return out, warnings


class CaisoWeimLoad(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for area, (region, name) in AREAS.items():
            extra = {"tier": "hourly", "tz": "UTC (OASIS GMT interval stamps)", "lag": "~30-40 min after the hour",
                     "oasis_tac_area": area, "region": region}
            out.append(SeriesMeta(series_id(area), self.id, f"{name}: actual hourly load (CAISO OASIS, WEIM {area})",
                                  "MW", "1h", GROUP, extra=extra))
        return out

    def _backfill_marker(self, since: str):
        return paths.home() / "cache" / "caiso_weim" / f"backfilled_from_{since}"

    def fetch(self, ctx: Context) -> FetchResult:
        end = ctx.now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=WINDOW_H)
        # One-off back-fill (Reviewer on #485): the first run after `params.backfill_from` is set reads from that date in
        # the same single request (<= MAX_BACKFILL), then a marker in <home>/cache/caiso_weim stops it repeating.
        since = str(self.spec.get("params", {}).get("backfill_from", "") or "")
        marker = self._backfill_marker(since) if since else None
        if marker is not None and not marker.exists():
            start = min(start, max(datetime.fromisoformat(since), end - MAX_BACKFILL))
        params = {"queryname": "SLD_FCST", "market_run_id": "ACTUAL", "version": "1", "resultformat": "6",
                  "startdatetime": _stamp(start), "enddatetime": _stamp(end)}
        r = ctx.get(URL, params=params, timeout=60, retries=0)          # OASIS: >= 5 s between requests, so no retry
        data, warnings = parse(r.content, ctx.now)
        if marker is not None and not marker.exists() and data:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(_stamp(start), encoding="utf-8")
        res = FetchResult(meta=self.declared_series(), warnings=warnings)
        for area, hours in sorted(data.items()):
            sid = series_id(area)
            for t, mw in sorted(hours.items()):
                res.obs.append(Obs(sid, t, mw, t))
        return res
