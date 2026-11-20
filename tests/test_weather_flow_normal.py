"""% of normal (L-066): the median window and the NO-RESULT rules, the parts that could put a wrong number on the map."""

from datetime import date, datetime, timedelta

from montney.connectors import weather_flow_normal as fn


def hist(years, value_of_year):
    out = {}
    for y in years:
        d = datetime(y, 1, 1)
        while d.year == y:
            out[d] = value_of_year(y)
            d += timedelta(days=1)
    return out


def test_median_over_the_window_and_all_years():
    h = hist(range(1995, 2025), float)                     # 30 years, value = year
    med, n, y0, y1 = fn.normal(h, date(2026, 11, 2))
    assert (n, y0, y1) == (30 * 15, 1995, 2024) and med == 2009.5
    now = datetime(2026, 11, 2, 18)
    pts = {now - timedelta(minutes=5 * i): 1004.75 for i in range(288)}
    assert round(100 * fn.current(pts, now, 144) / med, 1) == 50.0


def test_no_result_rules():
    assert fn.normal(hist(range(2016, 2025), lambda y: 50.0), date(2026, 11, 2)) is None     # 9 years
    assert fn.normal(hist(range(1995, 2025), lambda y: 0.4), date(2026, 11, 2)) is None      # median < 1 m3/s
    now = datetime(2026, 11, 2, 18)
    assert fn.current({now - timedelta(minutes=5 * i): 1.0 for i in range(100)}, now, 144) is None
