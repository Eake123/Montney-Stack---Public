"""National Fuel Gas Supply and Empire Pipeline: FERC Operationally Available Capacity by point, summed by region and
category (gas_ebb_common). Western and central New York: Buffalo, Niagara, Rochester, Syracuse, the Empire Connector.

**Method** (both boards are one Oracle PeopleSoft "informational postings" site, `sbsprd2.natfuel.com`, opened as the
public guest: no login, no captcha; robots.txt is a 404; the menus at informationalpostings.natfuel.com link here):
1. GET the OA component `/psc/<site>/<portal>/SBSPRD/c/NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL` (site/portal
   `sbsprd/NFSBS` = National Fuel Gas Supply, `ebsprd/NFSBSEMP` = Empire; ~30 KB). A client without the session cookie
   gets a 302 to the same URL + `?&` that sets it, then the page: 2 hits on the server. The page lists the cycles posted
   for the gas day in the Flow Date box (`NF_WRK_NF_FLOW_DT_FROM`), newest first (row `$0`): Preliminary, Timely,
   Evening, Intraday 1-3. **Cheap check:** when (gas day, newest cycle) equals the last run's, the board stops here.
2. When the box's date isn't the gas day wanted (before 09:00 CT it shows the calendar date), POST the form with the new
   date and `ICAction=NF_WRK_NF_SELECTED_SW` (the Retrieve button): the page for that day.
3. POST the form with `ICAction=NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0` ("Download CSV" of the newest cycle): the answer is
   the page again with `window.open('<…>/psc/<site>/view/<token>/OP_CAP_<MMDDYYYY>_<cycle>_<stamp>.csv')`.
4. GET that URL: the NAESB OA CSV (Supply ~57 KB / 300 rows, Empire ~7 KB / 38 rows). A header block (Effective Gas
   Day, Cycle Indicator Description, Meas Basis Desc = MMBTU, Posting Date / Posting Time in Central "CCT"), then
   columns `Loc Name, Loc, Loc Purp Desc (Receipt / Delivery Location, P/L Seg n), Loc/QTI, County, State, …, Total
   Scheduled Quantity, Flow Indicator, Qty Reason, Comments`. as_of = the posting time; ts = the gas day.
Once a day after 10:00 CT: the previous gas day's newest cycle (Intraday 3) by steps 2-4 on the same session.
Each board starts a fresh session (the two sites share one JSESSIONID cookie name), so the cookies are dropped first.

**Requests** (server hits) per board: 2 when nothing is new, 4 with a new cycle (5 before 09:00 CT), +3 once a day for
the final. Two boards every 2 h: ≤ 14 a run; ~6 cycles a day each: ~70-80 hits/day on sbsprd2.natfuel.com.

**No location file:** the CSV carries the state. The NAESB location list (Supply's is a 4.8 MB page, 1,100 rows) mostly
says INT where an LDC sits, so the spec carries a hand map (`type`) for the New York points instead, from those lists
(2026-11-13). Production meters (`PSP…`, `NSP…`) and Appalachian production pools (`PL0…`) are typed WHD by id: the
zone aggregate `APPZN3 New York Points` = the NY meters + the NY pools, so both are physical receipts. A New York point
with gas and no type counts as int_in / int_out (the pipe's net stays right) and warns.

**Traps:**
- Aggregates with no state (`APPZN1-9`, `Z20 GROUP`, `M- STORAGE PTS`, `M_LEIDY_LOCAL`, `IMBALANCE`, storage top-gas
  rows) would double count; they pass the shared no-state guard (gas_ebb_common.require_located) by name (the board's
  `unlocated_ok`) and are counted nowhere. Any other point with gas and no state fails the run (FormatDrift).
- City-gate subsets ("This point is a subset of NFGDC-NY") repeat part of the `NFGDC-NY` aggregate: skipped.
- `P/L Seg` rows are segments even when coded RPQ (YM-53 / YM-52).
- Supply's storage is posted only as a system total (`M- STORAGE PTS`, NY + PA fields): named system-wide series, kept
  out of the region sums, so `ebb.natfuel.nyc.net` is the pipe's New York balance before storage.
- Supply's "TCPL - Niagara" delivery (421079) moves on Tennessee's Niagara Spur (leased capacity): Tennessee books it
  as a receipt from National Fuel (East Aurora / Hamburg) and posts the whole Niagara meter as its own delivery to TC.
  So it's int_out here (cancels against Tennessee's int_in), never export_canada: TC's tcm.niagara.imports ≈ Tennessee's
  Niagara + Empire's Chippawa net, not + this.
"""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import date, datetime

