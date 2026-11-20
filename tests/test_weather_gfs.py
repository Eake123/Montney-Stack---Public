"""GFS grid connector: GRIB2 decode (runs on the Windows CI job too), payload, run discovery."""

import base64
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from montney import grids
from montney.connectors.base import Context
from montney.connectors.gfs import GFS, decode, split_grib

# Real NOMADS crop: GFS 0.25, run 2026-10-31 12Z, f048, lat 50..52, lon -115..-113, 9 x 9 points,
# messages orog/t(surface)/2t/10u/10v. Public domain (NOAA). Centre point = 51.0 N, -114.0 (Calgary).
FIXTURE = Path(__file__).parent / "fixtures" / "gfs_0p25_calgary_f048.grb2"


def blob() -> bytes:
    return FIXTURE.read_bytes()


def test_split_and_decode_fixture():
    assert len(split_grib(blob())) == 5
    fields, geom = decode(blob())
    assert geom == {"lon0": -115.0, "lat0": 50.0, "dx": 0.25, "dy": 0.25, "nx": 9, "ny": 9}
    assert set(fields) == {"t2m", "u10", "v10", "z"}  # surface skin temperature is ignored
    for a in fields.values():
        assert a.shape == (9, 9)
    # values cross-checked against the full-extent download (docs/research/weather.md 3.1)
    assert fields["z"][4, 4] == pytest.approx(1065.98, abs=0.01)
    assert fields["t2m"][4, 4] == pytest.approx(271.45 - 273.15, abs=0.01)
    assert fields["u10"][4, 4] == pytest.approx(2.86, abs=0.01)
    assert fields["v10"][4, 4] == pytest.approx(0.09, abs=0.01)
    # rows run south -> north: terrain rises toward the Rockies (west) more than the north/south edges
    assert fields["z"][:, 0].mean() > fields["z"][:, -1].mean()


def spec(**params):
    # extent = the fixture's crop, so a saved run counts as current
    p = {"short_until": 6, "step_short": 3, "step_long": 6, "horizon": 12, "pace_s": 0.0,
         "lon_min": -115, "lon_max": -113, "lat_min": 50, "lat_max": 52}
    p.update(params)
    return {"id": "weather_gfs", "type": "python", "params": p}


def make(handler, now=datetime(2026, 10, 31, 20, 40), **params):
    conn = GFS(spec(**params))
    sleeps = []
    conn.sleep = sleeps.append
    calls = []

    def h(req: httpx.Request):
        calls.append((req.url.params["dir"], req.url.params["file"], "var_HGT" in req.url.params))
        return handler(req)

    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=now)
    return conn, ctx, calls, sleeps


def serve_all(req):
    return httpx.Response(200, content=blob())


def not_posted(req):
    return httpx.Response(404, text="<html>Data file is not present</html>")


def test_steps_and_cycles():
    full = GFS({"id": "g", "params": {}})
    s = full.steps()
    assert s[:3] == [0, 3, 6] and 120 in s and 123 not in s and s[-2:] == [378, 384]
    assert len(s) == 85
    # 20:40Z: the 18Z cycle is only 2h40 old (< 3.5 h), so the newest candidate is 12Z
    assert [c.hour for c in full.cycles(datetime(2026, 10, 31, 20, 40))] == [12, 6, 0, 18]


