"""Open-Meteo point forecasts: past days, weighted composites, HDD outlook (revision) sums."""

from datetime import date, datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import Context

TODAY = date(2026, 11, 1)
OFFSET = {"Etc/GMT+6": -21600, "Etc/GMT+7": -25200}


def spec():
    return {"id": "weather", "type": "python", "module": "montney.connectors.open_meteo",
            "class": "OpenMeteo",
            "params": {"forecast_days": 16, "past_days": 2, "hdd_base": 18, "normal_years": 1,
                       "tz": "Etc/GMT+6",
                       "hdd_windows": [[1, 7], [8, 15]],
                       "locations": [{"name": "Cold Town", "lat": 50.0, "lon": -114.0},
                                     {"name": "Mild Town", "lat": 56.0, "lon": -120.0,
                                      "tz": "Etc/GMT+7"}],
                       "composites": [{"id": "comp", "name": "Comp",
                                       "weights": {"cold_town": 3, "mild_town": 1}}]}}


def handler(req: httpx.Request) -> httpx.Response:
    q = req.url.params
    if "archive" in req.url.host:  # normals (also requested in the location's zone)
        assert q["timezone"] in ("Etc/GMT+6", "Etc/GMT+7")
        # a flat -2 degC year -> HDD normal 20 every day
        days =[date(2025, 1, 1) + timedelta(days=i) for i in range(365)]
        return httpx.Response(200, json={"utc_offset_seconds": OFFSET[q["timezone"]],
                                         "daily": {"time": [d.isoformat() for d in days],
                                                   "temperature_2m_mean": [-2.0] * len(days)}})
    assert q["past_days"] == "2" and q["forecast_days"] == "16"
    assert q["timezone"] == ("Etc/GMT+6" if q["latitude"] == "50.0" else "Etc/GMT+7")
    t = 0.0 if q["latitude"] == "50.0" else 10.0  # Cold Town HDD 18, Mild Town HDD 8
    days = [TODAY + timedelta(days=i) for i in range(-2, 16)]
    n = len(days)
    return httpx.Response(200, json={"utc_offset_seconds": OFFSET[q["timezone"]], "daily": {
        "time": [d.isoformat() for d in days], "temperature_2m_max": [t + 5] * n,
        "temperature_2m_min": [t - 5] * n, "temperature_2m_mean": [t] * n,
        "precipitation_sum": [0.0] * n, "wind_speed_10m_max": [20.0] * n}})


def run(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)),
                  now=datetime(2026, 11, 1, 18, 0))
    res = build(spec()).run(ctx)
    out: dict[str, dict[datetime, float]] = {}
    for o in res.obs:
        out.setdefault(o.series_id, {})[o.ts] = o.value
    return res, out


def test_past_days_and_composite(tmp_path, monkeypatch):
    res, s = run(tmp_path, monkeypatch)
    d0 = datetime(2026, 11, 1)
    # past days are included (2 back), 18 days in total
    assert min(s["wx.cold_town.hdd"]) == d0 - timedelta(days=2) and len(s["wx.cold_town.hdd"]) == 18
    assert s["wx.cold_town.hdd"][d0] == 18 and s["wx.mild_town.hdd"][d0] == 8
    assert s["wx.comp.hdd"][d0] == pytest.approx((18 * 3 + 8) / 4)  # 15.5
    assert s["wx.comp.hdd_normal"][d0] == pytest.approx(20)


def test_hdd_outlook_windows(tmp_path, monkeypatch):
    res, s = run(tmp_path, monkeypatch)
    d0 = datetime(2026, 11, 1)
    assert s["wx.comp.hdd_1_7"] == {d0: pytest.approx(7 * 15.5)}
    assert s["wx.comp.hdd_8_15"] == {d0: pytest.approx(8 * 15.5)}
    assert s["wx.comp.hdd_1_7_normal"] == {d0: pytest.approx(140)}
    assert s["wx.comp.hdd_8_15_normal"] == {d0: pytest.approx(160)}
    declared = {m.series_id for m in res.meta}
    assert {"wx.comp.hdd_1_7", "wx.comp.hdd_8_15_normal"} <= declared
    assert "wx.cold_town.hdd_1_7" not in s  # windows are for composites only


def test_shipped_spec_ids_stable():
    spec = load_specs(include_disabled=True)["weather"]
    ids = {m.series_id for m in build(spec).declared_series()}
    # ids used by tabs/ and the weather widget must not change
    for sid in ("wx.alberta.hdd", "wx.alberta.hdd_normal", "wx.alberta.tmean", "wx.calgary.hdd",
                "wx.edmonton.tmax", "wx.calgary.tmin", "wx.edmonton.precip", "wx.calgary.wind"):
        assert sid in ids
    for sid in ("wx.nebc.hdd", "wx.alberta.hdd_1_7", "wx.alberta.hdd_8_15", "wx.nebc.hdd_8_15_normal",
                "wx.fort_st_john.tmin", "wx.fort_mcmurray.hdd", "wx.grande_prairie.hdd"):
        assert sid in ids


