"""National Fuel Gas Supply and Empire (gas_ebb_natfuel): the PeopleSoft request sequence (page, Retrieve, Download
CSV, file), the cheap check, the gas-day switch before 09:00 CT, sums with the shipped spec's hand map, aggregates and
subsets left out, named points, the no-state guard and FormatDrift. Shapes copy the live pages / CSVs (2026-11-13); the
volumes are made up (multiples of 1,037 Dth)."""

from __future__ import annotations

import copy
from datetime import date, datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_common
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 20, 0)          # 14:00 CST: gas day 11-13, yesterday's final due
D = 1037.0
SPEC = load_specs(include_disabled=True)["gas_ebb_natfuel"]
COLS = ('"Loc Name","Loc","Loc Purp Desc","Loc/QTI","County","State","All Qty Avail","Design Capacity",'
        '"Operating Capacity","Operationally Available Capacity","Scheduled Primary","Scheduled Secondary Prime",'
        '"Scheduled Secondary","Scheduled Interruptible","IT Indicator","Total Scheduled Quantity","Flow Indicator",'
        '"Qty Reason","Comments",')

# (name, loc, purpose, qti, state, MMcf/d, comment)
NFG = [("NFGSC - Concord Station N to S", "LINE X", "P/L Seg 1", "SGQ", "NY", 400, ""),
       ("YM-53 S TO N", "YM-53 S TO N", "P/L Seg 1", "RPQ", "", 65, ""),              # a segment coded RPQ
       ("M- Excess Storage", "M- STORAGE PTS", "Delivery Location", "DPQ", "", 1, "Total storage injections"),
       ("M- Excess Storage", "M- STORAGE PTS", "Receipt Location", "RPQ", "", 43, "Total storage withdrawals"),
       ("New York Points - Appl Zone 3", "APPZN3", "Receipt Location", "RPQ", "", 2, ""),
       ("Nfg Dist. City-Gate - NY", "NFGDC-NY", "Delivery Location", "DPQ", "NY", 150, ""),
       ("Nfg Dist. City-Gate - PA", "NFGDC-PA", "Delivery Location", "DPQ", "PA", 45, ""),
       ("Nfg Dist. Mineral Springs", "NFGDC-MSMX", "Delivery Location", "DPQ", "NY", 70,
        "This point is a subset of NFGDC-NY"),
       ("TCPL - Niagara*", "421079", "Receipt Location", "RPQ", "NY", 0, ""),
       ("TCPL - Niagara*", "421079", "Delivery Location", "DPQ", "NY", 310, ""),
       ("EMPIRE - Pendleton*", "012003020", "Receipt Location", "RPQ", "NY", 60, ""),
       ("TGP - East Aurora*", "420077", "Delivery Location", "DPQ", "NY", 17, ""),
       ("MPCL - Independence*", "2078", "Receipt Location", "RPQ", "NY", 40, ""),
       ("EGTS - DTI Rq Points*", "20900", "Receipt Location", "RPQ", "NY", 35, ""),
       ("IESI - Indeck Olean", "INDECKOLEAN", "Delivery Location", "DPQ", "NY", 3, ""),
       ("Nathan Petroleum Corp.", "NSP9235551", "Receipt Location", "RPQ", "NY", 1, ""),
       ("ENRGYMRK_APPZN3", "PL00000177", "Receipt Location", "RPQ", "NY", 1, ""),
       ("TGP - Roselake*", "420527", "Receipt Location", "RPQ", "PA", 380, ""),
       ("WALES MARKET POOL", "WALES", "Receipt Location", "RPQ", "NY", 5, "")]
