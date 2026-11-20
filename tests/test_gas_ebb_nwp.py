"""Northwest Pipeline EBB (gas_ebb_nwp): requests and the provisional final, region × category sums with the shipped
spec's overrides, NWP's type mapping (blank types borrowed, "METER STATION" end users stay industrial, Kelso–Beaver
loop dropped), the no-state guard and FormatDrift. Payload shapes copy the live pages (2026-11-13: the OA report's
HTML table and header, location.action's grid); the volumes are made up (multiples of 1,037 Dth)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, gas_ebb_common, load_specs
from montney.connectors import gas_ebb_nwp as nwp
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 17, 0)          # 11:00 CST: the previous gas day's final is due
D = 1037.0
SPEC = load_specs(include_disabled=True)["gas_ebb_nwp"]
T12, T13 = datetime(2026, 11, 12), datetime(2026, 11, 13)

# (Loc Prop, name, QTI, MMcf/d); location rows below
PTS = [("297", "SUMAS RECEIPT", "RPQ", 1000), ("187", "STANFIELD RECEIPT", "RPQ", 300),
       ("188", "STANFIELD DELIVERY", "DPQ", 90), ("267", "WNG ZONE 30", "DPQ", 130),
       ("717", "NAMPA", "DPQ", 70), ("159", "MERIDIAN/BOISE", "DPQ", 20), ("161", "CALDWELL", "DPQ", 0),
       ("128", "IDAHO FALLS", "DPQ", 0), ("158", "ALBERTSONS INC. (KUNA)", "DPQ", 0),
       ("640", "GRAYS HARBOR METER STATION", "DPQ", 100), ("978", "BEAVER GENERATING", "DPQ", 65),
       ("229", "KELSO/BEAVER", "DPQ", 65), ("971", "KELSO BEAVER RECEIPT", "RPQ", 65),
       ("228", "WEYEHAEUSER (OSTRANDER PLANT)", "DPQ", 15), ("950", "HAGINS DELIVERY METER STATION", "DPQ", 2),
       ("237", "JACKSON PRAIRIE DELIVERY", "SIQ", 30), ("235", "JACKSON PRAIRIE RECEIPT", "SWQ", 5),
       ("236", "JACKSON PRAIRIE STORAGE", "STQ", 25), ("620", "DEER ISLAND RECEIPT", "RPQ", 10),
       ("726", "CEDAR HILL RECEIPT", "RPQ", 3), ("647", "JACKSON PRAIRIE PARK RECEIPT", "RPQ", 4),
       ("459", "RENO LATERAL (TO PAIUTE)", "DPQ", 50), ("328", "ALBANY COMPRESSOR INCREASING", "MLQ", 120),
       ("754", "NORTH SEATTLE IN-LINE TRANSFER", "DPQ", 7),
       ("4", "IGNACIO PLANT", "RPQ", 240), ("92", "KERN RIVER MUDDY CREEK DELIV.", "DPQ", 330),
       ("542", "SHUTE CREEK PLANT DELIVERY", "DPQ", 24), ("748", "DOUBLE D DELIVERY", "DPQ", 21)]
# (Loc Prop, name, type, dir, counterparty, state)
LOCS = [("297", "SUMAS RECEIPT", "P/L Inter", "R", "Westcoast Energy Inc.", "WA"),
        ("187", "STANFIELD RECEIPT", "P/L Inter", "R", "Gas Transmission Northwest Corporation", "OR"),
        ("188", "STANFIELD DELIVERY", "P/L Inter", "D", "Gas Transmission Northwest Corporation", "OR"),
        ("267", "WNG ZONE 30", "LDC", "D", "Puget Sound Energy, Inc.", "WA"),
        ("717", "NAMPA", "", "D", "Intermountain Gas Company", "ID"),            # 3 of 4 typed rows LDC: borrows LDC
        ("159", "MERIDIAN/BOISE", "LDC", "D", "Intermountain Gas Company", "ID"),
        ("161", "CALDWELL", "LDC", "D", "Intermountain Gas Company", "ID"),
        ("128", "IDAHO FALLS", "LDC", "D", "Intermountain Gas Company", "ID"),
        ("158", "ALBERTSONS INC. (KUNA)", "End-User", "D", "Intermountain Gas Company", "ID"),
        ("640", "GRAYS HARBOR METER STATION", "Elec Gen", "D", "Puget Sound Energy, Inc.", "WA"),
        ("978", "BEAVER GENERATING", "Plant", "D", "Portland General Electric Company", "OR"),
        ("229", "KELSO/BEAVER", "End-User", "D", "", "WA"),
        ("971", "KELSO BEAVER RECEIPT", "P/L Inter", "R", "", "WA"),
        ("228", "WEYEHAEUSER (OSTRANDER PLANT)", "End-User", "D", "Nippon Dynawave Packaging Co", "WA"),
        ("950", "HAGINS DELIVERY METER STATION", "End-User", "D", "Darigold, Inc.", "WA"),
        ("237", "JACKSON PRAIRIE DELIVERY", "Stor", "D", "", "WA"),
        ("235", "JACKSON PRAIRIE RECEIPT", "Stor", "R", "", "WA"),
        ("236", "JACKSON PRAIRIE STORAGE", "Stor", "B", "", "WA"),
        ("620", "DEER ISLAND RECEIPT", "", "R", "Northwest Natural Gas Company", "OR"),
        ("726", "CEDAR HILL RECEIPT", "", "R", "King County Solid Waste Division", "WA"),
        ("647", "JACKSON PRAIRIE PARK RECEIPT", "Virtual", "R", "Northwest Pipeline - Park & Loan", "WA"),
        ("459", "RENO LATERAL (TO PAIUTE)", "P/L Inter", "D", "Great Basin Gas Transmission Company", "ID"),
        ("328", "ALBANY COMPRESSOR INCREASING", "Compressor", "B", "", "OR"),
        ("4", "IGNACIO PLANT", "Plant", "R", "Harvest Four Corners, LLC", "CO"),
        ("92", "KERN RIVER MUDDY CREEK DELIV.", "P/L Inter", "D", "Kern River Transmission Company", "WY"),
        ("542", "SHUTE CREEK PLANT DELIVERY", "Plant", "D", "ExxonMobil Gas & Power Marketing Company", "WY"),
        ("748", "DOUBLE D DELIVERY", "", "D", "Spire Storage West, LLC", "WY")]
# every other overridden point of the shipped spec is posted with 0 (an overridden point must be posted)
_have = {p[0] for p in PTS}
_extra = [k for k in SPEC["params"]["boards"][0]["overrides"] if k not in _have]
PTS += [(k, f"POINT {k}", "DPQ", 0) for k in _extra]
LOCS += [(k, f"POINT {k}", "LDC", "D", "", "WA") for k in _extra]
LOC_HEAD = ["Loc", "Loc Prop", "Loc Name", "Loc Type Ind", "Dir Flo", "Loc Stat Ind", "Eff Date", "Inact Date",
            "Up/Dn Ind", "Up/Dn ID", "Up/Dn ID Prop", "Up/Dn Name", "Up/Dn FERC CID Ind", "Up/Dn FERC CID", "Up/Dn Loc",
            "Up/Dn Loc Name", "Loc Zone", "Loc Cnty", "Loc St Abbrev", "Update D/T"]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(gas_ebb_common, "PAUSE", 0.0)


def oa_page(day="11/13/2026", cycle="Intra Day 1 Cycle 3", post="11/13/2026 12:34 PM CST", pts=None, scale=1.0,
            cols=None, basis="MMBtu"):
    cols = cols or nwp.OA_COLS
    purp = {"DPQ": ("Delivery Location", "Delivery"), "RPQ": ("Receipt Location", "Receipt"),
            "SIQ": ("Delivery Location", "Storage Injection"), "SWQ": ("Receipt Location", "Storage Withdrawal"),
            "STQ": ("Storage Area", "Bi-directional"), "MLQ": ("Mainline", "Mainline")}
    tr = "".join(f"<tr><td>{p}</td><td>{n}</td><td>{9000 + i}</td><td>{purp[q][0]}</td><td>{purp[q][1]}</td>"
                 f"<td>{q}</td><td>1</td><td>1</td><td>{round(v * D * scale):,}</td><td>0</td><td></td></tr>"
                 for i, (p, n, q, v) in enumerate(PTS if pts is None else pts))
    th = "".join(f"<th>{c}</th>" for c in cols)
    return (f"<html><script>var header = 'Meas Basis: MMBtu';</script><table><tr><td>Northwest Pipeline LLC</td></tr>"
            f"<tr><td>Cycle:</td><td>{cycle}</td><td>Post Date/Time:</td><td>{post}</td><td>Eff Gas Day/Time:</td>"
            f"<td>{day} 10:00 AM CST</td></tr><tr><td>TSP Name:</td><td>Northwest Pipeline LLC</td>"
            f"<td>Meas Basis Desc:</td><td>{basis}</td></tr></table>"
            f"<table id='grid'><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></html>")


def loc_page(locs=None, head=None):
    head = head or LOC_HEAD
    th = "".join(f"<th><div class=\"wrap\"><h2>{c}</h2></div></th>" for c in head)
    rows = []
    for p, n, t, d, cp, st in LOCS if locs is None else locs:
        x = {"Loc": f"{9000 + int(p)} ", "Loc Prop": p, "Loc Name": n, "Loc Type Ind": t, "Dir Flo": d,
             "Loc Stat Ind": "Active", "Up/Dn Name": f'"{cp}"' if cp else "", "Loc St Abbrev": st}
        rows.append("<tr class=\"x\">" + "".join(f"<td>{x.get(c, '')}</td>" for c in head) + "</tr>")
    return (f"<html><h1 class=\"DORh1\">Location Data Download</h1><table id=\"grid\" style=\"width: 110%\"><thead>"
            f"<tr>{th}</tr></thead><tbody>{''.join(rows)}</tbody></table></html>")


def handler(pages: dict, loc: str | None = None):
    """pages: {StartGasFlowDate or None: html}."""
    def h(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("location.action"):
            return httpx.Response(200, text=loc if loc is not None else loc_page())
        assert r.url.path.endswith("CapacityResultsScrollable.action")
        return httpx.Response(200, text=pages[r.url.params.get("StartGasFlowDate")])
    return h


def run(h, now=NOW, spec=None):
    seen = []

    def rec(r):
        seen.append(r)
        return h(r)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(rec)), now=now)
    return build(copy.deepcopy(spec or SPEC)).run(ctx), seen


def vals(res, ts):
    return {o.series_id: round(o.value, 6) for o in res.obs if o.ts == ts}


def what(seen):
    return ["LOC" if r.url.path.endswith("location.action") else f"OA:{r.url.params.get('StartGasFlowDate', '')}"
            for r in seen]


PAGES = {None: oa_page(), "11-12-2026": oa_page("11/12/2026", "Storage Realignment Cycle 6", "11/13/2026 10:08 AM CST",
                                                scale=0.5)}


def test_requests_sums_types_and_overrides():
    res, seen = run(handler(PAGES))
    assert what(seen) == ["LOC", "OA:", "OA:11-12-2026"]
    v = vals(res, T13)
    want = {"ldc": 130 + 70 + 20,                       # Nampa's blank type borrows Intermountain's LDC (3 of 4)
            "power": 100 + 65,                          # Grays Harbor (Elec Gen) + Beaver (Plant, override EGN)
            "industrial": 15 + 2,                       # "... METER STATION" end users stay industrial
            "import_canada": 1000, "int_in": 300, "int_out": 90 + 50,
            "storage_inj": 30, "storage_wd": 5 + 10,    # JP (SIQ / SWQ; STQ dropped) + NW Natural at Deer Island
            "production": 3}                            # Cedar Hill RNG; Park & Loan, Kelso–Beaver, MLQ dropped
    for cat, x in want.items():
        assert v[f"ebb.nwp.pnw.{cat}"] == pytest.approx(x), cat
    assert v["ebb.nwp.pnw.net"] == pytest.approx((220 + 165 + 17 + 140 + 30) - (1000 + 300 + 15 + 3))
    assert v["ebb.nwp.mtnw.production"] == 240 and v["ebb.nwp.mtnw.int_out"] == 330
    assert v["ebb.nwp.mtnw.industrial"] == 24           # Shute Creek plant fuel, not power (POWER_RE would say power)
    assert v["ebb.nwp.mtnw.storage_inj"] == 21          # Spire's Double D (blank type, override STR)
    assert v["ebb.nwp.mtnw.net"] == pytest.approx(330 + 24 + 21 - 240)
    assert vals(res, T12)["ebb.nwp.pnw.ldc"] == pytest.approx(110)
    o = next(o for o in res.obs if o.series_id == "ebb.nwp.pnw.ldc" and o.ts == T13)
    assert o.as_of == datetime(2026, 11, 13, 18, 34)    # 12:34 PM CST -> UTC
    assert not res.warnings
    meta = {m.series_id: m for m in res.meta}
    assert meta["ebb.nwp.pnw.ldc"].unit == "MMcf/d" and meta["ebb.nwp.pnw.ldc"].day == "gas"
    assert "scheduled" in meta["ebb.nwp.pnw.power"].label
    # two hours on: the final is stored and the location file cached -> one request
    _, seen2 = run(handler(PAGES), now=NOW + timedelta(hours=2))
    assert what(seen2) == ["OA:"]


EARLY = {None: oa_page(), "11-12-2026": oa_page("11/12/2026", "Intra Day 3 Cycle 5", "11/12/2026 9:40 PM CST")}


def test_final_is_provisional_until_the_storage_realignment():
    early = EARLY
    res, seen = run(handler(early))
    assert what(seen) == ["LOC", "OA:", "OA:11-12-2026"] and vals(res, T12)["ebb.nwp.pnw.ldc"] == 220
    _, seen2 = run(handler(PAGES), now=NOW + timedelta(hours=2))          # read again: now realigned
    assert what(seen2) == ["OA:", "OA:11-12-2026"]
    _, seen3 = run(handler(PAGES), now=NOW + timedelta(hours=4))
    assert what(seen3) == ["OA:"]


def test_after_the_accept_hour_the_newest_cycle_is_final():
    _, seen = run(handler(EARLY), now=datetime(2026, 11, 13, 22, 30))     # 16:30 CST, no realignment posted
    _, seen2 = run(handler(EARLY), now=datetime(2026, 11, 14, 0, 30))
    assert what(seen) == ["LOC", "OA:", "OA:11-12-2026"] and what(seen2) == ["OA:"]


def test_a_point_with_gas_and_no_state_fails_closed_but_in_line_transfers_pass():
    pts = PTS + [("999", "DEX DELIVERY", "DPQ", 12)]
    pages = {None: oa_page(pts=pts), "11-12-2026": PAGES["11-12-2026"]}
    with pytest.raises(FormatDrift, match=r"nwp: .*no state.*\['999'\]"):
        run(handler(pages))
    # the in-line transfer (7 MMcf/d, not in the location file) passed above and in every other test: no FormatDrift


def test_an_overridden_point_gone_fails_closed():
    pts = [p for p in PTS if p[0] != "229"]
    with pytest.raises(FormatDrift, match="229"):
        run(handler({None: oa_page(pts=pts), "11-12-2026": PAGES["11-12-2026"]}))


def test_header_and_unit_drift():
    with pytest.raises(FormatDrift, match="location.action: header changed"):     # no cached file to fall back on
        run(handler(PAGES, loc=loc_page(head=[c.replace("Loc St Abbrev", "State") for c in LOC_HEAD])))
    with pytest.raises(FormatDrift, match="columns changed"):
        run(handler({None: oa_page(cols=[c.replace("Total Scheduled Qty", "TSQ") for c in nwp.OA_COLS])}))
    with pytest.raises(FormatDrift, match="MMBtu"):
        run(handler({None: oa_page(basis="Dth")}))


def test_the_final_must_be_the_day_asked_for():
    with pytest.raises(FormatDrift, match="asked for 2026-11-12"):
        run(handler({None: oa_page(), "11-12-2026": oa_page()}))


def test_an_untyped_point_with_gas_is_counted_and_warned():
    locs = [x for x in LOCS if x[0] != "228"] + [("228", "WEYEHAEUSER (OSTRANDER PLANT)", "", "D", "", "WA")]
    res, _ = run(handler(PAGES, loc=loc_page(locs)))
    v = vals(res, T13)
    assert v["ebb.nwp.pnw.industrial"] == 2 and v["ebb.nwp.pnw.int_out"] == 90 + 50 + 15
    assert any("no location type" in w and "228 WEYEHAEUSER" in w for w in res.warnings)
