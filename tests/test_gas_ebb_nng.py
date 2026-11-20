"""Northern Natural Gas EBB (gas_ebb_nng): the cycle listing as cheap check, cycle choice (Non-Grid A.M. and Final A.M.
never used), zone totals and allocation groups as named series, point categories with the shipped overrides, no `net`,
the no-state guard and FormatDrift. Payload shapes copy the live ones (2026-11-13); volumes are made up (multiples of
1,037 Dth)."""

from __future__ import annotations

import copy
import json
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, gas_ebb_common, load_specs
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 17, 0)          # 11:00 CST
D = 1037.0
SPECS = load_specs(include_disabled=True)
BOARD = SPECS["gas_ebb_nng"]["params"]["boards"][0]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(gas_ebb_common, "PAUSE", 0.0)


def run(handler, now=NOW, spec=None):
    seen = []

    def h(r):
        seen.append(r)
        return handler(r)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=now)
    return build(copy.deepcopy(spec or SPECS["gas_ebb_nng"])).run(ctx), seen


def vals(res, day):
    return {o.series_id: round(o.value, 6) for o in res.obs if o.ts == day}


# ------------------------------------------------------------------ payloads
LISTING = {"data": {"maxDate": "2026-11-13", "maxCycle": 13, "cycleList": [], "columns": [], "data": [
    {"gasDay": "11/13/2026", "timely": 10, "evening": 11, "nonGridAM": 12, "intraday1": 13},
    {"gasDay": "11/12/2026", "timely": 10, "evening": 11, "nonGridAM": 12, "intraday1": 13, "intraday2": 14,
     "intraday3": 15, "finalAM": 16}]}}

# points: loc -> (name, state, NAESB type, QTI, MMcf/d)
POINTS = {
    "388": ("GRLKS/NNG CARLTON", "MN", "INT", "RPQ", 380),
    "78626": ("NSP CEDAR YARD POWER PLANTS", "MN", "END", "DPQ", 170),
    "2788": ("ST. PAUL MN #1Q", "MN", "LDC", "DPQ", 85),
    "2865": ("DULUTH MN #1", "MN", "LDC", "DPQ", 0),                      # zone member: 0 at the point
    "4007": ("RICHLAND CNTR WI #1", "WI", "LDC", "DPQ", 4),
    "3233": ("ANKENY IA #1", "IA", "LDC", "DPQ", 9),                      # outside umw
}
OVERRIDE_VALS = {"79396": 65, "79200": 100, "2249": 22, "71439": 8}      # power, industrial, industrial, OTH -> LDC
OVERRIDE_TYPES = {"79200": "VIR", "71439": "OTH", "79396": "LDC"}
GROUPS = {"453": ("ZONE EF - ALLOCATION ZONE POINTS", 1600), "456": ("ZONE D - ALLOCATION ZONE POINTS", 340),
          "454": ("ZONE ABC - ALLOCATION ZONE POINTS", 1250), "1021": ("VENTURA NORTH ALLOCATION GROUP", 1060),
          "809": ("WELCOME NORTH ALLOCATION GROUP", 70), "750": ("FARMINGTON NORTH ALLOCATION GROUP", 410),
          "811": ("CARLTON SOUTH RECEIPT ALLOCATION GROUP", 210), "1020": ("CARLTON EAST ALLOCATION GROUP", 70),
          "1030": ("WAKEFIELD EAST ALLOCATION GROUP", 69), "463": ("GALENA EAST ALLOCATION GROUP", 102),
          "488": ("BELLEVILLE EAST ALLOCATION GROUP", 39)}
OA_HEAD = ("TSP,TSP Name,Loc,Loc Zn,Loc Name,Flow Ind,Loc/QTI,Loc Purp Desc,All Qty Avail,Design Capacity,"
           "Operating Capacity,Total Scheduled Quantity,Operationally Available Capacity,Cycle Desc,"
           "Effective Gas Day/Time,Post Date/Time,Meas Basis,IT,Quantity Not Available Reason,Date Requested")


def all_points():
    pts = dict(POINTS)
    for loc in BOARD["overrides"]:
        loc = str(loc).split(":")[0]
        if loc not in pts:
            pts[loc] = (f"PT {loc}", "MI" if loc in ("79381", "79385", "79382", "79386", "2250") else
                        "WI" if loc.startswith("71") or loc == "78170" else "MN",
                        OVERRIDE_TYPES.get(loc, "LDC"), "DPQ", OVERRIDE_VALS.get(loc, 0))
    return pts


