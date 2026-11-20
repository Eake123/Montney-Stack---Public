"""Guardian and Viking (DT Midstream, Trellis): FERC Operationally Available Capacity by point, summed by region and
category (gas_ebb_common). Research: docs/research/pipelines/east_and_rockies.md §7. No login (the modal is for
customer functions only), no captcha; dtmidstream.trellisenergy.com has no robots.txt (404).

**Method** (`gas_ebb_dtm`, one board per pipe: Guardian GPL tspId 11, Viking VGT tspId 9):
1. GET `/ptms/home/infopost/<code>` opens a session (JSESSIONID) on that pipe; the page carries `var tspId = "<n>"`,
   checked against the spec. **Trap:** the report list follows the session's pipe and ignores its `tspId`: a Viking
   list asked under a Guardian session returns Guardian's reports (seen live 2026-11-13; the no-state guard caught it).
   So each pipe's page is read right before its list, every list row's `mngPplCode` must be the board's code, and the
   XML's Backbone must name it. The file download itself serves any pipe's id in any session.
2. GET `/ptms/public/infopost/getInfoPostRpts.do?tspId=<n>&rptId=2&startDate=<MM/DD/YYYY>&endDate=...`
   (X-Requested-With) lists the Operationally Available Capacity reports posted for those gas days, one row per cycle:
   `id` (e.g. 77732000000), `formattedGasDay`, `cycleCode` (Timely, Evening, Intraday 1-3), `runDate`. The list spans
   the last three gas days, so it serves the cheap check (newest cycle of today already read? then no download) and
   yesterday's final (its Intraday 3) without another call.
3. GET `/ptms/public/infopost/getInfoPostRptBinaryFile.do?infoPostDataId=<id>&tspId=<n>` returns
   `OperationalAvailableCapacity.xml` (Guardian ~17 KB, Viking ~30 KB): `<RptFld>` headers (Loc Name, Loc, Loc Prop,
   Loc Purp Desc, Flow Ind, Loc/QTI, All Qty Avail, DC, OPC, TSQ, OAC, IT, Qty Reason) after a hidden row-number field,
   `<nvpair>`s CycleDesc, Posting Date/Time (Central), Eff Gas Day, Meas Basis Desc (MMBtu), and `<row><cell>` rows.
   **Trap:** `infoPostDataId` takes the list row's `id` (77732000000), not its `infoPostDataId` (77732): that one
   answers HTTP 500.

**Points**: no state anywhere in Trellis's public pages, so the spec carries the whole hand map (state, NAESB type)
for every point; a point with gas and no override fails the run (`require_located`), and an overridden point that
disappears is FormatDrift. Segment rows (compressor stations, main-line valves) have no Loc: their key is the Loc Prop
(`BLUFF CRK:S:R`, `OSCEOLA:S:R`). Pool points (MLQ, Wisconsin Electric / Wisconsin Gas / WPS pools) are dropped.

**Borders** (named):
- Guardian IL -> WI = the Bluff Creek CS segment (Walworth County, WI): 430,408 Dth on 2026-11-13 Intraday 1, against
  430,548 for Guardian's Wisconsin deliveries − receipts (the same-basis `ebb.guardian.umw.net`).
- Viking Canada -> MN = the Emerson receipt (TC's side is `tcm.emerson.viking_deliveries`; pick one, not both).
- Viking MN -> WI = the Osceola CS segment (Polk County, WI, just past the St. Croix crossing): 173,457, against
  ~164,000 for Viking's Wisconsin net. The gap is fuel and a few Wisconsin points upstream of the station, and some
  small points' states are inferred (see the spec notes). MN and WI are both umw: this one is for the map, not for the
  region balance.
- Viking MN -> ND = its two North Dakota deliveries, Fargo and Grand Forks (bakken).

**Requests per run** (every 2 h): 2 pages + 2 lists + 0-2 XMLs (only when a new cycle is posted) + 0-2 XMLs once a
day (yesterday's Intraday 3): ~58 a day on dtmidstream.trellisenergy.com, ~1.2 MB a day.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from montney.connectors.base import Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.connectors.gas_ebb_common import EbbConnector, Pt, Pull, ct_to_utc, current_gas_day, num

BASE = "https://dtmidstream.trellisenergy.com/ptms/"
LIST = BASE + "public/infopost/getInfoPostRpts.do"
FILE = BASE + "public/infopost/getInfoPostRptBinaryFile.do"
FIELDS = ("Loc Name", "Loc", "Loc Prop", "Flow Ind", "Loc/QTI", "TSQ")
LIST_KEYS = ("id", "formattedGasDay", "cycleCode", "runDate", "mngPplCode")
FINAL_CYCLE = "Intraday 3"
QTI_DIR = {"DPQ": "D", "RPQ": "R"}


def parse_xml(content: bytes, day: date, pipe: str, code: str) -> Pull:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise FormatDrift(f"{pipe} OA: not XML ({content[:80]!r})") from e
    hdr = [x.text or "" for x in root.iter("RptFld")]
    if any(f not in hdr for f in FIELDS):
        raise FormatDrift(f"{pipe} OA: header changed ({hdr[:8]})")
    col = {f: hdr.index(f) for f in FIELDS}
    nv = {x.get("name"): (x.text or "").strip() for x in root.iter("nvpair")}
    if f"({code})" not in nv.get("Backbone", ""):
        raise FormatDrift(f"{pipe} OA: the file is for {nv.get('Backbone')!r}, not {code}")
    if nv.get("Meas Basis Desc") != "MMBtu":
        raise FormatDrift(f"{pipe} OA: unit is {nv.get('Meas Basis Desc')!r}, not MMBtu")
    try:
        if datetime.strptime(nv["Eff Gas Day"], "%m/%d/%Y").date() != day:
            raise FormatDrift(f"{pipe} OA: gas day {nv['Eff Gas Day']}, asked for {day}")
        posted = ct_to_utc(datetime.strptime(nv["Posting Date/Time"], "%m/%d/%Y %I:%M:%S %p"))
        cycle = nv["CycleDesc"]
    except (KeyError, ValueError) as e:
        raise FormatDrift(f"{pipe} OA: header fields changed ({e})") from e
    pts: dict[str, Pt] = {}
    for row in root.iter("row"):
        c = [x.text or "" for x in row.findall("cell")]
        if len(c) < len(hdr):
            raise FormatDrift(f"{pipe} OA: a row has {len(c)} cells for {len(hdr)} fields")
        loc, name, flow = c[col["Loc"]].strip(), c[col["Loc Name"]].strip(), c[col["Flow Ind"]].strip()
        q = num(c[col["TSQ"]])
        if loc:
            d = QTI_DIR.get(c[col["Loc/QTI"]].strip()) or {"D": "D", "R": "R"}.get(flow)
            if d is None:
                raise FormatDrift(f"{pipe} OA: point {loc} has no direction ({c[col['Loc/QTI']]!r}, {flow!r})")
            p = Pt(loc, name, d, q)
        else:                                            # a segment (station / valve): Loc Prop names it
            prop = c[col["Loc Prop"]].strip()
            if not prop or flow not in ("R", "D"):
                raise FormatDrift(f"{pipe} OA: segment row without Loc Prop / flow ({name!r})")
            p = Pt(prop, name, "S", q, sub=flow)
        pts.setdefault(p.key, p)
    if not pts:
        raise FormatDrift(f"{pipe} OA: no rows")
    return Pull(day, cycle, posted, list(pts.values()))


def _run_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc).replace(tzinfo=None)


class EbbDtm(EbbConnector):
    FAMILY = "dtm"

    def fetch(self, ctx: Context) -> FetchResult:
        self._lists: dict[str, list[dict]] = {}
        return super().fetch(ctx)

    def _open(self, ctx: Context, board: dict) -> None:
        """Point the session at this board's pipe: the report list read next answers for it."""
        r = self.get(ctx, BASE + f"home/infopost/{board['code']}", timeout=60, retries=1)
        m = re.search(r'var\s+tspId\s*=\s*"(\d+)"', r.text)
        if not m or int(m.group(1)) != int(board["tsp"]):
            raise FormatDrift(f"{board['pipe']}: the infopost page's tspId is {m and m.group(1)}, not {board['tsp']}")

    def _list(self, ctx: Context, board: dict, day: date) -> list[dict]:
        if board["pipe"] not in self._lists:
            self._open(ctx, board)
            r = self.get(ctx, LIST, params={
                "tspId": board["tsp"], "rptId": 2, "cycleId": "",
                "startDate": (day - timedelta(days=2)).strftime("%m/%d/%Y"), "endDate": day.strftime("%m/%d/%Y"),
                "_search": "false", "rows": 50, "page": 1, "sidx": "", "sord": "asc"},
                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=60, retries=1)
            try:
                rows = r.json()["rows"]
            except (ValueError, KeyError, TypeError) as e:
                raise FormatDrift(f"{board['pipe']} report list: not the expected JSON ({r.text[:80]!r})") from e
            if rows and any(k not in rows[0] for k in LIST_KEYS):
                raise FormatDrift(f"{board['pipe']} report list: keys changed ({sorted(rows[0])[:10]})")
            other = {x["mngPplCode"] for x in rows} - {board["code"]}
            if other:
                raise FormatDrift(f"{board['pipe']} report list: rows for {sorted(other)}, not {board['code']}")
            self._lists[board["pipe"]] = rows
        return self._lists[board["pipe"]]

    def _day_rows(self, ctx: Context, board: dict, day: date) -> list[dict]:
        rows = self._list(ctx, board, current_gas_day(ctx.now))
        mine = [r for r in rows if r["formattedGasDay"] == day.strftime("%m/%d/%Y")]
        return sorted(mine, key=lambda r: _run_utc(r["runDate"]))

    def _pull(self, ctx: Context, board: dict, day: date, row: dict) -> Pull:
        r = self.get(ctx, FILE, params={"infoPostDataId": row["id"], "tspId": board["tsp"]}, timeout=60, retries=1)
        pull = parse_xml(r.content, day, board["pipe"], board["code"])
        if pull.cycle != row["cycleCode"]:
            raise FormatDrift(f"{board['pipe']} OA: file says {pull.cycle!r}, list said {row['cycleCode']!r}")
        return pull

    def latest(self, ctx, board, bstate):
        day = current_gas_day(ctx.now)
        rows = self._day_rows(ctx, board, day)
        if not rows:
            return None
        row = rows[-1]
        if bstate.get("cycle") == f"{day.isoformat()}|{row['cycleCode']}":
            return None
        return self._pull(ctx, board, day, row)

    def final(self, ctx, board, day):
        rows = self._day_rows(ctx, board, day)
        if not rows or rows[-1]["cycleCode"] != FINAL_CYCLE:
            return None                                  # not posted yet (or outside the list): next run
        return self._pull(ctx, board, day, rows[-1])
