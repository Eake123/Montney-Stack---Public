"""Guardian and Viking EBBs (gas_ebb_dtm, DT Midstream Trellis): each pipe's session page before its list (the mock
server, like the live one, answers the list for the session's pipe whatever the tspId), the report list as
cheap check and as the source of yesterday's Intraday 3, the hand-mapped regions and categories, the segment and point
borders, the no-state guard and FormatDrift. Payload shapes copy the live ones (2026-11-13); volumes are made up
(multiples of 1,037 Dth)."""

from __future__ import annotations

import copy
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, gas_ebb_common, load_specs
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 19, 0)          # 13:00 CST
D = 1037.0
SPECS = load_specs(include_disabled=True)
BOARDS = {b["pipe"]: b for b in SPECS["gas_ebb_dtm"]["params"]["boards"]}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(gas_ebb_common, "PAUSE", 0.0)


def run(handler, now=NOW):
    seen = []

    def h(r):
        seen.append(r)
        return handler(r)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=now)
    return build(copy.deepcopy(SPECS["gas_ebb_dtm"])).run(ctx), seen


def vals(res, day):
    return {o.series_id: round(o.value, 6) for o in res.obs if o.ts == day}


# ------------------------------------------------------------------ payloads
FIELDS = ["loc_hidden", "Loc Name", "Loc", "Loc Prop", "Loc Purp Desc", "Flow Ind", "Loc/QTI", "All Qty Avail", "DC",
          "OPC", "TSQ", "OAC", "IT", "Qty Reason"]
# (loc, direction) -> MMcf/d; every other mapped point posts 0
GPL_VALS = {("359490", "D"): 60, ("379832", "D"): 190, ("359488", "D"): 21, ("880045", "R"): 1, ("880060", "D"): 2,
            ("394756", "R"): 175, ("359485", "R"): 187}
VGT_VALS = {("33973", "R"): 340, ("15225", "D"): 30, ("15274", "D"): 13, ("288044", "D"): 36, ("12002", "D"): 5,
            ("28693", "D"): 12, ("11971", "D"): 42, ("179031", "R"): 30, ("28872", "D"): 78, ("28872", "R"): 47}
SEGMENTS = {"guardian": [("BLUFF CREEK CS", "BLUFF CRK", 415), ("JOLIET CS", "JOLIET", 413)],
            "viking": [("OSCEOLA CS#2222", "OSCEOLA", 167), ("ADA CS #2207", "ADA", 310)]}
LISTS = {"11": [("77731000000", "11/13/2026", "Intraday 1", "2026-11-13T12:30:13.000-0600"),
                ("77705000000", "11/13/2026", "Evening", "2026-11-12T20:31:30.000-0600"),
                ("77710000000", "11/12/2026", "Intraday 3", "2026-11-12T21:30:24.000-0600"),
                ("77686000000", "11/12/2026", "Intraday 2", "2026-11-12T17:00:46.000-0600")],
         "9": [("77732000000", "11/13/2026", "Intraday 1", "2026-11-13T12:30:19.000-0600"),
               ("77708000000", "11/12/2026", "Intraday 3", "2026-11-12T21:30:11.000-0600")]}
FILES = {r[0]: ("guardian" if tsp == "11" else "viking", r[1], r[2]) for tsp, rows in LISTS.items() for r in rows}
PAGE = '<html><script>var tspId = "{tsp}";\nvar x = 1;</script></html>'


