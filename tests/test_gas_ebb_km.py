"""Kinder Morgan EBB connector (montney/connectors/gas_ebb_km.py): request sequence, classification, KM-to-KM removal,
the unchanged-cycle skip, the once-a-day final pull, borders and drift.

Synthetic boards shaped like pipeline2.kindermorgan.com's (the page, its Excel downloads, the location CSV); every
number is made up.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_km as km
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

K = 1037.0
# 2026-11-13 16:30 UTC = 10:30 CT: after the 09:00 final hour, before ID1 (14:00)
NOW = datetime(2026, 11, 13, 16, 30)
TODAY, YDAY = date(2026, 11, 13), date(2026, 11, 12)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    return tmp_path


# ------------------------------------------------------------------ synthetic site
def _ref(r: int, c: int) -> str:
    return f"{chr(65 + c)}{r + 1}"


def make_xlsx(rows: list[list]) -> bytes:
    """A one-sheet .xlsx; strings through sharedStrings (as KM's are)."""
    shared: list[str] = []
    body = []
    for i, row in enumerate(rows):
        cells = []
        for j, v in enumerate(row):
            if v is None:
                continue
            shared.append(str(v))
            cells.append(f'<c r="{_ref(i, j)}" t="s"><v>{len(shared) - 1}</v></c>')
        body.append(f'<row r="{i + 1}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)
                   + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(body)}</sheetData></worksheet>")
    return buf.getvalue()


HEAD = ["TSP", "TSP Name", "Eff Gas Day/Eff Time", "CycleDesc", "Loc Purp Desc", "Meas Basis Desc",
        "Post Date/Post Time", "Loc/QTI Desc"]
PCOLS = ["Loc", "Loc Name", "Loc Zn", "Loc (Segment)", "Design Capacity", "Operating Capacity",
         "Total Scheduled Quantity", "Operationally Available Capacity", "IT", "Flow Ind", "All Qty Avail", "Qty Reason"]
SCOLS = ["Loc (Segment)", "Loc Name (Segment)", "Loc Zn", "Design Capacity", "Operating Capacity",
         "Total Scheduled Quantity", "Operationally Available Capacity", "IT", "Flow Ind", "All Qty Avail", "Qty Reason"]


def head_row(day: date, cycle: str, posted: str, purpose: str) -> list:
    return ["1", "PIPE", f"{day.month}/{day.day}/{day.year} 09:00 AM CCT", cycle, purpose,
            "Million BTU's (displayed as Dth)", posted, "x"]


def point_xlsx(day, cycle, posted, side, pts: list[tuple[str, float]]) -> bytes:
    rows = [HEAD, head_row(day, cycle, posted, f"{side} Location"), PCOLS]
    rows += [[loc, f"NAME {loc}", "1", "10", "999", "999", f"{q:,.0f}", "0", "N", side[0], "Y", ""] for loc, q in pts]
    rows += [[f"Row Count: {len(pts)}"], ["Comments and Notes"], ["1) Design Capacity ..."]]
    return make_xlsx(rows)


def seg_xlsx(day, cycle, posted, segs: list[tuple[str, str, str, float]]) -> bytes:
    rows = [HEAD, head_row(day, cycle, posted, "Pipeline segment defined by 1 Location"), SCOLS]
    rows += [[n, name, "1", "999", "999", f"{q:,.0f}", "0", "N", fi, "Y", ""] for n, name, fi, q in segs]
    return make_xlsx(rows)


LOC_HDR = ("TSP,TSP Name,TSP FERC CID,Date/Time,Comments,Loc,Loc Name,Dir Flo,Loc Cnty,Loc St Abbrev,Loc Type Ind,"
           "Loc Zone (Rec),Loc Zone (Del),Seg Nbr,Nom Ind,Loc Stat Ind,Eff Date,Inact Date,Up/Dn Ind,Up/Dn Name,Up/Dn ID,"
           "Up/Dn ID Prop,Up/Dn FERC CID Ind,Up/Dn FERC CID,Up/Dn Loc,Up/Dn Loc Name,Up/Dn Loc 2,Up/Dn Loc Name2,Update D/T")


def loc_csv(points: list[tuple[str, str, str, str, str]]) -> str:
    """points: (loc, name, state, type, counterparty)."""
    lines = [LOC_HDR] + [f'1,PIPE,C1,x,x,{loc},"{name}",D,CNTY,{st},{t},Z,Z,10,Y,A,,,Y,"{cp}",1,1,N,,,,,,x'
                         for loc, name, st, t, cp in points]
    return "\n".join(lines)


def page(radio: bool = True) -> str:
    r = ('<input id="a" type="radio" name="ctl00$C$location" value="rbDelivery" checked="checked" />'
         '<input id="b" type="radio" name="ctl00$C$location" value="rbReceipt" />') if radio else ""
    return ('<form><input type="hidden" name="__VIEWSTATE" value="VS" />'
            f'<input type="hidden" name="{km.DATE_FIELD}" value="" />{r}'
            '<input type="image" name="ctl00$C$HeaderBTN1$btnDownload" src="x.png" /></form>')


# Board AAA: an EPNG-like board in TX + AZ with a segment border; BBB: another KM board (interconnect with AAA);
# SEG: a segment-only board.
LOCS = {
    "AAA": [("1", "LDC TX", "TX", "LDC", "ATMOS"), ("2", "POWER AZ", "AZ", "EGN", "SRP"),
            ("3", "PLANT NM", "NM", "PLT", "DCP"), ("4", "MEXICO TX", "TX", "INT", "IENOVA PIPELINES"),
            ("5", "TO BBB", "AZ", "INT", "BBB PIPELINE CO"), ("6", "STORAGE TX", "TX", "STR", "WASHINGTON RANCH"),
            ("7", "OTHER PIPE TX", "TX", "INT", "GULF COAST EXPRESS"), ("8", "OKLAHOMA", "OK", "LDC", "ONG"),
            ("9", "SOCAL EHRENBERG", "AZ", "INT", "SOUTHERN CALIFORNIA GAS"),
            ("10", "NGPL/AAA CDP", "TX", "VIR", ""), ("11", "POOL", "TX", "VIR", "")],
    "BBB": [("5", "TO BBB", "AZ", "INT", "AAA PIPELINE CO"), ("21", "BBB LDC AZ", "AZ", "LDC", "SOUTHWEST GAS")],
}
SPEC = {"id": "gas_ebb_km_test", "type": "python", "module": "montney.connectors.gas_ebb_km", "class": "GasEbbKm",
        "params": {
            "base": "https://km.test", "max_requests": 60, "pause_s": 1.0, "final_after_ct": "09:00",
            "km_counterparties": ["AAA PIPELINE", "BBB PIPELINE"], "mexico_counterparties": ["IENOVA"],
            "canada_counterparties": ["TRANSCANADA"], "lng_counterparties": ["CHENIERE CORPUS CHRISTI"],
            "boards": {"AAA": {"name": "Aaa", "regions": {"tx": ["ldc", "storage_inj", "storage_wd", "export_mexico"],
                                                         "sunbelt": ["power", "production"]}},
                       "BBB": {"name": "Bbb", "regions": {"sunbelt": ["ldc"]}},
                       "SEG": {"name": "Seg"}},
            "borders": [{"id": "ebb.aaa.border.tx_sunbelt", "board": "AAA", "forward": "TD1",
                         "segments": [["100", "EL PASO"], ["100", "EL PASO E"]]},
                        {"id": "ebb.aaa.border.sunbelt_cal", "board": "AAA", "locs": ["9"]},
                        {"id": "ebb.seg.border.ok_tx", "board": "SEG", "forward": "TD1",
                         "segments": [["7", "OK/TX BORDER"]]}]}}

DTH = {   # (board, side) -> [(loc, Dth)] for today; yesterday is these × 2
    ("AAA", "Delivery"): [("1", 103_700), ("2", 207_400), ("4", 51_850), ("5", 1_037_000), ("6", 10_370),
                          ("7", 500_000), ("8", 99_999), ("9", 518_500), ("10", 20_740), ("11", 77_777),
                          ("3", 10_370)],
    ("AAA", "Receipt"): [("3", 311_100), ("6", 20_740), ("4", 10_370), ("7", 300_000)],
    ("BBB", "Delivery"): [("21", 62_220)],
    ("BBB", "Receipt"): [("5", 1_037_000)],
}
SEGS = {"AAA": [("100", "EL PASO", "TD1", 1_037_000), ("100", "EL PASO E", "TD1", 518_500),
                ("100", "EL PASO E", "TD2", 103_700), ("200", "TOPOCK", "TD1", 5)],
        "SEG": [("7", "OK/TX BORDER", "TD1", 933_300), ("7", "OK/TX BORDER", "TD2", 0)]}


class Site:
    """The mock host: records every request; cycles/post times per board can be changed between runs."""

    def __init__(self):
        self.log: list[tuple[str, str, str]] = []
        self.cycle = {"AAA": ("EVENING", "11/12/2026 08:32 PM"), "BBB": ("EVENING", "11/12/2026 08:45 PM"),
                      "SEG": ("EVENING", "11/12/2026 08:47 PM")}
        self.segs = {k: list(v) for k, v in SEGS.items()}
        self.locs = dict(LOCS)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, code = req.url.path, req.url.params.get("code")
        if req.method == "GET":
            self.log.append(("GET", path, code))
            return httpx.Response(200, text=page(radio="OpAvailPoint" in path))
        f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        day = TODAY
        if f.get(km.DATE_FIELD):
            y, m, d = f[km.DATE_FIELD].split('"')[1][2:].split("-")[:3]
            day = date(int(y), int(m), int(d))
        mult = 2 if day == YDAY else 1
        cycle, posted = self.cycle[code] if day == TODAY else ("ID3", "11/12/2026 09:50 PM")
        if "LocDataDwnld" in path:
            self.log.append(("POST", "loc", code))
            return httpx.Response(200, text=loc_csv(self.locs[code]))
        if "OpAvailSegment" in path:
            self.log.append(("POST", f"seg {day}", code))
            return httpx.Response(200, content=seg_xlsx(day, cycle, posted,
                                                        [(n, nm, fi, q * mult) for n, nm, fi, q in self.segs[code]]))
        side = "Delivery" if f.get("ctl00$C$location") == "rbDelivery" else "Receipt"
        self.log.append(("POST", f"{side} {day}", code))
        return httpx.Response(200, content=point_xlsx(day, cycle, posted, side,
                                                      [(loc, q * mult) for loc, q in DTH[(code, side)]]))


def run(site: Site, now: datetime = NOW, spec: dict = SPEC):
    conn = km.GasEbbKm(spec)
    slept: list[float] = []
    conn.sleep = slept.append
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(site.handler)), now=now)
    res = conn.run(ctx)
    return res, ctx, {(o.series_id, o.ts.date()): o for o in res.obs}


