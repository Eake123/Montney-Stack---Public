"""Westcoast scheduled volumes (PLAN 6A): the OA CSV parse, GJ → MMcf/d at 40 GJ/10³m³, signs and the derived series."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from montney.connectors import gas_westcoast_oa as wo
from montney.connectors.gas_common import FormatDrift

CSV = """Gas Date: 2026-11-02  Cycle: LATE_2026-11-02_0901
Station Name,Cap,Nom,Cap2
Aitken Creek Lateral,721615,414481,
26" Alberta West,404119,-161753,
Sunset Creek Interconnect,1813361,-633980,
Station 2 South,1858306,1858306,
Huntingdon Delivery Area,2040040,1799819,
26" Alberta East,419584,211598,
Fort Nelson to Stn 2,2625401,1528445,
Fort St John to Stn 2,1375286,-443167,
"""


def test_parse_and_units():
    day, cycle, rows = wo.parse_csv(CSV)
    assert day == date(2026, 11, 2) and cycle == "LATE_2026-11-02_0901"
    assert rows["Station 2 South"] == (1858306.0, 1858306.0)
    # Westcoast's own ratio: 1,858,306 GJ ÷ 40 = 46,458 10³m³ = 1,640.6 MMcf/d (its notice capacity)
    assert wo.mmcfd(1858306.0) == pytest.approx(1640.64, abs=0.01)
    assert wo.cycle_time(cycle) == datetime(2026, 11, 2, 15, 1)            # 09:01 Alberta (UTC-6) → UTC
    with pytest.raises(FormatDrift, match="Station Name"):
        wo.parse_csv(CSV.replace("Station Name,Cap,Nom", "Point,Capacity,Scheduled"))


def test_derived_series_from_a_run(monkeypatch):
    from montney.connectors import build, load_specs
    from montney.connectors.base import Context
    conn = build(load_specs(include_disabled=True)["gas_westcoast_oa"])
    monkeypatch.setattr(wo, "local_today", lambda: date(2026, 11, 2))
    conn._day = lambda ctx, d, today: CSV if d == today else None       # yesterday unposted; today is the CSV above
    res = conn.run(Context(client=None, now=datetime(2026, 11, 2, 18)))
    pts = {o.series_id: o.value for o in res.obs}
    assert pts["westcoast.oa.sunset_creek.to_ngtl"] == pytest.approx(wo.mmcfd(633980))
    assert pts["westcoast.oa.tnorth.out"] == pytest.approx(wo.mmcfd(1858306 + 633980 + 211598))
    assert pts["westcoast.oa.fort_st_john.scheduled"] < 0                  # eastbound, as posted


def _run_aitken(monkeypatch, stored):
    from montney.connectors import build, load_specs
    from montney.connectors.base import Context
    conn = build(load_specs(include_disabled=True)["gas_westcoast_oa"])
    monkeypatch.setattr(wo, "local_today", lambda: date(2026, 11, 2))
    conn._day = lambda ctx, d, today: CSV if d == today else None
    conn.stored = lambda ids, lo, hi: stored
    return conn.run(Context(client=None, now=datetime(2026, 11, 2, 18)))


def _month(y, m, n, v):
    from datetime import timedelta
    return {datetime(y, m, 1) + timedelta(days=i): v for i in range(n)}


def test_aitken_uses_the_same_month_last_year(monkeypatch):
    """6A (Reviewer on #254): P from Nov 2025, band = max(164, |P(Nov 2025) − P(Sep 2026)|)."""
    lateral = {**_month(2025, 11, 30, 268.0), **_month(2026, 9, 30, 150.0)}
    res = _run_aitken(monkeypatch, {wo.AITKEN_NET: {datetime(2025, 11, 1): 182.0, datetime(2026, 9, 1): -228.0},
                                    wo.LATERAL: lateral})
    pts = {o.series_id: o.value for o in res.obs}
    p_nov, p_sep = 268.0 - 182.0, 150.0 + 228.0
    storage = wo.mmcfd(414481) - p_nov
    assert pts["westcoast.oa.aitken_storage_est"] == pytest.approx(storage)
    assert pts["westcoast.oa.tnorth.production_est"] == pytest.approx(pts["westcoast.oa.tnorth.out"] - storage)
    assert pts["westcoast.oa.aitken_storage_band"] == pytest.approx(max(164.0, abs(p_nov - p_sep)))


def test_aitken_without_a_same_month_anchor_is_no_result(monkeypatch):
    res = _run_aitken(monkeypatch, {wo.AITKEN_NET: {datetime(2026, 9, 1): -228.0},
                                    wo.LATERAL: _month(2026, 9, 30, 150.0)})
    assert not any(o.series_id == "westcoast.oa.aitken_storage_est" for o in res.obs)
    assert any("NO-RESULT Aitken storage" in w and "no same-month anchor" in w for w in res.warnings)
    assert any(o.series_id == "westcoast.oa.tnorth.out" for o in res.obs)        # the measured number still writes


def test_bcer_aitken_net_is_well_production_minus_injection():
    from montney.connectors import gas_bcer_production as bp
    hdr = ("Facilityid,Px Facility Id,Organizationname,Fac Name,Legal Description,Facility Type,Prod Period,"
           "Ss Pgr Patr Productid,Flaredgas,Ventedgas,Fuelgas,Deliverablevolumes,Productionvolumes,Receiptvolumes")
    rows = [hdr, "00000160,BCBT0000160,Aitken Creek Gas Storage Ltd.,A,x,Battery,January   2026,GAS,,,,100,31000,",
            "00000160,BCIF0000160,Aitken Creek Gas Storage Ltd.,A,x,Injection Facility,January   2026,GAS,,,,,,6200",
            "00026827,BCIF0026827,Aitken Creek Gas Storage Ltd.,A,x,Injection Facility,January   2026,GAS,,,,,,3100"]
    net = bp.aitken_net("\n".join(rows))
    assert net[datetime(2026, 1, 1)] == pytest.approx((31000 - 6200 - 3100) * 0.0353147 / 31)
