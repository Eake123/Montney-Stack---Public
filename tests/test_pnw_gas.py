"""Pacific Northwest gas (L-085): NWP's OA report, Jackson Prairie inventory (GWT), GTN's historical OA, and the derived
Lower Mainland demand. Payload shapes copy the pipelines' (NWP's HTML table and header, GTN's JSON, the GWT-RPC
`//OK[…]` line); the locations' volumes are made up.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import pnw_gas as pg
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 3, 18, 0)           # 12:00 Central: after the realignment recheck hour


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """The NWP state (<home>/cache): each test starts with none, and never touches the user's data dir."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))


def nwp_page(day="11/03/2026", cycle="Evening Cycle 3", post="11/02/2026 8:32 PM CST", rows=None, basis="MMBtu"):
    rows = rows if rows is not None else [
        ("SUMAS RECEIPT", "Receipt", 1_400_000, 1_037_000), ("SUMAS DELIVERY", "Delivery", 0, 10_370),
        ("JACKSON PRAIRIE RECEIPT", "Storage Withdrawal", 1_300_000, 20_740),
        ("JACKSON PRAIRIE DELIVERY", "Storage Injection", 815_000, 124_440),
        ("STANFIELD RECEIPT", "Receipt", 692_920, 362_950), ("STANFIELD DELIVERY", "Delivery", 244_560, 31_110),
        ("SUMAS COMPRESSOR NET (DECREASING)", "Mainline", 1_400_000, 999_999)]
    tr = "".join(f"<tr><td>1</td><td>{n}</td><td>2</td><td>Loc</td><td>{f}</td><td>Q</td><td>0</td>"
                 f"<td>{c:,}</td><td>{q:,}</td><td>0</td><td></td></tr>" for n, f, c, q in rows)
    return (f"<table><tr><td>Cycle:</td><td>{cycle}</td></tr><tr><td>Post Date/Time:</td><td>{post}</td>"
            f"<td>Eff Gas Day/Time:</td><td>{day} 10:00 AM CST</td></tr><tr><td>Meas Basis Desc:</td><td>{basis}</td>"
            "</tr><tr><th>Loc Prop</th><th>Loc Name</th><th>Loc</th><th>Loc Purp Desc</th><th>Flow Ind Desc</th>"
            "<th>Loc/QTI</th><th>Design Cap</th><th>Operating Capacity</th><th>Total Scheduled Qty</th>"
            f"<th>Operationally Available Capacity</th><th>IT</th></tr>{tr}</table>")


GWT = ('//OK[27,0,26,19,22854942,25584002,0,0,9,9,-1169,0,6569027,8528002,18,1421968,1382819,1,'
       '["com.williams.gwt.client.DorPortalData/149936662","11/03/2026","10/31/2026","11/04/2026","11/01/2026"],0,7]')


def full_window(ids, start, end):
    """A store that already has the heal series for every day: no back-fill."""
    days = {start + timedelta(days=i): 1.0 for i in range((end - start).days + 1)}
    return {i: dict(days) for i in ids}


def run_nwp(pages: dict, stored: dict | None = None, gwt: str = GWT, now: datetime = NOW, seen: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        if req.method == "POST":
            return httpx.Response(200, text=gwt)
        return httpx.Response(200, text=pages[req.url.params.get("StartGasFlowDate")])
    conn = build(load_specs(include_disabled=True)["gas_nwp"])
    store = dict(stored or {})
    if pg.NWP_HEAL_SERIES not in store:          # default: the balance window already has Pegram (no back-fill)
        store[pg.NWP_HEAL_SERIES] = full_window([pg.NWP_HEAL_SERIES], now - timedelta(days=30), now)[pg.NWP_HEAL_SERIES]
    conn.stored = lambda ids, lo, hi: {i: v for i, v in store.items() if i in ids}
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now))