from montney.connectors.base import Context, FetchResult
from montney.connectors.gas_ebb_common import (EbbConnector, Pt, Pull, _override, _overrides, ct_to_utc,
                                               current_gas_day, num, state_regions)
from montney.connectors.gas_common import FormatDrift, drop_cookies

HOST = "sbsprd2.natfuel.com"
OA = f"https://{HOST}/psc/{{site}}/{{portal}}/SBSPRD/c/NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL"
RETRIEVE = "NF_WRK_NF_SELECTED_SW"
DOWNLOAD = "NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0"
DATE_BOX = "NF_WRK_NF_FLOW_DT_FROM"
CYCLE_RE = re.compile(r"id='NV_OPER_AVAIL_T_NF_SM_CYCL_PROC_CD\$(\d+)'\s*>([^<]*)<")
DATE_RE = re.compile(r"id='" + DATE_BOX + r"'[^>]*?value=[\"']([0-9/]*)[\"']")
OPEN_RE = re.compile(r"window\.open\('([^']+?\.csv)'")
COLS = ("Loc Name", "Loc", "Loc Purp Desc", "Loc/QTI", "State", "Total Scheduled Quantity", "Comments")
PRODUCTION_IDS = ("PSP", "NSP", "PL0")


def form_fields(page: str) -> dict[str, str]:
    """The PeopleSoft form's post-back fields (hidden inputs and the date box)."""
    m = re.search(r"<form[^>]*name=['\"]win0['\"].*?</form>", page, re.S)
    out: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", m.group(0) if m else page):
        if re.search(r"type=['\"]?(checkbox|radio|button|submit|image)", tag, re.I):
            continue
        n = re.search(r"name=['\"]([^'\"]+)['\"]", tag)
        v = re.search(r"value=(['\"])(.*?)\1", tag)
        if n:
            out[n.group(1)] = html.unescape(v.group(2)) if v else ""
    return out


def page_day(page: str) -> date | None:
    m = DATE_RE.search(page)
    try:
        return datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
    except ValueError:
        return None


def page_cycles(page: str) -> list[str]:
    """Cycle names on the page, newest first (row $0 first)."""
    return [html.unescape(c).strip() for _, c in sorted(CYCLE_RE.findall(page), key=lambda t: int(t[0]))]


def parse_oa(text: str) -> tuple[date, str, datetime, list[Pt]]:
    """(gas day, cycle, posted (naive UTC), points) from a National Fuel / Empire OA CSV."""
    i = text.find('"Loc Name"')
    if i < 0:
        raise FormatDrift(f"National Fuel OA CSV: no column header ({text[:80]!r})")
    head = {}
    for line in text[:i].splitlines():
        k, sep, v = line.partition(":")
        if sep:
            head[k.strip()] = v.strip()
    need = ("Effective Gas Day", "Cycle Indicator Description", "Meas Basis Desc", "Posting Date", "Posting Time")
    if any(k not in head for k in need):
        raise FormatDrift(f"National Fuel OA CSV: header block changed ({sorted(head)[:8]})")
    if head["Meas Basis Desc"].upper() != "MMBTU":
        raise FormatDrift(f"National Fuel OA CSV: unit is {head['Meas Basis Desc']!r}, not MMBTU")
    try:
        day = datetime.strptime(head["Effective Gas Day"], "%m/%d/%Y").date()
        when = f"{head['Posting Date']} {head['Posting Time'].replace('CCT', '').strip()}"
        posted = ct_to_utc(datetime.strptime(when, "%m/%d/%Y %I:%M %p"))
    except ValueError as e:
        raise FormatDrift(f"National Fuel OA CSV: bad gas day / posting time ({e})") from e
    rows = list(csv.DictReader(io.StringIO(text[i:])))
    if not rows or any(c not in rows[0] for c in COLS):
        raise FormatDrift(f"National Fuel OA CSV: columns changed ({list(rows[0])[:8] if rows else 'empty'})")
    pts: dict[str, Pt] = {}
    for r in rows:
        loc = (r.get("Loc") or "").strip()
        if not loc:
            continue                                     # blank lines, the footnote
        name = " ".join((r["Loc Name"] or "").split())
        qti = (r["Loc/QTI"] or "").strip()
        if (r["Loc Purp Desc"] or "").startswith("P/L Seg"):
            d = "S"                                      # segments, even the ones coded RPQ
        elif qti in ("DPQ", "RPQ"):
            d = "D" if qti == "DPQ" else "R"
            if "subset of" in (r["Comments"] or "").lower():
                continue                                 # part of an aggregate city gate posted on its own row too
        else:
            continue
        ltype = "WHD" if d == "R" and loc.upper().startswith(PRODUCTION_IDS) else ""
        p = Pt(loc, name, d, num(r["Total Scheduled Quantity"]), state=(r["State"] or "").strip().upper(), ltype=ltype)
        pts.setdefault(p.key, p)
    return day, head["Cycle Indicator Description"], posted, list(pts.values())


