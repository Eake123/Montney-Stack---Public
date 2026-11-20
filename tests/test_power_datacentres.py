"""Data-centre series (PLAN 9): Epoch AI frontier sites (power_dc_epoch), ERCOT large loads approved to energize
(power_ercot_lli) and the AESO connection queue's data load (power_aeso_dc_queue). Mocked payloads in the live shapes
(2026-11-13): Epoch's ZIP of two CSVs, ERCOT's board page + a slide PDF, AESO's index page + an .xlsx."""

from __future__ import annotations

import io
import zipfile
import zlib
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_aeso_dc_queue as aq
from montney.connectors import power_dc_epoch as ep
from montney.connectors import power_ercot_lli as el
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
NOW = datetime(2026, 11, 13, 18)


def ctx(handler, now=NOW, source=""):
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now, source=source)


def values(res):
    return {(o.series_id, o.ts): o.value for o in res.obs}


# ------------------------------------------------------------------ Epoch AI
SITES = """Name,Current H100 equivalents,Current power (MW),Current total capital cost (2025 USD billions),Owner,Users,Selected Sources,Calculations sheet,Project,Current chip types,All chip types,Investors,Construction companies,Energy companies,Country,Address
OpenAI Stargate Abilene,1,824,1,Crusoe,OpenAI,"- [a](https://example.org)
- [b](https://example.org)
",,Stargate,,,,,,United States,"5502 Spinks Rd, Abilene, TX 79601"
Meta Kuna,1,152,1,Meta,Meta,,,,,,,,,United States,"601 Kuna Mora Rd,  Kuna ID 83634"
Amazon Ridgeland,1,228,1,Amazon,Amazon,,,,,,,,,United States,"1626 County Line Road Ridgeland, Mississippi"
Google Mesa,1,183,1,Google,Google,,,,,,,,,United States,
Meta Prometheus,1,471,1,Meta,Meta,,,,,,,,,United States,"1 Community Cir, New Albany, OH 43054"
Mystery Campus,1,50,1,X,X,,,,,,,,,United States,somewhere
Colossus Norway,1,90,1,X,X,,,,,,,,,Norway,"Oslo"
"""
TIMELINES = """Data center,Date,Construction status,Buildings operational,IT power (MW),Power (MW),H100 equivalents
OpenAI Stargate Abilene,2025-06-28,"Building 1 looks close, with ""chillers"" in place",0.0,0.0,0.0,0
OpenAI Stargate Abilene,2025-09-26,Building 1 operational,1.0,103.0,144.0,1
OpenAI Stargate Abilene,2026-09-10,Buildings 5-6,6.0,618.0,865.0,1
OpenAI Stargate Abilene,2026-11-01,Buildings 7-8,8.0,824.0,1154.0,1
OpenAI Stargate Abilene,2027-06-01,projection,10.0,1100.0,1500.0,1
Meta Kuna,2026-10-15,,1.0,152.0,198.0,1
Amazon Ridgeland,2026-01-10,,1.0,228.0,273.0,1
Google Mesa,2026-08-15,,1.0,183.0,237.0,1
Meta Prometheus,2025-12-01,,1.0,471.0,612.0,1
"""


def epoch_zip(sites=SITES, timelines=TIMELINES):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data_centers.csv", sites)
        z.writestr("data_center_timelines.csv", timelines)
    return buf.getvalue()


def run_epoch(content, status=200):
    spec = {**SPECS["power_dc_epoch"], "params": {"from": "2025-08"}}
    return build(spec).run(ctx(lambda r: httpx.Response(status, content=content if status == 200 else b"")))