def test_wrong_utc_offset_is_rejected(tmp_path, monkeypatch):
    """If the provider buckets days in another zone than asked, fail instead of storing them."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def stale(req):
        r = handler(req)
        body = r.json()
        body["utc_offset_seconds"] = -25200 if req.url.params["timezone"] == "Etc/GMT+6" else -21600
        return httpx.Response(200, json=body)

    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(stale)),
                  now=datetime(2026, 11, 1, 18, 0))
    with pytest.raises(ValueError, match="utc_offset_seconds"):
        build(spec()).run(ctx)


def test_check_offset_skips_dst_zones():
    from montney.connectors.open_meteo import check_offset
    check_offset({"utc_offset_seconds": -21600}, "Etc/GMT+6", 2026)
    check_offset({"utc_offset_seconds": 0}, "Europe/London", 2026)  # DST zone: not checked
    with pytest.raises(ValueError):
        check_offset({"utc_offset_seconds": -25200}, "Etc/GMT+6", 2026)


# ---- L-106: one batched request for every location (forecast and normals)
def _batched(reverse=False, calls=None):
    """Answer Open-Meteo's multi-location form (comma lists) with the single-location handler's bodies, as a list."""
    def h(req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        if calls is not None:
            calls.append(req.url.host)
        lats, lons, tzs = q["latitude"].split(","), q["longitude"].split(","), q["timezone"].split(",")
        out = []
        for i, (la, lo, tz) in enumerate(zip(lats, lons, tzs)):
            one = httpx.Request("GET", req.url.copy_with(params={**q, "latitude": la, "longitude": lo, "timezone": tz}))
            body = {**handler(one).json(), "latitude": float(la), "longitude": float(lo)}
            if i:                                   # as Open-Meteo: location_id is the position, left out for the first
                body["location_id"] = i
            out.append(body)
        return httpx.Response(200, json=out[::-1] if reverse else out)
    return h


def _run_batched(tmp_path, monkeypatch, h):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    sp = spec()
    sp["params"]["batch"] = True
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=datetime(2026, 11, 1, 18, 0))
    return build(sp).run(ctx)


def test_batched_fetch_is_one_request_and_the_same_values(tmp_path, monkeypatch):
    calls = []
    res = _run_batched(tmp_path / "b", monkeypatch, _batched(calls=calls))
    one, _ = run(tmp_path / "a", monkeypatch)
    key = lambda r: sorted((o.series_id, o.ts, round(o.value, 9)) for o in r.obs)
    assert key(res) == key(one)
    assert sorted(calls) == ["api.open-meteo.com", "archive-api.open-meteo.com"]     # 2 locations, 2 requests


def test_seeded_red_a_swapped_close_pair_fails_closed(tmp_path, monkeypatch):
    """Two cities 0.3 deg apart (closer than the 0.5 deg distance check) swapped in the answer: location_id catches it."""
    def h(req):
        q = dict(req.url.params)
        n = len(q["latitude"].split(","))
        out = [{"latitude": 50.0, "longitude": -114.0, "utc_offset_seconds": -21600, "daily": {"time": []}}
               for _ in range(n)]
        for i in range(1, n):
            out[i]["location_id"] = i
        return httpx.Response(200, json=out[::-1])
    sp = spec()
    sp["params"].update(batch=True, normal_years=0)
    sp["params"]["locations"][1].update(lat=50.3, lon=-114.0)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=datetime(2026, 11, 1, 18, 0))
    with pytest.raises(Exception, match="carries location_id"):
        build(sp).run(ctx)


def test_seeded_red_a_reordered_batch_answer_fails_closed(tmp_path, monkeypatch):
    """A list answer in another order must not put Cold Town's weather on Mild Town: the grid-point check refuses it."""
    with pytest.raises(Exception, match="answer for Cold Town"):
        _run_batched(tmp_path, monkeypatch, _batched(reverse=True))       # reordered: location_id 1 in position 0


def test_cdd_outlook_windows(tmp_path, monkeypatch):
    """L-106: cdd_1_7 / cdd_8_15 are summed like the HDD windows. Base 5 degC: Mild Town (10 degC) has CDD 5 a day,
    Cold Town (0 degC) none, so the 3:1 composite has 1.25 a day: 8.75 for days 1-7, 10.0 for days 8-15."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    sp = spec()
    sp["params"]["hdd_base"] = 5
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 1, 18, 0))
    s = {(o.series_id, o.ts): o.value for o in build(sp).run(ctx).obs}
    d = datetime(2026, 11, 1)
    assert s[("wx.comp.cdd_1_7", d)] == pytest.approx(8.75) and s[("wx.comp.cdd_8_15", d)] == pytest.approx(10.0)
    assert ("wx.comp.cdd_1_7_normal", d) in s                      # the normals' window too