# ------------------------------------------------------------------ tests
def test_first_run_request_sequence_and_count():
    site = Site()
    res, ctx, _ = run(site)
    aaa = [e for e in site.log if e[2] == "AAA"]
    assert aaa == [("GET", "/LocationDataDownload/LocDataDwnld.aspx", "AAA"), ("POST", "loc", "AAA"),
                   ("GET", "/Capacity/OpAvailPoint.aspx", "AAA"),
                   ("POST", f"Delivery {TODAY}", "AAA"), ("POST", f"Receipt {TODAY}", "AAA"),
                   ("GET", "/Capacity/OpAvailSegment.aspx", "AAA"), ("POST", f"seg {TODAY}", "AAA"),
                   ("POST", f"Delivery {YDAY}", "AAA"), ("POST", f"Receipt {YDAY}", "AAA"),
                   ("POST", f"seg {YDAY}", "AAA")]
    seg = [e for e in site.log if e[2] == "SEG"]          # segment-only: no location file, no point page
    assert seg == [("GET", "/Capacity/OpAvailSegment.aspx", "SEG"), ("POST", f"seg {TODAY}", "SEG"),
                   ("POST", f"seg {YDAY}", "SEG")]
    assert len(site.log) == ctx.n_requests == 10 + 7 + 3
    assert not res.warnings