EMP = [("Connector South to North", "CONNECTOR S-N", "P/L Seg 1", "SGQ", "NY", 620, ""),
       ("Connector North to South", "CONNECTOR N-S", "P/L Seg 1", "SGQ", "NY", 0, ""),
       ("EMPIRE - Imbalance Point", "IMBALANCE", "Delivery Location", "DPQ", "", 1, ""),
       ("TEUI - Repsol Oil & Gas USA, LLC", "EPRU91101", "Receipt Location", "RPQ", "PA", 310, ""),
       ("NFGMIDCOV - NFG Midstream Covington", "EPRU91111", "Receipt Location", "RPQ", "PA", 190, ""),
       ("TCPL - Chippawa*", "012000010", "Receipt Location", "RPQ", "NY", 40, ""),
       ("TCPL - Chippawa*", "012000010", "Delivery Location", "DPQ", "NY", 230, ""),
       ("NFGSC - Pendleton*", "012003020", "Delivery Location", "DPQ", "NY", 60, ""),
       ("NYSEG - Royalton*", "012004010", "Delivery Location", "DPQ", "NY", 21, ""),
       ("NG - Phoenix Line 63 - Sithe*", "012002010", "Delivery Location", "DPQ", "NY", 165, ""),
       ("TGP - Hopewell*", "412856", "Delivery Location", "DPQ", "NY", 155, ""),
       ("MPCL - Corning*", "022008810", "Receipt Location", "RPQ", "NY", 110, ""),
       ("BYJL - Byron from Plant", "BYRONFROM", "Receipt Location", "RPQ", "NY", 1, "")]
HAND = {"natfuel": {"NFGDC-NY", "NSX1U47021", "421079", "012003010", "012003020", "012003015", "022009010", "420076",
                    "420077", "420088", "420092", "420243", "420326", "420428", "420497", "49756", "20900", "020502",
                    "606000", "633863", "2078", "NSX9223491", "623906", "WYCKOFF", "INDECKOLEAN", "STBONA",
                    "DRESSEROLEAN", "NSX3247071", "PSN1242991", "WALES"},
        "empire": {"012000010", "012003010", "012003020", "012003015", "022009010", "412856", "022008810", "012006100",
                   "012004010", "012004020", "012001010", "012002020", "012002010", "022008610", "012005010",
                   "BYRONTO", "EPP247081", "BYRONFROM", "EPRC47091"}}


def zero_rows(pipe, rows):
    """The hand-mapped points the sample leaves out, posted at 0 (the board lists every active point)."""
    have = {r[1] for r in rows}
    return [(f"pt {loc}", loc, "Delivery Location", "DPQ", "NY", 0, "") for loc in sorted(HAND[pipe] - have)]


def oa_csv(pipe, day, cycle, posted, rows, unit="MMBTU", scale=1.0):
    rows = rows + zero_rows(pipe, rows)
    tsp = "National Fuel Gas Supply Corporation" if pipe == "natfuel" else "Empire Pipeline, Inc."
    head = ["Operationally Available Capacity", f"TSP Name: {tsp}", "TSP: 007912959", "",
            f"Effective Gas Day: {day:%m/%d/%Y}", "Effective Time: 02:00 PM CCT", "", "Cap Type: 1",
            f"Cycle Indicator Description: {cycle}", f"Meas Basis Desc: {unit}",
            f"Posting Date: {posted:%m/%d/%Y}", f"Posting Time: {posted:%I:%M %p} CCT", "", "IT Indicator", "",
            COLS, ""]
    body = [f'"{n}","{loc}","{purp}","{qti}","x","{st}","Y","1","1","1","0","0","0","0","N","{round(v * D * scale)}",'
            f'"{qti[0]}","","{c}",' for n, loc, purp, qti, st, v, c in rows]
    return "\r\n".join(head + body + ["", "* Associated Transfer Point has been assigned to this Interconnect Point"])


def page(day, cycles, link=""):
    spans = "".join(f"<span class='PSEDITBOX_DISPONLY' id='NV_OPER_AVAIL_T_NF_SM_CYCL_PROC_CD${i}' >{c}</span>"
                    for i, c in enumerate(cycles))
    js = f"<script>window.open('{link}','','');</script>" if link else ""
    return (f"<html><head>{js}</head><body><form name='win0' method='post'>"
            "<input type='hidden' name='ICType' id='ICType' value='Panel' />"
            "<input type='hidden' name='ICSID' id='ICSID' value='s1d+x=' />"
            "<input type='hidden' name='ICAction' id='ICAction' value='None' />"
            f"<input type='text' name='NF_WRK_NF_FLOW_DT_FROM' id='NF_WRK_NF_FLOW_DT_FROM' tabindex='15' "
            f"value=\"{day:%m/%d/%Y}\"  class='PSEDITBOX' />{spans}</form></body></html>")