def test_epoch_places_sites_by_state_and_sums_regions():
    res = run_epoch(epoch_zip())
    v = values(res)
    nov, oct_, sep = datetime(2026, 11, 1), datetime(2026, 10, 1), datetime(2026, 9, 1)
    # current month = Epoch's Current power (MW); a past month = the timeline at its end (projections never read)
    assert v[("dc.epoch.site.openai_stargate_abilene.it_mw", nov)] == 824
    assert v[("dc.epoch.site.openai_stargate_abilene.it_mw", oct_)] == 618
    assert v[("dc.epoch.site.openai_stargate_abilene.it_mw", datetime(2025, 9, 1))] == 103
    assert v[("dc.epoch.site.openai_stargate_abilene.it_mw", datetime(2025, 8, 1))] == 0
    assert v[("dc.epoch.tx.it_mw", nov)] == 824 and v[("dc.epoch.tx.sites", nov)] == 1
    # "Kuna ID 83634" -> pnw, from its first timeline row (Oct 2026) on
    assert v[("dc.epoch.pnw.it_mw", oct_)] == 152 and v[("dc.epoch.pnw.it_mw", sep)] == 0
    assert ("dc.epoch.site.meta_kuna.it_mw", sep) not in v
    # Mississippi (by name) and Ohio aren't in a region; Google Mesa (no address) is placed by PLACES -> sunbelt
    assert v[("dc.epoch.sunbelt.it_mw", nov)] == 183
    assert not any("ridgeland" in s or "prometheus" in s for s, _ in v)
    # identity: this month's region sums = the sum of Current power of the placed sites
    regional = sum(x for (s, t), x in v.items() if t == nov and s.endswith(".it_mw") and ".site." not in s)
    assert regional == 824 + 152 + 183
    assert any("Mystery Campus" in w for w in res.warnings)              # unplaced, with power: warned, not counted
    assert v[("dc.epoch.cal.it_mw", nov)] == 0                             # every region gets a value
    site_meta = {m.series_id: m for m in res.meta if ".site." in m.series_id}
    assert site_meta["dc.epoch.site.openai_stargate_abilene.it_mw"].extra["region"] == "tx"


def test_epoch_member_of_address_forms():
    assert ep.member_of("x", "5502 Spinks Rd, Abilene, TX 79601", "US") == "US-TX"
    assert ep.member_of("x", "601 Kuna Mora Rd,  Kuna ID 83634", "US") == "US-ID"
    assert ep.member_of("x", "Cheyenne, WY 82007, USA", "US") == "US-WY"
    assert ep.member_of("x", "3500 River Rd, The Dalles, OR", "US") == "US-OR"
    assert ep.member_of("x", "14651 Gold Coast Rd 68138 Papillion Nebraska, USA", "US") == "US-NE"
    assert ep.member_of("x", "100 Main St, Calgary, AB T2P 1J9", "CA") == "CA-AB"
    assert ep.member_of("x", "", "US") is None


def test_epoch_unchanged_zip_writes_nothing():
    res = run_epoch(b"", status=304)
    assert res.obs == [] and {m.series_id for m in res.meta} >= {"dc.epoch.tx.it_mw", "dc.epoch.wcan.it_mw"}