def test_nwp_sumas_storage_stanfield_and_the_lower_mainland():
    pages = {None: nwp_page(), "11-02-2026": nwp_page("11/02/2026", "Storage Realignment", "11/03/2026 10:08 AM CST")}
    seen: list = []
    t2, t3 = datetime(2026, 11, 2), datetime(2026, 11, 3)
    res = run_nwp(pages, {pg.HUNTINGDON: {t3: 1_600.0}}, seen=seen)
    v = {(o.series_id, o.ts): o for o in res.obs}
    assert v[("nwp.sumas.receipts", t3)].value == pytest.approx(1_000 - 10)           # 1,037 MMBtu = 1 MMcf
    assert v[("nwp.jackson_prairie.net_withdrawal", t3)].value == pytest.approx(20 - 120)   # an injection
    assert v[("nwp.stanfield.from_gtn", t3)].value == pytest.approx(350 - 30)
    assert v[("nwp.sumas.receipts", t3)].as_of == datetime(2026, 11, 3, 2, 32)        # CST posting -> UTC
    assert v[(pg.LM, t3)].value == pytest.approx(1_600 - 990 - 89.0)                 # November's bias
    assert pg.LM_BIAS_MONTH.get(8, pg.LM_BIAS) == 81.0                                 # no back-test month: overall
    assert (pg.LM, t2) not in v and any("2026-11-02: no Westcoast Huntingdon" in w for w in res.warnings)
    inv = v[("nwp.jackson_prairie.inventory", datetime(2026, 11, 1))]
    assert inv.value == pytest.approx(22_854_942 / 1_037_000) and inv.as_of == inv.ts
    assert sum("StartGasFlowDate" in u for u in seen) == 1 and sum("DorPortal" in u for u in seen) == 1
    seen.clear()
    run_nwp(pages, seen=seen)                     # final day stored, inventory read today: one request only
    assert len(seen) == 1 and "StartGasFlowDate" not in seen[0]


def test_nwp_waits_for_the_realignment_cycle():
    pages = {None: nwp_page(), "11-02-2026": nwp_page("11/02/2026", "Intraday 3", "11/02/2026 9:34 PM CST")}
    seen: list = []
    run_nwp(pages, seen=seen)
    run_nwp(pages, seen=seen)                     # ID3 isn't final: yesterday is read again
    assert sum("StartGasFlowDate" in u for u in seen) == 2
    seen.clear()
    run_nwp(pages, now=datetime(2026, 11, 3, 14), seen=seen)   # 08:00 Central: before the recheck hour
    assert not any("StartGasFlowDate" in u for u in seen)


def test_nwp_inventory_failure_is_a_warning_and_drift_is_loud():
    res = run_nwp({None: nwp_page()}, gwt="//EX[2,1,[\"IncompatibleRemoteServiceException\"],0,7]",
                  now=datetime(2026, 11, 3, 14))
    assert any("Jackson Prairie inventory not read" in w for w in res.warnings)
    assert any(o.series_id == "nwp.sumas.receipts" for o in res.obs)
    with pytest.raises(FormatDrift, match="MMBtu"):
        run_nwp({None: nwp_page(basis="Dth")})
    with pytest.raises(FormatDrift, match="SUMAS RECEIPT"):
        run_nwp({None: nwp_page(rows=[("STANFIELD RECEIPT", "Receipt", 1, 1)])})


def test_jp_inventory_needs_exactly_one_facility_pair():
    assert pg.jp_inventory(GWT, date(2026, 11, 3)) == (date(2026, 11, 1), 22_854_942.0, 25_584_002.0)
    with pytest.raises(ValueError, match="found 0"):
        pg.jp_inventory(GWT.replace("25584002", "5584002"), date(2026, 11, 3))


def gtn_rows(day, cycle, posted, pts):
    return [{"GasDay": f"{day} 9:00 AM CCT", "PostedDateTime": posted, "CycleName": cycle, "LocationName": n,
             "LocationID": "1", "LocationPurposeDescription": "x", "LocQti": q, "FlowIndicatorDescription": "D",
             "IT": "", "DesignCapacity": "0", "OperatingCapacity": f"{c:,}", "TotalScheduledQuantity": f"{s:,}",
             "OperationallyAvailableCapacity": "0", "AllQtyAvailable": "Y"} for n, q, c, s in pts]