def loc_csv(pts=None, head="TSP,TSP Name,TSP FERC CID,Loc,Loc Name,Loc Loctr ID,Loc Loctr,Loc Cnty,Loc St Abbrev,"
                           "Loc Zone,Loc Type Ind,Dir Flo,Loc Stat Ind,Eff Date,Inact Date,Up/Dn Ind,Up/Dn Name"):
    out = [head]
    for loc, (nm, st, t, _, _) in (pts or all_points()).items():
        out.append(f'784158214,NNG,C000060,{loc},"{nm}",SRV,x,x,{st},MID 17,{t},D,A,2005-01-01,,Y,"CP {loc}"')
    for i in range(1200):                                                 # the real file has ~2,500 points
        out.append(f"784158214,NNG,C000060,9{i:05d},FILLER {i},SRV,x,x,KS,MID 1,LDC,D,A,2005-01-01,,Y,x")
    out.append("")
    out.append("Comments:")
    out.append("Inactive status does not include abandoned points.")
    return "﻿" + "\r\n".join(out)


def oa_csv(day, cycle, posted, scale=1.0, pts=None, unit="BZ", head=OA_HEAD, extra=()):
    out = [head, f"784158214,NNG,,,<b>ZONE GROUPS</b>,,,,,,,,,{cycle},{day}T09:00,{posted},{unit},,,x"]
    for loc, (nm, v) in GROUPS.items():
        out.append(f'784158214,NNG,{loc},,"{nm}",D,DPQ,Delivery Location,N,,1,{round(v * D * scale)},0,{cycle},'
                   f'{day}T09:00,{posted},{unit},N,Summary Record,x')
    out.append(f'784158214,NNG,,,"-->BEAVER C SYSTEM",D,DPQ,Delivery Location,,,,999,,{cycle},{day}T09:00,'
               f'{posted},{unit},,,x')                                     # field-group detail row: skipped
    for loc, (nm, _, _, qti, v) in (pts or all_points()).items():
        out.append(f'784158214,NNG,{loc},MID 17,"{nm}",{"D" if qti == "DPQ" else "R"},{qti},x,N,1,1,'
                   f'{round(v * D * scale)},0,{cycle},{day}T09:00,{posted},{unit},N,,x')
    out += list(extra)
    out += ["", ('"The Total Scheduled Quantity at group locations represents the quantity scheduled in the general '
                 'direction of physical flows."')]
    return "﻿" + "\r\n".join(out)


CYCLE_NAME = {10: "Timely", 11: "Evening", 12: "Non-Grid A.M.", 13: "Intraday 1", 14: "Intraday 2",
              15: "Intraday 3", 16: "Final A.M."}


def handler(listing=LISTING, **kw):
    def h(r):
        u = str(r.url)
        if u.endswith("/capacity/oac"):
            assert r.method == "GET"
            return httpx.Response(200, json=listing)
        if u.endswith("/api/locations"):
            assert r.method == "POST" and json.loads(r.content)["download"] is True
            return httpx.Response(200, text=loc_csv(kw.get("loc_pts")))
        assert u.endswith("/capacity/oac/report") and r.method == "POST"
        b = json.loads(r.content)
        assert b["searchType"] == "All group and point locations" and b["download"] is True
        scale = 0.5 if b["gasDay"] == "2026-11-12" else 1.0
        posted = "2026-11-12T21:41:04" if b["gasDay"] == "2026-11-12" else "2026-11-13T10:42:06"
        return httpx.Response(200, text=oa_csv(b["gasDay"], CYCLE_NAME[b["cycle"]], posted, scale,
                                               **{k: v for k, v in kw.items() if k != "loc_pts"}))
    return h


def reqs(seen):
    out = []
    for r in seen:
        u = str(r.url)
        if u.endswith("/capacity/oac"):
            out.append("listing")
        elif u.endswith("/api/locations"):
            out.append("locations")
        else:
            b = json.loads(r.content)
            out.append(f"oa {b['gasDay']} {b['cycle']}")
    return out


