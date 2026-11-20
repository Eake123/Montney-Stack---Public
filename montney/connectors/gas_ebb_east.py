"""PNGTS, Iroquois, Transco and Millennium: FERC Operationally Available Capacity by point, summed by region and category
(gas_ebb_common: categories, overrides, the location cache and the no-state guard). Research:
docs/research/pipelines/east_and_rockies.md §2–3. No logins, no captchas.

**PNGTS** (`gas_ebb_pngts`; Quorum myQuorum IPWS, tspno 17):
- POST `web-prd.myquorumcloud.com/PNGPA1IPWS/OpAvailPosting/GetOpAvailPostings?tspno=17` (Kendo form: GasDay=MM/DD/YYYY,
  page/pageSize, X-Requested-With) returns JSON `{"Data": [...]}` with every cycle posted so far for that gas day (~60 KB):
  LocId, LocNm, CycleDescr, PostDate (Central), TotalSchdQty, LocQtyTypeCode (RPQ/DPQ), MeasBasisCode (BZ = MMBtu).
  The newest PostDate is the latest cycle. No cheap check: 1 request per run, +1 a day for the previous gas day.
- Locations (weekly): `…/IPWSFile/IPWSFileHandler?path=%5CTSP_17%5C&fileName=Locations%2FLOCATIONDATA.CSV&d=True` (5 KB).
- Trap: two Newington END points are power plants (PSNH, Essential Power) without "power" in the name: overrides.

**Iroquois** (`gas_ebb_iroquois`; ioly.iroquois.com ExtJS app; it loads an Imperva script but serves no challenge):
- GET `…/infopost/classes/common/RouterClass.php?class=<b64 OperationallyAvailableClass>&type=<b64 getGrdCpctyOperAvail>
  &param=<b64 {"searchDateValue": "MM/DD/YYYY hh:mm AM", "cycleDescValue": "Evening", "locationValue": "All"}>`:
  ~21 KB JSON, 50 rows (25 points × receipt/delivery).
- Trap: a cycle not posted yet still returns every row, with zeros, and "Posting Date/Time" is the moment the report was
  generated, not posted. So the cycle is picked by the clock (IROQUOIS_CYCLES: the board's own scheduled-quantity
  times), an all-zero answer steps back one cycle, and as_of is the cycle's scheduled time. Once a cycle is read the
  board is skipped (0 requests) until the next one is due. No location file: the spec carries the hand map (state,
  type) for every point; power plants per Iroquois's own Power Plant Scheduled Quantities list. A new point with gas
  that the map lacks fails the run (gas_ebb_common's no-state guard) until it is mapped or named in `unlocated_ok`.

**Transco** (`gas_ebb_transco`; Williams 1Line portal API, BUID 80):
- GET `www.1line.williams.com/portal-service/api/portal-service/v1/public/oac/cycle?buid=80&mapId=0&startDate=MM/DD/YYYY`
  (111 B) = the newest cycle number of that gas day: the **cheap check**. Then GET `…/public/oac/report/download/csv?
  buid=80&startDate=…&endDate=…&cycle=<n>&zoneId=0&sortType=HDR` (`oac_data.csv`, ~128 KB, 587 rows; MLQ points, SGQ
  segments, storage STQ/SIQ/SWQ).
- Locations (weekly): `…/public/location-data-download?buId=80&forDate=YYYY-MM-DD` (JSON, ~570 KB): stateAbbr, naesbType
  and Transco's own type (PwGn-Transco = power, Industrial).
- Trap: the market area is posted as aggregates, e.g. `1099998 NEW YORK FAC GROUP` (all of Zone 6 NY, not in the
  location file; an override makes it nyc ldc). The Linden segment (S→N) is the flow north toward NYC.

**Millennium** (`gas_ebb_millennium`; TC eConnects SSRS, asset 26; the midwest_gas.ssrs_export pattern):
- GET the viewer `ReportViewer.aspx?/InfoPost/OperationallyAvailableCapacity&pAssetNbr=26`, then its ExportUrlBase + CSV
  (~80 KB). The report only renders its default, today's **Timely** cycle (later cycles need SSRS parameter post-backs),
  so this board is read once per gas day (2 requests), and has no final.
- Locations (weekly): `ReportViewer.aspx?/InfoPost/LocationDataDownload&assetNbr=26&rs:Format=CSV&rc:NoHeader=true`
  (~70 KB, 20–35 s). Trap: after that direct-CSV render, the next viewer's export in the same ASP.NET session lands on
  Error.aspx, so the TC eConnects cookies are dropped after it and before each viewer (gas_common.drop_cookies).
- Trap: every Millennium point is in New York, but Stagecoach, Sanford (Bluestone) and Laser carry Pennsylvania gas:
  overrides make them int_in (not storage / NY production), and `ebb.millennium.pa.receipts` names them.
- Columbia (asset 51) is left out: its CSV is 7.7 MB (the report footer repeats per row) for ~50 MMcf/d of NY deliveries.

**Requests per run** (every 2 h): PNGTS 1, Iroquois 0–2, Transco 1–2, Millennium 0 (2 once a day); finals +1 to +2 a day
each; location files weekly.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from datetime import date, datetime, timedelta

from montney.connectors.base import Context
from montney.connectors.gas_ebb_common import EbbConnector, Pt, Pull, central, ct_to_utc, current_gas_day, num
from montney.connectors.gas_common import FormatDrift, drop_cookies

# ------------------------------------------------------------------ PNGTS (Quorum)
PNGTS_BASE = "https://web-prd.myquorumcloud.com/PNGPA1IPWS/"
PNGTS_OA = PNGTS_BASE + "OpAvailPosting/GetOpAvailPostings"
PNGTS_LOC = PNGTS_BASE + "IPWSFile/IPWSFileHandler"
QTI_DIR = {"DPQ": "D", "RPQ": "R"}


class EbbPngts(EbbConnector):
    FAMILY = "pngts"

    def _pull(self, ctx: Context, board: dict, day: date) -> Pull | None:
        r = self.post(ctx, PNGTS_OA, params={"tspno": board.get("tspno", 17)}, data={
            "sort": "", "page": 1, "pageSize": 500, "group": "", "filter": "",
            "GasDay": day.strftime("%m/%d/%Y"), "CycleId": "", "LocId": "", "LocNm": ""},
            headers={"X-Requested-With": "XMLHttpRequest"}, timeout=60)
        try:
            rows = r.json()["Data"]
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"PNGTS OA: not the expected Kendo JSON ({e})") from e
        if not rows:
            return None
        need = ("LocId", "LocNm", "CycleDescr", "PostDate", "TotalSchdQty", "LocQtyTypeCode", "MeasBasisCode", "GasDay")
        if any(k not in rows[0] for k in need):
            raise FormatDrift(f"PNGTS OA: fields changed ({sorted(rows[0])[:10]})")
        posted = max(r["PostDate"] for r in rows)
        rows = [r for r in rows if r["PostDate"] == posted]
        if {r["MeasBasisCode"] for r in rows} != {"BZ"} or {r["GasDay"][:10] for r in rows} != {day.isoformat()}:
            raise FormatDrift("PNGTS OA: unit is not BZ (MMBtu) or the gas day is not the one asked for")
        pts: dict[str, Pt] = {}
        for x in rows:
            d = QTI_DIR.get(x["LocQtyTypeCode"])
            if d is None:
                continue
            p = Pt(str(x["LocId"]).strip(), x["LocNm"].strip(), d, float(x["TotalSchdQty"] or 0))
            pts.setdefault(p.key, p)
        return Pull(day, rows[0]["CycleDescr"], ct_to_utc(datetime.fromisoformat(posted[:19])), list(pts.values()))

    def latest(self, ctx, board, bstate):
        return self._pull(ctx, board, current_gas_day(ctx.now))

    def final(self, ctx, board, day):
        return self._pull(ctx, board, day)

    def locations(self, ctx, board):
        r = self.get(ctx, PNGTS_LOC, params={"path": "\\TSP_17\\", "fileName": "Locations/LOCATIONDATA.CSV", "d": "True"},
                     timeout=60)
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace"))))
        if not rows or any(c not in rows[0] for c in ("LOC", "LOC ST ABBREV", "LOC TYPE IND", "UP/DN NAME")):
            raise FormatDrift(f"PNGTS LOCATIONDATA.CSV: header changed ({list(rows[0])[:6] if rows else 'empty'})")
        return {r["LOC"].strip(): (r["LOC ST ABBREV"].strip(), r["LOC TYPE IND"].strip(), (r["UP/DN NAME"] or "").strip())
                for r in rows if r["LOC"]}


# ------------------------------------------------------------------ Iroquois (ExtJS router)
IROQUOIS_URL = "https://ioly.iroquois.com/infopost/classes/common/RouterClass.php"
# (cycle value, nomination effective time, scheduled-quantity time relative to the gas day's date in Central; the
# board's own getCycleDescCpctyOperAvail answer, 2026-11-13: schedQtyTime -1800 = 18:00 the day before)
IROQUOIS_CYCLES = (("Timely", "09:00 AM", -1, 18, 0), ("Evening", "09:00 AM", -1, 22, 0),
                   ("Intraday1", "02:00 PM", 0, 14, 0), ("Intraday2", "06:00 PM", 0, 18, 30),
                   ("Intraday3", "10:00 PM", 0, 23, 0))
IROQUOIS_SLACK = timedelta(minutes=15)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _sched(day: date, c: tuple) -> datetime:
    return datetime(day.year, day.month, day.day, c[3], c[4]) + timedelta(days=c[2])


class EbbIroquois(EbbConnector):
    FAMILY = "iroquois"

    def _grid(self, ctx: Context, day: date, c: tuple) -> Pull | None:
        param = {"searchDateValue": f"{day.strftime('%m/%d/%Y')} {c[1]}", "cycleDescValue": c[0], "locationValue": "All"}
        r = self.get(ctx, IROQUOIS_URL, params={"class": b64("OperationallyAvailableClass"),
                                                "type": b64("getGrdCpctyOperAvail"), "param": b64(json.dumps(param))},
                     timeout=60)
        try:
            rows = r.json()
        except ValueError as e:
            raise FormatDrift(f"Iroquois OA: not JSON ({r.text[:80]!r})") from e
        if not isinstance(rows, list) or not rows or rows[0].get("statusCode") != 1:
            raise FormatDrift(f"Iroquois OA: {str(rows)[:160]}")
        need = ("Loc Name", "Loc", "Loc/QTI Desc", "Total Scheduled Quantity", "Meas Basis Desc")
        if any(k not in rows[0] for k in need):
            raise FormatDrift(f"Iroquois OA: keys changed ({sorted(rows[0])[:8]})")
        if {x["Meas Basis Desc"] for x in rows} != {"MMBtu"}:
            raise FormatDrift("Iroquois OA: unit is not MMBtu")
        pts = []
        for x in rows:
            q = x["Loc/QTI Desc"].lower()
            d = "D" if q.startswith("delivery") else "R" if q.startswith("receipt") else None
            if d is None:
                raise FormatDrift(f"Iroquois OA: unknown Loc/QTI Desc {x['Loc/QTI Desc']!r}")
            pts.append(Pt(str(x["Loc"]).strip(), x["Loc Name"].strip(), d, num(x["Total Scheduled Quantity"])))
        if not any(p.dth for p in pts):
            return None                                  # not posted yet: every row is zero
        return Pull(day, c[0], ct_to_utc(_sched(day, c)), pts)

    def _newest(self, ctx: Context, day: date, upto: int, bstate: dict | None) -> Pull | None:
        for i in range(upto, max(-1, upto - 3), -1):     # step back at most twice
            c = IROQUOIS_CYCLES[i]
            if bstate is not None and bstate.get("cycle") == f"{day.isoformat()}|{c[0]}":
                return None                              # already have the newest posted cycle
            pull = self._grid(ctx, day, c)
            if pull is not None:
                return pull
        return None

    def latest(self, ctx, board, bstate):
        now_c = central(ctx.now)
        day = current_gas_day(ctx.now)
        due = [i for i, c in enumerate(IROQUOIS_CYCLES) if _sched(day, c) + IROQUOIS_SLACK <= now_c]
        return self._newest(ctx, day, due[-1], bstate) if due else None

    def final(self, ctx, board, day):
        return self._newest(ctx, day, len(IROQUOIS_CYCLES) - 1, None)


# ------------------------------------------------------------------ Transco (1Line portal API)
TRANSCO_API = "https://www.1line.williams.com/portal-service/api/portal-service/v1/public/"
TRANSCO_COLS = ("Effective Gas Day", "Cycle Desc", "Posting Date", "Posting Time", "Meas Basis Desc", "Loc",
                "Flow Ind", "Loc QTI", "Loc Name", "Total Scheduled Quantity")
TRANSCO_TYPES = {"PwGn-Transco": "EGN", "Industrial": "IND"}


def parse_transco(text: str) -> tuple[date, str, datetime, list[Pt]]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or any(c not in rows[0] for c in TRANSCO_COLS):
        raise FormatDrift(f"Transco OAC CSV: header changed ({list(rows[0])[:8] if rows else text[:60]!r})")
    if {r["Meas Basis Desc"] for r in rows} != {"MMBtu"}:
        raise FormatDrift("Transco OAC CSV: unit is not MMBtu")
    days, cycles = {r["Effective Gas Day"] for r in rows}, {r["Cycle Desc"] for r in rows}
    if len(days) != 1 or len(cycles) != 1:
        raise FormatDrift(f"Transco OAC CSV: {len(days)} gas days / {len(cycles)} cycles in one file")
    try:
        day = datetime.strptime(days.pop(), "%m/%d/%Y").date()
        posted = max(ct_to_utc(datetime.strptime(f"{r['Posting Date']} {r['Posting Time'].replace(' CT', '')}",
                                                 "%m/%d/%Y %I:%M %p")) for r in rows)
    except ValueError as e:
        raise FormatDrift(f"Transco OAC CSV: bad gas day / posting time ({e})") from e
    pts: dict[str, Pt] = {}
    for r in rows:
        qti, fi = r["Loc QTI"].strip(), r["Flow Ind"].strip()
        if qti == "SGQ":
            d = "S"
        elif qti == "MLQ" and fi in ("D", "R"):
            d = fi
        else:
            continue                                     # storage STQ/SIQ/SWQ, two-way TD1/TD2/BD rows
        p = Pt(r["Loc"].strip(), " ".join(r["Loc Name"].split()), d, num(r["Total Scheduled Quantity"]))
        pts.setdefault(p.key, p)
    return day, cycles.pop(), posted, list(pts.values())


class EbbTransco(EbbConnector):
    FAMILY = "transco"

    def _cycle(self, ctx: Context, board: dict, day: date) -> int:
        r = self.get(ctx, TRANSCO_API + "oac/cycle", params={"buid": board.get("buid", 80), "mapId": 0,
                                                             "startDate": day.strftime("%m/%d/%Y")}, timeout=60)
        try:
            return int(r.json()["data"]["cycle"])
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"Transco oac/cycle: not the expected JSON ({r.text[:80]!r})") from e

    def _csv(self, ctx: Context, board: dict, day: date, cycle: int) -> Pull:
        d = day.strftime("%m/%d/%Y")
        r = self.get(ctx, TRANSCO_API + "oac/report/download/csv", params={
            "buid": board.get("buid", 80), "startDate": d, "endDate": d, "cycle": cycle, "zoneId": 0,
            "sortType": "HDR"}, timeout=120)
        gday, cyc, posted, pts = parse_transco(r.content.decode("utf-8-sig", "replace"))
        if gday != day:
            raise FormatDrift(f"Transco OAC CSV: asked for {day}, got {gday}")
        return Pull(gday, str(cycle), posted, pts, {"desc": cyc})

    def latest(self, ctx, board, bstate):
        day = current_gas_day(ctx.now)
        n = self._cycle(ctx, board, day)
        if n <= 0 or bstate.get("cycle") == f"{day.isoformat()}|{n}":
            return None
        return self._csv(ctx, board, day, n)

    def final(self, ctx, board, day):
        n = self._cycle(ctx, board, day)
        return self._csv(ctx, board, day, n) if n > 0 else None

    def locations(self, ctx, board):
        r = self.get(ctx, TRANSCO_API + "location-data-download",
                     params={"buId": board.get("buid", 80), "forDate": ctx.now.date().isoformat()}, timeout=120)
        try:
            data = r.json()["data"]
            out = {str(d["location"]).strip(): (d.get("stateAbbr") or "",
                                                TRANSCO_TYPES.get(d.get("type") or "", d.get("naesbType") or ""),
                                                d.get("upDnEntityName") or "") for d in data}
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"Transco location-data-download: not the expected JSON ({e})") from e
        if not out:
            raise FormatDrift("Transco location-data-download: empty")
        return out


# ------------------------------------------------------------------ Millennium (TC eConnects SSRS)
TCE_HOST = "ebb.tceconnects.com"
TCE = f"https://{TCE_HOST}"
TCE_OA = TCE + "/infopost/ReportViewer.aspx?/InfoPost/OperationallyAvailableCapacity&pAssetNbr={asset}"
TCE_LOC = TCE + "/infopost/ReportViewer.aspx?/InfoPost/LocationDataDownload&assetNbr={asset}&rs:Format=CSV&rc:NoHeader=true"
TCE_COLS = ("EffGasDay", "Cycle", "PostingDate", "MeasurementBasis", "LocationName", "Location",
            "TotalScheduledQuantity", "LocQTI", "Direction")


def parse_tce(text: str) -> tuple[date, str, datetime, list[Pt]]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or any(c not in rows[0] for c in TCE_COLS):
        raise FormatDrift(f"TC eConnects OA CSV: header changed ({list(rows[0])[:8] if rows else 'empty'})")
    if {r["MeasurementBasis"] for r in rows} != {"BZ"}:
        raise FormatDrift("TC eConnects OA CSV: unit is not BZ (MMBtu)")
    days, cycles = {r["EffGasDay"] for r in rows}, {r["Cycle"] for r in rows}
    if len(days) != 1 or len(cycles) != 1:
        raise FormatDrift(f"TC eConnects OA CSV: {len(days)} gas days / {len(cycles)} cycles in one export")
    try:
        day = datetime.strptime(days.pop(), "%m/%d/%Y").date()
        posted = max(ct_to_utc(datetime.strptime(r["PostingDate"], "%m/%d/%Y %I:%M %p")) for r in rows)
    except ValueError as e:
        raise FormatDrift(f"TC eConnects OA CSV: bad gas day / posting date ({e})") from e
    pts: dict[str, Pt] = {}
    for r in rows:                                       # rows repeat per rate schedule: the first copy counts
        qti = r["LocQTI"].strip()
        d = {"DPQ": "D", "RPQ": "R", "SGQ": "S"}.get(qti)
        if d is None:
            continue
        p = Pt(r["Location"].strip(), " ".join(r["LocationName"].split()), d, num(r["TotalScheduledQuantity"]),
               sub=r["Direction"].strip() if d == "S" else "")
        pts.setdefault(p.key, p)
    return day, cycles.pop(), posted, list(pts.values())


class EbbTce(EbbConnector):
    FAMILY = "tce"

    def latest(self, ctx, board, bstate):
        today = central(ctx.now).date()
        if bstate.get("read") == today.isoformat():
            return None                                  # the report only shows today's Timely: once a day
        drop_cookies(ctx, TCE_HOST)                      # a fresh SSRS session (see locations())
        page = self.get(ctx, TCE_OA.format(asset=board["asset"]), timeout=120).text
        m = re.search(r'"ExportUrlBase":"([^"]+)"', page)
        if not m:
            raise FormatDrift(f"TC eConnects viewer (asset {board['asset']}): no ExportUrlBase")
        r = self.get(ctx, TCE + m.group(1).encode().decode("unicode_escape") + "CSV", timeout=120)
        if "csv" not in r.headers.get("content-type", ""):
            raise FormatDrift(f"TC eConnects export: {r.headers.get('content-type')!r}, not CSV")
        day, cyc, posted, pts = parse_tce(r.content.decode("utf-8-sig", "replace"))
        bstate["read"] = today.isoformat()
        return Pull(day, cyc, posted, pts)

    def locations(self, ctx, board):
        r = self.get(ctx, TCE_LOC.format(asset=board["asset"]), timeout=120)
        drop_cookies(ctx, TCE_HOST)      # the direct-CSV render breaks the next viewer's export in the same session
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace"))))
        if not rows or any(c not in rows[0] for c in ("Loc", "Loc St Abbrev", "Loc Type Ind", "Up/Dn Name")):
            raise FormatDrift(f"TC eConnects LocationDataDownload: header changed ({list(rows[0])[:6] if rows else ''})")
        return {r["Loc"].strip(): (r["Loc St Abbrev"].strip(), r["Loc Type Ind"].strip(), r["Up/Dn Name"].strip())
                for r in rows if r["Loc"].strip()}