PTS = [("KINGSGATE", "RPQ", 3_094_000, 2_799_900), ("Flow Past Kingsgate", "SGQ", 2_903_600, 2_799_900),
       ("SPOKANE NPC", "DPQ", 311_508, 155_550), ("HERMISTON GENERATING", "DPQ", 150_000, 51_850),
       ("STANFIELD DELIVERY", "DPQ", 706_085, 362_950), ("STANFIELD RECEIPT", "RPQ", 249_207, 31_110),
       ("TUSCARORA/GTN INTERCONNECT", "DPQ", 249_419, 155_550), ("MALIN", "DPQ", 2_315_545, 1_762_900),
       ("PALOUSE", "DPQ", 30_000, 20_740), ("TURQUOISE FLATS METER STATION", "RPQ", 200_000, 103_700),
       ("RIDGE ROAD RECEIPT", "RPQ", 10_000, 5_185), ("THREEMILE RD DAIRY RECEIPT", "RPQ", 3_000, 2_074)]


def run_gtn(content, total=None, stored=full_window, now=NOW, seen=None):
    body = {"data": {"Cycle": "All", "MeasurementBasis": "Million BTU's", "Content": content},
            "total": len(content) if total is None else total}
    conn = build(load_specs(include_disabled=True)["gas_gtn"])
    conn.stored = stored

    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(200, text=json.dumps(body))
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now))


def test_gtn_latest_cycle_per_day():
    later = [(n, q, c, s * 2 if n == "MALIN" else s) for n, q, c, s in PTS]
    content = (gtn_rows("11/02/2026", "ID3", "11/2/2026 9:34:53 PM", later)
               + gtn_rows("11/02/2026", "TIM", "11/1/2026 4:33:50 PM", PTS)
               + gtn_rows("11/03/2026", "EVE", "11/2/2026 8:34:11 PM", PTS))
    res = run_gtn(content)
    v = {(o.series_id, o.ts): o for o in res.obs}
    t2, t3 = datetime(2026, 11, 2), datetime(2026, 11, 3)
    assert v[("gtn.malin.deliveries", t2)].value == pytest.approx(3_400)               # ID3 beats TIM
    assert v[("gtn.malin.deliveries", t2)].as_of == datetime(2026, 11, 3, 3, 34, 53)
    assert v[("gtn.malin.deliveries", t3)].value == pytest.approx(1_700)
    assert v[("gtn.kingsgate.receipts", t3)].value == pytest.approx(2_700)
    assert v[("gtn.kingsgate.capacity", t3)].value == pytest.approx(2_800)             # Flow Past Kingsgate
    assert v[("gtn.stanfield.to_nwp", t3)].value == pytest.approx(350 - 30)
    assert v[("gtn.tuscarora.deliveries", t3)].value == pytest.approx(150)
    assert v[("gtn.local.deliveries", t3)].value == pytest.approx(50)                  # Hermiston, not NWP's gas
    assert v[("gtn.nwp_interconnect.deliveries", t3)].value == pytest.approx(150 + 20)  # Spokane NPC + Palouse
    assert v[("gtn.receipts.ruby_turquoise", t3)].value == pytest.approx(100)
    assert v[("gtn.receipts.other", t3)].value == pytest.approx(5 + 2)                 # Ridge Road + RNG dairy
    received = 2_700 + 30 + 100 + 5 + 2
    delivered = 150 + 50 + 350 + 150 + 1_700 + 20
    assert v[("gtn.fuel_linepack", t3)].value == pytest.approx(received - delivered)


