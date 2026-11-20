from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from montney.store import Obs, Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from montney.server import create_app
    st = Store(tmp_path / "t.duckdb")
    st.write([Obs("ngtl.csr.empress", datetime(2026, 10, 31, 19), 4576.0, datetime(2026, 10, 31, 19))])
    with TestClient(create_app(store=st, schedule=False)) as c:
        yield c


def test_catalog_seeded_before_fetch(client):
    ids = {s["id"] for s in client.get("/api/catalog").json()}
    assert "ngtl.gdsr.empress.flow" in ids and "wx.calgary.hdd" in ids


def test_series(client):
    d = client.get("/api/series", params={"ids": "ngtl.csr.empress", "start": "2026-10-01"}).json()
    assert d["ngtl.csr.empress"]["v"] == [4576.0]
    assert d["ngtl.csr.empress"]["meta"]["unit"] == "MMcf/d"


def test_status(client):
    s = client.get("/api/status").json()
    by = {x["id"]: x for x in s["sources"]}
    assert "AESO_API_KEY" in by["aeso_csd"]["secrets"]


# Runner heartbeat → UI state (#90; the beat can lag during a long `du`, hence a "stale" band before "down").
@pytest.mark.parametrize("status,state", [
    (None, "down"),
    ({"age_s": 0.0}, "running"),
    ({"age_s": 60.0}, "running"),     # boundary: RUNNER_STALE_S is inclusive
    ({"age_s": 60.1}, "stale"),
    ({"age_s": 300.0}, "stale"),      # boundary: RUNNER_DOWN_S is inclusive
    ({"age_s": 300.1}, "down"),
    ({"age_s": None}, "down"),        # an unreadable age must never read as alive (fail closed)
])
def test_runner_state_bands(status, state):
    from montney.server import runner_state

    assert runner_state(status)["state"] == state


def test_status_survives_an_unreadable_runner_heartbeat(tmp_path, monkeypatch):
    """If runner_status() raises (missing migration, grants, dropped connection), /api/status must still answer 200 and
    report the runner as down, failing closed. It must never 500 and blank every status dot."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from montney.server import create_app

    class BrokenHeartbeatStore(Store):
        def runner_status(self):
            raise RuntimeError("relation montney.runner_state does not exist")

    st = BrokenHeartbeatStore(tmp_path / "t.duckdb")
    with TestClient(create_app(store=st, schedule=False)) as c:
        r = c.get("/api/status")
    assert r.status_code == 200
    assert r.json()["runner"]["state"] == "down"
    assert r.json()["runner"]["reason"].startswith("unreadable: RuntimeError")
