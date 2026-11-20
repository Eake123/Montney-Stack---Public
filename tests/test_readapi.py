"""Read-only series access (Krasnodar D29): montney/readapi.py over HTTP (/api/read/*) and MCP (/mcp/read)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from montney import readapi, readmcp
from montney.store import Obs, SeriesMeta, Store

D1, D2 = date(2026, 11, 8), date(2026, 11, 9)


def _seed(st: Store) -> None:
    st.upsert_meta([SeriesMeta("aeso.price.pool", "power_aeso", unit="$/MWh", freq="1h"),
                    SeriesMeta("aeso.csd.ail", "power_aeso_csd", unit="MW", freq="5min"),
                    SeriesMeta("ngtl.gdsr.field_receipts.flow", "ngtl_gdsr", unit="MMcf/d", freq="D", day="gas"),
                    SeriesMeta("eia.storage.lower48.working_gas", "gas_eia_storage_weekly", unit="Bcf", freq="W")])
    fetched = datetime(2026, 11, 10, 12)
    obs = []
    # pool price: Nov 8 complete (24 h, mean 20), Nov 9 only 20 of 24 hours (< 90 %: left out)
    for h in range(24):
        obs.append(Obs("aeso.price.pool", datetime(2026, 11, 8, 6) + timedelta(hours=h), 10.0 + (h % 2) * 20, fetched))
    for h in range(20):
        obs.append(Obs("aeso.price.pool", datetime(2026, 11, 9, 6) + timedelta(hours=h), 50.0, fetched))
    # AIL: Nov 8 at 5-min (288 samples, 10 000 MW), minus 20 samples (268 >= 259.2: kept)
    for i in range(288 - 20):
        obs.append(Obs("aeso.csd.ail", datetime(2026, 11, 8, 6) + timedelta(minutes=5 * i), 10000.0, fetched))
    # field receipts: Nov 8 revised (latest vintage wins), Nov 9 once
    obs += [Obs("ngtl.gdsr.field_receipts.flow", datetime(2026, 11, 8), 14000.0, datetime(2026, 11, 9, 1)),
            Obs("ngtl.gdsr.field_receipts.flow", datetime(2026, 11, 8), 14100.0, datetime(2026, 11, 9, 20)),
            Obs("ngtl.gdsr.field_receipts.flow", datetime(2026, 11, 9), 14200.0, datetime(2026, 11, 10, 1))]
    obs.append(Obs("eia.storage.lower48.working_gas", datetime(2026, 10, 30), 3900.0, fetched))
    st.write(obs)


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    _seed(st)
    yield st
    st.close()


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from montney.server import create_app
    with TestClient(create_app(store=store, schedule=False)) as c:
        yield c


def test_day_means_complete_days_only(store):
    out = readapi.series(store, "aeso.price.pool", "2026-11-01", "2026-11-10")
    assert out["points"] == [["2026-11-08", 20.0]]          # Nov 9 has 20 of 24 hours: left out, not zero-filled
    assert out["tz"] == "America/Edmonton" and out["unit"] == "$/MWh" and out["freq"] == "day"
    assert out["source"] == "power_aeso" and out["as_of"]
    assert readapi.series(store, "aeso.csd.ail", "2026-11-08", "2026-11-08")["points"] == [["2026-11-08", 10000.0]]


def test_stored_daily_latest_vintage(store):
    out = readapi.series(store, "ngtl.gdsr.field_receipts.flow", "2026-11-08", "2026-11-09")
    assert out["points"] == [["2026-11-08", 14100.0], ["2026-11-09", 14200.0]]
    assert out["as_of"].startswith("2026-11-10")


def test_weekly_refuses_day(store):
    with pytest.raises(readapi.ReadError, match="week"):
        readapi.series(store, "eia.storage.lower48.working_gas", "2026-10-01", "2026-11-01")
    out = readapi.series(store, "eia.storage.lower48.working_gas", "2026-10-01", "2026-11-01", freq="week")
    assert out["points"] == [["2026-10-30", 3900.0]]


@pytest.mark.parametrize("args,msg", [
    (("nope.metric", "2026-11-01", "2026-11-02"), "unknown metric"),
    (("aeso.price.pool", "2026-11-05", "2026-11-01"), "before start"),
    (("aeso.price.pool", "11/01/2026", "2026-11-02"), "ISO date"),
    (("aeso.price.pool", "2020-01-01", "2026-11-02"), "5 years"),
    (("aeso.price.pool", "2026-11-01", "2026-11-02", "hour"), "offers freq"),
])
def test_bad_requests(store, args, msg):
    with pytest.raises(readapi.ReadError, match=msg):
        readapi.series(store, *args)


def test_list_series(store):
    rows = {r["metric"]: r for r in readapi.list_series(store, today=date(2026, 11, 10))}
    assert set(rows) == set(readapi.METRICS) and len(rows) == 14
    assert rows["aeso.price.pool"]["first_date"] == "2026-11-08" == rows["aeso.price.pool"]["last_date"]
    assert rows["ngtl.gdsr.field_receipts.flow"]["last_date"] == "2026-11-09"
    assert rows["eia.storage.lower48.working_gas"]["freqs"] == ["week"]
    assert rows["wx.calgary.tmean.realized"]["first_date"] is None        # no data: listed, no dates
    assert all(r["description"] and r["unit"] for r in rows.values())


def test_metrics_allowlist_is_curated():
    assert len(readapi.METRICS) == 14
    assert not any(k.startswith("region.") or "forecast" in k for k in readapi.METRICS)   # agreed: no estimates/forecasts


def test_http_routes(client):
    r = client.get("/api/read/series", params={"metric": "aeso.price.pool", "start": "2026-11-01", "end": "2026-11-10"})
    assert r.status_code == 200 and r.json()["points"] == [["2026-11-08", 20.0]]
    assert client.get("/api/read/series", params={"metric": "x.y", "start": "2026-11-01", "end": "2026-11-02"}).status_code == 404
    assert client.get("/api/read/series", params={"metric": "aeso.price.pool", "start": "x", "end": "y"}).status_code == 400
    ls = client.get("/api/read/list_series")
    assert ls.status_code == 200 and len(ls.json()) == 14


def test_http_routes_get_only(client):
    assert client.post("/api/read/series", params={"metric": "aeso.price.pool", "start": "2026-11-01",
                                                   "end": "2026-11-02"}).status_code == 405
    assert client.post("/api/read/list_series").status_code == 405


H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _rpc(c, method, params=None, i=1):
    r = c.post(readmcp.PATH, headers=H, content=json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                                            "params": params or {}}))
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_tools_read_only(client):
    _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
    tools = {t["name"]: t for t in _rpc(client, "tools/list", i=2)["result"]["tools"]}
    assert set(tools) == {"list_series", "series", "how_to_use"}
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools.values())
    res = _rpc(client, "tools/call", {"name": "series", "arguments": {
        "metric": "aeso.price.pool", "start": "2026-11-01", "end": "2026-11-10"}}, i=3)["result"]
    assert not res.get("isError")
    payload = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    payload = payload.get("result", payload)
    assert payload["points"] == [["2026-11-08", 20.0]]
    bad = _rpc(client, "tools/call", {"name": "series", "arguments": {
        "metric": "eia.storage.lower48.working_gas", "start": "2026-10-01", "end": "2026-11-01"}}, i=4)["result"]
    assert bad.get("isError") is True


def test_mcp_failure_leaves_map_serving(tmp_path, monkeypatch, store):
    """If the MCP can't be built, the map, /api/status and /api/read/* serve as before; /mcp/read answers 503."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def boom(get_store):
        raise ImportError("mcp SDK broken")
    monkeypatch.setattr(readmcp, "build", boom)
    from montney.server import create_app
    with TestClient(create_app(store=store, schedule=False)) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/read/list_series").status_code == 200
        assert c.post(readmcp.PATH, headers=H, content="{}").status_code == 503


def test_mcp_session_crash_leaves_map_serving(tmp_path, monkeypatch, store):
    """The SDK's session manager failing after the build only turns /mcp/read into a 503; the map serves on."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    real = readmcp.build

    class Dies:
        async def __aenter__(self):
            raise RuntimeError("session manager died")

        async def __aexit__(self, *exc):
            return False

    def build(get_store):
        server = real(get_store)
        orig = server.streamable_http_app

        def app(**kw):
            sub = orig(**kw)
            server.session_manager.run = Dies          # instance attribute shadows the method
            return sub
        server.streamable_http_app = app
        return server
    monkeypatch.setattr(readmcp, "build", build)
    from montney.server import create_app
    with TestClient(create_app(store=store, schedule=False)) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/read/list_series").status_code == 200
        assert c.post(readmcp.PATH, headers=H, content="{}").status_code == 503
