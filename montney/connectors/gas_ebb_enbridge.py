"""Enbridge US interstate pipelines (Algonquin, Texas Eastern, Maritimes & Northeast US): FERC Operationally Available
Capacity by point, summed by region and category (gas_ebb_common: categories, overrides, the location cache and the
no-state guard). Research: docs/research/pipelines/east_and_rockies.md §1.

**Method** (rtba.enbridge.com, ASP.NET WebForms; no login, no captcha; robots.txt is a 404):
1. GET `/InformationalPosting/Default.aspx?bu=<AG|TE|mn>&Type=OA` (~36 KB). The form's cycle dropdown
   (`…$ucSelector$ddlSelector`) defaults to the newest cycle of the gas day in the date picker (`…$ucDate$rdpDate`).
   **Cheap check:** when (date, selected cycle) equals the last run's, the board is skipped after this one GET.
2. POST the form back with `__EVENTTARGET=…$oaDefault$hlDown$LinkButton1`: a CSV attachment
   (`AG_OA_20261113_LATE_2026-11-13_0901.csv`; AG 45 KB, TE 177 KB, mn 7 KB). Columns: Cycle_Desc, Post_Date, Eff_Gas_Day,
   Post_Time, Loc, Loc_Name, Flow_Ind_Desc (Delivery / Receipt / Storage Injection / Storage Withdrawal), Loc_QTI_Desc,
   Meas_Basis_Desc (MMBtu), Total_Scheduled_Quantity, …; numbers carry thousands commas; times are Central.
3. Once a day after 10:00 CT, the previous gas day's final: POST the date picker (`__EVENTTARGET=…$ucDate$rdpDate`, the
   new date in rdpDate, its text input and the Telerik ClientState), which returns that day's cycle list (~56 cycles:
   TIMELY, LATE/LATEC, then INTRDY/INTRDYC through 09:00 the next morning), then POST the download with the LAST one.
- Location file (weekly, cached): GET `infopost.enbridge.com/api/point-data?pipelineId=<AG|TE|MNUS>&pointType=AllPoints`
  (the Azure-function API behind LINK's "Location Data Download"; AG 50 KB, TE 220 KB, MNUS 12 KB): Loc (leading tab),
  Loc St Abbrev, Loc Type Ind, Up/Dn Name.

**Requests** per board: 1 when nothing new, 2 with a new cycle, +2 once a day for the final, +1 weekly for the location
file. Three boards, every 2 h: ≤ 6 per run, ~3–6 typical; ~60/day + 6 finals + ~0.4 location files.

**Traps:** MNUS is `bu=mn` on rtba but `MNUS` on the location API. Tetco's Con Ed Manhattan meter (73722) is coded NJ /
Hudson; its spec override puts it in nyc. M&NE flows either way at Baileyville (an override makes it export_canada and
the Brunswick meter import_canada). STA points (laterals, in-line transfer points) are internal and dropped.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta

from montney.connectors.base import Context
from montney.connectors.gas_ebb_common import EbbConnector, Pt, Pull, ct_to_utc, num
from montney.connectors.gas_common import FormatDrift

RTBA = "https://rtba.enbridge.com/InformationalPosting/Default.aspx"
POINTS_API = "https://infopost.enbridge.com/api/point-data"
SEL = "ctl00$MainContent$ctl01$oaDefault$ucSelector$ddlSelector"
DATE = "ctl00$MainContent$ctl01$oaDefault$ucDate$rdpDate"
DATE_STATE = "ctl00_MainContent_ctl01_oaDefault_ucDate_rdpDate_dateInput_ClientState"
DOWN = "ctl00$MainContent$ctl01$oaDefault$hlDown$LinkButton1"
OA_COLS = ("Cycle_Desc", "Post_Date", "Post_Time", "Eff_Gas_Day", "Loc", "Loc_Name", "Flow_Ind_Desc",
           "Total_Scheduled_Quantity", "Meas_Basis_Desc")
LOC_COLS = ("Loc", "Loc St Abbrev", "Loc Type Ind", "Up/Dn Name")
DIRECTION = {"delivery": "D", "storage injection": "D", "receipt": "R", "storage withdrawal": "R"}


def form_fields(page: str) -> tuple[dict[str, str], list[str]]:
    """(the form's post-back fields, the cycle dropdown's options in page order)."""
    f: dict[str, str] = {}
    for m in re.finditer(r"<input[^>]+>", page):
        tag = m.group(0)
        n = re.search(r'name="([^"]+)"', tag)
        typ = re.search(r'type="([^"]+)"', tag)
        if not n or (typ and typ.group(1).lower() in ("submit", "button", "image", "checkbox", "radio")):
            continue
        v = re.search(r'value="([^"]*)"', tag)
        f[n.group(1)] = (v.group(1) if v else "").replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    opts: list[str] = []
    m = re.search(r'<select[^>]+name="%s"[^>]*>(.*?)</select>' % re.escape(SEL), page, re.S)
    if m:
        found = re.findall(r'<option([^>]*)value="([^"]*)"', m.group(1))
        opts = [v for _, v in found]
        sel = [v for a, v in found if "selected" in a]
        if opts:
            f[SEL] = sel[0] if sel else opts[-1]
    if "__VIEWSTATE" not in f or DATE not in f or not opts:
        raise FormatDrift("Enbridge rtba: the OA form lost its viewstate, date picker or cycle list")
    return f, opts


def parse_oa(text: str) -> tuple[date, str, datetime, list[Pt]]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or any(c not in rows[0] for c in OA_COLS):
        raise FormatDrift(f"Enbridge OA CSV: header changed ({list(rows[0])[:8] if rows else 'empty'})")
    if {r["Meas_Basis_Desc"] for r in rows} != {"MMBtu"}:
        raise FormatDrift(f"Enbridge OA CSV: unit {sorted({r['Meas_Basis_Desc'] for r in rows})}, not MMBtu")
    days, cycles = {r["Eff_Gas_Day"] for r in rows}, {r["Cycle_Desc"] for r in rows}
    if len(days) != 1 or len(cycles) != 1:
        raise FormatDrift(f"Enbridge OA CSV: {len(days)} gas days / {len(cycles)} cycles in one file")
    try:
        day = datetime.strptime(days.pop(), "%m-%d-%Y").date()
        posted = max(ct_to_utc(datetime.strptime(f"{r['Post_Date']} {r['Post_Time']}", "%m-%d-%Y %H:%M")) for r in rows)
    except ValueError as e:
        raise FormatDrift(f"Enbridge OA CSV: bad gas day / post time ({e})") from e
    pts: dict[str, Pt] = {}
    for r in rows:
        d = DIRECTION.get(r["Flow_Ind_Desc"].strip().lower())
        if d is None:
            raise FormatDrift(f"Enbridge OA CSV: unknown Flow_Ind_Desc {r['Flow_Ind_Desc']!r}")
        p = Pt(r["Loc"].strip(), " ".join(r["Loc_Name"].split()), d, num(r["Total_Scheduled_Quantity"]))
        pts.setdefault(p.key, p)
    return day, cycles.pop(), posted, list(pts.values())


class EbbEnbridge(EbbConnector):
    FAMILY = "enbridge"

    def _page(self, ctx: Context, board: dict) -> tuple[str, dict, list[str]]:
        url = f"{RTBA}?bu={board['bu']}&Type=OA"
        f, opts = form_fields(self.get(ctx, url, timeout=60).text)
        return url, f, opts

    def _download(self, ctx: Context, url: str, f: dict, cycle: str) -> Pull:
        body = {**f, "__EVENTTARGET": DOWN, "__EVENTARGUMENT": "", SEL: cycle}
        r = self.post(ctx, url, data=body, timeout=120)
        if "attachment" not in r.headers.get("content-disposition", ""):
            raise FormatDrift(f"Enbridge rtba download: no CSV attachment ({r.headers.get('content-type')})")
        day, cyc, posted, pts = parse_oa(r.content.decode("utf-8-sig", "replace"))
        if cyc != cycle:
            raise FormatDrift(f"Enbridge rtba: asked for cycle {cycle}, got {cyc}")
        return Pull(day, cyc, posted, pts)

    def latest(self, ctx, board, bstate):
        url, f, opts = self._page(ctx, board)
        self._last = (url, f)
        if bstate.get("cycle") == f"{f[DATE]}|{f[SEL]}":
            return None
        return self._download(ctx, url, f, f[SEL])

    def final(self, ctx, board, day):
        url, f = getattr(self, "_last", (None, None)) or (None, None)
        if f is None or url != f"{RTBA}?bu={board['bu']}&Type=OA":
            url, f, _ = self._page(ctx, board)
        iso = day.isoformat()
        stamp = f"{iso}-00-00-00"
        body = {**f, "__EVENTTARGET": DATE, "__EVENTARGUMENT": "", DATE: iso,
                f"{DATE}$dateInput": f"{day.month}/{day.day}/{day.year}",
                DATE_STATE: json.dumps({"enabled": True, "emptyMessage": "", "validationText": stamp,
                                        "valueAsString": stamp, "minDateStr": "1980-01-01-00-00-00",
                                        "maxDateStr": "2099-12-31-00-00-00",
                                        "lastSetTextBoxValue": f"{day.month}/{day.day}/{day.year}"})}
        f2, opts = form_fields(self.post(ctx, url, data=body, timeout=60).text)
        if f2[DATE] != iso:
            raise FormatDrift(f"Enbridge rtba: date post-back gave {f2[DATE]}, not {iso}")
        pull = self._download(ctx, url, f2, opts[-1])
        if pull.day != day:
            raise FormatDrift(f"Enbridge rtba: final for {iso} came back as {pull.day}")
        return pull

    def locations(self, ctx, board):
        r = self.get(ctx, POINTS_API, params={"pipelineId": board["points_id"], "pointType": "AllPoints"},
                     headers={"Accept": "text/csv, text/plain, */*"}, timeout=60)
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace"))))
        if not rows or any(c not in rows[0] for c in LOC_COLS):
            raise FormatDrift(f"Enbridge point-data: header changed ({list(rows[0])[:6] if rows else 'empty'})")
        return {r["Loc"].strip(): (r["Loc St Abbrev"].strip(), r["Loc Type Ind"].strip(), r["Up/Dn Name"].strip())
                for r in rows if r["Loc"].strip()}