def test_gtn_fails_loudly():
    with pytest.raises(FormatDrift, match="rows"):
        run_gtn(gtn_rows("11/03/2026", "TIM", "11/2/2026 4:33:50 PM", PTS), total=999)
    with pytest.raises(FormatDrift, match="KINGSGATE or MALIN"):
        run_gtn(gtn_rows("11/03/2026", "TIM", "11/2/2026 4:33:50 PM", PTS[2:4]))
    for gone in ("PALOUSE", "TURQUOISE FLATS METER STATION"):      # renamed: local high / imports low, never silent
        with pytest.raises(FormatDrift, match=gone):
            run_gtn(gtn_rows("11/03/2026", "TIM", "11/2/2026 4:33:50 PM", [p for p in PTS if p[0] != gone]))
    assert run_gtn([]).obs == []


# ---- L-086: the PacNW summary
def test_nwp_baker_net_is_signed_and_plymouth_lng_is_read():
    rows = [("SUMAS RECEIPT", "Receipt", 1, 1_037), ("PLYMOUTH LNG RECEIPT", "Storage Withdrawal", 1, 51_850),
            ("PLYMOUTH LNG DELIVERY", "Storage Injection", 1, 10_370),
            ("BAKER COMPRESSOR DECREASING", "Mainline", 1, 99_999),
            ("BAKER COMPRESSOR NET (DECREASING)", "Mainline", 446_000, 207_400)]
    v = pg.nwp_values(pg.nwp_report(nwp_page(rows=rows))[3])
    assert v["nwp.baker.net_north"] == pytest.approx(-200)              # net southbound: PacNW gas flowing east
    assert v["nwp.plymouth_lng.net_withdrawal"] == pytest.approx(50 - 10)
    rows[-1] = ("BAKER COMPRESSOR NET (INCREASING)", "Mainline", 446_000, 207_400)
    assert pg.nwp_values(pg.nwp_report(nwp_page(rows=rows))[3])["nwp.baker.net_north"] == pytest.approx(200)
    assert "nwp.baker.net_north" not in pg.nwp_values(pg.nwp_report(nwp_page())[3])   # no row: no value, not 0


def test_nwp_pegram_net_is_the_rockies_into_idaho_and_the_reno_lateral_is_read():
    rows = [("SUMAS RECEIPT", "Receipt", 1, 1_037), ("PEGRAM COMPRESSOR INCREASING", "Mainline", 1, 99_999),
            ("PEGRAM COMPRESSOR NET (INCREASING)", "Mainline", 600_000, 240_584),
            ("BAKER COMPRESSOR NET (DECREASING)", "Mainline", 446_000, 124_440),
            ("RENO LATERAL (TO PAIUTE)", "Delivery", 157_800, 57_035)]
    v = pg.nwp_values(pg.nwp_report(nwp_page(rows=rows))[3])
    assert v["nwp.pegram.net_north"] == pytest.approx(232)             # northbound: into Idaho from Wyoming
    assert v["nwp.baker.net_north"] == pytest.approx(-120)             # both nets read from the same page
    assert v["nwp.reno_lateral.deliveries"] == pytest.approx(55)
    rows[2] = ("PEGRAM COMPRESSOR NET (DECREASING)", "Mainline", 600_000, 10_370)
    assert pg.nwp_values(pg.nwp_report(nwp_page(rows=rows))[3])["nwp.pegram.net_north"] == pytest.approx(-10)
    none = pg.nwp_values(pg.nwp_report(nwp_page())[3])
    assert "nwp.pegram.net_north" not in none and "nwp.reno_lateral.deliveries" not in none     # no row: no value
    ids = {m.series_id for m in build(load_specs(include_disabled=True)["gas_nwp"]).declared_series()}
    assert {"nwp.pegram.net_north", "nwp.reno_lateral.deliveries"} <= ids