# ------------------------------------------------------------------ tests
def test_requests_zones_groups_and_categories():
    res, seen = run(handler())
    assert reqs(seen) == ["locations", "listing", "oa 2026-11-13 13", "oa 2026-11-12 15"]   # Intraday 3, not Final A.M.
    v = vals(res, datetime(2026, 11, 13))
    assert v["ebb.nng.zone.ef"] == 1600 and v["ebb.nng.zone.d"] == 340 and v["ebb.nng.zone.abc"] == 1250
    assert v["ebb.nng.border.ia_mn"] == 1060 + 70                       # Ventura North + Welcome North
    assert v["ebb.nng.border.il_wi"] == 102 == v["ebb.nng.group.galena_east"]
    assert v["ebb.nng.umw.power"] == 170 + 65                           # Cedar Yard by name; Mankato-S by override
    assert v["ebb.nng.umw.industrial"] == 100 + 22                      # Pine Bend (VIR) and U.S. Steel (LDC)
    assert v["ebb.nng.umw.ldc"] == 85 + 4 + 8                           # + the OTH master meter; not Ankeny IA
    assert v["ebb.nng.umw.int_in"] == 380
    assert not any(k.endswith(".net") for k in v)                       # the zone deliveries aren't in the points
    assert not any(".beaver" in k for k in v)
    o = next(o for o in res.obs if o.ts == datetime(2026, 11, 13))
    assert o.as_of == datetime(2026, 11, 13, 16, 42, 6)                 # 10:42:06 CST
    y = vals(res, datetime(2026, 11, 12))
    assert y["ebb.nng.zone.ef"] == 800 and y["ebb.nng.umw.power"] == pytest.approx(117.5, abs=1e-3)
    meta = {m.series_id: m.label for m in res.meta}
    assert "Minnesota" in meta["ebb.nng.zone.ef"] and "inside the zone totals" in meta["ebb.nng.umw.power"]
    # 2 h later, same listing: the cheap check only (locations cached, final done)
    _, seen2 = run(handler(), now=NOW.replace(hour=19))
    assert reqs(seen2) == ["listing"]
    # a new cycle: listing + its CSV
    newer = copy.deepcopy(LISTING)
    newer["data"]["data"][0]["intraday2"] = 14
    _, seen3 = run(handler(newer), now=NOW.replace(hour=23))
    assert reqs(seen3) == ["listing", "oa 2026-11-13 14"]


def test_non_grid_am_is_skipped_and_final_waits_for_intraday3():
    listing = copy.deepcopy(LISTING)
    listing["data"]["data"][0] = {"gasDay": "11/13/2026", "timely": 10, "evening": 11, "nonGridAM": 12}
    listing["data"]["data"][1] = {"gasDay": "11/12/2026", "timely": 10, "evening": 11, "nonGridAM": 12,
                                  "intraday1": 13, "intraday2": 14}
    res, seen = run(handler(listing))
    assert reqs(seen) == ["locations", "listing", "oa 2026-11-13 11"]   # Evening, not Non-Grid A.M.; no final yet
    assert {o.ts for o in res.obs} == {datetime(2026, 11, 13)}


def test_point_with_gas_and_no_state_fails_closed():
    pts = all_points()
    pts["99999"] = ("NEW MN TOWN BORDER STATION", "", "", "DPQ", 12)    # posted, but not in the location file
    loc = {k: v for k, v in pts.items() if k != "99999"}
    with pytest.raises(FormatDrift, match="no state"):
        run(handler(pts=pts, loc_pts=loc))
    pts["99999"] = ("NEW MN TOWN BORDER STATION", "", "", "DPQ", 0)     # no gas: passes
    res, _ = run(handler(pts=pts, loc_pts=loc))
    assert vals(res, datetime(2026, 11, 13))["ebb.nng.zone.ef"] == 1600


def test_overridden_point_or_named_group_gone_is_drift():
    pts = {k: v for k, v in all_points().items() if k != "79200"}
    with pytest.raises(FormatDrift, match="79200"):
        run(handler(pts=pts))
    try:
        GROUPS.pop("1021")
        with pytest.raises(FormatDrift, match="1021"):
            run(handler())
    finally:
        GROUPS.update({"1021": ("VENTURA NORTH ALLOCATION GROUP", 1060)})


def test_header_unit_and_cycle_drift():
    with pytest.raises(FormatDrift, match="header changed"):
        run(handler(head=OA_HEAD.replace("Total Scheduled Quantity", "TSQ")))
    with pytest.raises(FormatDrift, match="BZ"):
        run(handler(unit="DTH"))
    bad = copy.deepcopy(LISTING)
    bad["data"]["data"][0]["intraday1"] = 17                          # cycle numbers renumbered
    with pytest.raises(FormatDrift, match="expected 13"):
        run(handler(bad))