def test_pause_between_requests():
    site = Site()
    conn = km.GasEbbKm(SPEC)
    slept: list[float] = []
    conn.sleep = slept.append
    conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(site.handler)), now=NOW))
    assert len(slept) == len(site.log) - 1 and all(0 < s <= 1.0 for s in slept)


def test_categories_regions_and_km_internal_removal():
    _, _, o = run(Site())
    v = {k: ob.value for k, ob in o.items()}
    assert v[("ebb.aaa.tx.ldc", TODAY)] == pytest.approx(100 + 20)          # LDC + the virtual "… CDP" citygate
    assert v[("ebb.aaa.sunbelt.power", TODAY)] == pytest.approx(200)
    assert v[("ebb.aaa.sunbelt.production", TODAY)] == pytest.approx(300 - 10)   # receipts − deliveries (NM plant)
    assert v[("ebb.aaa.tx.export_mexico", TODAY)] == pytest.approx(50 - 10)
    assert v[("ebb.aaa.tx.storage_inj", TODAY)] == pytest.approx(10)
    assert v[("ebb.aaa.tx.storage_wd", TODAY)] == pytest.approx(20)
    assert v[("ebb.bbb.sunbelt.ldc", TODAY)] == pytest.approx(60)
    # the 1,000 MMcf/d AAA → BBB transfer is in no series; other interconnects, OK points and paper pools neither
    assert v[("ebb.km.sunbelt.ldc", TODAY)] == pytest.approx(60)
    assert v[("ebb.km.tx.ldc", TODAY)] == pytest.approx(120)
    assert v[("ebb.aaa.tx.ldc", YDAY)] == pytest.approx(240)                # yesterday's final (× 2 in the mock)
    ob = o[("ebb.aaa.tx.ldc", TODAY)]
    assert ob.ts == datetime(2026, 11, 13) and ob.as_of == datetime(2026, 11, 13, 2, 32)   # 20:32 CST → UTC


