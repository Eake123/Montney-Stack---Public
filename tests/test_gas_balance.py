"""AB + BC gas balance vs storage (L-079): the identity's arithmetic, signs, bands and the no-guess rule.

Values are made up; the stored inputs are injected.
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_balance as gb
from montney.connectors.base import Context

T = datetime(2026, 11, 1)
DAY = {gb.P: 20_000.0, gb.RAB: 1_100.0, gb.RBC: -250.0, gb.E: 11_000.0, gb.L: 1_500.0, gb.D: 7_100.0,
       gb.NS: 400.0, gb.A: 270.0}


def run(stored: dict):
    conn = build(load_specs(include_disabled=True)["gas_balance"])
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    return conn.run(Context(client=None, now=datetime(2026, 11, 2, 18)))


def test_identity_signs_and_bands():
    res = run({**{k: {T: v} for k, v in DAY.items()}, gb.AB: {T: 293.0}})
    v = {o.series_id.removeprefix(gb.PFX): o.value for o in res.obs}
    assert v["exports_est"] == pytest.approx(11_000 + 1_500)                           # pipelines + LNG (Erik)
    assert v["implied_storage_est"] == pytest.approx(11_000 + 1_500 + 7_100 - 20_000)   # −400: a surplus = injection
    assert v["storage_actual"] == pytest.approx(670.0)                                  # + = withdrawal
    assert v["residual_est"] == pytest.approx(670 + 400)                                # actual − implied
    assert v["residual_est"] == pytest.approx(20_000 - 11_000 - 1_500 - 7_100 + 400 + 270)
    assert v["residual_metered_est"] == pytest.approx(1_070 - 1_100 + 250)              # remainders out
    assert v["band_est"] == pytest.approx(math.sqrt(800 ** 2 + 166 ** 2 + 293 ** 2))
    assert v["metered_band_est"] == pytest.approx(math.sqrt(300 ** 2 + 293 ** 2))
    assert {o.ts for o in res.obs} == {T} and all(w.startswith("unaccounted") for w in res.warnings)


def test_aitken_band_defaults_and_missing_inputs_give_no_day():
    v = gb.balance(DAY)
    assert v[gb.PFX + "metered_band_est"] == pytest.approx(math.sqrt(300 ** 2 + 164 ** 2))
    stored = {k: {T: x} for k, x in DAY.items() if k != gb.A}                           # no Aitken estimate
    res = run(stored)
    assert res.obs == [] and any("NO-RESULT" in w and gb.A in w for w in res.warnings)
    stored[gb.A] = {datetime(2026, 10, 31): 100.0}                                      # a different day only
    res = run(stored)
    assert res.obs == [] and any("NO-RESULT" in w for w in res.warnings)


def _days(month: datetime, value: float, n: int | None = None) -> dict:
    import calendar
    last = calendar.monthrange(month.year, month.month)[1]
    return {month.replace(day=d): value for d in range(1, (n or last) + 1)}


def run_monthly(stored: dict):
    conn = build(load_specs(include_disabled=True)["gas_balance_monthly"])
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    return conn.run(Context(client=None, now=datetime(2026, 11, 2, 18)))


def test_monthly_tie_out_with_fallbacks():
    """L-079 history: per regulator month; Alliance and T-South fall back to the CER key points; LNG Canada is 0
    before its first reported month; a month with < 90 % of its days is skipped."""
    jun, jul, aug = datetime(2024, 6, 1), datetime(2024, 7, 1), datetime(2024, 8, 1)
    stored = {"production.ab.monthly": {jun: 11_000.0, jul: 11_000.0, aug: 11_000.0},
              "production.bc.monthly": {jun: 7_000.0, jul: 7_000.0, aug: 7_000.0},
              gb.M_AIT: {jun: -300.0, jul: -200.0, aug: -100.0},
              gb.M_LNG: {aug: 50.0},                                                 # LNG Canada reports from Aug
              gb.M_DEMAND: {**_days(jun, 5_500.0), **_days(jul, 5_500.0, 20), **_days(aug, 5_500.0)},  # Jul: 20 d
              gb.M_NS: {**_days(jun, -1_000.0), **_days(jul, -1_000.0), **_days(aug, -1_000.0)}}
    for b in gb.M_BORDERS:
        stored[b] = {**_days(jun, 2_000.0), **_days(jul, 2_000.0), **_days(aug, 2_000.0)}
    stored[gb.M_ALLIANCE[1]] = {**_days(jun, 1_500.0), **_days(jul, 1_500.0), **_days(aug, 1_500.0)}   # CER border
    for c in gb.M_TSOUTH[1]:
        stored[c] = {**_days(jun, 400.0), **_days(jul, 400.0), **_days(aug, 400.0)}                  # CER points
    res = run_monthly(stored)
    v = {(o.series_id.removeprefix(gb.PFX), o.ts): o.value for o in res.obs}
    assert {o.ts for o in res.obs} == {jun, aug}                                     # Jul: demand only 20 of 31 days
    assert v[("exports_monthly", jun)] == pytest.approx(8_000 + 1_500 + 1_200 + 0)   # LNG 0 before Aug
    assert v[("exports_monthly", aug)] == pytest.approx(8_000 + 1_500 + 1_200 + 50)
    implied = 8_000 + 1_500 + 1_200 + 5_500 - 18_000                                 # = −1,800 (an injection)
    assert v[("implied_storage_monthly", jun)] == pytest.approx(implied)
    assert v[("storage_actual_monthly", jun)] == pytest.approx(-1_300)
    assert v[("residual_monthly", jun)] == pytest.approx(-1_300 - implied)
    assert any("CER key points" in w for w in res.warnings)
    stored[gb.M_TSOUTH[0]] = _days(jun, 500.0)                                       # Westcoast OA wins when stored
    stored[gb.M_ALLIANCE[0]] = _days(jun, 1_550.0)                                   # Alliance receipts too
    v = {(o.series_id.removeprefix(gb.PFX), o.ts): o.value for o in run_monthly(stored).obs}
    assert v[("exports_monthly", jun)] == pytest.approx(8_000 + 1_550 + 500)


def test_unaccounted_parts_sum_to_the_tie_out_error():
    """L-080 item 4: the tie-out error = NGTL basis (k − 1)·field + NGTL's GDSR closure + AB gas off NGTL (latest
    Petrinex month, by system) + BC remainder + unexplained, exactly."""
    import calendar
    sep = datetime(2026, 9, 1)
    days = [sep.replace(day=d) for d in range(1, calendar.monthrange(2026, 9)[1] + 1)] + [T]
    flat = lambda v: {t: v for t in days}                                              # noqa: E731
    stored = {**{k: {T: v} for k, v in DAY.items()}, gb.AB: {T: 293.0},
              gb.RD[0]: flat(15_000.0), gb.RD[1]: flat(300.0), gb.RD[2]: flat(100.0), gb.RD[3]: flat(200.0),   # field 14,400
              gb.GF: flat(14_000.0), gb.GGD: flat(100.0), gb.GGB: flat(200.0),
              **{g: flat(1_000.0) for g in gb.G_OUT}}
    stored.update({sid: {sep: 100.0} for sid in gb.SALES_TO.values()})                 # 7 systems × 100
    res = run(stored)
    v = {o.series_id: o.value for o in res.obs}
    k = 14_400 / 14_000
    assert v[gb.UA + "ngtl_basis_est"] == pytest.approx((k - 1) * 14_000)                # = 400
    assert v[gb.UA + "ngtl_closure"] == pytest.approx(14_000 + 300 + 400 - 7_100 - 5_000)
    assert v[gb.UA + "ab_offsystem_est"] == pytest.approx(700) and v[gb.UA + "gas_alberta_est"] == 100
    assert v[gb.UA + "bc_remainder_est"] == pytest.approx(-250)
    parts = sum(v[gb.UA + p] for p in ("ngtl_basis_est", "ngtl_closure", "ab_offsystem_est", "bc_remainder_est",
                                       "unexplained_est"))
    assert parts == pytest.approx(v[gb.PFX + "residual_est"])
    assert v[gb.UA + "unexplained_band_est"] == pytest.approx(math.sqrt(50 ** 2 + (0.4 * 700) ** 2 + 300 ** 2))
    meta = {m.series_id: m for m in build(load_specs(include_disabled=True)["gas_balance"]).declared_series()}
    assert all("±40%" in meta[gb.UA + f"{g}_est"].label for g in gb.OFF_GROUPS)       # Reviewer on #313


def test_cgl_direct_uses_one_vintage_on_both_sides():
    """Production's CGL direct (ngtl_split's last run) and LNG's (revised later) are the same gas: the check uses LNG's
    current value in production too, so a stale ngtl_split vintage doesn't land in the tie-out error (2026-11-02)."""
    base = {**{k: {T: v} for k, v in DAY.items()}, gb.AB: {T: 293.0}}
    r0 = {o.series_id: o.value for o in run(base).obs}
    res = run({**base, gb.CGL_P: {T: 1_445.0}, gb.CGL_L: {T: 1_201.0}})
    r1 = {o.series_id: o.value for o in res.obs}
    assert r1[gb.PFX + "residual_est"] == pytest.approx(r0[gb.PFX + "residual_est"] - 244)
    assert r1[gb.PFX + "production_est"] == pytest.approx(DAY[gb.P] - 244)                # the bar the waterfall shows
    assert r1[gb.PFX + "production_est"] - r1[gb.PFX + "exports_est"] - DAY[gb.D] + r1[gb.PFX + "storage_actual"]         == pytest.approx(r1[gb.PFX + "residual_est"])                                    # so the waterfall sums
    assert any("CGL direct" in w for w in res.warnings)


