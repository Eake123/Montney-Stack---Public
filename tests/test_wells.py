"""/api/wells (L-106 8b): every reporting AB + BC facility as a map node, tier by rank, value inline, hand-placed
facilities left out, and a click on one resolving through /api/snapshot?features=.

Facilities and volumes are made up; the store is a throwaway DuckDB file and the registry sits in tmp_path.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from montney import top_producers as tp
from montney import wells
from montney.server import create_app
from montney.store import Obs, SeriesMeta, Store

AS_OF = datetime(2026, 11, 1)
SEP = datetime(2026, 9, 1)


def fid(i: int) -> str:
    return f"ABGP{i:07d}"


def seed(st: Store, home, n: int, province: str = "ab", month: datetime = SEP) -> list[str]:
    ids = [fid(i) if province == "ab" else f"BCGP{3165000 + i}" for i in range(n)]
    tp.save_registry(province, {f: {"name": f"Plant {f}", "operator": "Op", "subtype": "GAS PLANT",
                                    "location": "04-08-075-07W6" if province == "ab" else "01-01-080-15W6"}
                                for f in ids}, home)
    sids = [tp.SERIES[province].format(f.lower()) for f in ids]
    st.upsert_meta([SeriesMeta(s, "t", s, "MMcf/d", "M") for s in sids]
                   + [SeriesMeta(tp.TOTAL[province], "t", "total", "MMcf/d", "M")])
    obs = [Obs(tp.TOTAL[province], month, 100_000.0, AS_OF)]
    obs += [Obs(s, month, float(1000 - i), AS_OF) for i, s in enumerate(sids)]
    obs.append(Obs(sids[-1], month.replace(month=month.month - 1), 5.0, AS_OF))     # an older month: not "latest"
    st.write(obs)
    return ids


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "t.duckdb")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    wells._cache.clear()
    monkeypatch.setattr(wells, "utcnow", lambda: datetime(2026, 11, 7))


def test_every_facility_tier_by_rank_value_inline(st, tmp_path):
    seed(st, tmp_path, n=120)
    fc = wells.collection(st, {"features": []}, home=tmp_path)
    assert fc["licence"] == "personal" and fc["provinces"]["ab"]["facilities"] == 120
    assert fc["provinces"]["bc"]["facilities"] == 0 and fc["provinces"]["bc"]["reason"]
    feats = fc["features"]
    assert len(feats) == 120                                                        # all of them, not the top 100
    p = feats[0]["properties"]
    assert feats[0]["id"] == p["id"] == "gas.well.ab.abgp0000000"
    assert (p["kind"], p["system"], p["type"], p["licence"], p["province"]) == ("node", "gas", "facility",
                                                                                 "personal", "ab")
    assert p["value"] == "petrinex.abgp0000000.sales_gas" and p["panel"] == {"series": [p["value"]]}
    assert (p["v"], p["rank"], p["share"], p["unit"], p["freq"]) == (1000.0, 1, 0.01, "MMcf/d", "M")
    assert p["ts"] == (SEP - datetime(1970, 1, 1)).total_seconds() and p["stale"] is False
    tiers = [f["properties"]["tier"] for f in feats]
    assert tiers[:wells.TOP_N] == [2] * 100 and set(tiers[wells.TOP_N:]) == {3}
    assert "callout" not in p and "callouts_extra" not in p                         # dots only: never crowd the boxes


def test_hand_placed_facilities_are_left_out(st, tmp_path):
    seed(st, tmp_path, n=5)
    net = {"features": [{"id": "gas.x", "properties": {"value": "petrinex.abgp0000001.sales_gas"}},
                        {"id": "gas.y", "properties": {"panel": {"series": ["petrinex.abgp0000003.sales_gas"]}}}]}
    assert wells.placed_by_hand(net) == {"ABGP0000001", "ABGP0000003"}
    ids = [f["properties"]["facility_id"] for f in wells.collection(st, net, home=tmp_path)["features"]]
    assert ids == [fid(0), fid(2), fid(4)]


def test_stale_month_is_flagged(st, tmp_path):
    seed(st, tmp_path, n=2, month=datetime(2026, 5, 1))
    assert all(f["properties"]["stale"] for f in wells.collection(st, {"features": []}, home=tmp_path)["features"])


def test_route_gzip_etag_and_click(st, tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    seed(st, tmp_path, n=3, province="bc")
    with TestClient(create_app(store=st, schedule=False)) as c:
        r = c.get("/api/wells", headers={"accept-encoding": "gzip"})
        assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip" and r.headers["etag"]
        fc = r.json()
        assert [f["properties"]["facility_id"] for f in fc["features"]] == ["BCGP3165000", "BCGP3165001",
                                                                            "BCGP3165002"]
        assert c.get("/api/wells", headers={"if-none-match": r.headers["etag"]}).status_code == 304
        wid = fc["features"][0]["id"]
        snap = c.get("/api/snapshot", params={"features": wid}).json()                # a click on a well resolves
        assert snap["values"][wid]["v"] == pytest.approx(1000.0)
        assert snap["values"][wid]["series"] == "bcer.bcgp3165000.plant_outlet"
        net = c.get("/api/network").json()
        assert not any(f["id"].startswith("gas.well.") for f in net["features"])       # /api/network unchanged
