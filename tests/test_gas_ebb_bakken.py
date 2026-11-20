"""Bakken EBBs (gas_ebb_bakken.py): WBI's ASP.NET postback download and Alliance US's JSON. Layouts copy the sites'
(WBI tab-separated OA file + NAESB location CSV; Alliance's operationallyavailablecapacities JSON); points and volumes
are made up (1,037 Dth = 1 MMcf)."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_et as ebb
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 18, 0)          # 12:00 CT: today + yesterday


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(ebb, "PACE", 0)


def run(sid, handler, now=NOW):
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)
    return build(load_specs(include_disabled=True)[sid]).run(ctx), ctx


# ------------------------------------------------------------------ WBI
FORM = ('<form><input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="vs+1/=" />'
        '<input type="hidden" name="__EVENTVALIDATION" value="ev" />'
        '<input name="calPrvGasDay" type="text" value="11/13/2026" readonly="readonly" id="calPrvGasDay" />'
        '<select name="drpLocSelection"><option selected="selected" value="--&#32;Choose&#32;One&#32;--">x</option>'
        '<option value="Receipt/Delivery">r</option></select>'
        '<input type="submit" name="btnDownload" value="Download" id="btnDownload" /></form>')
WBI_HEAD = ["TSP", "Post Date", "Post Time", "Loc", "Loc Name", "Loc Purp", "Loc/QTI", "Flow Ind", "All Qty Avail", "DC",
            "OPC", "TSQ", "OAC", "IT", "Meas Basis", "Press Base Ind", "Eff Gas Day", "Eff Time", "Cycle", "Qty Reason"]
# (loc, name, flow, Dth, state, type, counterparty)
WBI = [("01335", "Demicks Lake Plant", "R", 1_037_000, "ND", "PLT", "ONEOK Rockies Midstream L.L.C."),
       ("00990", "Tioga Plant", "R", 103_700, "ND", "PLT", "Hess Tioga Gas Plant LLC"),
       ("01316", "NBPL-Spring Creek (D)", "D", 829_600, "ND", "INT", "Northern Border Pipeline Co."),
       ("03820", "MDU-Billings Border", "D", 51_850, "MT", "LDC", "Montana-Dakota Utilities Co."),
       ("01396", "BEPC-Pioneer Station", "D", 20_740, "ND", "END", "Basin Electric Power Cooperative"),
       ("01775", "GRE-Spiritwood", "D", 10_370, "ND", "END", "Great River Energy"),
       ("05800", "WBI-Agg Storage (Inject)", "SI", 31_110, "MU", "STR", ""),
       ("05801", "WBI-Agg Storage (Withdraw)", "SW", 124_440, "MU", "STR", ""),
       ("01951", "VGT-Felton (R)", "R", 20_740, "MN", "INT", "Viking Gas Transmission Company"),
       ("05225", "BHGG-Wild Horse Creek (D)", "D", 82_960, "WY", "INT", "Big Horn Gas Gathering"),
       ("05380", "MDU-Rapid-Black Hawk Agg Pt", "D", 10_370, "SD", "LDC", "Montana-Dakota Utilities Co."),
       ("00005", "Imbalance -Prior Month", "R", 494, "MU", "VIR", "")]
WBI_LOC_HEAD = ["Loc", "Loc Name", "Dir Flo", "Loc Type Ind", "Loc Stat Ind", "Eff Date", "Inact Date", "Loc St Abbrev",
                "Loc Cnty", "Loc Zone", "TSP", "TSP Name", "Up/Dn Name"]


def wbi_file(day="20261113", rows=WBI, head=WBI_HEAD):
    lines = ["\t".join(head)]
    for r in rows:
        v = {"TSP": "131537896", "Post Date": "20261112", "Post Time": "20:39", "Loc": r[0], "Loc Name": r[1],
             "Flow Ind": r[2], "TSQ": str(r[3]), "Eff Gas Day": day, "Cycle": "EVE"}
        lines.append("\t".join(v.get(h, "") for h in head))
    return "\n".join(lines) + "\n"


def wbi_locs():
    lines = [",".join(WBI_LOC_HEAD)]
    for r in WBI:
        v = {"Loc": r[0], "Loc Name": r[1], "Loc Type Ind": r[5], "Loc St Abbrev": r[4], "Up/Dn Name": f'"{r[6]}"'}
        lines.append(",".join(v.get(h, "") for h in WBI_LOC_HEAD))
    return "\n".join(lines) + "\n"


def wbi_handler(posts, oa=wbi_file, form=FORM):
    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, text=form)
        f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        posts.append((req.url.path, f))
        if "LocationsData" in req.url.path:
            return httpx.Response(200, text=wbi_locs())
        day = datetime.strptime(f["calPrvGasDay"], "%m/%d/%Y").strftime("%Y%m%d")
        return httpx.Response(200, text=oa(day))
    return h


def test_wbi_postback_sums_and_borders():
    posts = []
    res, ctx = run("gas_ebb_wbi", wbi_handler(posts))
    v = {(o.series_id, o.ts): round(o.value, 3) for o in res.obs}
    d = datetime(2026, 11, 13)
    assert v[("ebb.wbi.bakken.production", d)] == 1100                # plants, Tioga included
    assert v[("ebb.wbi.bakken.to_nbpl", d)] == 800                    # internal transfer, its own series
    assert v[("ebb.wbi.bakken.int_out", d)] == 800
    assert v[("ebb.wbi.bakken.ldc", d)] == 50                         # SD's LDC isn't bakken
    assert v[("ebb.wbi.bakken.power", d)] == 30                       # Basin Electric by name + Spiritwood override
    assert v[("ebb.wbi.bakken.storage_inj", d)] == 30 and v[("ebb.wbi.bakken.storage_wd", d)] == 120
    assert v[("ebb.wbi.border.mn_nd", d)] == 20
    assert v[("ebb.wbi.border.bakken_wy", d)] == 80 and v[("ebb.wbi.border.bakken_sd", d)] == 10
    assert {o.as_of for o in res.obs} == {datetime(2026, 11, 13, 2, 39)}    # 20:39 CT on 11-12 -> UTC
    assert {o.ts for o in res.obs} == {d, datetime(2026, 11, 12)}
    oa_posts = [f for p, f in posts if "Operational" in p]
    assert [f["calPrvGasDay"] for f in oa_posts] == ["11/13/2026", "11/12/2026"]
    assert all(f["__VIEWSTATE"] == "vs+1/=" and f["btnDownload"] == "Download" for f in oa_posts)
    loc_post = [f for p, f in posts if "LocationsData" in p][0]
    assert loc_post["drpLocSelection"] == "Receipt/Delivery"
    assert ctx.n_requests == 1 + 2 + 2                               # form + today + yesterday + locations (GET+POST)


def test_wbi_drift():
    with pytest.raises(FormatDrift, match="header lacks"):
        run("gas_ebb_wbi", wbi_handler([], oa=lambda day: wbi_file(day, head=[h for h in WBI_HEAD if h != "TSQ"])))
    with pytest.raises(FormatDrift, match="VIEWSTATE"):
        run("gas_ebb_wbi", wbi_handler([], form="<form></form>"))
    with pytest.raises(FormatDrift, match="05801"):
        run("gas_ebb_wbi", wbi_handler([], oa=lambda day: wbi_file(day, rows=[r for r in WBI if r[0] != "05801"])))


def test_wbi_row_with_gas_and_no_state_fails_the_run():
    """The no-state guard (gas_ebb_common.require_located, Reviewer on #478): a delivery with gas that the location file
    (read this run, so no early re-read) lacks would drop out of every total and the state borders."""
    rows = WBI + [("09999", "New Meter", "D", 20_740, "", "", "")]
    with pytest.raises(FormatDrift, match=r"WBI 2026-11-13: 1 point\(s\) with scheduled gas but no state.*\['09999'\]"):
        run("gas_ebb_wbi", wbi_handler([], oa=lambda day: wbi_file(day, rows=rows)))


# ------------------------------------------------------------------ Alliance US
def alliance(day, rows):
    return {"data": [{"locNm": n, "postDt": "11/12/2026 9:15:33 PM", "gasDay": day, "cycleDescr": "Evening",
                      "locId": i, "flowIndCd": f, "totalSchdQty": q, "measBasisDescr": "MMBtu"} for i, n, f, q in rows],
            "totalCount": len(rows), "page": 0, "pageSize": 500}


AP = [("UX", "BORDER USA", "R", 2_074_000.0), ("ND08", "BANTRY", "R", 103_700.0), ("ND11", "TIOGA", "R", 103_700.0),
      ("ND07", "HANKINSON", "D", 10_370.0), ("MN18", "STEVENS", "R", 1_037.0), ("IL05", "ALLIANCE/ANR", "D", 1_000_000.0)]


def ap_handler(bodies, rows=AP):
    def h(req: httpx.Request) -> httpx.Response:
        b = json.loads(req.content)
        bodies.append(b)
        day = datetime.strptime(b["startDate"][:10], "%Y-%m-%d").strftime("%m/%d/%Y")
        return httpx.Response(200, json=alliance(day, rows))
    return h


def test_alliance_us_border_production_and_exit():
    bodies = []
    res, ctx = run("gas_ebb_alliance_us", ap_handler(bodies))
    v = {(o.series_id, o.ts): round(o.value, 3) for o in res.obs}
    d = datetime(2026, 11, 13)
    assert v[("ebb.alliance.border.canada_nd", d)] == v[("ebb.alliance.bakken.import_canada", d)] == 2000
    assert v[("ebb.alliance.bakken.production", d)] == 200                  # Tioga + Bantry
    assert v[("ebb.alliance.bakken.industrial", d)] == 10
    assert v[("ebb.alliance.border.nd_mn", d)] == 2200 - 10
    assert v[("ebb.alliance.border.mn_ia", d)] == 2190 + 1
    assert v[("ebb.alliance.umw.int_in", d)] == 1
    assert {o.as_of for o in res.obs} == {datetime(2026, 11, 13, 3, 15, 33)}
    assert [b["startDate"] for b in bodies] == ["2026-11-13T00:00:00", "2026-11-12T00:00:00"]
    assert ctx.n_requests == 2
    res2, ctx2 = run("gas_ebb_alliance_us", ap_handler([]), now=datetime(2026, 11, 13, 20, 0))
    assert ctx2.n_requests == 1                                              # yesterday already read today


def test_alliance_us_drift():
    with pytest.raises(FormatDrift, match="UX"):
        run("gas_ebb_alliance_us", ap_handler([], rows=[r for r in AP if r[0] != "UX"]))

    def bad(req):
        return httpx.Response(200, json={"data": [{"locId": "UX", "locNm": "BORDER USA"}]})
    with pytest.raises(FormatDrift, match="lacks"):
        run("gas_ebb_alliance_us", bad)