def test_epoch_fails_closed():
    with pytest.raises(FormatDrift, match="missing columns"):
        run_epoch(epoch_zip(sites=SITES.replace("Current power (MW)", "Power now")))
    with pytest.raises(FormatDrift, match="isn't a number"):
        run_epoch(epoch_zip(timelines=TIMELINES.replace(",618.0,", ",lots,")))
    with pytest.raises(FormatDrift, match="outside"):
        run_epoch(epoch_zip(sites=SITES.replace(",824,", ",82400,")))
    with pytest.raises(FormatDrift, match="date"):
        run_epoch(epoch_zip(timelines=TIMELINES.replace("2026-09-10", "Sep 2026")))
    with pytest.raises(FormatDrift, match="no data_center_timelines"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("data_centers.csv", SITES)
        run_epoch(buf.getvalue())


# ------------------------------------------------------------------ ERCOT
def slide_pdf(sentence_parts: list[str]) -> bytes:
    """A one-page PDF whose content stream shows the parts as TJ arrays, like ERCOT's PowerPoint exports."""
    ops = [b"BT", b"/TT1 1 Tf", b"-0.002 Tc 9 0 0 9 5.1 488.0 Tm", b"[(PUBL)-4.6 (I)-4.4 (C)]TJ", b"ET", b"BT"]
    for p in sentence_parts:
        esc = p.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")
        half = len(esc) // 2
        ops.append(b"[(" + esc[:half] + b")-0.6 (" + esc[half:] + b")]TJ")
        ops.append(b"0 -1.2 Td")
    ops.append(b"ET")
    stream = zlib.compress(b"\n".join(ops))
    return (b"%PDF-1.6\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n4 0 obj\n<</Filter/FlateDecode/Length "
            + str(len(stream)).encode() + b">>stream\n" + stream
            + b"\nendstream\nendobj\ntrailer\n<</Root 1 0 R>>\n%%EOF")


def sentence(approved, observed, month):
    return ["Observations • Of the " + approved + " MW that have received Approval to Energize, ERCOT has observed a",
            "non - simultaneous monthly", "peak consumption of " + observed + " MW in " + month + ", which is a slight",
            "decrease (since August 2026)."]


def board(links):
    rows = "".join(f'<div class="row"><a rel="noopener" target="_blank" href="{u}" title="{t}" class="d-block pb-2">'
                   f'{t}</a><span>Oct 16, 2026 - pdf - 1.4 MB</span></div>' for u, t in links)
    return f"<html><body>{rows}<a href='/files/docs/2026/10/16/Other.pdf'>Board agenda</a></body></html>"


E = "https://www.ercot.com/files/docs"
ISSUES_2026 = [(f"{E}/2026/02/18/ERCOT-Monthly-Operational-Overview-January-2026.pdf",
                "ERCOT Monthly Operational Overview January 2026"),
               (f"{E}/2026/09/16/ERCOT-Monthly-Operational-Overview-August-2026.pdf",
                "ERCOT Monthly Operational Overview August 2026"),
               (f"{E}/2026/10/16/ERCOT-Monthly-Operational-Overview-September.pdf",
                "ERCOT Monthly Operational Overview September 2026")]
ISSUES_2025 = [(f"{E}/2025/02/18/ercot-monthly-operational-overview-january-2025.pdf",
                "ERCOT Monthly Operational Overview January 2025"),
               (f"{E}/2026/01/15/ERCOT-Monthly-Operational-Overview-December-2025.pdf",
                "ERCOT Monthly Operational Overview December 2025")]
PDFS = {ISSUES_2026[0][0]: slide_pdf(sentence("8,787", "4,049", "January 2026")),
        ISSUES_2026[1][0]: slide_pdf(sentence("9,456", "4,316", "August 2026")),
        ISSUES_2026[2][0]: slide_pdf(sentence("9,456", "4,132", "September 2026")),
        ISSUES_2025[0][0]: slide_pdf(["Current Large Load Interconnection Queue", "Observed Energized"]),
        ISSUES_2025[1][0]: slide_pdf(sentence("8787", "3349", "January 2026"))}    # month to date, superseded


class Ercot:
    def __init__(self, pdfs=None, issues=None):
        self.pdfs, self.issues, self.calls = pdfs or PDFS, issues or ISSUES_2026, []

    def __call__(self, req):
        u = str(req.url)
        self.calls.append(u)
        if u == el.INDEX:
            return httpx.Response(200, text=board(self.issues))
        if u == el.INDEX + "/2025":
            return httpx.Response(200, text=board(ISSUES_2025))
        if u in self.pdfs:
            return httpx.Response(200, content=self.pdfs[u])
        return httpx.Response(404)


def run_ercot(h, now=NOW):
    return build(SPECS["power_ercot_lli"]).run(ctx(h, now))


def test_ercot_backfill_month_named_by_sentence_later_issue_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    h = Ercot()
    v = values(run_ercot(h))
    assert v[("ercot.lli.observed_mw", datetime(2026, 9, 1))] == 4132
    assert v[("ercot.lli.approved_mw", datetime(2026, 9, 1))] == 9456
    assert v[("ercot.lli.observed_mw", datetime(2026, 8, 1))] == 4316
    # the Dec 2025 issue's "3349 MW in January 2026" (month to date) loses to the January issue's full month
    assert v[("ercot.lli.observed_mw", datetime(2026, 1, 1))] == 4049
    assert len(v) == 6                                                    # Jan 2025 (no slide yet) is skipped
    assert len([c for c in h.calls if c.endswith(".pdf")]) == 5
    # next run: the board page again, every PDF from the cache; 2025 is complete (read after 1 Feb 2026)
    h2 = Ercot()
    assert values(run_ercot(h2)) == v
    assert h2.calls == [el.INDEX]


def test_ercot_month_to_date_values_are_dropped():
    """Reviewer on #497: an issue naming the month it was posted in gives a month-to-date peak (to ~the 17th): never
    stored, so Oct-Dec 2025 (each issue's own month) have no value unless a later issue gives the full month."""
    issues = {"a": {"posted": "2025-10-16", "month": "2025-10", "observed": 4126, "approved": 7000},
              "b": {"posted": "2026-01-15", "month": "2026-01", "observed": 3349, "approved": 8787},
              "c": {"posted": "2026-02-18", "month": "2026-01", "observed": 4049, "approved": 8787},
              "d": {"posted": "2026-10-16", "month": "2026-09", "observed": 4132, "approved": 9456}}
    got = el.by_month(issues)
    assert sorted(got) == ["2026-01", "2026-09"] and got["2026-01"]["observed"] == 4049


def test_ercot_new_issue_downloads_only_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    run_ercot(Ercot())
    new = (f"{E}/2026/11/16/ERCOT-Monthly-Operational-Overview-October-2026.pdf",
           "ERCOT Monthly Operational Overview October 2026")
    h = Ercot(pdfs={**PDFS, new[0]: slide_pdf(sentence("9,500", "4,200", "October 2026"))},
              issues=ISSUES_2026 + [new])
    v = values(run_ercot(h, datetime(2026, 11, 17)))
    assert v[("ercot.lli.observed_mw", datetime(2026, 10, 1))] == 4200
    assert h.calls == [el.INDEX, new[0]]


def test_ercot_newest_without_sentence_fails_closed_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    pdfs = {**PDFS, ISSUES_2026[2][0]: slide_pdf(["ERCOT Monthly Operational Overview", "(September 2026)"])}
    with pytest.raises(FormatDrift, match="newest issue"):
        run_ercot(Ercot(pdfs=pdfs))
    h = Ercot()                                                           # fixed: only the newest is read again
    assert values(run_ercot(h))[("ercot.lli.observed_mw", datetime(2026, 9, 1))] == 4132
    assert [c for c in h.calls if c.endswith(".pdf")] == [ISSUES_2026[2][0]]


def test_ercot_bad_numbers_fail_closed():
    with pytest.raises(FormatDrift, match="observed"):
        el.parse_issue(el.pdf_text(slide_pdf(sentence("4,000", "4,132", "September 2026"))))
    with pytest.raises(FormatDrift, match="outside"):
        el.parse_issue(el.pdf_text(slide_pdf(sentence("945,600", "4,132", "September 2026"))))
    assert el.parse_issue(el.pdf_text(slide_pdf(["nothing here"]))) is None


def test_ercot_pdf_text_reads_escapes_and_joins_hyphens():
    t = el.pdf_text(slide_pdf(["(September 2026)", "non - simultaneous", "Tab\\there"]))
    assert "(September 2026)" in t and "non-simultaneous" in t and "Tab\\there" in t


def test_ercot_unchanged_board_page_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    run_ercot(Ercot())
    res = run_ercot(lambda r: httpx.Response(304) if str(r.url) == el.INDEX else httpx.Response(404))
    assert res.obs == []


# ------------------------------------------------------------------ AESO
HEAD = ["Status", "Project Name", "Planning Area", "Cluster", "Project Type", "MW Type", "Stage", "CA Modelled",
        "Inclusion", "Applied On", "EN1 STS MW", "EN1 DTS MW", "EN1 ISD", "EN2 STS MW", "EN2 DTS MW", "EN2 ISD",
        "EN3 STS MW", "EN3 DTS MW", "EN3 ISD"]
ROWS = [
    ["Active", "P2936 GLDC Load Phase 1.1", "", "", "Connection", "Data Load", "2", "Yes", "Yes", "45700", "0", "60",
     "46518", "0", "910", "47727", "", "", ""],
    ["Active", "P3126 Sollair Load", "", "", "BTF", "Data Load", "2", "Yes", "Yes", "45800", "0", "4.99", "46600"],
    ["Active", "P1828 hep Energy Alderson MPC Solar", "04-Medicine Hat", "", "Connection", "Solar", "5", "Yes",
     "Yes", "42580", "0", "0.5", "46541", "100", "0", "46541"],
    ["Cluster 3", "P3280 Nexus East MPC Gas Data Load", "SE-03", "Cluster", "Cluster", "Gas + Data Load", "1", "",
     "", "", "300", "200", "", "300", "200", "", "0", "200", ""],
    ["ISD Under Review", "P2942 Genesee Data Center 1 MPC Load", "", "", "Connection", "Data Load", "2", "", "", "",
     "0", "1000", ""],
    ["Recently Energized", "P9999 Small Data Load", "", "", "BTF", "Data Load", "5", "", "", "", "0", "20", ""],
    ["Recently Cancelled", "P9998 Gone Data Load", "", "", "Connection", "Data Load", "1", "", "", "", "0", "300", ""],
]


def xlsx(rows, inline_status=False):
    """A minimal .xlsx: shared strings for text, numbers as values, blank cells left out (like AESO's file)."""
    ss, idx = [], {}
    body = []
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, val in enumerate(row):
            if val == "":
                cells.append(f'<c r="{chr(65 + ci)}{ri}" s="3"/>')       # a styled empty cell
                continue
            ref = f"{chr(65 + ci)}{ri}"
            try:
                float(val)
                cells.append(f'<c r="{ref}" s="1"><v>{val}</v></c>')
            except ValueError:
                if inline_status and ci == 0 and ri > 1:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{val}</t></is></c>')
                    continue
                if val not in idx:
                    idx[val] = len(ss)
                    ss.append(val)
                cells.append(f'<c r="{ref}" t="s"><v>{idx[val]}</v></c>')
        body.append(f'<row r="{ri}">{"".join(cells)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns:r="r"><sheets><sheet name="Connection Project List" '
                                      'sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Type="ws" '
                                                 'Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   "<sst>" + "".join(f"<si><r><t>{s[:3]}</t></r><r><t>{s[3:]}</t></r></si>" for s in ss) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{''.join(body)}</sheetData></worksheet>")
    return buf.getvalue()


OLD = xlsx([["Project Name", "Planning Area", "Project Type", "MW Type", "Stage", "Inclusion", "Applied On",
             "STS MW", "DTS MW", "ISD"], ["P1 Load", "x", "Connection", "Load", "1", "Yes", "1", "0", "10", "1"]])
A = "/assets/Uploads/project-reporting/"


class Aeso:
    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, req):
        u = str(req.url)
        self.calls.append(u)
        if u == aq.INDEX:
            links = "".join(f'<li><a href="{A}{name}">{name}</a></li>' for name in self.files)
            return httpx.Response(200, text=f"<html>{links}</html>")
        name = u.rsplit("/", 1)[-1]
        if name in self.files:
            return httpx.Response(200, content=self.files[name])
        return httpx.Response(404)


def run_aeso(h, now=NOW):
    return build(SPECS["power_aeso_dc_queue"]).run(ctx(h, now))


def test_aeso_data_load_dts_summed_over_stages_by_status(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    files = {"November-2026-Project-List.xlsx": xlsx([HEAD, *ROWS], inline_status=True),
             "October-2026-Project-List.xlsx": xlsx([HEAD, *ROWS[:2]]),
             "October-2024-Project-List.xlsx": OLD}                        # before `from`: never downloaded
    h = Aeso(files)
    v = values(run_aeso(h))
    nov, oct_ = datetime(2026, 11, 1), datetime(2026, 10, 1)
    # Active 60 + 910 + 4.99; Cluster 3: DTS only (STS is the gas plant); Under review 1000; not energized/cancelled
    assert v[("aeso.queue.data_load_mw.active", nov)] == pytest.approx(974.99)
    assert v[("aeso.queue.data_load_mw.cluster", nov)] == 600
    assert v[("aeso.queue.data_load_mw.under_review", nov)] == 1000
    assert v[("aeso.queue.data_load_mw", nov)] == pytest.approx(974.99 + 600 + 1000)
    assert v[("aeso.queue.data_load_projects", nov)] == 4
    assert v[("aeso.queue.data_load_energized_mw", nov)] == 20
    assert v[("aeso.queue.data_load_mw.on_hold", nov)] == 0
    assert v[("aeso.queue.data_load_mw", oct_)] == pytest.approx(974.99)
    assert not any("October-2024" in c for c in h.calls)
    # next run: the index only
    h2 = Aeso(files)
    assert values(run_aeso(h2)) == v and h2.calls == [aq.INDEX]


def test_aeso_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    with pytest.raises(FormatDrift, match="unknown Status"):
        aq.parse_list(xlsx([HEAD, ["Paused", "P1 Data Load", "", "", "BTF", "Data Load", "1", "", "", "", "0", "5"]]))
    with pytest.raises(FormatDrift, match="isn't a number"):
        aq.parse_list(xlsx([HEAD, ["Active", "P1 Data Load", "", "", "BTF", "Data Load", "1", "", "", "", "0", "TBD"]]))
    with pytest.raises(FormatDrift, match="outside"):
        aq.parse_list(xlsx([HEAD, ["Active", "P1 Data Load", "", "", "BTF", "Data Load", "1", "", "", "", "0",
                                   "25000"]]))
    with pytest.raises(FormatDrift, match="not the expected layout"):
        run_aeso(Aeso({"November-2026-Project-List.xlsx": OLD}))
    with pytest.raises(FormatDrift, match="not the expected .xlsx"):
        aq.parse_list(b"<html>maintenance</html>")
    assert aq.parse_list(OLD) is None                                     # an old list in the back-fill: skipped


def test_aeso_unchanged_index_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    res = run_aeso(lambda r: httpx.Response(304))
    assert res.obs == []


# ------------------------------------------------------------------ specs
def test_specs_region_retention_and_declared_ids():
    for sid, region in (("power_dc_epoch", "rest"), ("power_ercot_lli", "tx"), ("power_aeso_dc_queue", "ab_nebc")):
        s = SPECS[sid]
        assert s["region"] == region and s["retention"] == "daily" and s["interval"] == "1d" and s["mode"] == "cached"
    ids = {m.series_id for sid in ("power_dc_epoch", "power_ercot_lli", "power_aeso_dc_queue")
           for m in build(SPECS[sid]).declared_series()}
    assert {"dc.epoch.tx.it_mw", "dc.epoch.wcan.it_mw", "ercot.lli.observed_mw", "ercot.lli.approved_mw",
            "aeso.queue.data_load_mw", "aeso.queue.data_load_projects"} <= ids