class EbbNatFuel(EbbConnector):
    FAMILY = "natfuel"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._page: dict[str, str] = {}              # pipe -> the component's last page in this run (its form state)
        self._warn: list[str] = []

    # ---- the PeopleSoft component
    def _url(self, board: dict) -> str:
        return OA.format(site=board["site"], portal=board["portal"])

    def _check(self, board: dict, page: str) -> str:
        if DATE_RE.search(page) is None or "ICSID" not in page:
            raise FormatDrift(f"{board['pipe']}: not the PeopleSoft OA page ({page[:80]!r})")
        self._page[board["pipe"]] = page
        return page

    def _open(self, ctx: Context, board: dict) -> str:
        drop_cookies(ctx, HOST)                          # a fresh guest session per board
        return self._check(board, self.get(ctx, self._url(board), timeout=60).text)

    def _post(self, ctx: Context, board: dict, action: str, **extra: str) -> str:
        data = {**form_fields(self._page[board["pipe"]]), "ICAction": action, **extra}
        return self.post(ctx, self._url(board), data=data, timeout=120).text

    def _for_day(self, ctx: Context, board: dict, day: date) -> list[str]:
        """Put the page on gas day `day` (Retrieve when needed); the cycles posted for it, newest first."""
        page = self._page.get(board["pipe"]) or self._open(ctx, board)
        if page_day(page) != day:
            page = self._check(board, self._post(ctx, board, RETRIEVE, **{DATE_BOX: day.strftime("%m/%d/%Y")}))
            if page_day(page) != day:
                raise FormatDrift(f"{board['pipe']}: asked for {day}, the page shows {page_day(page)}")
        return page_cycles(page)

    def _download(self, ctx: Context, board: dict, day: date, cycle: str) -> Pull:
        page = self._post(ctx, board, DOWNLOAD)
        m = OPEN_RE.search(page)
        if not m:
            raise FormatDrift(f"{board['pipe']}: the CSV download didn't answer with a file link")
        self._check(board, page)
        r = self.get(ctx, html.unescape(m.group(1)), timeout=120)
        if "csv" not in r.headers.get("content-type", "") and '"Loc Name"' not in r.text[:3000]:
            raise FormatDrift(f"{board['pipe']}: the download is {r.headers.get('content-type')!r}, not CSV")
        gday, cyc, posted, pts = parse_oa(r.content.decode("utf-8-sig", "replace"))
        if gday != day or cyc.replace(" ", "").lower() != cycle.replace(" ", "").lower():
            raise FormatDrift(f"{board['pipe']}: asked for {day} {cycle}, got {gday} {cyc}")
        self._untyped(board, pts)
        return Pull(gday, cyc, posted, pts)        # aggregate() applies the no-state guard (require_located)

    def _untyped(self, board: dict, pts: list[Pt]) -> None:
        ov = _overrides(board)
        states = {s for s, r in state_regions().items() if r in set(board.get("regions") or [])}
        loose = sorted({p.name or p.loc for p in pts if p.dirn in ("D", "R") and p.dth > 0 and p.state in states
                        and not p.ltype and not any(_override(ov, p).get(k) for k in ("type", "category", "drop"))})
        if loose:
            self._warn.append(f"{board['pipe']}: {len(loose)} in-region point(s) with gas and no type, counted as "
                              f"int_in / int_out: {', '.join(loose[:8])}")

    # ---- EbbConnector
    def latest(self, ctx, board, bstate):
        self._page.pop(board["pipe"], None)
        day = current_gas_day(ctx.now)
        cycles = self._for_day(ctx, board, day)
        if not cycles or bstate.get("cycle") == f"{day.isoformat()}|{cycles[0]}":
            return None
        return self._download(ctx, board, day, cycles[0])

    def final(self, ctx, board, day):
        cycles = self._for_day(ctx, board, day)
        return self._download(ctx, board, day, cycles[0]) if cycles else None

    def fetch(self, ctx: Context) -> FetchResult:
        self._page.clear()
        self._warn.clear()
        res = super().fetch(ctx)
        res.warnings += self._warn
        return res
