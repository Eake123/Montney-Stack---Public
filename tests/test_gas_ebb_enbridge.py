"""Enbridge rtba EBB (gas_ebb_enbridge): the request sequence and its cheap check, region × category sums, overrides,
named points and FormatDrift. Payload shapes copy the live ones (2026-11-13); the volumes are made up (multiples of
1,037 Dth so MMcf/d come out round)."""

from __future__ import annotations

import copy
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_common
from montney.connectors import gas_ebb_enbridge as enb
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 17, 0)         # 11:00 CST: the 11-12 final is due
D = 1037.0
OA_HEAD = ("Cycle_Desc,Post_Date,Eff_Gas_Day,Cap_Type_Desc,Post_Time,Eff_Time,Loc,Loc_Name,Loc_Zn,Flow_Ind_Desc,"
           "Loc_Purp_Desc,Loc_QTI_Desc,Meas_Basis_Desc,IT,All_Qty_Avail,Total_Design_Capacity,Operating_Capacity,"
           "Total_Scheduled_Quantity,Operationally_Available_Capacity,TSP_Name,TSP")
LOC_HEAD = "Loc,Loc Name,Loc St Abbrev,Loc Cnty,Loc Zone,Dir Flo,Loc Stat Ind,Loc Type Ind,Up/Dn Name"
# (loc, name, flow, MMcf/d, state, type, counterparty)
POINTS = [
    ("30014", "BAILEYVILLE BORDER M&R (OUT)", "Delivery", 200, "ME", "INT", "M&NE LIMITED"),
    ("30012", "BAILYVILLE BORDER M&R (IN)", "Receipt", 0, "ME", "INT", "M&NE LIMITED"),
    ("30029", "MARITIMES/BRUNSWICK PIPELINE METER", "Receipt", 10, "ME", "INT", "EMERA BRUNSWICK"),
    ("35006", "PNGTS (REC.30006/DEL.30026)", "Receipt", 300, "ME", "INT", "PNGTS"),
    ("30018", "BANGOR GAS - VEAZIE", "Delivery", 50, "ME", "LDC", "BANGOR GAS COMPANY"),
    ("30009", "CASCO BAY", "Delivery", 100, "ME", "END", "CASCO BAY ENERGY COMPANY"),
    ("30033", "Woodland Pulp", "Delivery", 20, "ME", "END", "WOODLAND PULP LLC"),
    ("30050", "Some Mill Power Station", "Delivery", 5, "ME", "END", "MILL CO"),
    ("38012", "MNCA - BAILEYVILLE", "Receipt", 7, "ME", "STA", ""),          # internal: dropped
    ("30025", "Beverly-Salem Essex Co. MA", "Delivery", 30, "MA", "INT", "ALGONQUIN"),
    ("30088", "SOMEWHERE, PA", "Delivery", 40, "PA", "LDC", "X"),            # outside every region: dropped
    ("30090", "STORAGE IN, MA", "Storage Injection", 3, "MA", "STR", ""),
]
SPEC = {"id": "ebb_test", "type": "python", "module": "montney.connectors.gas_ebb_enbridge", "class": "EbbEnbridge",
        "params": {"boards": [{
            "pipe": "mne", "label": "M&NE", "bu": "mn", "points_id": "MNUS", "regions": ["upne"],
            "overrides": {"30012": {"category": "import_canada"}, "30014": {"category": "export_canada"},
                          "30029": {"category": "import_canada"}, "30009": {"category": "power"}},
            "named": {"ebb.mne.baileyville.net_exports": {"label": "net exports", "plus": ["30014:D"],
                                                         "minus": ["30012:R", "30029:R"]}}}]}}


def oa_csv(day="11-13-2026", cycle="LATE_2026-11-13_0900", post=("11-13-2026", "09:00"), points=POINTS, scale=1.0,
           head=OA_HEAD, unit="MMBtu"):
    out = [head]
    for loc, name, flow, v, *_ in points:
        q = f'"{round(v * D * scale):,}"'
        out.append(f'{cycle},{post[0]},{day},Operational Capacity,{post[1]},09:00,{loc},"{name}",,{flow},x,x,{unit},'
                   f'N,Y,"999,999","999,999",{q},"1",M&N US,081787256')
    return "\r\n".join(out)


def loc_csv(points=POINTS):
    return "\r\n".join([LOC_HEAD] + [f'"\t{p[0]}","{p[1]}",{p[4]},X,N/A,B,A,{p[5]},"{p[6]}"' for p in points])


