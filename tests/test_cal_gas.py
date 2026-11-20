"""California gas (L-090): PG&E's Pipe Ranger supplydemand JSON, SoCalGas's ENVOY Daily Operations, and the totals.
Payload shapes copy the utilities' (PG&E's PlanData records, ENVOY's HTML table); the volumes are made up.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 3, 21, 0)


def plan(date, which, **kw):
    base = {"Date": date, "whichDay": which, "System_Demand": "2,100", "Core": 650, "NonCore_Elec_Gen": 830,
            "NonCore_Industrial": 620, "PgtNW": "1,600", "Ruby": 300, "Epng": 400, "Tw": 90, "Krgt": 10,
            "Southern_Trails": 30, "KRS_SoCal": 100, "Calif": 20, "Injection": 50, "Withdrawal": 10,
            "Bal_Injection": 100, "Bal_Withdrawal": 0, "WG_Net_Wd": 300, "WG_Net_Inj": 0, "Lodi_Net_Wd": 0,
            "Lodi_Net_Inj": 100,
            "GRS_LLC_WD": 0, "GRS_LLC_Inj": 150, "CVGS_WD_Sched": 40, "CVGS_Inj_Sched": 240,
            "PGE_Working_Gas_in_Storage": "40,800", "Additional_Data": {"Other_Info": {"Posted_Date": "11/03/2026",
                                                                                      "Posted_Time": "07:53 AM"}}}
    return {"PlanData": {**base, **kw}}


PGE = [plan("11/1/2026", "Yesterday-1 (F)"), plan("11/2/2026", "Yesterday (F)", PGE_Working_Gas_in_Storage="40,900"),
       plan("11/3/2026", "Today (2)", PGE_Working_Gas_in_Storage=0), plan("11/4/2026", "Today+1 (2)")]


def run_pge(body):
    conn = build(load_specs(include_disabled=True)["gas_pge"])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=body))), now=NOW))


def test_pge_demand_imports_storage():
    res = run_pge(json.dumps(PGE))
    v = {(o.series_id, o.ts): o for o in res.obs}
    t2 = datetime(2026, 11, 2)
    assert v[("pge.demand.total", t2)].value == 2_100 and v[("pge.demand.core", t2)].value == 650
    assert v[("pge.imports.north", t2)].value == 1_900                                   # GTN + Ruby at Malin
    assert v[("pge.imports.south", t2)].value == 400 + 90 + 10 + 30                      # not KRS_SoCal (in-state)
    assert v[("pge.receipts.from_socal", t2)].value == 100
    assert v[("pge.storage.net_withdrawal", t2)].value == 10 - 50 - 100                  # balancing injection counts
    assert v[("pge.storage.independent_net_withdrawal", t2)].value == 300 - 100 - 150 + 40 - 240
    # opening inventory = the previous day's close; today's plan (0) and forecasts aren't stored
    assert v[("pge.storage.working_gas", datetime(2026, 11, 1))].value == pytest.approx(40.9)
    assert v[("pge.storage.working_gas", datetime(2026, 10, 31))].value == pytest.approx(40.8)
    assert ("pge.storage.working_gas", t2) not in v
    assert not any(o.ts == datetime(2026, 11, 4) for o in res.obs)                        # Today+1: a forecast
    assert v[("pge.demand.total", t2)].as_of == datetime(2026, 11, 3, 15, 53)             # 07:53 PT -> UTC


def test_pge_fails_loudly():
    with pytest.raises(FormatDrift, match="JSON"):
        run_pge("<html>maintenance</html>")
    with pytest.raises(FormatDrift, match="missing"):
        run_pge(json.dumps([{"PlanData": {"Date": "11/2/2026"}}]))


def scg_page(rows=None, header=("Actual (Dth) 11/02/2026", "Estimate (Dth) 11/03/2026")):
    rows = rows if rows is not None else [
        ("CP - Line 85", "51,850", "51,850"), ("Elk Hills - Wheeler Ridge", "20,740", "20,740"),
        ("El Paso - Ehrenberg", "518,500", "518,500"), ("Transwestern - North Needles", "622,200", "622,200"),
        ("Kern River/Mojave - Kramer Junction", "373,320", "373,320"),
        ("Kern River/Mojave - Wheeler Ridge", "435,540", "435,540"), ("TGN - Otay Mesa", "0", "0"),
        ("PG & E - Wheeler Ridge", "103,700", "51,850"), ("Total Receipts", "2,125,850", "2,074,000"),
        ("System Sendout", "2,177,700", "2,074,000"), ("Net Injections(Withdrawals)", "(51,850)", "0"),
        ("Injection Capacity", "", "224,000"), ("Ending Storage Balance (MCF)", "104,030,000", "104,030,000")]
    tr = "".join(f"<tr><td>&nbsp;{n}&nbsp;</td><td>&nbsp;{a}&nbsp;</td><td>&nbsp;{e}&nbsp;</td><td>1</td><td>1</td>"
                 f"<td>1</td></tr>" for n, a, e in rows)
    return (f'<div class="updated py-2">Last updated: 11/03/2026 12:16 PM PCT</div><table><tr><th></th>'
            f"<th>{header[0]}</th><th>{header[1]}</th><th>Forecast (Dth) 11/04/2026</th><th>F</th><th>F</th></tr>"
            f"{tr}</table>")


def run_scg(body):
    conn = build(load_specs(include_disabled=True)["gas_socalgas"])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=body))), now=NOW))


def test_socalgas_sendout_imports_storage():
    res = run_scg(scg_page())
    v = {(o.series_id, o.ts): o for o in res.obs}
    t2 = datetime(2026, 11, 2)
    assert v[("socal.demand.sendout", t2)].value == pytest.approx(2_100)               # 2,177,700 Dth / 1,037
    assert v[("socal.imports.south", t2)].value == pytest.approx(500 + 600 + 360 + 420)
    assert v[("socal.imports.ehrenberg", t2)].value == pytest.approx(500)
    assert v[("socal.receipts.california", t2)].value == pytest.approx(50 + 20)        # CP lines + Elk Hills
    assert v[("socal.receipts.from_pge", t2)].value == pytest.approx(100)              # a transfer, not an import
    assert v[("socal.storage.net_withdrawal", t2)].value == pytest.approx(50)          # (51,850) injected -> +50
    assert v[("socal.storage.inventory", t2)].value == pytest.approx(104.03)
    assert v[("socal.demand.sendout", t2)].as_of == datetime(2026, 11, 3, 20, 16)       # 12:16 PST -> UTC
    assert {o.ts for o in res.obs} == {t2, datetime(2026, 11, 3)}                        # no forecast days


def test_socalgas_fails_loudly():
    with pytest.raises(FormatDrift, match="header"):
        run_scg("<table><tr><td>moved</td></tr></table>")
    with pytest.raises(FormatDrift, match="System Sendout"):
        run_scg(scg_page(rows=[("El Paso - Ehrenberg", "1", "1")]))


def test_california_totals():
    t = datetime(2026, 11, 2)
    stored = {"pge.demand.total": {t: 2_139.0}, "socal.demand.sendout": {t: 2_049.0},
              "pge.receipts.from_socal": {t: 93.0},
              "pge.imports.north": {t: 1_809.0}, "pge.imports.south": {t: 564.0}, "socal.imports.south": {t: 1_793.0},
              "pge.storage.net_withdrawal": {t: -97.0}, "pge.storage.independent_net_withdrawal": {t: -186.0},
              "socal.storage.net_withdrawal": {t: 78.0}, "pge.storage.working_gas": {t: 40.9},
              "socal.storage.inventory": {t: 104.0}}
    conn = build(load_specs(include_disabled=True)["gas_cal_balance"])
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    v = {o.series_id: o.value for o in conn.run(Context(client=None, now=NOW)).obs}
    assert v["cal.demand.total"] == pytest.approx(2_139 + 2_049 - 93)       # KRS_SoCal is in both (Reviewer #348)
    assert v["cal.imports.north"] == 1_809 and v["cal.imports.south"] == pytest.approx(2_357)
    assert v["cal.imports.total"] == pytest.approx(4_166)
    assert v["cal.storage.net_withdrawal"] == pytest.approx(-97 - 186 + 78)
    assert v["cal.storage.inventory"] == pytest.approx(144.9)


def test_southern_import_points_sum_to_southern_imports():
    """L-090 map nodes: Topock/Needles + Ehrenberg/Blythe + Kramer + Wheeler Ridge + Otay = cal.imports.south."""
    from montney.connectors import cal_gas
    t = datetime(2026, 11, 2)
    parts = {s: {t: float(i + 1) * 10} for i, s in enumerate(sorted(
        {p for k, v in cal_gas.CAL_PARTS.items() if k.startswith("cal.imports.") for p in v}))}
    parts["pge.imports.south"] = {t: sum(parts[f"pge.imports.{s}"][t] for s in cal_gas.PGE_SOUTH.values())}
    parts["socal.imports.south"] = {t: sum(parts[f"socal.imports.{s}"][t] for s in cal_gas.SCG_IMPORTS.values())}
    conn = build(load_specs(include_disabled=True)["gas_cal_balance"])
    conn.stored = lambda ids, lo, hi: {i: v for i, v in parts.items() if i in ids}
    v = {o.series_id: o.value for o in conn.run(Context(client=None, now=NOW)).obs}
    points = ("topock", "ehrenberg", "kramer", "wheeler_ridge", "otay_mesa")
    assert sum(v[f"cal.imports.{p}"] for p in points) == pytest.approx(v["cal.imports.south"])


def test_kern_mojave_into_california_sums_its_three_points():
    """L-094: the Kern River line's value = Kramer (SoCalGas) + Daggett (PG&E) + Wheeler Ridge."""
    t = datetime(2026, 11, 2)
    stored = {"socal.imports.kramer_junction": {t: 370.0}, "pge.imports.kern_river": {t: 9.0},
              "socal.imports.wheeler_ridge": {t: 420.0}}
    conn = build(load_specs(include_disabled=True)["gas_cal_balance"])
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    v = {o.series_id: o.value for o in conn.run(Context(client=None, now=NOW)).obs}
    assert v["cal.imports.kern_mojave"] == pytest.approx(799)