EIA = ("<table><tr><td class='B4'>&nbsp;&nbsp;2025</td>" + "".join(f"<td class='B3'>{v:,}</td>" for v in range(
    12_000, 24_000, 1_000)) + "</tr><tr><td class='B4'>&nbsp;&nbsp;2026</td><td class='B3'>15,170</td>"
       "<td class='B3'>NA</td>" + "<td class='B3'></td>" * 10
       + "</tr></table><td class='F2'>Release Date: 10/30/2026</td>")


def test_eia_monthly_history_page():
    vals, rel = pg.eia_monthly(EIA)
    assert vals[date(2026, 1, 1)] == 15_170 and date(2026, 2, 1) not in vals and len(vals) == 13
    assert rel == datetime(2026, 10, 30)


def run_balance(stored):
    conn = build(load_specs(include_disabled=True)["gas_pnw_balance"])
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    return conn.run(Context(client=None, now=NOW))


def test_pnw_imports_demand_and_storage():
    t, t2 = datetime(2026, 11, 2), datetime(2026, 11, 3)
    one = {"gtn.kingsgate.receipts": 2_800.0, "nwp.sumas.receipts": 970.0, "nwp.baker.net_north": -150.0,
           "gtn.malin.deliveries": 1_700.0, "gtn.tuscarora.deliveries": 150.0,
           "nwp.jackson_prairie.net_withdrawal": 30.0, "nwp.plymouth_lng.net_withdrawal": 0.0,
           "gtn.receipts.ruby_turquoise": 107.0, "gtn.receipts.other": 8.0}
    stored = {k: {t: v} for k, v in one.items()}
    stored["gtn.local.deliveries"] = {t: 650.0}
    stored["nwp.sumas.receipts"][t2] = 1_000.0                                   # t2 has Sumas only
    stored["nwp.jackson_prairie.inventory"] = {datetime(2026, 11, 1): 22.0}
    stored["eia.or.storage.working_gas"] = {datetime(2026, 7, 1): 19.9, datetime(2026, 8, 1): 18.9}
    res = run_balance(stored)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    imp = 2_800 + 970 - 150 + 107 + 8                                           # + GTN's Turquoise Flats / other
    assert v[("pnw.imports.total", t)] == pytest.approx(imp)
    assert v[("pnw.exports.total", t)] == pytest.approx(1_850)
    assert v[("pnw.demand_est", t)] == pytest.approx(imp - 1_850 + 30)
    assert v[("pnw.demand.nwp_est", t)] == pytest.approx(imp - 1_850 + 30 - 650)     # parts sum to the total
    assert v[("pnw.storage.inventory_est", datetime(2026, 11, 1))] == pytest.approx(22.0 + 18.9)   # latest Mist month
    assert ("pnw.demand_est", t2) not in v and any("2026-11-03: no gtn.kingsgate" in w for w in res.warnings)


def test_bc_implied_demand_is_station2_south_less_the_sumas_export():
    """L-089: BC demand off T-South = Westcoast Station 2 South scheduled − NWP Sumas receipts (±110 vs StatCan)."""
    t2, t3 = datetime(2026, 11, 2), datetime(2026, 11, 3)
    pages = {None: nwp_page(), "11-02-2026": nwp_page("11/02/2026", "Storage Realignment", "11/03/2026 10:08 AM CST")}
    res = run_nwp(pages, {pg.S2S: {t3: 1_641.0}, pg.HUNTINGDON: {t3: 1_600.0}})
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v[(pg.BC_DEMAND, t3)] == pytest.approx(1_641 - 990)
    assert (pg.BC_DEMAND, t2) not in v and any("2026-11-02: no Westcoast Station 2 South" in w for w in res.warnings)
    meta = {m.series_id: m for m in build(load_specs(include_disabled=True)["gas_nwp"]).declared_series()}
    assert meta[pg.BC_DEMAND].extra["band_abs"] == 110.0 and "demand" in pg.BC_DEMAND      # reads negative by its id