class Board:
    """One fake PeopleSoft site: today's cycles (newest first), yesterday's, the CSV rows per pipe."""
    CYCLES = {date(2026, 11, 13): ["Intraday 1", "Evening", "Timely", "Preliminary"],
              date(2026, 11, 12): ["Intraday 3", "Intraday 2", "Intraday 1", "Evening", "Timely", "Preliminary"]}

    def __init__(self, nfg=NFG, emp=EMP, unit="MMBTU", default=date(2026, 11, 13), csv=None):
        self.rows = {"natfuel": nfg, "empire": emp}
        self.unit, self.default, self.csv = unit, default, csv
        self.shown: dict[str, date] = {}
        self.seen: list[tuple[str, str, str]] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        u = str(r.url)
        site = "natfuel" if "/psc/sbsprd/" in u else "empire"
        if r.url.path.endswith(".csv"):
            self.seen.append((site, "GET", "file"))
            day = self.shown[site]
            cyc = self.CYCLES[day][0]
            text = self.csv or oa_csv(site, day, cyc, datetime(day.year, day.month, day.day, 13, 0),
                                      self.rows[site], self.unit, 1.0 if day == date(2026, 11, 13) else 0.5)
            return httpx.Response(200, text=text, headers={"content-type": "text/csv"})
        if r.method == "GET":
            self.seen.append((site, "GET", "page"))
            assert "SRVPSSBSPR16-86-PORTAL-PSJSESSIONID" not in r.headers.get("cookie", "")  # a fresh session
            self.shown[site] = self.default
            return httpx.Response(200, text=page(self.default, self.CYCLES.get(self.default, [])),
                                  headers={"set-cookie": "SRVPSSBSPR16-86-PORTAL-PSJSESSIONID=abc; path=/"})
        form = dict(httpx.QueryParams(r.content.decode()))
        assert form["ICSID"] == "s1d+x="
        self.seen.append((site, "POST", form["ICAction"]))
        if form["ICAction"] == "NF_WRK_NF_SELECTED_SW":
            self.shown[site] = datetime.strptime(form["NF_WRK_NF_FLOW_DT_FROM"], "%m/%d/%Y").date()
        day = self.shown[site]
        link = ""
        if form["ICAction"] == "NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0":
            ps = "sbsprd" if site == "natfuel" else "ebsprd"
            link = f"https://sbsprd2.natfuel.com/psc/{ps}/view/%7bV2%7dTok/OP_CAP_{day:%m%d%Y}_cycle_1.csv"
        return httpx.Response(200, text=page(day, self.CYCLES.get(day, []), link))


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(gas_ebb_common, "PAUSE", 0.0)


def run(board, now=NOW, spec=None):
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(board)), now=now)
    return build(copy.deepcopy(spec or SPEC)).run(ctx)


def vals(res, day):
    return {o.series_id: round(o.value, 6) for o in res.obs if o.ts == day}


