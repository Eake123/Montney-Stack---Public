"""Monthly series (`freq: M`) are judged stale on a monthly scale, not the 3-hour default (#160 review)."""
from montney import snapshot

DAY = 86400.0
NOW = 1_793_500_000.0  # a fixed epoch; only differences matter


def _entry(age_days: float, freq: str = "M") -> dict:
    ts = [NOW - age_days * DAY - 31 * DAY, NOW - age_days * DAY]
    return snapshot.summarize({"ts": ts, "v": [1.0, 2.0]}, {"freq": freq}, None, NOW)


def test_last_month_is_not_stale():
    # Station % Utilization: a month published a few weeks after it ends is ~60 days old by its timestamp.
    assert _entry(61)["stale"] is False


def test_a_quarter_old_month_is_stale():
    assert _entry(100)["stale"] is True


def test_seeded_red_without_monthly_freq_everything_monthly_reads_stale():
    # The bug #160's review found: an unknown freq fell back to 1 h, so a 61-day-old month was "stale".
    saved = snapshot.FREQ_SECONDS.pop("m")
    try:
        assert _entry(61)["stale"] is True
    finally:
        snapshot.FREQ_SECONDS["m"] = saved


def test_monthly_has_no_window_mean():
    meta = {"freq": "M"}
    s = {"ts": [NOW - 10 * DAY], "v": [1.0]}
    assert snapshot.window_mean(s, meta, "24h", NOW)["mean"] is None


def test_annual_capacity_has_a_value_ten_months_after_its_year():
    """PLAN 9e: StatCan's annual capacity (freq A, ts = 1 Jan 2025, released Oct 2026) read nothing in the 130-day
    monthly window, so Canada's capacity groups were blank. Annual series get their own ~3-year lookback."""
    from datetime import datetime

    sid = "cap.ab.hydro"
    pts = {"ts": [1704067200.0, 1735689600.0], "v": [900.0, 914.0]}           # 2024-01-01, 2025-01-01

    class Store:
        def meta(self, ids):
            return {i: {"freq": "A", "source": "power_capacity_ca", "unit": "MW"} for i in ids if i == sid}

        def series(self, ids, start=None, end=None, as_of_max=None):
            lo = (start - datetime(1970, 1, 1)).total_seconds() if start else float("-inf")
            return {i: {"ts": [t for t in pts["ts"] if t >= lo], "v": [v for t, v in zip(pts["ts"], pts["v"]) if t >= lo]}
                    for i in ids}

    import pytest
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snapshot, "utcnow", lambda: datetime(2026, 11, 13, 20))
        snap = snapshot.build(Store(), {}, {}, [sid], gauges=False)
    e = snap["series"][sid]
    assert e["v"] == 914.0 and e["stale"] is False