def test_gtn_backfills_missing_window_days_once():
    """Orchestrator after #524: days in the balance window without the new GTN series are re-read on the first run
    after deploy, in the same single (range) request; once stored, the next run is back to yesterday + today."""
    store: dict = {pg.GTN_HEAL_SERIES: {}}

    def stored(ids, start, end):
        return {i: {t: v for t, v in store.get(i, {}).items() if start <= t <= end} for i in ids}

    content = gtn_rows("11/03/2026", "EVE", "11/2/2026 8:34:11 PM", PTS)
    seen: list = []
    res = run_gtn(content, stored=stored, seen=seen)
    today = NOW.replace(tzinfo=ZoneInfo("UTC")).astimezone(pg.CENTRAL).date()
    assert len(seen) == 1                                                       # one request, a wider range
    assert seen[0].url.params["GasDay"] == (today - timedelta(days=pg.GTN_HEAL_DAYS)).strftime("%m/%d/%Y")
    assert any("back-filling" in w for w in res.warnings)
    for i in range(2, pg.GTN_HEAL_DAYS + 1):                                   # the run stored the window
        d = today - timedelta(days=i)
        store[pg.GTN_HEAL_SERIES][datetime(d.year, d.month, d.day)] = 60.0
    seen.clear()
    res = run_gtn(content, stored=stored, seen=seen)
    assert seen[0].url.params["GasDay"] == (today - timedelta(days=1)).strftime("%m/%d/%Y")   # back to normal
    assert not any("back-filling" in w for w in res.warnings)
    store[pg.GTN_HEAL_SERIES].pop(datetime(*(today - timedelta(days=4)).timetuple()[:3]))
    seen.clear()
    run_gtn(content, stored=stored, seen=seen)                                  # a hole later: from that day only
    assert seen[0].url.params["GasDay"] == (today - timedelta(days=4)).strftime("%m/%d/%Y")


def test_gtn_unreadable_store_is_the_usual_two_days():
    def broken(ids, start, end):
        raise RuntimeError("no server")
    seen: list = []
    res = run_gtn(gtn_rows("11/03/2026", "EVE", "11/2/2026 8:34:11 PM", PTS), stored=broken, seen=seen)
    today = NOW.replace(tzinfo=ZoneInfo("UTC")).astimezone(pg.CENTRAL).date()
    assert seen[0].url.params["GasDay"] == (today - timedelta(days=1)).strftime("%m/%d/%Y")
    assert any("no back-fill check" in w for w in res.warnings)


def test_nwp_backfills_missing_pegram_days_once():
    """#524 added Pegram / the Reno lateral to gas_nwp: window days without them are re-read on the first run after
    deploy, one report per day; once stored, the next run is back to the usual request."""
    now_c = NOW.replace(tzinfo=ZoneInfo("UTC")).astimezone(pg.CENTRAL).date()
    want = [now_c - timedelta(days=i) for i in range(pg.NWP_HEAL_DAYS, 1, -1)]
    pages = {None: nwp_page(), "11-02-2026": nwp_page("11/02/2026", "Storage Realignment", "11/03/2026 10:08 AM CST")}
    pages.update({d.strftime("%m-%d-%Y"): nwp_page(d.strftime("%m/%d/%Y"), "Storage Realignment",
                                                   f"{d.strftime('%m/%d/%Y')} 10:08 AM CST") for d in want})
    seen: list = []
    res = run_nwp(pages, {pg.NWP_HEAL_SERIES: {}}, seen=seen)
    asked = [u for u in seen if "StartGasFlowDate" in u]
    assert any("back-filling" in w for w in res.warnings)
    assert len(asked) >= len(want) - 2 and len(asked) <= pg.NWP_HEAL_DAYS          # one per missing day, capped
    seen.clear()
    full = {pg.NWP_HEAL_SERIES: {datetime(d.year, d.month, d.day): 150.0 for d in want}}
    res = run_nwp(pages, full, seen=seen)
    assert not any("back-filling" in w for w in res.warnings)
    assert not any(any(d.strftime("%m-%d-%Y") in u for d in want[:-2]) for u in seen)   # no re-fetch of the window
