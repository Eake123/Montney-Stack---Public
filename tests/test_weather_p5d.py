"""P5d groundwork scripts (docs/research/v2/weather-p5d-scripts/): mocked payloads, with seeded reds."""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "docs" / "research" / "v2" / "weather-p5d-scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bal = _load("peace_balance")
pct = _load("flow_percentile")


def feats(rows):
    """hydrometric-daily-mean features, as the OGC API returns them."""
    return [{"properties": {"DATE": d, "DISCHARGE": q, "DISCHARGE_SYMBOL_EN": s}} for d, q, s in rows]


# ---- §1 Peace balance
def test_balance_closes_on_consistent_flows():
    days = ["2024-06-01", "2024-06-02", "2024-12-01"]
    above = bal.daily(feats([(d, 1000.0, None) for d in days]))
    pine = bal.daily(feats([(d, 100.0, None) for d in days]))
    taylor = bal.daily(feats([(d, 1100.0, None) for d in days]))
    res = bal.residuals(taylor, above, pine, k=1.0)
    assert res == {d: pytest.approx(0.0) for d in days}
    s = bal.summarize(res, bal.season)
    assert s["open (May-Oct)"]["n"] == 2 and s["ice (Nov-Apr)"]["n"] == 1


def test_balance_skips_nulls_and_ice_and_fails_closed():
    f = feats([("2024-01-05", 900.0, "Ice Conditions"), ("2024-06-01", None, None), ("2024-06-02", 1.0, None)])
    assert bal.daily(f) == {"2024-01-05": 900.0, "2024-06-02": 1.0}
    assert bal.daily(f, drop_ice=True) == {"2024-06-02": 1.0}
    assert bal.residuals({"2024-06-01": 1.0}, {"2024-06-02": 1.0}, {"2024-06-02": 1.0}) == {}   # no overlap


def test_area_factor_matches_the_drainage_areas():
    assert bal.K_AREA == pytest.approx((100800 - 87150) / 12140)


def test_seeded_red_wrong_pairing_beatton_breaks_the_balance():
    """P2's pairing added the Beatton (it joins below Taylor). The area check must reject it: upstream areas
    can't exceed the downstream gauge's area."""
    assert bal.pairing_problems("07FD002", ["07FA004", "07FB001"]) == []
    bad = bal.pairing_problems("07FD002", ["07FA004", "07FB001", "07FC001"])
    assert bad and "a gauge is downstream" in bad[0]
    assert "NO-RESULT" in bal.pairing_problems("07FD002", ["07XX999"])[0]


def test_regime_split_detects_a_step():
    """A +2-point step in 2016 must show in the regime medians (the review of #117 found one in the real record)."""
    t = {f"{y}-07-01": 1100.0 + (22.0 if y >= 2016 else 0.0) for y in range(2000, 2026)}
    a = {d: 1000.0 for d in t}
    p = {d: 100.0 for d in t}
    res = bal.residuals(t, a, p, k=1.0)
    s = bal.summarize({d: v for d, v in res.items() if bal.regime(d)}, bal.regime)
    assert s["2000-2009"]["median"] == pytest.approx(0.0)
    assert s["2016-2025"]["median"] == pytest.approx(22 / 1122)
    assert bal.regime("1999-12-31") is None and bal.regime("2015-06-01") == "2010-2015"


def test_seeded_red_per_year_at_the_wrong_k_differs():
    """The draft's per-year numbers were printed at k = 1.124, not the headline k = 1. They must differ, so a doc that
    mixes the two is detectably wrong."""
    t, a, p = {"2004-07-01": 1100.0}, {"2004-07-01": 1000.0}, {"2004-07-01": 100.0}
    assert bal.residuals(t, a, p, 1.0)["2004-07-01"] != pytest.approx(bal.residuals(t, a, p, bal.K_AREA)["2004-07-01"])


# ---- §3 flow percentile
def history(value_of_year):
    d, out = date(1990, 1, 1), {}
    while d <= date(2025, 12, 31):
        out[d] = value_of_year(d.year)
        d += timedelta(days=1)
    return out


def test_percentile_ranks_against_the_reference_years_only():
    h = history(lambda y: float(y))                      # 1995..2024 -> 30 distinct yearly levels
    s = pct.sample(h, date(2026, 6, 15))
    assert len(s) == 30 * 15 and min(s) == 1995 and max(s) == 2024
    assert pct.percentile(2010.0, s) == pytest.approx(100 * (15 * 15 + 7.5) / 450)   # mid-rank
    assert pct.percentile(1e9, s) == 100.0 and pct.band(pct.percentile(1e9, s)) == "very high"
    assert pct.band(pct.percentile(0.0, s)) == "very low"


def test_window_wraps_the_year_end_and_handles_feb_29():
    keys = pct.window_keys(date(2027, 1, 2))
    assert (12, 26) in keys and (1, 9) in keys and len(keys) == 15
    assert pct.doy_key(date(2024, 2, 29)) == (2, 28)
    h = history(lambda y: 1.0)
    assert len(pct.sample(h, date(2027, 1, 2))) == 30 * 15


def test_seeded_red_short_record_is_no_result():
    """A seasonal gauge with no winter record must not get a percentile (NO-RESULT, grey)."""
    h = {d: v for d, v in history(lambda y: 1.0).items() if 5 <= d.month <= 10}
    assert pct.sample(h, date(2027, 1, 15)) == []
    assert pct.percentile(1.0, pct.sample(h, date(2027, 1, 15))) is None
    assert pct.band(None) == "no-result"
    nine_years = {d: v for d, v in history(lambda y: 1.0).items() if 2016 <= d.year}
    assert len(pct.sample(nine_years, date(2027, 6, 1))) == 9 * 15 < pct.MIN_N
    assert pct.percentile(1.0, pct.sample(nine_years, date(2027, 6, 1))) is None
