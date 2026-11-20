"""Realized daily actuals from Open-Meteo's past_days (L-039): wx.<loc>.tmean.realized / hdd.realized.

The fixture gives every ENDED day (before the API's local "today") a distinct temperature from today and the forecast
days, so a forecast value that leaks into a realized series shows up by its date and by its value.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest

from montney.connectors import build
from montney.connectors.base import Context
from montney.connectors.open_meteo import OpenMeteo
from test_weather_points import OFFSET, spec

TODAY = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 18, 0)
PAST_T, FCST_T = -10.0, 0.0           # Cold Town: past days HDD 28, today and forecast HDD 18 (Mild Town +10 degC)


def handler(req: httpx.Request) -> httpx.Response:
    q = req.url.params
    if "archive" in req.url.host:      # normals: flat -2 degC
        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(365)]
        return httpx.Response(200, json={"utc_offset_seconds": OFFSET[q["timezone"]],
                                         "daily": {"time": [d.isoformat() for d in days],
                                                   "temperature_2m_mean": [-2.0] * len(days)}})
    off = 0.0 if q["latitude"] == "50.0" else 10.0
    days = [TODAY + timedelta(days=i) for i in range(-2, 16)]
    tm = [(PAST_T if d < TODAY else FCST_T) + off for d in days]
    return httpx.Response(200, json={"utc_offset_seconds": OFFSET[q["timezone"]], "daily": {
        "time": [d.isoformat() for d in days], "temperature_2m_max": [t + 5 for t in tm],
        "temperature_2m_min": [t - 5 for t in tm], "temperature_2m_mean": tm,
        "precipitation_sum": [0.0] * len(days), "wind_speed_10m_max": [20.0] * len(days)}})


def run(tmp_path, monkeypatch, cmp: str | None = None):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    if cmp:
        monkeypatch.setattr(OpenMeteo, "_REALIZED_CMP", cmp)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    return build(spec()).run(ctx)


def leaks(res, past_days: int = 2) -> list[str]:
    """Every way the realized series break L-039. Fails closed: too few realized points is a problem too."""
    out = []
    realized = [o for o in res.obs if o.series_id.endswith(".realized")]
    by_series: dict[str, list] = {}
    for o in realized:
        by_series.setdefault(o.series_id, []).append(o)
    places = ("cold_town", "mild_town", "comp")
    want = {f"wx.{p}.{k}.realized" for p in places for k in ("tmean", "hdd", "cdd")}      # cdd: L-106
    if set(by_series) != want:
        out.append(f"realized series {sorted(by_series)} != {sorted(want)} (NO-RESULT if empty)")
    expect = {"cold_town": PAST_T, "mild_town": PAST_T + 10, "comp": PAST_T + 10 / 4}
    for s, obs in sorted(by_series.items()):
        place, key = s.split(".")[1], s.split(".")[2]
        if len(obs) != past_days:
            out.append(f"{s}: {len(obs)} points, want {past_days} ended days")
        for o in obs:
            if o.ts.date() >= TODAY:
                out.append(f"{s}: {o.ts.date()} is not an ended day (forecast leak)")
            if o.as_of != o.ts:
                out.append(f"{s}: as_of {o.as_of} != ts {o.ts}")
            tm = expect[place]
            v = tm if key == "tmean" else max(0.0, 18 - tm) if key == "hdd" else max(0.0, tm - 18)
            if o.value != pytest.approx(v):
                out.append(f"{s} {o.ts.date()}: {o.value} != analysis value {v} (forecast leak?)")
    return out


def test_realized_are_ended_days_only(tmp_path, monkeypatch):
    res = run(tmp_path, monkeypatch)
    assert leaks(res) == []
    s = {(o.series_id, o.ts): o.value for o in res.obs}
    d = datetime(2026, 10, 31)
    assert s[("wx.cold_town.hdd.realized", d)] == pytest.approx(28.0)
    assert s[("wx.comp.hdd.realized", d)] == pytest.approx((28 * 3 + 18) / 4)          # 25.5
    assert ("wx.cold_town.hdd.realized", datetime(2026, 11, 1)) not in s


def test_plain_series_keep_the_fetch_vintage(tmp_path, monkeypatch):
    """The forecast-tier series are unchanged: still every day, stamped with the fetch hour."""
    res = run(tmp_path, monkeypatch)
    plain = [o for o in res.obs if o.series_id == "wx.cold_town.hdd"]
    assert len(plain) == 18 and {o.as_of for o in plain} == {NOW}


def test_seeded_red_today_leaks_into_realized(tmp_path, monkeypatch):
    """`le` lets the API's "today" (a forecast value) into the realized series. The check must catch it."""
    bad = leaks(run(tmp_path, monkeypatch, cmp="le"))
    assert any("2026-11-01 is not an ended day (forecast leak)" in b for b in bad), bad
    assert any("analysis value" in b for b in bad), bad