def page(date="2026-11-13", options=("TIMELY_2026-11-12_1605", "LATE_2026-11-13_0900"), selected=None):
    selected = selected or options[-1]
    opts = "".join(f'<option {"selected=" + chr(34) + "selected" + chr(34) if o == selected else ""} value="{o}">{o}'
                   f'</option>' for o in options)
    return (f'<form><input type="hidden" name="__VIEWSTATE" value="vs" /><input type="hidden" name="__EVENTVALIDATION" '
            f'value="ev" /><input name="{enb.DATE}" type="text" value="{date}" /><select name="{enb.SEL}">{opts}</select>'
            f'<input type="submit" name="go" value="Go" /></form>')


class Board:
    """A fake rtba + point-data host: today's page and CSVs by cycle; the date post-back serves any day, whose last
    cycle (INTRDY_…) downloads at half the volumes."""

    def __init__(self, points=POINTS):
        self.seen: list[str] = []
        self.points = points
        self.today_page = page()
        self.csv = {"LATE_2026-11-13_0900": oa_csv(points=points)}
        self.locs = loc_csv(points)
        self.prev_day = ""

    def __call__(self, r: httpx.Request) -> httpx.Response:
        u = str(r.url)
        if "point-data" in u:
            self.seen.append("GET locations")
            return httpx.Response(200, text=self.locs, headers={"content-type": "text/csv"})
        if r.method == "GET":
            self.seen.append("GET page")
            return httpx.Response(200, text=self.today_page)
        form = dict(httpx.QueryParams(r.content.decode()))
        if form["__EVENTTARGET"] == enb.DATE:
            self.prev_day = form[enb.DATE]
            self.seen.append(f"POST date {self.prev_day}")
            return httpx.Response(200, text=page(self.prev_day, ("TIMELY_X", "INTRDY_2026-11-13_0900")))
        assert form["__EVENTTARGET"] == enb.DOWN
        cyc = form[enb.SEL]
        self.seen.append(f"POST download {cyc}")
        if cyc.startswith("INTRDY"):
            y, m, d = self.prev_day.split("-")
            body = oa_csv(f"{m}-{d}-{y}", cyc, points=self.points, scale=0.5)
        else:
            body = self.csv[cyc]
        return httpx.Response(200, text=body, headers={
            "content-type": "text/plain", "content-disposition": f"attachment;filename=MN_OA_{cyc}.csv"})


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(gas_ebb_common, "PAUSE", 0.0)


def run(spec=SPEC, board=None, now=NOW):
    board = board or Board()
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(board)), now=now)
    return build(copy.deepcopy(spec)).run(ctx), board, ctx


def vals(res, day):
    return {o.series_id: round(o.value, 6) for o in res.obs if o.ts == day}


def test_sequence_cheap_check_and_final():
    res, board, ctx = run()
    assert board.seen == ["GET locations", "GET page", "POST download LATE_2026-11-13_0900", "POST date 2026-11-12",
                          "POST download INTRDY_2026-11-13_0900"]
    assert ctx.n_requests == 5
    v = vals(res, datetime(2026, 11, 13))
    assert v == {"ebb.mne.upne.export_canada": 200, "ebb.mne.upne.import_canada": 10, "ebb.mne.upne.int_in": 300,
                 "ebb.mne.upne.ldc": 50, "ebb.mne.upne.power": 105, "ebb.mne.upne.industrial": 20,
                 "ebb.mne.upne.int_out": 30, "ebb.mne.upne.storage_inj": 3,
                 "ebb.mne.upne.net": 200 + 50 + 105 + 20 + 30 + 3 - 10 - 300,
                 "ebb.mne.baileyville.net_exports": 190}
    assert vals(res, datetime(2026, 11, 12))["ebb.mne.upne.net"] == pytest.approx(98 / 2)    # the final, at half
    o = next(o for o in res.obs if o.ts == datetime(2026, 11, 13))
    assert o.as_of == datetime(2026, 11, 13, 15, 0)              # 09:00 CST -> UTC
    meta = {m.series_id: m for m in res.meta}
    assert meta["ebb.mne.upne.ldc"].unit == "MMcf/d" and meta["ebb.mne.upne.ldc"].day == "gas"
    assert "scheduled" in meta["ebb.mne.baileyville.net_exports"].label
    # the next run, same cycle, final done, locations cached: one GET, nothing stored
    res2, board2, ctx2 = run(board=Board(), now=NOW.replace(hour=19))
    assert board2.seen == ["GET page"] and res2.obs == []
    # a new cycle: page + download
    b3 = Board()
    b3.today_page = page(options=("TIMELY_2026-11-12_1605", "LATE_2026-11-13_0900", "LATE_2026-11-13_1000"))
    b3.csv["LATE_2026-11-13_1000"] = oa_csv(cycle="LATE_2026-11-13_1000", post=("11-13-2026", "10:00"), scale=2)
    res3, _, _ = run(board=b3, now=NOW.replace(hour=20))
    assert b3.seen == ["GET page", "POST download LATE_2026-11-13_1000"]
    assert vals(res3, datetime(2026, 11, 13))["ebb.mne.upne.ldc"] == pytest.approx(100)