def xml(pipe, day, cycle, scale=1.0, extra=(), drop=(), unit="MMBtu", fields=FIELDS):
    vals_ = GPL_VALS if pipe == "guardian" else VGT_VALS
    rows = []
    for loc in BOARDS[pipe]["overrides"]:
        loc = str(loc)
        if loc in drop:
            continue
        for d, purp, qti in (("D", "Delivery Location", "DPQ"), ("R", "Receipt Location", "RPQ")):
            rows.append(("x", f"P{loc}", loc, loc, purp, d, qti, round(vals_.get((loc, d), 0) * D * scale)))
    for name, prop, v in SEGMENTS[pipe]:
        rows.append(("x", name, "", prop, "Rec", "R", "RPQ", round(v * D * scale)))
        rows.append(("x", name, "", prop, "Del", "D", "DPQ", 0))
    rows += list(extra)
    head = "".join(f'<RptFld hidden="0" align="left">{f}</RptFld>' for f in fields)
    body = "".join(f'<Block key="{i}"><row><cell>{i}</cell><cell>{r[1]}</cell><cell>{r[2]}</cell><cell>{r[3]}</cell>'
                   f'<cell>{r[4]}</cell><cell>{r[5]}</cell><cell>{r[6]}</cell><cell>N</cell><cell>9</cell><cell>9'
                   f'</cell><cell>{r[7]}</cell><cell>0</cell><cell>N</cell><cell></cell></row></Block>'
                   for i, r in enumerate(rows, 2))
    posted = "11/13/2026 12:30:13 PM" if cycle == "Intraday 1" else "11/12/2026 09:30:24 PM"
    return (f'<ip:InfoPostReport xmlns:ip="http://us.bstonetech.com/trellis/infopost"><ColumnHeaders>'
            f'<ColumnHeader id="1" prefix="l_">{head}</ColumnHeader></ColumnHeaders><SubReport hdrid="1" level="1">'
            f'<Dictionary><nvpair column="1" name="TSP Name">{pipe}</nvpair><nvpair column="1" name="Backbone">'
            f'{pipe.title()} ({BOARDS[pipe]["code"]})</nvpair><nvpair column="1" name="CycleDesc">'
            f'{cycle}</nvpair><nvpair column="2" name="Posting Date/Time">{posted}</nvpair><nvpair column="2" '
            f'name="Eff Gas Day">{day}</nvpair><nvpair column="2" name="Meas Basis Desc">{unit}</nvpair></Dictionary>'
            f'{body}</SubReport></ip:InfoPostReport>')


def handler(lists=LISTS, page_tsp=None, **kw):
    session = {}

    def h(r):
        u, q = str(r.url), dict(r.url.params)
        if "/home/infopost/" in u:
            code = u.rsplit("/", 1)[1]
            session["tsp"] = {"GPL": "11", "VGT": "9"}[code]
            return httpx.Response(200, text=PAGE.format(tsp=page_tsp or session["tsp"]))
        if "getInfoPostRpts.do" in u:
            assert q["rptId"] == "2" and r.headers["X-Requested-With"] == "XMLHttpRequest"
            assert (q["startDate"], q["endDate"]) == ("11/11/2026", "11/13/2026")
            tsp = session["tsp"]                                     # the session's pipe, not q["tspId"]
            rows = [{"id": int(i), "infoPostDataId": int(i[:5]), "formattedGasDay": d, "cycleCode": c, "runDate": rd,
                     "mngPplCode": {"11": "GPL", "9": "VGT"}[tsp], "statusCode": "Downloadable"}
                    for i, d, c, rd in lists[tsp]]
            return httpx.Response(200, json={"total": 1, "page": 1, "records": len(rows), "rows": rows})
        assert "getInfoPostRptBinaryFile.do" in u
        pipe, day, cycle = FILES[q["infoPostDataId"]]
        assert q["tspId"] == str(BOARDS[pipe]["tsp"])
        return httpx.Response(200, text=xml(pipe, day, cycle, 0.5 if day == "11/12/2026" else 1.0,
                                            **({} if kw.get("only") not in (None, pipe) else
                                               {k: v for k, v in kw.items() if k != "only"})),
                              headers={"content-type": "application/xml"})
    return h


def reqs(seen):
    out = []
    for r in seen:
        u, q = str(r.url), dict(r.url.params)
        out.append("page " + u.rsplit("/", 1)[1] if "/home/infopost/" in u else
                   f"list {q['tspId']}" if "getInfoPostRpts" in u else f"file {q['infoPostDataId'][:5]}")
    return out


