"""PNGTS, Iroquois, Transco and Millennium EBBs (gas_ebb_east): request sequences and cheap checks, cycle choice, region ×
category sums with the shipped specs' overrides, named points and FormatDrift. Payload shapes copy the live ones
(2026-11-13); the volumes are made up (multiples of 1,037 Dth)."""

from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_common
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 17, 0)          # 11:00 CST
D = 1037.0
SPECS = load_specs(include_disabled=True)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(gas_ebb_common, "PAUSE", 0.0)


def run(sid, handler, now=NOW, spec=None):
    seen = []

    def h(r):
        seen.append(r)
        return handler(r)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=now)
    return build(copy.deepcopy(spec or SPECS[sid])).run(ctx), seen


def vals(res, day):
    return {o.series_id: round(o.value, 6) for o in res.obs if o.ts == day}


# ------------------------------------------------------------------ PNGTS
PNGTS_LOC = ("LOC NAME,LOC,LOC ST ABBREV,LOC STATE,LOC CNTY ABBREV,LOC COUNTY,LOC ZN ABBREV,LOC ZONE,LOC TYPE IND,"
             "LOC TYPE,DIR FLO,LOC STAT IND,EFF DATE,INACT DATE,UP/DN IND,UP/DN NAME\r\n"
             "Pittsburg - East Hereford,012723,NH,x,x,COOS,x,x,INT,x,R,A,x,,N,\r\n"
             "GORHAM,230019,ME,x,x,x,x,x,LDC,x,D,A,x,,N,\r\n"
             "RUMFORD POWER,020595,ME,x,x,x,x,x,END,x,D,A,x,,N,\r\n"
             "NEWINGTON PSNH,230007,NH,x,x,x,x,x,END,x,D,A,x,,N,\r\n"
             "NEWINGTON EP,230022,NH,x,x,x,x,x,END,x,D,A,x,,N,\r\n"
             "JAY,020596,ME,x,x,x,x,x,END,x,D,A,x,,N,\r\n"
             "TGP HAVERHILL,230002,MA,x,x,x,x,x,INT,x,D,A,x,,Y,Tennessee Gas Pipeline Company\r\n"
             "WESTBROOK M&N,235006,ME,x,x,x,x,x,INT,x,D,A,x,,N,\r\n")
PNGTS_PTS = [("012723", "Pittsburg - East Hereford", "RPQ", 440), ("230019", "GORHAM", "DPQ", 60),
             ("020595", "RUMFORD POWER", "DPQ", 15), ("230007", "NEWINGTON PSNH", "DPQ", 10),
             ("230022", "NEWINGTON EP", "DPQ", 5), ("020596", "JAY", "DPQ", 7), ("230002", "TGP HAVERHILL", "DPQ", 20),
             ("235006", "WESTBROOK M&N", "DPQ", 320)]


def pngts_rows(day, cycle, post, scale=1.0, unit="BZ"):
    return [{"LocId": loc, "LocNm": nm, "GasDay": f"{day}T00:00:00", "CycleDescr": cycle, "PostDate": post,
             "TotalSchdQty": v * D * scale, "LocQtyTypeCode": q, "MeasBasisCode": unit}
            for loc, nm, q, v in PNGTS_PTS]


def pngts_handler(unit="BZ"):
    def h(r):
        if "LOCATIONDATA" in str(r.url):
            return httpx.Response(200, text=PNGTS_LOC)
        form = dict(httpx.QueryParams(r.content.decode()))
        gd = datetime.strptime(form["GasDay"], "%m/%d/%Y")
        day, before = gd.date().isoformat(), (gd - timedelta(days=1)).date().isoformat()   # posted the day before
        rows = (pngts_rows(day, "Timely", f"{before}T16:59:16", 0.5, unit)
                + pngts_rows(day, "Evening", f"{before}T21:04:15", 1.0, unit))
        return httpx.Response(200, json={"Data": rows, "Total": len(rows)})
    return h