def test_km_transfer_goes_to_diagnostics_never_a_category(home):
    """The AAA → BBB transfer is KM-internal even when its counterparty would also match an export/LNG list."""
    spec = {**SPEC, "params": {**SPEC["params"], "lng_counterparties": ["BBB PIPELINE"],
                               "mexico_counterparties": ["IENOVA", "AAA PIPELINE"]}}
    _, _, o = run(Site(), spec=spec)
    assert o[("ebb.aaa.tx.export_mexico", TODAY)].value == pytest.approx(40)
    assert ("ebb.aaa.sunbelt.lng", TODAY) not in o and ("ebb.km.sunbelt.lng", TODAY) not in o
    diag = km.cache_load(km.STATE)["diag"]
    assert diag["AAA"][str(TODAY)]["regions"]["sunbelt"]["km_internal"] == pytest.approx(1000)
    assert diag["AAA"][str(TODAY)]["regions"]["sunbelt"]["int_out"] == pytest.approx(500)          # SoCal Ehrenberg
    assert diag["BBB"][str(TODAY)]["regions"]["sunbelt"]["km_internal"] == pytest.approx(1000)
    assert "int_in" not in diag["BBB"][str(TODAY)]["regions"]["sunbelt"]


def test_borders_signed_by_direction():
    _, _, o = run(Site())
    assert o[("ebb.aaa.border.tx_sunbelt", TODAY)].value == pytest.approx(1000 + 500 - 100)
    assert o[("ebb.aaa.border.sunbelt_cal", TODAY)].value == pytest.approx(500)
    assert o[("ebb.seg.border.ok_tx", TODAY)].value == pytest.approx(900)
    assert o[("ebb.seg.border.ok_tx", YDAY)].value == pytest.approx(1800)