def test_importer_skips_closed_petrinex_months_already_stored():
    """L-081: a re-run of the gas_balance importer reads only missing closed months (or just the trailing window)."""
    from datetime import date

    from montney.importers import gas_balance as ib
    today = date(2026, 11, 3)
    every = {date(y, m, 1) for y in (2024, 2025, 2026) for m in range(1, 13) if date(y, m, 1) < date(2026, 6, 1)}
    assert ib.petrinex_from("2024-01", today, every) is None                       # all closed months stored
    assert ib.petrinex_from("2024-01", today, every - {date(2025, 3, 1)}) == "2025-03"
    assert ib.petrinex_from("2024-01", today, set()) == "2024-01"                  # nothing stored: full history
    s = ib.specs("2024-01", 36, skip_petrinex=False, stored=every, today=today)
    assert "from_month" not in s["gas_petrinex"].get("params", {})                # the connector's normal trailing read
    s = ib.specs("2024-01", 36, skip_petrinex=False, force_petrinex=True, stored=every, today=today)
    assert s["gas_petrinex"]["params"]["from_month"] == "2024-01"


def test_bc_remainder_leaves_out_its_ngtl_basis_part():
    """L-088: R_BC now holds (k − 1) × BC's NGTL field receipts (GDSR basis), which the breakdown's basis term already
    counts; the BC remainder part drops it, the unexplained takes it back, and the parts still sum to the error."""
    import calendar
    sep = datetime(2026, 9, 1)
    days = [sep.replace(day=d) for d in range(1, calendar.monthrange(2026, 9)[1] + 1)] + [T]
    flat = lambda v: {t: v for t in days}                                              # noqa: E731
    stored = {**{k: {T: v} for k, v in DAY.items()}, gb.AB: {T: 293.0},
              gb.RD[0]: flat(15_000.0), gb.RD[1]: flat(300.0), gb.RD[2]: flat(100.0), gb.RD[3]: flat(200.0),
              gb.GF: flat(14_000.0), gb.GGD: flat(100.0), gb.GGB: flat(200.0),
              **{g: flat(1_000.0) for g in gb.G_OUT}}
    stored.update({sid: {sep: 100.0} for sid in gb.SALES_TO.values()})
    a = {o.series_id: o.value for o in run(stored).obs}
    b = {o.series_id: o.value for o in run({**stored, gb.RBC_BASIS: {T: 80.0}}).obs}
    assert b[gb.UA + "bc_remainder_est"] == pytest.approx(a[gb.UA + "bc_remainder_est"] - 80)
    assert b[gb.UA + "unexplained_est"] == pytest.approx(a[gb.UA + "unexplained_est"] + 80)
    parts = sum(b[gb.UA + p] for p in ("ngtl_basis_est", "ngtl_closure", "ab_offsystem_est", "bc_remainder_est",
                                       "unexplained_est"))
    assert parts == pytest.approx(b[gb.PFX + "residual_est"])