# ------------------------------------------------------------------ tests
def test_requests_hand_map_and_borders():
    res, seen = run(handler())
    assert reqs(seen) == ["page GPL", "list 11", "file 77731", "file 77710", "page VGT", "list 9", "file 77732",
                          "file 77708"]
    v = vals(res, datetime(2026, 11, 13))
    assert v["ebb.guardian.border.il_wi"] == 415                     # the Bluff Creek CS segment
    assert v["ebb.guardian.umw.ldc"] == 60 + 190 + 21
    assert v["ebb.guardian.umw.industrial"] == 2 and v["ebb.guardian.umw.production"] == 1
    assert v["ebb.guardian.umw.net"] == 60 + 190 + 21 + 2 - 1        # Joliet (IL) is outside every region
    assert not any(k.startswith("ebb.guardian.umw.int") and v[k] for k in v)
    assert v["ebb.viking.border.canada_mn"] == 340 == v["ebb.viking.umw.import_canada"]
    assert v["ebb.viking.border.mn_wi"] == 167
    assert v["ebb.viking.border.mn_nd"] == 30 + 13 == v["ebb.viking.bakken.ldc"] == v["ebb.viking.bakken.net"]
    assert v["ebb.viking.umw.power"] == 36                            # Wheaton Generation
    assert v["ebb.viking.umw.ldc"] == 5 + 12
    assert v["ebb.viking.umw.int_out"] == 42 + 78 and v["ebb.viking.umw.int_in"] == 30 + 47
    assert v["ebb.viking.umw.net"] == pytest.approx(36 + 17 + 120 - 340 - 77)
    o = next(o for o in res.obs if o.series_id == "ebb.guardian.border.il_wi" and o.ts == datetime(2026, 11, 13))
    assert o.as_of == datetime(2026, 11, 13, 18, 30, 13)              # 12:30:13 PM CST
    assert vals(res, datetime(2026, 11, 12))["ebb.viking.border.canada_mn"] == 170   # yesterday's Intraday 3
    # 2 h later, nothing new: the page and the two lists, no file
    _, seen2 = run(handler(), now=NOW.replace(hour=21))
    assert reqs(seen2) == ["page GPL", "list 11", "page VGT", "list 9"]


def test_final_waits_for_intraday3():
    lists = {"11": [r for r in LISTS["11"] if r[2] != "Intraday 3"], "9": LISTS["9"]}
    res, seen = run(handler(lists))
    assert reqs(seen) == ["page GPL", "list 11", "file 77731", "page VGT", "list 9", "file 77732", "file 77708"]
    assert "ebb.guardian.border.il_wi" not in vals(res, datetime(2026, 11, 12))


def test_unmapped_point_with_gas_fails_closed():
    new = ("x", "New Town", "999001", "999001", "Delivery Location", "D", "DPQ", round(5 * D))
    with pytest.raises(FormatDrift, match="no state"):
        run(handler(only="viking", extra=[new]))
    res, _ = run(handler(only="viking", extra=[new[:7] + (0,)]))        # posted with no gas: fine
    assert vals(res, datetime(2026, 11, 13))["ebb.viking.border.canada_mn"] == 340


def test_drift():
    with pytest.raises(FormatDrift, match="28872"):              # an overridden point gone
        run(handler(only="viking", drop=("28872",)))
    with pytest.raises(FormatDrift, match="header changed"):
        run(handler(fields=[f if f != "TSQ" else "Sched Qty" for f in FIELDS]))
    with pytest.raises(FormatDrift, match="MMBtu"):
        run(handler(unit="Dth"))
    with pytest.raises(FormatDrift, match="tspId"):
        run(handler(page_tsp=12))


def test_a_list_or_file_for_the_other_pipe_is_drift():
    """Live 2026-11-13: under a Guardian session the Viking list returned Guardian's reports."""
    def stale(r):                                                    # the server ignores the Viking page
        if str(r.url).endswith("/VGT"):
            return httpx.Response(200, text=PAGE.format(tsp=9))
        return inner(r)
    inner = handler()
    with pytest.raises(FormatDrift, match=r"rows for \['GPL'\], not VGT"):
        run(stale)

    def wrong_file(r):                                               # a Guardian file under a Viking id
        q = dict(r.url.params)
        if q.get("infoPostDataId") == "77732000000":
            q = {**q, "infoPostDataId": "77731000000", "tspId": "11"}
            r = httpx.Request("GET", r.url.copy_with(params=q))
        return inner2(r)
    inner2 = handler()
    with pytest.raises(FormatDrift, match="not VGT"):
        run(wrong_file)