def test_unchanged_cycle_skips_the_other_posts_and_final_runs_once_a_day():
    site = Site()
    run(site)
    # 18:00 UTC = 12:00 CT: nothing can have changed (no new day, no cycle boundary, final done) → not one request
    site.log.clear()
    res, ctx, o = run(site, now=datetime(2026, 11, 13, 18, 0))
    assert ctx.n_requests == 0 and not res.obs
    # 20:30 UTC = 14:30 CT: ID1 boundary passed → each board probes (GET + one POST); AAA posted ID1, the others didn't
    site.cycle["AAA"] = ("INTRADAY 1", "11/13/2026 02:05 PM")
    site.log.clear()
    res, ctx, o = run(site, now=datetime(2026, 11, 13, 20, 30))
    assert [e for e in site.log if e[2] == "BBB"] == [("GET", "/Capacity/OpAvailPoint.aspx", "BBB"),
                                                      ("POST", f"Delivery {TODAY}", "BBB")]
    assert [e for e in site.log if e[2] == "SEG"] == [("GET", "/Capacity/OpAvailSegment.aspx", "SEG"),
                                                      ("POST", f"seg {TODAY}", "SEG")]
    assert [e[1] for e in site.log if e[2] == "AAA"] == ["/Capacity/OpAvailPoint.aspx", f"Delivery {TODAY}",
                                                         f"Receipt {TODAY}", "/Capacity/OpAvailSegment.aspx",
                                                         f"seg {TODAY}"]
    assert not any(str(YDAY) in e[1] for e in site.log)                   # final already done for 11-12
    assert o[("ebb.aaa.tx.ldc", TODAY)].as_of == datetime(2026, 11, 13, 20, 5)
    assert ("ebb.bbb.sunbelt.ldc", TODAY) not in o
    # the roll-up is re-emitted from the stored per-board totals (BBB unchanged, AAA new)
    assert o[("ebb.km.sunbelt.ldc", TODAY)].value == pytest.approx(60)
    assert o[("ebb.km.tx.ldc", TODAY)].as_of == datetime(2026, 11, 13, 20, 5)
    # next day 10:00 CT: a new gas day and a new final (11-13)
    site.log.clear()
    run(site, now=datetime(2026, 11, 14, 16, 0))
    assert site.log.count(("POST", "Delivery 2026-11-13", "AAA")) == 2   # the probe (mock serves 11-13) + the final


def test_final_waits_for_the_final_hour():
    site = Site()
    run(site, now=datetime(2026, 11, 13, 13, 0))                          # 07:00 CT
    assert not any(str(YDAY) in e[1] for e in site.log)


def test_budget_defers_boards():
    spec = {**SPEC, "params": {**SPEC["params"], "max_requests": 12}}
    site = Site()
    res, ctx, o = run(site, spec=spec)
    assert ctx.n_requests <= 12
    assert any("deferred" in w for w in res.warnings)
    assert ("ebb.km.sunbelt.ldc", TODAY) not in o                         # BBB deferred: that family is incomplete
    assert ("ebb.km.tx.ldc", TODAY) in o                                  # AAA alone carries tx ldc


def test_missing_segment_is_drift_and_keeps_the_board_state():
    site = Site()
    site.segs["AAA"] = [s for s in site.segs["AAA"] if s[1] != "EL PASO E"]
    res, _, o = run(site)
    assert any("AAA" in w and "FormatDrift" in w and "EL PASO E" in w for w in res.warnings)
    assert ("ebb.aaa.tx.ldc", TODAY) not in o and ("ebb.bbb.sunbelt.ldc", TODAY) in o
    with pytest.raises(FormatDrift, match="EL PASO E"):
        km.segment_border([{"Loc (Segment)": "100", "Loc Name (Segment)": "EL PASO", "Flow Ind": "TD1",
                            "Total Scheduled Quantity": "1"}], SPEC["params"]["borders"][0], "t")
    site.segs = {k: list(v) for k, v in SEGS.items()}                    # fixed: AAA is pulled again
    site.log.clear()
    _, _, o = run(site, now=datetime(2026, 11, 13, 17, 0))
    assert ("ebb.aaa.tx.ldc", TODAY) in o


def test_changed_header_is_drift():
    good = point_xlsx(TODAY, "EVENING", "11/12/2026 08:32 PM", "Delivery", [("1", 1037)])
    key, rows = km.point_rows(good, "Delivery", "t")
    assert key == (TODAY, "EVENING", datetime(2026, 11, 13, 2, 32)) and rows[0]["Loc"] == "1"
    with pytest.raises(FormatDrift, match="not Receipt"):
        km.point_rows(good, "Receipt", "t")
    renamed = make_xlsx([HEAD, head_row(TODAY, "EVENING", "11/12/2026 08:32 PM", "Delivery Location"),
                         [c.replace("Total Scheduled Quantity", "Scheduled Qty") for c in PCOLS]])
    with pytest.raises(FormatDrift, match="Total Scheduled Quantity"):
        km.point_rows(renamed, "Delivery", "t")
    mcf = make_xlsx([HEAD, ["1", "P", "11/13/2026 09:00 AM CCT", "EVENING", "Delivery Location", "Mcf",
                            "11/12/2026 08:32 PM", "x"], PCOLS])
    with pytest.raises(FormatDrift, match="not Dth"):
        km.point_rows(mcf, "Delivery", "t")
    with pytest.raises(FormatDrift, match="not an .xlsx"):
        km.point_rows(b"<html>The data range allowed is 90 days</html>", "Delivery", "t")
    with pytest.raises(FormatDrift, match="Up/Dn Name"):
        km.parse_locations(LOC_HDR.replace("Up/Dn Name,", "Counterparty,") + "\n", "t")


