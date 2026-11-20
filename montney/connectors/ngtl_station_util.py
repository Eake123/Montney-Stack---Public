"""NGTL Station Percent Utilization: monthly receipt volume per NGTL receipt station (PLAN 5a).

Source: TC Customer Express, https://www.tccustomerexpress.com/2883.html. One receipt file and one delivery file
per year, 2010 to now. The current year's file is year-to-date and is replaced each month, about 20 days after
month end. We read **receipts only** (DECISIONS 5a; deliveries are Power's P5b).

File names follow no pattern. The 2017 file has no year in its name at all. So the links are picked by their
anchor text, "<YYYY> NGTL Station Percent Utilization - Receipt", and the year is checked against the month
headers inside the file.

Three layouts (docs/research/v2/gas-stations.md §1), all handled by finding the month row:
  A  2010-2016  .xls,  header on row ~14, "Station Number/Name", no segment column
  B  2017-2018  .xlsx, header on row ~4, no segment column
  C  2019-      .xlsx, months on row ~4, then "Station Number, Name, _, Segment, _, %, Demand, IT, ..."
Each month has three columns:
  %       firm-allocated volume ÷ Demand, in percent
  Demand  contract demand × days in effect: a monthly total in 10³m³
  IT      interruptible + over-run allocation: a monthly total in 10³m³

Monthly receipt = %/100 × Demand + IT  (10³m³ in the month).
Stored as **MMcf/d** (L-031): 10³m³ × 0.0353147 ÷ days in the month. No heat value is involved for receipts.

Series (freq M, ts = first day of the month, naive):
  ngtl.station.<tc_number>.receipt   MMcf/d; the TC station number as published, e.g. ngtl.station.5106.receipt
  ngtl.station.<tc_number>.firm_pct  % = TC's "%" column (firm-allocated volume ÷ contract demand), only in months with
                                     contract demand > 0
SeriesMeta.extra carries {station, name, segment}. The name and segment come from the newest file read.

as_of = the file's own "As of: m/d/yyyy h:mm:ss AM/PM" stamp (Alberta time, converted to naive UTC). Each monthly
republication is a vintage, and change-only writes keep only the values that moved. It is a report vintage
published after the month, so the tier is `daily` (kept forever), not forecast.

These are TC's allocated (billing) volumes, not metered flow. Station totals run about 2.7 % below TC's measured
receipts (Jan-Sep 2026 mean −2.73 %, sd 0.21; gas-stations.md §1), so use them as shares, or rescale.

Fail loudly (FormatDrift: nothing stored) when:
  - the newest receipt link is older than last year;
  - the file is not a receipt file;
  - the month headers don't match the link's year, or a month lacks its %/Demand/IT columns;
  - the "As of" stamp is missing;
  - too few stations are parsed;
  - a month's system total is implausible (a unit or column shift).

Params:
  page        the 2883.html URL
  years_back  live mode: read files for years >= this year - years_back (default 1, so January still gets
              last year's final file)
  from_year   backfill: read every year from this one on (montney/importers/ngtl_station_util.py)
"""

from __future__ import annotations

import calendar
import html
import io
import re
import zipfile
from datetime import date, datetime
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import E3M3_TO_MMCF, FormatDrift, local_to_utc
from montney.store import Obs, SeriesMeta

PAGE = "https://www.tccustomerexpress.com/2883.html"
GROUP = "NGTL / Station receipts"
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
ANCHOR_RE = re.compile(r'<a\s[^>]*href="(?P<href>[^"]+\.xlsx?)"[^>]*>(?P<text>.*?)</a>', re.I | re.S)
# Anchor text after unescaping and stripping inner tags. The page mixes "2019 NGTL ...", "2021&nbsp;NGTL ..." and
# "<font>2020&nbsp;NGTL ...</font>".
TEXT_RE = re.compile(r"^(?P<year>\d{4})\s+NGTL\s+Station\s+Percent\s+Utilization\s*[-–—]\s*(?P<kind>Receipt|Delivery)$", re.I)
AS_OF_RE = re.compile(r"As of:\s*(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)", re.I)
MIN_STATIONS = 50                     # every real file has 450-900 receipt stations
SYSTEM_MMCFD = (5_000.0, 25_000.0)    # plausible NGTL total receipts in any month since 2010 (~10-15 Bcf/d)


def series_id(station: int, measure: str = "receipt") -> str:
    return f"ngtl.station.{station}.{measure}"