def test_requests_sums_named_and_final():
    b = Board()
    res = run(b)
    dl, rt = ("POST", "NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0"), ("POST", "NF_WRK_NF_SELECTED_SW")
    per = [("GET", "page"), dl, ("GET", "file"), rt, dl, ("GET", "file")]   # latest, then yesterday's final
    assert b.seen == [("natfuel", *s) for s in per] + [("empire", *s) for s in per]
    v = vals(res, datetime(2026, 11, 13))
    # National Fuel: NY points only; aggregates (APPZN3, storage) and the city-gate subset are left out of the sums
    assert v["ebb.natfuel.nyc.ldc"] == 150                           # NFGDC-NY, not + its 70 subset
    assert v["ebb.natfuel.nyc.int_out"] == 310 + 17                  # Niagara rides Tennessee's spur: int_out
    assert "ebb.natfuel.nyc.export_canada" not in v
    assert v["ebb.natfuel.nyc.int_in"] == 60 + 40 + 35
    assert v["ebb.natfuel.nyc.production"] == 2                      # NSP meter + PL0 pool, typed by id
    assert v["ebb.natfuel.nyc.power"] == 3
    assert v["ebb.natfuel.nyc.net"] == pytest.approx(150 + 327 + 3 - 135 - 2)
    assert v["ebb.natfuel.niagara.deliveries"] == 310
    assert v["ebb.natfuel.storage.withdrawals"] == 43 and v["ebb.natfuel.storage.injections"] == 1
    # Empire
    assert v["ebb.empire.chippawa.receipts"] == 40
    assert v["ebb.empire.nyc.import_canada"] == 40 and v["ebb.empire.nyc.export_canada"] == 230
    assert v["ebb.empire.border.nyc_sont"] == 190
    assert v["ebb.empire.border.pa_nyc"] == 500
    assert v["ebb.empire.connector.flow"] == 620
    assert v["ebb.empire.nyc.power"] == 165 and v["ebb.empire.nyc.ldc"] == 21
    assert v["ebb.empire.nyc.int_out"] == 60 + 155 and v["ebb.empire.nyc.int_in"] == 110
    assert v["ebb.empire.nyc.production"] == 1                       # renewable gas (RNG)
    assert v["ebb.empire.nyc.net"] == pytest.approx(230 + 215 + 21 + 165 - 40 - 110 - 1)
    y = vals(res, datetime(2026, 11, 12))
    assert y["ebb.empire.border.pa_nyc"] == 250                      # the final (half volumes in the fake)
    o = next(o for o in res.obs if o.series_id == "ebb.empire.nyc.net" and o.ts == datetime(2026, 11, 13))
    assert o.as_of == datetime(2026, 11, 13, 19, 0)                  # 13:00 CST posting time
    assert {m.day for m in res.meta} == {"gas"} and {m.unit for m in res.meta} == {"MMcf/d"}
    # cheap check: same cycle two hours later -> one page per board, no download; the final is done
    b2 = Board()
    run(b2, now=NOW.replace(hour=22))
    assert b2.seen == [("natfuel", "GET", "page"), ("empire", "GET", "page")]


def test_before_nine_retrieves_the_gas_day():
    b = Board(default=date(2026, 11, 13))
    run(b, now=datetime(2026, 11, 13, 13, 0))                        # 07:00 CST: gas day 11-12
    assert b.seen[:4] == [("natfuel", "GET", "page"), ("natfuel", "POST", "NF_WRK_NF_SELECTED_SW"),
                          ("natfuel", "POST", "NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0"), ("natfuel", "GET", "file")]
    # then the final due at 07:00 is 11-11's (none in the fake: a Retrieve, no download), then Empire the same way
    assert b.seen[4] == ("natfuel", "POST", "NF_WRK_NF_SELECTED_SW") and len(b.seen) == 10


def test_untyped_new_york_point_warns():
    res = run(Board(nfg=NFG + [("XYZ - New Plant", "NEWPLANT", "Delivery Location", "DPQ", "NY", 9, "")]))
    assert any("New Plant" in w and "int_in / int_out" in w for w in res.warnings)
    assert vals(res, datetime(2026, 11, 13))["ebb.natfuel.nyc.int_out"] == 310 + 17 + 9


def test_no_state_point_with_gas_fails_closed():
    with pytest.raises(FormatDrift, match="no state"):
        run(Board(emp=EMP + [("Mystery Meter", "MYST", "Delivery Location", "DPQ", "", 12, "")]))


def test_hand_mapped_point_gone_fails_closed():
    class Gone(Board):
        def __call__(self, r):
            resp = super().__call__(r)
            if r.url.path.endswith(".csv"):
                text = "\r\n".join(ln for ln in resp.text.split("\r\n") if '"012002010"' not in ln)
                return httpx.Response(200, text=text, headers={"content-type": "text/csv"})
            return resp
    with pytest.raises(FormatDrift, match="012002010"):
        run(Gone())


@pytest.mark.parametrize("kw, match", [({"unit": "DTH"}, "MMBTU"),
                                       ({"csv": "Operationally Available Capacity\r\n\"Loc\",\"Qty\"\r\n"}, "header"),
                                       ({"csv": COLS.replace("Total Scheduled Quantity", "TSQ")}, "header")])
def test_format_drift(kw, match):
    with pytest.raises(FormatDrift, match=match):
        run(Board(**kw))