def test_pngts_latest_cycle_final_and_categories():
    res, seen = run("gas_ebb_pngts", pngts_handler())
    assert [(r.method, "LOC" if "LOCATIONDATA" in str(r.url) else dict(httpx.QueryParams(r.content.decode()))
             .get("GasDay")) for r in seen] == [("GET", "LOC"), ("POST", "11/13/2026"), ("POST", "11/12/2026")]
    v = vals(res, datetime(2026, 11, 13))
    assert v["ebb.pngts.east_hereford.receipts"] == 440               # the Evening cycle, not Timely's half
    assert v["ebb.pngts.upne.import_canada"] == 440
    assert v["ebb.pngts.upne.ldc"] == 60
    assert v["ebb.pngts.upne.power"] == 15 + 10 + 5                   # Rumford by name; the Newingtons by override
    assert v["ebb.pngts.upne.industrial"] == 7
    assert v["ebb.pngts.upne.int_out"] == 320 + 20
    assert v["ebb.pngts.upne.net"] == pytest.approx(60 + 30 + 7 + 340 - 440)
    o = next(o for o in res.obs if o.ts == datetime(2026, 11, 13))
    assert o.as_of == datetime(2026, 11, 13, 3, 4, 15)                # 21:04:15 CST on 11-12 -> UTC
    assert datetime(2026, 11, 12) in {o.ts for o in res.obs}          # the previous gas day's final
    res2, seen2 = run("gas_ebb_pngts", pngts_handler(), now=NOW.replace(hour=19))
    assert len(seen2) == 1                                            # one POST; final done, locations cached


def test_pngts_unit_drift():
    with pytest.raises(FormatDrift, match="BZ"):
        run("gas_ebb_pngts", pngts_handler(unit="DTH"))


# ------------------------------------------------------------------ Iroquois
IRQ = SPECS["gas_ebb_iroquois"]["params"]["boards"][0]
IRQ_VALS = {("2250", "R"): 800, ("2249", "D"): 260, ("2251", "D"): 100, ("2363", "D"): 20, ("2366", "D"): 150,
            ("2479", "D"): 100, ("2255", "D"): 165, ("2261", "R"): 90, ("2256", "R"): 255, ("2339", "D"): 150,
            ("2348", "D"): 85, ("2252", "D"): 9, ("2257", "D"): 2}


def irq_rows(scale=1.0):
    locs = sorted({k.split(":")[0] for k in IRQ["overrides"]})
    out = []
    for loc in locs:
        for d, desc in (("D", "Delivery point(s) quantity"), ("R", "Receipt point(s) quantity")):
            out.append({"Loc Name": f"P{loc}", "Loc": int(loc), "Loc/QTI Desc": desc, "Design Capacity": 1,
                        "Total Scheduled Quantity": f"{round(IRQ_VALS.get((loc, d), 0) * D * scale):,}",
                        "Meas Basis Desc": "MMBtu", "Posting Date": "11/13/2026", "Posting Time": "10:04:20 AM",
                        "statusCode": 1, "statusText": "Records fetched successfully"})
    return out


def irq_handler(posted=("Timely", "Evening", "Intraday3")):
    def h(r):
        q = dict(r.url.params)
        assert base64.b64decode(q["class"]).decode() == "OperationallyAvailableClass"
        p = json.loads(base64.b64decode(q["param"]))
        rows = irq_rows(1.0 if p["cycleDescValue"] in posted else 0.0)   # unposted cycles: every row zero
        return httpx.Response(200, json=rows, headers={"content-type": "text/html"})
    return h


def irq_cycles(seen):
    return [(json.loads(base64.b64decode(dict(r.url.params)["param"]))["searchDateValue"][:10],
             json.loads(base64.b64decode(dict(r.url.params)["param"]))["cycleDescValue"]) for r in seen]


def test_iroquois_cycle_by_clock_hand_map_and_named():
    res, seen = run("gas_ebb_iroquois", irq_handler())
    assert irq_cycles(seen) == [("11/13/2026", "Evening"), ("11/12/2026", "Intraday3")]
    v = vals(res, datetime(2026, 11, 13))
    assert v["ebb.iroquois.waddington.receipts"] == 800
    assert v["ebb.iroquois.nyc.import_canada"] == 800
    assert v["ebb.iroquois.li_bronx.deliveries"] == 260 + 100 + 20
    assert v["ebb.iroquois.nyc.power"] == 150 + 100                    # Athens, Dover
    assert v["ebb.iroquois.nyc.ldc"] == 260 + 100 + 20 + 2
    assert v["ebb.iroquois.nyc.int_out"] == 165 and v["ebb.iroquois.nyc.int_in"] == 90
    assert v["ebb.iroquois.upne.power"] == 85                          # Milford B
    assert v["ebb.iroquois.upne.ldc"] == 150 + 9
    assert v["ebb.iroquois.upne.net"] == pytest.approx(85 + 159 - 255)
    o = next(o for o in res.obs if o.ts == datetime(2026, 11, 13))
    assert o.as_of == datetime(2026, 11, 13, 4, 0)                     # Evening: 22:00 CST the day before
    # the same cycle is still the newest due: nothing requested
    _, seen2 = run("gas_ebb_iroquois", irq_handler(), now=NOW.replace(hour=19))
    assert seen2 == []
    # 14:30 CST: Intraday 1 is due but posts zeros -> step back to Evening, already read: one request, no data
    res3, seen3 = run("gas_ebb_iroquois", irq_handler(), now=datetime(2026, 11, 13, 20, 30))
    assert irq_cycles(seen3) == [("11/13/2026", "Intraday1")] and res3.obs == []