def test_no_final_before_ten_central():
    _, board, _ = run(now=datetime(2026, 11, 13, 15, 30))       # 09:30 CST: 11-12 isn't final yet, 11-11 is due
    assert board.seen[-2] == "POST date 2026-11-11"


def test_real_spec_overrides():
    """The shipped spec: Tetco's Con Ed Manhattan meter (coded NJ) counts in nyc; M&NE's Baileyville is an export."""
    spec = load_specs(include_disabled=True)["gas_ebb_enbridge"]
    boards = {b["pipe"]: b for b in spec["params"]["boards"]}
    assert boards["tetco"]["overrides"]["73722"]["region"] == "nyc"
    tet = {**copy.deepcopy(spec), "params": {"boards": [boards["tetco"]]}}
    pts = [("73722", "ConEd-Manhattan Delivery", "Delivery", 300, "NJ", "LDC", "CON ED"),
           ("74058", "National Grid Metro NY, Goethals", "Delivery", 280, "NY", "LDC", "BROOKLYN UNION"),
           ("74059", "BUG CO NAT- Brooklyn Union", "Delivery", 1, "", "", ""),
           ("75131", "TETCO - ACCIDENT FIELD, M.M., MD", "Storage Withdrawal", 50, "", "", ""),
           ("79970", "HEAD OF MANHATTAN EXTENSION - MAINLINE", "Delivery", 190, "NY", "STA", ""),
           ("78214", "RAMAPO", "Receipt", 45, "NY", "INT", "MILLENNIUM"),
           ("70001", "SOMEWHERE NJ", "Delivery", 999, "NJ", "LDC", "X")]
    b = Board(pts)
    b.locs = loc_csv([p for p in pts if p[4]])
    res, _, _ = run(spec=tet, board=b)
    v = vals(res, datetime(2026, 11, 13))
    assert v["ebb.tetco.nyc.ldc"] == pytest.approx(300 + 280 + 1)     # Con Ed Manhattan + Goethals + BUG (override)
    assert v["ebb.tetco.nyc.net"] == pytest.approx(581 - 45)          # the STA row is internal; NJ is outside nyc
    assert not any(".tx." in s for s in v)                            # tetco ships nyc only for now


def test_overridden_point_gone_fails_closed():
    b = Board([p for p in POINTS if p[0] != "30009"])
    b.locs = loc_csv()
    with pytest.raises(FormatDrift, match="overridden point.*30009"):
        run(board=b)


def test_named_point_gone_fails_closed():
    spec = copy.deepcopy(SPEC)
    spec["params"]["boards"][0]["named"]["ebb.mne.baileyville.net_exports"]["minus"].append("99999:R")
    with pytest.raises(FormatDrift, match="named point.*99999:R"):
        run(spec=spec)


def test_header_and_unit_drift():
    b = Board()
    b.csv["LATE_2026-11-13_0900"] = oa_csv(head=OA_HEAD.replace("Total_Scheduled_Quantity", "TSQ"))
    with pytest.raises(FormatDrift, match="header changed"):
        run(board=b)
    b = Board()
    b.csv["LATE_2026-11-13_0900"] = oa_csv(unit="Dth")
    with pytest.raises(FormatDrift, match="not MMBtu"):
        run(board=b)
    b = Board()
    b.today_page = "<html>maintenance</html>"
    with pytest.raises(FormatDrift, match="viewstate"):
        run(board=b)


def test_location_file_drift_and_cache_fallback():
    b = Board()
    b.locs = "Loc,Name\r\n1,x"
    with pytest.raises(FormatDrift, match="point-data: header changed"):
        run(board=b)
    run()                                                        # caches the location file
    b = Board()
    b.locs = "Loc,Name\r\n1,x"
    res, _, _ = run(board=b, now=datetime(2026, 11, 21, 17, 0))  # 8 days on: refresh fails, the cache serves
    assert any("using the cached one" in w for w in res.warnings)