# Station series ids exist only once fetched, so declared_series() can't list them. The map's network test accepts refs
# that match this pattern (tests/test_gas_network.py).
SERIES_PATTERN = re.compile(r"^ngtl\.station\.\d+\.(receipt|firm_pct|receipt_est)$")   # receipt_est: ngtl_split


# ------------------------------------------------------------------ links
def receipt_links(page_html: str, base: str = PAGE, kind: str = "Receipt") -> dict[int, str]:
    """{year: absolute url} for every "<YYYY> NGTL Station Percent Utilization - Receipt" link."""
    out: dict[int, str] = {}
    for a in ANCHOR_RE.finditer(page_html):
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", a.group("text")))).strip()
        m = TEXT_RE.match(text)
        if not m or m.group("kind").lower() != kind.lower():
            continue
        year = int(m.group("year"))
        if year in out:
            raise FormatDrift(f"two {kind.lower()} links for {year} on the utilization page")
        out[year] = urljoin(base, html.unescape(a.group("href")))
    return out


# ------------------------------------------------------------------ spreadsheet readers
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx_sheets(data: bytes) -> list[list[list]]:
    """Every worksheet of an .xlsx, in workbook order, each as a list of rows (stdlib only: zip + XML).
    Numbers are floats (dates stay Excel serial numbers), text is str."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        sheets = []
        for sh in wb.findall("m:sheets/m:sheet", _NS):
            target = rels[sh.get(_REL)]
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            sheets.append(_sheet_rows(ET.fromstring(z.read(path)), shared))
    return sheets


def read_xlsx(data: bytes) -> list[list]:
    """First worksheet of an .xlsx as a list of rows."""
    return read_xlsx_sheets(data)[0]


def _sheet_rows(sheet, shared: list[str]) -> list[list]:
    rows: list[list] = []
    for row in sheet.iter(f"{{{_NS['m']}}}row"):
        cells: dict[int, object] = {}
        for c in row.findall("m:c", _NS):
            t, v = c.get("t"), c.find("m:v", _NS)
            if t == "s":
                val: object = shared[int(v.text)] if v is not None else None
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
            elif v is None:
                val = None
            elif t in ("str", "e"):
                val = v.text
            elif t == "b":
                val = v.text == "1"
            else:
                val = float(v.text)
            cells[_col(c.get("r"))] = val
        r_idx = int(row.get("r")) - 1
        while len(rows) < r_idx:
            rows.append([])
        rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return rows


def read_xls(data: bytes) -> list[list]:
    """First worksheet of a legacy .xls (2010-2016 files), via xlrd."""
    import xlrd   # only the history load needs it

    s = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
    return [s.row_values(i) for i in range(s.nrows)]


def read_sheet(url: str, data: bytes) -> list[list]:
    return read_xls(data) if url.lower().endswith(".xls") else read_xlsx(data)


# ------------------------------------------------------------------ parsing
def _month(cell) -> date | None:
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{4})$", str(cell or "").strip())
    if m and m.group(1).title() in MONTHS:
        return date(int(m.group(2)), MONTHS[m.group(1).title()], 1)
    return None


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _text(r: list, j: int | None) -> str:
    return str(r[j]).strip() if j is not None and j < len(r) and r[j] not in (None, "") else ""


def parse_as_of(rows: list[list]) -> datetime:
    for r in reversed(rows[-15:] + rows[:15]):
        for c in r:
            m = AS_OF_RE.search(str(c or ""))
            if m:
                local = datetime.strptime(re.sub(r"\s+", " ", m.group(1).upper()), "%m/%d/%Y %I:%M:%S %p")
                return local_to_utc(local)
    raise FormatDrift("no 'As of:' stamp in the utilization file")


def parse_receipts(rows: list[list], year: int, kind: str = "receipt") -> tuple[list[dict], datetime]:
    """Rows of one utilization file → ([{station, name, segment, month, pct, demand, it, flow_e3m3}], as_of).
    kind "delivery" reads a Delivery file; its values are **GJ** per month (the key stays `flow_e3m3`)."""
    title = " ".join(str(c) for r in rows[:2] for c in r if c).lower()   # row 0 is the title in every era
    if kind not in title:
        raise FormatDrift(f"not a {kind} utilization file (no '{kind}' in its title)")
    hi = next((i for i, r in enumerate(rows[:40]) if sum(1 for c in r if _month(c)) >= 1), None)
    if hi is None:
        raise FormatDrift("no month header row (e.g. 'Jan 2026') in the first 40 rows")
    hdr, sub = rows[hi], rows[hi + 1] if hi + 1 < len(rows) else []
    mcols = [(j, _month(c)) for j, c in enumerate(hdr) if _month(c)]
    if {m.year for _, m in mcols} != {year}:
        raise FormatDrift(f"month headers {sorted({m.year for _, m in mcols})} don't match the link year {year}")
    labels = {str(c).strip().lower(): j for j, c in enumerate(sub) if c not in (None, "")}
    seg_col = labels.get("segment")
    first_month_col = mcols[0][0]
    spans = []
    for k, (j, m) in enumerate(mcols):
        end = mcols[k + 1][0] if k + 1 < len(mcols) else len(sub)
        pos = {str(sub[x]).strip(): x for x in range(j, min(end, len(sub))) if sub[x] not in (None, "")}
        if not {"%", "Demand", "IT"} <= set(pos):
            raise FormatDrift(f"{m:%b %Y}: expected %/Demand/IT columns, found {sorted(pos)}")
        spans.append((m, pos["%"], pos["Demand"], pos["IT"]))
    as_of = parse_as_of(rows)
    out = []
    for r in rows[hi + 2:]:
        st = _num(r[0]) if r else None
        if st is None or st != int(st) or st <= 0:
            continue
        name = _text(r, 1)
        seg = _text(r, seg_col) if seg_col is not None and seg_col < first_month_col else ""
        recs = []
        for m, a, b, c in spans:
            vals = [_num(r[x]) if x < len(r) else None for x in (a, b, c)]
            if all(v is None for v in vals):
                continue
            pct, dem, it = (v or 0.0 for v in vals)
            recs.append(dict(station=int(st), name=name, segment=seg, month=m, pct=pct, demand=dem, it=it,
                             flow_e3m3=pct / 100.0 * dem + it))
        if any(x["flow_e3m3"] or x["demand"] for x in recs):   # a station with nothing all year is skipped
            out.extend(recs)
    if len({x["station"] for x in out}) < MIN_STATIONS:
        raise FormatDrift(f"only {len({x['station'] for x in out})} stations parsed (expected hundreds)")
    return out, as_of


def to_mmcfd(flow_e3m3: float, month: date) -> float:
    return flow_e3m3 * E3M3_TO_MMCF / calendar.monthrange(month.year, month.month)[1]


def _month_end(m: date) -> datetime:
    return datetime(m.year + (m.month == 12), m.month % 12 + 1, 1)


class NgtlStationUtil(Connector):
    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        page = p.get("page", PAGE)
        links = receipt_links(ctx.get(page).text, page)
        if not links:
            raise FormatDrift("no receipt links found on the utilization page")
        this_year = local_today().year
        if max(links) < this_year - 1:
            raise FormatDrift(f"newest receipt file is {max(links)}; expected {this_year - 1} or {this_year}")
        first = int(p["from_year"]) if p.get("from_year") else this_year - int(p.get("years_back", 1))
        res = FetchResult()
        names: dict[int, tuple[str, str]] = {}
        for year in sorted(y for y in links if y >= first):
            url = links[year]
            recs, as_of = parse_receipts(read_sheet(url, ctx.get(url).content), year)
            totals: dict[date, float] = {}
            for x in recs:
                if _month_end(x["month"]) > as_of:     # a month not over when the file was cut: not data
                    continue
                v = to_mmcfd(x["flow_e3m3"], x["month"])
                totals[x["month"]] = totals.get(x["month"], 0.0) + v
                ts = datetime(x["month"].year, x["month"].month, 1)
                res.obs.append(Obs(series_id(x["station"]), ts, v, as_of))
                if x["demand"] > 0:                    # firm utilization only means something with a contract
                    res.obs.append(Obs(series_id(x["station"], "firm_pct"), ts, x["pct"], as_of))
                names[x["station"]] = (x["name"], x["segment"] or names.get(x["station"], ("", ""))[1])
            for m, tot in totals.items():
                if not SYSTEM_MMCFD[0] <= tot <= SYSTEM_MMCFD[1]:
                    raise FormatDrift(f"{m:%b %Y}: system receipts {tot:,.0f} MMcf/d outside {SYSTEM_MMCFD}")
        res.meta = [SeriesMeta(series_id(st), self.id, f"NGTL {nm.title()} ({st}) receipts", "MMcf/d", "M", GROUP,
                               extra={"station": st, "name": nm, "segment": seg})
                    for st, (nm, seg) in sorted(names.items())]
        res.meta += [SeriesMeta(series_id(st, "firm_pct"), self.id, f"NGTL {nm.title()} ({st}) firm utilization",
                                "%", "M", GROUP, extra={"station": st, "name": nm, "segment": seg})
                     for st, (nm, seg) in sorted(names.items())]
        return res