def test_iroquois_unmapped_point_with_gas_fails_closed(tmp_path, monkeypatch):
    """The no-state guard (Reviewer on #478; this used to warn and drop the point): Iroquois has no location file, so a
    point its hand map lacks would fall out of every region total. With gas it fails the run; with zero TSQ, or named
    in `unlocated_ok`, it passes and the totals are those of the mapped points."""
    def extra(tsq):
        def h(r):
            rows = irq_handler()(r).json() + [{**irq_rows()[0], "Loc": 9999, "Loc Name": "New Point",
                                               "Total Scheduled Quantity": tsq}]
            return httpx.Response(200, json=rows)
        return h

    homes = iter(range(10))

    def fresh(handler, spec=None):                      # each run from an empty home (no "cycle already read")
        monkeypatch.setenv("MONTNEY_HOME", str(tmp_path / f"home{next(homes)}"))
        return vals(run("gas_ebb_iroquois", handler, spec=spec)[0], datetime(2026, 11, 13))
    with pytest.raises(FormatDrift, match=r"iroquois: 1 point\(s\) with scheduled gas but no state.*\['9999'\]"):
        run("gas_ebb_iroquois", extra("5,185"))
    base = fresh(irq_handler())
    assert base and fresh(extra("0")) == base
    spec = copy.deepcopy(SPECS["gas_ebb_iroquois"])
    spec["params"]["boards"][0]["unlocated_ok"] = "^new point$"
    assert fresh(extra("5,185"), spec) == base


def test_east_guard_reads_a_stale_location_file_once_more():
    """PNGTS: a delivery with gas that the cached location file lacks. On the day the file was read it fails the run
    (no extra request); two days on, the run re-reads the file once (after the OA, before the guard) and passes."""
    new = ("230099", "NEW CITY GATE", "DPQ", 12)
    pts = PNGTS_PTS + [new]

    def handler(loc_text):
        def h(r):
            if "LOCATIONDATA" in str(r.url):
                return httpx.Response(200, text=loc_text)
            form = dict(httpx.QueryParams(r.content.decode()))
            gd = datetime.strptime(form["GasDay"], "%m/%d/%Y")
            day, before = gd.date().isoformat(), (gd - timedelta(days=1)).date().isoformat()
            rows = [{"LocId": loc, "LocNm": nm, "GasDay": f"{day}T00:00:00", "CycleDescr": "Evening",
                     "PostDate": f"{before}T21:04:15", "TotalSchdQty": v * D, "LocQtyTypeCode": q,
                     "MeasBasisCode": "BZ"} for loc, nm, q, v in pts]
            return httpx.Response(200, json={"Data": rows, "Total": len(rows)})
        return h
    with pytest.raises(FormatDrift, match=r"pngts: .*no state.*\['230099'\]"):
        run("gas_ebb_pngts", handler(PNGTS_LOC))
    later = NOW + timedelta(days=2)
    with_new = PNGTS_LOC + "NEW CITY GATE,230099,ME,x,x,x,x,x,LDC,x,D,A,x,,N,\r\n"
    res, seen = run("gas_ebb_pngts", handler(with_new), now=later)
    assert ["LOC" if "LOCATIONDATA" in str(r.url) else "OA" for r in seen] == ["OA", "OA", "LOC"]
    assert vals(res, datetime(2026, 11, 15))["ebb.pngts.upne.ldc"] == 60 + 12
    # the next run: the refreshed file is cached, no location request
    _, seen2 = run("gas_ebb_pngts", handler(with_new), now=later + timedelta(hours=2))
    assert not any("LOCATIONDATA" in str(r.url) for r in seen2)


def test_iroquois_mapped_point_gone_fails_closed():
    def gone(r):
        return httpx.Response(200, json=[x for x in irq_handler()(r).json() if x["Loc"] != 2366])
    with pytest.raises(FormatDrift, match="2366"):
        run("gas_ebb_iroquois", gone)