def test_location_file_cached_weekly(home):
    site = Site()
    run(site)
    site.log.clear()
    site.cycle = {k: ("INTRADAY 1", "11/13/2026 02:05 PM") for k in site.cycle}
    run(site, now=datetime(2026, 11, 13, 20, 30))
    assert not any(e[1] == "loc" for e in site.log)                      # cached
    assert (home / "cache" / "ebb_km_loc_AAA.json").exists()
    site.log.clear()
    run(site, now=datetime(2026, 11, 21, 3, 0))                            # 8 days on: refreshed
    assert ("POST", "loc", "AAA") in site.log


def test_real_spec_series_and_borders():
    conn = build(load_specs(include_disabled=True)["gas_ebb_km"])
    ids = [m.series_id for m in conn.declared_series()]
    assert len(ids) == len(set(ids))
    assert {"ebb.epng.sunbelt.power", "ebb.tgp.upne.ldc", "ebb.km.mtnw.production", "ebb.km.tx.lng",
            "ebb.tgp.border.nyc_upne", "ebb.epng.border.sunbelt_cal"} <= set(ids)
    regions = set(km.us_state_regions().values())
    p = conn.spec["params"]
    for code, b in p["boards"].items():
        assert set(b.get("regions", {})) <= regions, code
        for cats in b.get("regions", {}).values():
            assert set(cats) <= set(km.CATS), code
    for b in p["borders"]:
        assert b["board"] in p["boards"] and ("segments" in b or "locs" in b) and "counterparties" not in b
        assert b["id"].startswith(f"ebb.{b['board'].lower()}.border.")


def test_no_state_guard_fails_the_board_and_rereads_its_location_file(monkeypatch):
    """The no-state guard (Reviewer on #478; this used to warn and drop): a point with gas that AAA's location file lacks
    would fall out of every total. AAA writes nothing that run (the other boards do, like any per-board FormatDrift);
    once its cached location file is over a day old it is re-read early, and when it carries the point AAA writes
    again."""
    monkeypatch.setitem(DTH, ("AAA", "Delivery"), DTH[("AAA", "Delivery")] + [("12", 41_480)])
    site = Site()
    res, _, o = run(site)
    assert any(w.startswith("AAA: FormatDrift: AAA current: 1 point(s) with scheduled gas but no state") and "['12']" in w
               for w in res.warnings)
    assert not any(k[0].startswith("ebb.aaa.") for k in o) and ("ebb.km.tx.ldc", TODAY) not in o
    assert ("ebb.bbb.sunbelt.ldc", TODAY) in o and ("ebb.seg.border.ok_tx", TODAY) in o
    assert site.log.count(("POST", "loc", "AAA")) == 1                    # read this run: no early re-read
    # 12:00 CT: only AAA is due (it never advanced) and fails alone, so the run fails; its cached file is > 1 day old
    # (aged here), so it is read again first, even in a failed run
    locs, _ = km.loc_cache_read("ebb_km_loc_AAA", "locs")
    km.loc_cache_write("ebb_km_loc_AAA", "locs", NOW - timedelta(days=2), locs)
    site.locs = {**LOCS, "AAA": LOCS["AAA"] + [("12", "NEW LDC TX", "TX", "LDC", "ATMOS")]}
    site.log.clear()
    with pytest.raises(FormatDrift, match=r"\['12'\]"):
        run(site, now=datetime(2026, 11, 13, 18, 0))
    assert {e[2] for e in site.log} == {"AAA"}
    assert site.log[-2:] == [("GET", "/LocationDataDownload/LocDataDwnld.aspx", "AAA"), ("POST", "loc", "AAA")]
    # the run after: AAA is pulled again and counts the point; no location request
    site.log.clear()
    res, _, o = run(site, now=datetime(2026, 11, 13, 18, 30))
    assert not any("FormatDrift" in w for w in res.warnings)
    assert o[("ebb.aaa.tx.ldc", TODAY)].value == pytest.approx(100 + 20 + 40)
    assert not any(e[1] == "loc" for e in site.log)


