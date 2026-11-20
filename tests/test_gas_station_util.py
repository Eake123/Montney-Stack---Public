"""NGTL Station % Utilization receipts (PLAN 5a): link scraping, the three file layouts, units, vintages, guards.

The workbooks are built here as minimal synthetic .xlsx files, shaped like TC's (title row, month header, %/Demand/IT
triplets, "As of" stamp). The numbers are made up. No TC file is committed.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import ngtl_station_util as su
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

PAGE = "https://www.tccustomerexpress.com/2883.html"
NOW = datetime(2026, 11, 1, 18, 0)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ------------------------------------------------------------------ synthetic workbooks
def _cell(ref: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f'<c r="{ref}"><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'


def _ref(row: int, col: int) -> str:
    s, n = "", col + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return f"{s}{row + 1}"


def make_xlsx(rows: list[list], shared_title: bool = True) -> bytes:
    """A one-sheet .xlsx. The title cell goes through sharedStrings (TC's files do), the rest are inline strings."""
    body, shared = [], []
    for i, r in enumerate(rows):
        cells = []
        for j, v in enumerate(r):
            if i == 0 and j == 0 and shared_title and isinstance(v, str):
                shared.append(v)
                cells.append(f'<c r="{_ref(i, j)}" t="s"><v>0</v></c>')
            else:
                cells.append(_cell(_ref(i, j), v))
        body.append(f'<row r="{i + 1}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel_ns}><sheets><sheet name="Station Percent Utilization – R" '
                                      f'sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(body)}</sheetData></worksheet>')
    return buf.getvalue()


def era_c_rows(year=2026, months=9, n=60, as_of="As of: 10/20/2026 1:29:06 PM", title="NGTL Station Percent Utilization – Receipt",
               scale=1.0):
    """Layout C (2019-): months on one row, then Station Number, Name, _, Segment, _, %, Demand, IT ..."""
    hdr = [None] * 5 + sum(([f"{MONTHS[m]} {year}", None, None] for m in range(months)), [])
    sub = ["Station Number", "Name", None, "Segment", None] + ["%", "Demand", "IT"] * months
    rows = [[title], [], ["The following report provides ..."], [], hdr, sub]
    rows.append([5106, "TREMBLAY NO 2", None, "GRDL", None] + [50, 10000, 1000] * months)   # hand-checked station
    rows.append([5242, "TREMBLAY SUMMARY", None, "GRDL", None] + [0, 0, 0] * months)        # nothing all year
    for k in range(n):
        rows.append([3000 + k, f"STATION {k}", None, "WAEX", None] + [100, 100000 * scale, 0] * months)
    rows += [[], [as_of]]
    return rows


def era_a_rows(year=2010, n=60):
    """Layout A (2010-2016 .xls): header on row 14 as 'Station Number/Name', no segment column."""
    rows = [["RECEIPT CONTRACT UTILIZATION BY STATION"]] + [[]] * 13
    rows.append(["Station Number/Name", None] + sum(([f"{m} {year}", None, None] for m in MONTHS), []))
    rows.append([None, None] + ["%", "Demand", "IT"] * 12)
    rows.append([1337, "ABEE"] + [64.1, 1033.0, 325.4] * 12)
    for k in range(n):
        rows.append([2000 + k, f"OLD {k}"] + [100, 100000, 0] * 12)
    rows.append(["As of: 10/14/2015 4:13:55 PM"])
    return rows


def page_html(years=(2026, 2025, 2017, 2010)):
    href = {2026: "/docs/x/2026-09%20Station%20Percent%20Utilization%20%e2%80%93%20Receipt.xlsx",
            2025: "/docs/x/12-2025%20Station%20Percent%20Utilization%20%e2%80%93%20Receipt.xlsx",
            2017: "/assets/12%20-%20December%20-%20Station%20Percent%20Utilization%20-%20Receipt.xlsx",
            2010: "/docs/x/2010_NGTL_Station_Percent_Utilization_-_Receipt.xls"}
    lis = "".join(f'<li><a href="{href[y]}">{y} NGTL Station Percent Utilization - Receipt</a></li>'
                  f'<li><a href="{href[y].replace("Receipt", "Delivery")}">{y} NGTL Station Percent Utilization - '
                  f'Delivery</a></li>' for y in years)
    return f"<html><body><ul>{lis}</ul></body></html>"


def run(files: dict[str, bytes], html: str | None = None, params: dict | None = None, today=date(2026, 11, 1)):
    """Run the shipped connector against a mocked TC site. "today" is pinned so the live window (this year and
    last) doesn't drift with the calendar."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == PAGE:
            return httpx.Response(200, text=html or page_html())
        for key, body in files.items():
            if key in url:
                return httpx.Response(200, content=body)
        return httpx.Response(404)
    spec = load_specs(include_disabled=True)["ngtl_station_util"]
    if params:
        spec = {**spec, "params": {**spec["params"], **params}}
    real, su.local_today = su.local_today, (lambda: today)
    try:
        return build(spec).run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))
    finally:
        su.local_today = real


def live_files():
    return {"2026-09": make_xlsx(era_c_rows()), "12-2025": make_xlsx(era_c_rows(2025, 12, as_of="As of: 1/20/2026 2:55:41 PM"))}


# ------------------------------------------------------------------ links
def test_receipt_links_by_anchor_text_including_the_nameless_2017_file():
    links = su.receipt_links(page_html(), PAGE)
    assert sorted(links) == [2010, 2017, 2025, 2026]
    assert links[2017] == ("https://www.tccustomerexpress.com/assets/12%20-%20December%20-%20Station%20Percent%20"
                           "Utilization%20-%20Receipt.xlsx")
    assert all("Delivery" not in u for u in links.values())


def test_receipt_links_anchor_variants_seen_on_the_real_page():
    """2019: plain space; 2021-2023: &nbsp;; 2020: wrapped in <font> with &nbsp; (all seen on 2883.html, 2026-11-01)."""
    html = ('<a href="/d/2019-R.xlsx"><font>2019 NGTL Station Percent Utilization - Receipt</font></a>'
            '<a href="/d/2020-R.xlsx"><font>2020&nbsp;NGTL Station Percent Utilization - Receipt</font></a>'
            '<a href="/d/2021-R.xlsx">2021&nbsp;NGTL Station Percent Utilization - Receipt</a>'
            '<a href="/d/2021-D.xlsx">2021&nbsp;NGTL Station Percent Utilization - Delivery</a>')
    assert sorted(su.receipt_links(html, PAGE)) == [2019, 2020, 2021]


def test_newest_receipt_file_too_old_fails_loudly():
    with pytest.raises(FormatDrift, match="newest receipt file is 2017"):
        run({}, html=page_html(years=(2017, 2010)))


def test_duplicate_year_link_fails_loudly():
    html = page_html(years=(2026,)) + page_html(years=(2026,))
    with pytest.raises(FormatDrift, match="two receipt links for 2026"):
        su.receipt_links(html)


# ------------------------------------------------------------------ parsing + units
def test_flow_formula_and_units_against_a_hand_value():
    recs, as_of = su.parse_receipts(su.read_xlsx(make_xlsx(era_c_rows())), 2026)
    jan = next(x for x in recs if x["station"] == 5106 and x["month"] == date(2026, 1, 1))
    assert jan["flow_e3m3"] == pytest.approx(6000.0)                   # 50 % × 10,000 + 1,000
    assert su.to_mmcfd(jan["flow_e3m3"], jan["month"]) == pytest.approx(6.8351, abs=1e-4)   # 6000 × 0.0353147 / 31
    feb = next(x for x in recs if x["station"] == 5106 and x["month"] == date(2026, 2, 1))
    assert su.to_mmcfd(feb["flow_e3m3"], feb["month"]) == pytest.approx(7.5674, abs=1e-4)   # / 28 days
    assert jan["segment"] == "GRDL" and jan["name"] == "TREMBLAY NO 2"
    assert as_of == datetime(2026, 10, 20, 19, 29, 6)                  # 1:29:06 PM Alberta (UTC-6) → UTC
    assert not any(x["station"] == 5242 for x in recs)                 # a station with nothing all year is skipped


def test_layout_a_without_segment_column():
    recs, as_of = su.parse_receipts(era_a_rows(), 2010)
    abee = [x for x in recs if x["station"] == 1337]
    assert len(abee) == 12 and abee[0]["segment"] == "" and abee[0]["flow_e3m3"] == pytest.approx(0.641 * 1033.0 + 325.4)
    assert as_of == datetime(2015, 10, 14, 22, 13, 55)                 # MDT in 2015


def test_layout_c_header_without_shared_strings():
    recs, _ = su.parse_receipts(su.read_xlsx(make_xlsx(era_c_rows(), shared_title=False)), 2026)
    assert {x["station"] for x in recs} >= {5106, 3000}


@pytest.mark.parametrize("rows, year, needle", [
    (era_c_rows(title="NGTL Station Percent Utilization – Delivery"), 2026, "not a receipt"),
    (era_c_rows(), 2025, "don't match the link year 2025"),
    (era_c_rows(as_of="printed today"), 2026, "no 'As of:' stamp"),
    (era_c_rows(n=10), 2026, "stations parsed"),
])
def test_guards_fail_loudly(rows, year, needle):
    with pytest.raises(FormatDrift, match=needle):
        su.parse_receipts(rows, year)


def test_missing_it_column_fails_loudly():
    rows = era_c_rows()
    rows[5] = [c if c != "IT" else "Other" for c in rows[5]]
    with pytest.raises(FormatDrift, match="%/Demand/IT"):
        su.parse_receipts(rows, 2026)


# ------------------------------------------------------------------ the shipped connector path
def test_live_run_reads_this_and_last_year_and_stamps_the_file_vintage():
    res = run(live_files())
    sids = {o.series_id for o in res.obs}
    assert "ngtl.station.5106.receipt" in sids and "ngtl.station.5242.receipt" not in sids
    jan = [o for o in res.obs if o.series_id == "ngtl.station.5106.receipt" and o.ts == datetime(2026, 1, 1)]
    assert len(jan) == 1 and jan[0].value == pytest.approx(6.8351, abs=1e-4)
    assert jan[0].as_of == datetime(2026, 10, 20, 19, 29, 6)
    dec25 = [o for o in res.obs if o.series_id == "ngtl.station.5106.receipt" and o.ts == datetime(2025, 12, 1)]
    assert dec25 and dec25[0].as_of == datetime(2026, 1, 20, 21, 55, 41)
    assert not any(o.ts.year < 2025 for o in res.obs)                 # years_back: 1 → 2025 and 2026 only
    meta = {m.series_id: m for m in res.meta}
    m = meta["ngtl.station.5106.receipt"]
    assert (m.unit, m.freq, m.extra) == ("MMcf/d", "M", {"station": 5106, "name": "TREMBLAY NO 2", "segment": "GRDL"})


def test_months_not_over_at_the_as_of_stamp_are_dropped():
    # The YTD file's header runs to Oct, but the file was cut on Oct 20: October isn't data yet.
    res = run({"2026-09": make_xlsx(era_c_rows(months=10)), "12-2025": live_files()["12-2025"]})
    assert max(o.ts for o in res.obs) == datetime(2026, 9, 1)


def test_implausible_system_total_fails_loudly():
    files = {**live_files(), "2026-09": make_xlsx(era_c_rows(scale=1000.0))}   # e.g. a unit or column shift
    with pytest.raises(FormatDrift, match="system receipts"):
        run(files)


def test_backfill_from_year_reads_every_year_and_routes_xls_to_xlrd(monkeypatch):
    seen = []

    def fake_xls(data):
        seen.append(data)
        return era_a_rows()
    monkeypatch.setattr(su, "read_xls", fake_xls)
    files = {**live_files(), "2010_NGTL": b"xls-bytes", "12%20-%20December": make_xlsx(era_c_rows(2017, 12,
             as_of="As of: 1/22/2018 9:00:00 AM"))}
    res = run(files, params={"from_year": 2010})
    assert seen == [b"xls-bytes"]
    assert {o.ts.year for o in res.obs} == {2010, 2017, 2025, 2026}


def test_history_importer_overrides_only_from_year():
    from montney.importers.ngtl_station_util import history_spec
    spec = history_spec(2010)
    assert spec["params"]["from_year"] == 2010 and spec["params"]["page"] == PAGE
    assert "from_year" not in load_specs(include_disabled=True)["ngtl_station_util"]["params"]


def test_firm_utilization_series_only_with_contract_demand():
    res = run(live_files())
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    assert pts[("ngtl.station.5106.firm_pct", datetime(2026, 1, 1))] == 50.0
    assert ("ngtl.station.5242.firm_pct", datetime(2026, 1, 1)) not in pts      # no contract → no utilization
    units = {m.series_id: m.unit for m in res.meta}
    assert units["ngtl.station.5106.firm_pct"] == "%" and units["ngtl.station.5106.receipt"] == "MMcf/d"
    assert su.SERIES_PATTERN.match("ngtl.station.5106.firm_pct") and not su.SERIES_PATTERN.match("ngtl.station.x.receipt")