# ------------------------------------------------------------------ Transco
TGPL_HEAD = ('"TSP","TSP Name","Effective Gas Day","Effective Time","Cycle Desc","Posting Date","Posting Time",'
             '"Meas Basis Desc","Loc","Loc Purp Desc","Flow Ind","Loc QTI","Loc Name","Loc Zn","Design Capacity",'
             '"Operating Capacity","Total Scheduled Quantity","Operationally Available Capacity","IT Indicator",'
             '"All Qty Avail","Qty Reason"')
TGPL_PTS = [("1099998", "D", "MLQ", "NEW YORK FAC GROUP MP 1829.08", 1000),
            ("9008266", "D", "MLQ", "ROCKAWAY FBF-BUG MP 3.2", 40),
            ("9008502", "D", "MLQ", "MARINACE FBF - LI MP 3.2", 18),
            ("1099999", "D", "MLQ", "WASHINGTON GAS F GRP MP 1598.2", 500),
            ("1001300", "R", "SGQ", "LINDEN MP 1808.191 S to N NL", 1400),
            ("9012943", "R", "MLQ", "CLARKS BRANCH MATTERHORN MP 272.7", 400),
            ("1002687", "SI", "SIQ", "WASHINGTON STORAGE MP 542.06", 50)]
TGPL_LOC = {"data": [{"location": "9008266", "stateAbbr": "NY", "naesbType": "LDC", "type": "VirtualMeas"},
                     {"location": "9008502", "stateAbbr": "NY", "naesbType": "OTH", "type": "VirtualMeas"},
                     {"location": "9012943", "stateAbbr": "TX", "naesbType": "INT", "type": "PiplnIntrcon"},
                     {"location": "1002687", "stateAbbr": "LA", "naesbType": "STR", "type": "Storage"}]}


def tgpl_csv(day, cycle_desc, scale=1.0, head=TGPL_HEAD):
    out = [head]
    for loc, fi, qti, nm, v in TGPL_PTS:
        out.append(f'"007933021","TRANSCO","{day}","09:00 AM CT","{cycle_desc}","11/12/2026","08:46 PM CT","MMBtu",'
                   f'{loc},"MQ","{fi}","{qti}","{nm}","6",9,9,{round(v * D * scale)},1,"N",Y,')
    return "\r\n".join(out)


def tgpl_handler(cycle=2, head=TGPL_HEAD):
    def h(r):
        u = str(r.url)
        if "location-data-download" in u:
            return httpx.Response(200, json=TGPL_LOC)
        q = dict(r.url.params)
        if "oac/cycle" in u:
            return httpx.Response(200, json={"data": {"commitDate": q["startDate"], "cycle": cycle}})
        assert "download/csv" in u
        return httpx.Response(200, text=tgpl_csv(q["startDate"], "EVENING" if q["cycle"] == "2" else "ID5",
                                                 0.5 if q["startDate"] == "11/12/2026" else 1.0, head))
    return h


def test_transco_cycle_check_overrides_and_linden():
    res, seen = run("gas_ebb_transco", tgpl_handler())
    paths = [str(r.url.path).rsplit("/", 1)[-1] + ":" + dict(r.url.params).get("startDate", "") for r in seen]
    assert paths == ["location-data-download:", "cycle:11/13/2026", "csv:11/13/2026", "cycle:11/12/2026",
                     "csv:11/12/2026"]
    v = vals(res, datetime(2026, 11, 13))
    assert v == {"ebb.transco.nyc.ldc": 1000 + 40 + 18, "ebb.transco.nyc.net": 1058, "ebb.transco.linden.flow": 1400}
    assert vals(res, datetime(2026, 11, 12))["ebb.transco.linden.flow"] == 700
    _, seen2 = run("gas_ebb_transco", tgpl_handler(), now=NOW.replace(hour=19))
    assert len(seen2) == 1                                           # same cycle: the 111-byte check only
    _, seen3 = run("gas_ebb_transco", tgpl_handler(cycle=3), now=NOW.replace(hour=21))
    assert len(seen3) == 2                                           # a new cycle: check + CSV


def test_transco_header_drift():
    with pytest.raises(FormatDrift, match="header changed"):
        run("gas_ebb_transco", tgpl_handler(head=TGPL_HEAD.replace('"Loc QTI"', '"QTI"')))