def test_no_state_guard_zero_tsq_and_unlocated_ok(tmp_path, monkeypatch):
    """A blank state in the location file is no state. With TSQ 0 the point passes; with gas it fails the board unless
    the board's `unlocated_ok` names it (matched on the OA or the location-file name); then nothing else changes."""
    blanco = ("13", "EPNG/EPNG (BALBLNCO) BLANCO BALANCI", "", "VIR", "")
    homes = iter(range(10))

    def run_fresh(site, spec=SPEC):                     # each run from an empty home (state, location cache)
        monkeypatch.setenv("MONTNEY_HOME", str(tmp_path / f"home{next(homes)}"))
        return run(site, spec=spec)
    base = {k: ob.value for k, ob in run_fresh(Site())[2].items()}
    receipts = DTH[("AAA", "Receipt")]
    for q in (0, 10_370):
        monkeypatch.setitem(DTH, ("AAA", "Receipt"), receipts + [("13", q)])
        site = Site()
        site.locs = {**LOCS, "AAA": LOCS["AAA"] + [blanco]}
        res, _, o = run_fresh(site)
        if q == 0:
            assert {k: ob.value for k, ob in o.items()} == base and not res.warnings
        else:
            assert any("AAA: FormatDrift" in w and "['13']" in w for w in res.warnings)
            assert not any(k[0].startswith("ebb.aaa.") for k in o)
    spec = {**SPEC, "params": {**SPEC["params"], "boards": {
        **SPEC["params"]["boards"], "AAA": {**SPEC["params"]["boards"]["AAA"], "unlocated_ok": r"\(BALBLNCO\)"}}}}
    site = Site()
    site.locs = {**LOCS, "AAA": LOCS["AAA"] + [blanco]}
    res, _, o = run_fresh(site, spec=spec)
    assert not res.warnings and {k: ob.value for k, ob in o.items()} == base
    boards = load_specs(include_disabled=True)["gas_ebb_km"]["params"]["boards"]
    km.require_located(km.point_locations({"Receipt": [{"Loc": "53711", "Loc Name": "BLANCO BALANCING",
                                                        "Total Scheduled Quantity": "5,000"}]},
                                          {"53711": {"name": blanco[1], "st": ""}}), boards["EPNG"]["unlocated_ok"], "EPNG")
    km.require_located(km.point_locations({"Delivery": [{"Loc": "40802", "Loc Name": "", "Total Scheduled Quantity": "9"}]},
                                          {"40802": {"name": "VAR/VAR DIME DESIGN PT - SEG. 1 - B", "st": ""}}),
                       boards["NGPL"]["unlocated_ok"], "NGPL")


def test_point_border_is_listed_locs_and_a_missing_loc_is_drift():
    """Reviewer on #476: EPNG's California border is listed by loc, so a re-coded or vanished point can't silently drop
    it (from ~1,213 to ~673 or 0); a loc that's gone is FormatDrift for that board."""
    b = SPEC["params"]["borders"][1]
    rows = {"Delivery": [{"Loc": "9", "Total Scheduled Quantity": "1037"}], "Receipt": []}
    assert km.point_border(rows, {}, b, "t") == pytest.approx(1.0)
    with pytest.raises(FormatDrift, match=r"\['9'\] gone"):
        km.point_border({"Delivery": [{"Loc": "8", "Total Scheduled Quantity": "1037"}], "Receipt": []}, {}, b, "t")
    with pytest.raises(ValueError, match="needs"):
        km.point_border(rows, {}, {"id": "x", "counterparties": ["SOUTHERN CALIFORNIA GAS"]}, "t")
    real = {x["id"]: x for x in load_specs(include_disabled=True)["gas_ebb_km"]["params"]["borders"]}
    assert real["ebb.epng.border.sunbelt_cal"]["locs"] == ["301016", "332503", "301693", "314604"]