def test_seeded_red_no_realized_is_no_result():
    class Empty:
        obs: list = []
    assert any("NO-RESULT" in b for b in leaks(Empty()))


# ---- L-039 precision: realized(D) = the value from the LATEST fetch in which D is still within past_days.
# Last write wins (as_of = ts, so the store overwrites); D settles once it leaves past_days. City day windows are
# the spec's Etc/GMT+6/+7 zones (AGENTS.md exception), not L-029's Alberta day.
def handler_for(today: date, past_t: float):
    def h(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        if "archive" in req.url.host:
            return handler(req)
        off = 0.0 if q["latitude"] == "50.0" else 10.0
        days = [today + timedelta(days=i) for i in range(-2, 16)]
        tm = [(past_t if d < today else FCST_T) + off for d in days]
        return httpx.Response(200, json={"utc_offset_seconds": OFFSET[q["timezone"]], "daily": {
            "time": [d.isoformat() for d in days], "temperature_2m_max": [t + 5 for t in tm],
            "temperature_2m_min": [t - 5 for t in tm], "temperature_2m_mean": tm,
            "precipitation_sum": [0.0] * len(days), "wind_speed_10m_max": [20.0] * len(days)}})
    return h


# (local today, analysis temperature for the ended days): each fetch "improves" the analysis.
FETCHES = [(date(2026, 11, 1), -10.0), (date(2026, 11, 2), -12.0), (date(2026, 11, 4), -20.0)]
# Cold Town HDD = 18 - t. past_days = 2, so each fetch covers today-2 .. today-1.
WANT = {date(2026, 10, 30): 28.0,      # only fetch 1 covers it, so frozen there
        date(2026, 10, 31): 30.0,      # fetch 1 (28), then overwritten by fetch 2 (30); out of fetch 3's window
        date(2026, 11, 1): 30.0,       # a forecast day in fetch 1 (never realized there); realized by fetch 2
        date(2026, 11, 2): 38.0, date(2026, 11, 3): 38.0}   # fetch 3


def run_sequence(tmp_path, monkeypatch, cmp: str | None = None):
    from montney.store import Store

    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    if cmp:
        monkeypatch.setattr(OpenMeteo, "_REALIZED_CMP", cmp)
    conn = build(spec())
    st = Store(tmp_path / "t.duckdb")
    for today, t in FETCHES:
        ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler_for(today, t))),
                      now=datetime(today.year, today.month, today.day, 18))
        st.write(conn.run(ctx).obs)
    rows = st._q("SELECT ts, value, as_of FROM observations WHERE series_id = 'wx.cold_town.hdd.realized' ORDER BY ts")
    st.close()
    return rows


def sequence_problems(rows, last_today: date = FETCHES[-1][0]) -> list[str]:
    """Stored realized rows vs L-039. Fails closed: missing days are problems."""
    out = []
    got = {r[0].date(): r[1] for r in rows}
    if len(rows) != len(got):
        out.append(f"{len(rows)} rows for {len(got)} days: realized must be one row per day (as_of = ts)")
    for r in rows:
        if r[2] != r[0]:
            out.append(f"{r[0].date()}: as_of {r[2]} != ts")
        if r[0].date() >= last_today:
            out.append(f"{r[0].date()}: realized on or after the last fetch's today (forecast-horizon leak)")
    for d, v in WANT.items():
        if d not in got:
            out.append(f"{d}: no realized value (NO-RESULT)")
        elif got[d] != pytest.approx(v):
            out.append(f"{d}: realized {got[d]} != {v} (latest fetch within past_days)")
    return out


def test_latest_fetch_within_past_days_wins_then_frozen(tmp_path, monkeypatch):
    rows = run_sequence(tmp_path, monkeypatch)
    assert sequence_problems(rows) == []
    assert len(rows) == 5


def test_seeded_red_forecast_horizon_value_lands_in_realized(tmp_path, monkeypatch):
    """With `le`, each fetch's "today" (a forecast value) is written as realized. Fetch 3's today (2026-11-04) has
    no later fetch to overwrite it, and Nov 1 keeps the right value only because fetch 2 overwrote fetch 1's leak.
    The check must report the surviving leak."""
    bad = sequence_problems(run_sequence(tmp_path, monkeypatch, cmp="le"))
    assert any("2026-11-04: realized on or after" in b for b in bad), bad


def test_seeded_red_frozen_day_overwritten_is_caught():
    """If a day outside past_days were rewritten (e.g. Oct 31 taking fetch 3's analysis), the value check catches it."""
    rows = [(datetime.combine(d, datetime.min.time()), v, datetime.combine(d, datetime.min.time()))
            for d, v in WANT.items()]
    rows[1] = (rows[1][0], 38.0, rows[1][2])          # Oct 31 overwritten after leaving the window
    assert any("2026-10-31: realized 38.0 != 30.0" in b for b in sequence_problems(rows))