# ------------------------------------------------------------------ Millennium (TC eConnects)
VIEWER = ('<html><script>{"ExportUrlBase":"/infopost/Reserved.ReportViewerWebControl.axd?ReportSession=abc\\u0026'
          'OpType=Export\\u0026Format="}</script></html>')
TCE_COLS = ["TSPName", "EffGasDay", "Cycle", "PostingDate", "MeasurementBasis", "LocationName", "Location",
            "LocPurpDesc", "TotalScheduledQuantity", "LocQTI", "Direction", "RateSchedule"]
MPC_PTS = [("640168", "Stagecoach Receipt", "RPQ", "", 590), ("642638", "Sanford", "RPQ", "", 470),
           ("640608", "Laser Midstream", "RPQ", "", 210), ("638167", "Cooper Hill", "RPQ", "", 2),
           ("633395", "Ramapo AGT", "DPQ", "", 840), ("643196", "CPV Valley Energy Center", "DPQ", "", 110),
           ("600314", "CENTRAL HUDSON", "DPQ", "", 13), ("643206", "Valley Lateral", "DPQ", "", 110),
           ("WAGONERW", "Wagoner West", "SGQ", "Forwardhaul", 1080)]
MPC_LOC = ("Loc,Loc Name,Loc Type Ind,Loc Cnty,Loc St Abbrev,Up/Dn Name\r\n"
           "640168,Stagecoach Receipt,STR,TIOGA,NY,Stagecoach\r\n642638,Sanford,WHD,BROOME,NY,Bluestone\r\n"
           "640608,Laser Midstream,GTH,BROOME,NY,Laser\r\n638167,Cooper Hill,WHD,CHEMUNG,NY,Talisman\r\n"
           "633395,Ramapo AGT,INT,ROCKLAND,NY,Algonquin\r\n643196,CPV Valley Energy Center,EGN,ORANGE,NY,CPV\r\n"
           "600314,CENTRAL HUDSON,LDC,ORANGE,NY,Central Hudson\r\n643206,Valley Lateral,VIR,ORANGE,NY,\r\n")


def tce_csv(rows=MPC_PTS):
    out = [",".join(TCE_COLS)]
    for loc, nm, qti, d, v in rows:
        for rs in ("FT-1", "IT"):                                    # rate-schedule copies: counted once
            out.append(f'"Millennium","11/13/2026","TIM","11/12/2026 5:04 PM","BZ","{nm}","{loc}  ","x",'
                       f'"{round(v * D)}","{qti}","{d}","{rs}"')
    return "﻿" + "\r\n".join(out)


def tce_handler(rows=MPC_PTS):
    def h(r):
        u = str(r.url)
        if "LocationDataDownload" in u:
            return httpx.Response(200, text=MPC_LOC, headers={"set-cookie": "ASP.NET_SessionId=loc; path=/"})
        if "ReportViewerWebControl" in u:
            assert u.endswith("Format=CSV")
            return httpx.Response(200, text=tce_csv(rows), headers={"content-type": "text/csv; charset=utf-8"})
        return httpx.Response(200, text=VIEWER)
    return h


def test_millennium_once_a_day_pa_receipts_and_fresh_session():
    res, seen = run("gas_ebb_millennium", tce_handler())
    assert ["LocationDataDownload" in str(r.url) for r in seen] == [True, False, False]
    assert "ASP.NET_SessionId=loc" not in seen[1].headers.get("cookie", "")   # dropped after the location render
    v = vals(res, datetime(2026, 11, 13))
    assert v["ebb.millennium.pa.receipts"] == 590 + 470 + 210
    assert v["ebb.millennium.nyc.int_in"] == 1270                    # Stagecoach / Sanford / Laser by override
    assert v["ebb.millennium.nyc.production"] == 2                   # the real NY wells only
    assert v["ebb.millennium.nyc.power"] == 110 and v["ebb.millennium.nyc.ldc"] == 13
    assert v["ebb.millennium.nyc.int_out"] == 840                    # Valley Lateral (VIR) is internal
    assert v["ebb.millennium.nyc.net"] == pytest.approx(840 + 110 + 13 - 1272)
    _, seen2 = run("gas_ebb_millennium", tce_handler(), now=NOW.replace(hour=21))
    assert seen2 == []                                               # Timely only: read once per day


def test_millennium_override_gone_fails_closed():
    with pytest.raises(FormatDrift, match="642638"):
        run("gas_ebb_millennium", tce_handler([p for p in MPC_PTS if p[0] != "642638"]))