def test_pull_end_to_end_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    conn, ctx, calls, sleeps = make(serve_all)
    res = conn.fetch(ctx)
    assert set(res.grids) == {"t2m", "u10", "v10", "z"}
    # probe the last step first, then f000 (with terrain height), f003, f006
    assert [c[1][-4:] for c in calls] == ["f012", "f000", "f003", "f006"]
    assert [c[2] for c in calls] == [False, True, False, False]
    assert calls[0][0] == "/gfs.20261031/12/atmos"
    assert len(sleeps) == 3
    t = res.grids["t2m"]
    grids.validate(t)
    assert t["run"] == "2026-10-31T12:00:00Z" and t["model"] == "gfs_0p25"
    assert t["times"] == ["2026-10-31T12:00:00Z", "2026-10-31T15:00:00Z",
                          "2026-10-31T18:00:00Z", "2026-11-01T00:00:00Z"]
    a = np.frombuffer(base64.b64decode(t["data"]), "<i2").reshape(4, 9, 9)
    assert a[0, 4, 4] == -170  # -1.70 degC at scale 0.01
    z = res.grids["z"]
    assert z["times"] == [] and z["unit"] == "m" and z["scale"] == 1.0
    assert np.frombuffer(base64.b64decode(z["data"]), "<i2").reshape(9, 9)[4, 4] == 1066
    # the runner's save path accepts it, and a re-check finds nothing new (no download)
    for p in res.grids.values():
        grids.save(p)
    conn2, ctx2, calls2, _ = make(serve_all)
    assert conn2.fetch(ctx2).grids == {} and calls2 == []


def test_run_discovery_falls_back_to_previous_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def handler(req):  # 12Z still posting, 06Z complete
        return not_posted(req) if "/12/" in req.url.params["dir"] else serve_all(req)

    conn, ctx, calls, _ = make(handler)
    res = conn.fetch(ctx)
    assert res.grids["t2m"]["run"] == "2026-10-31T06:00:00Z"
    assert calls[0][:2] == ("/gfs.20261031/12/atmos", "gfs.t12z.pgrb2.0p25.f012")


def test_no_newer_run_costs_one_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    conn, ctx, _, _ = make(serve_all, now=datetime(2026, 10, 31, 14, 0))  # newest eligible: 06Z
    for p in conn.fetch(ctx).grids.values():
        grids.save(p)
    conn, ctx, calls, _ = make(not_posted)  # 20:40Z: 12Z not complete yet; we already have 06Z
    res = conn.fetch(ctx)
    assert res.grids == {} and len(calls) == 1 and not res.warnings


def test_failed_step_keeps_previous_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def handler(req):
        if req.url.params["file"].endswith("f003"):
            return httpx.Response(200, text="<html>oops</html>")
        return serve_all(req)

    conn, ctx, _, _ = make(handler)
    with pytest.raises(Exception):
        conn.fetch(ctx)
    assert grids.path_for("t2m") is None  # nothing half-written


def test_nothing_posted_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    conn, ctx, calls, _ = make(not_posted)
    res = conn.fetch(ctx)
    assert res.grids == {} and len(calls) == 4 and res.warnings


def fail_f003(req):
    if req.url.params["file"].endswith("f003"):
        return httpx.Response(200, text="<html>throttled</html>")  # a 200 that isn't GRIB
    return serve_all(req)


def test_persistent_failure_resumes_from_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    conn, ctx, calls, _ = make(fail_f003)
    with pytest.raises(Exception):
        conn.fetch(ctx)
    assert [c[1][-4:] for c in calls] == ["f012", "f000", "f003"]
    # each retry (the runner backs off between them) re-requests only the failing step
    for minute in (45, 55):
        conn, ctx, calls, _ = make(fail_f003, now=datetime(2026, 10, 31, 20, minute))
        with pytest.raises(Exception):
            conn.fetch(ctx)
        assert [c[1][-4:] for c in calls] == ["f003"]
    # once NOMADS serves f003 again, only the steps >= f003 that are missing are fetched
    conn, ctx, calls, _ = make(serve_all, now=datetime(2026, 10, 31, 21, 0))
    res = conn.fetch(ctx)
    assert [c[1][-4:] for c in calls] == ["f003", "f006"]
    assert res.grids["t2m"]["run"] == "2026-10-31T12:00:00Z"
    assert list((tmp_path / "cache" / "gfs").iterdir()) == []  # partial cache cleared


def test_extent_change_forces_repull(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    conn, ctx, _, _ = make(serve_all)
    for p in conn.fetch(ctx).grids.values():
        grids.save(p)
    conn, ctx, calls, _ = make(serve_all, lon_min=-116)  # wider spec than the saved grid
    assert conn.current_run() is None
