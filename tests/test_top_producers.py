"""/api/top-producers (L-075): selection (≥ 80th percentile, capped), history, placement, and the empty cases.

Facilities and volumes are made up; the store is a throwaway DuckDB file.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from montney import top_producers as tp
from montney.server import create_app
from montney.store import Obs, SeriesMeta, Store

AS_OF = datetime(2026, 11, 1)
SEP, AUG = datetime(2026, 9, 1), datetime(2026, 8, 1)


def seed(st: Store, home, n: int = 10, province: str = "ab", unplaced: tuple[str, ...] = ()) -> list[str]:
    """n facilities F00..F(n-1); F00 is the biggest (Sep = 100 − i). F01 has no Aug report."""
    ids = [f"F{i:02d}" for i in range(n)]
    tp.save_registry(province, {f: {"name": f"Plant {f}", "operator": "Op", "subtype": "GAS PLANT",
                                    "location": "bad" if f in unplaced else "04-08-075-07W6"} for f in ids}, home)
    sids = [tp.SERIES[province].format(f.lower()) for f in ids]
    st.upsert_meta([SeriesMeta(s, "t", s, "MMcf/d", "M") for s in sids]
                   + [SeriesMeta(tp.TOTAL[province], "t", "total", "MMcf/d", "M")])
    obs = [Obs(tp.TOTAL[province], SEP, 1000.0, AS_OF)]
    for i, s in enumerate(sids):
        obs.append(Obs(s, SEP, float(1000 - i), AS_OF))
        if i != 1:
            obs.append(Obs(s, AUG, float(900 - i), AS_OF))
    st.write(obs)
    return ids


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "t.duckdb")
    yield s
    s.close()


def test_top_fifth_ranked_with_24_months_of_history(st, tmp_path):
    seed(st, tmp_path, n=10)
    fc = tp.build(st, "ab", home=tmp_path, use_cache=False)
    assert fc["type"] == "FeatureCollection" and fc["province"] == "ab" and fc["unit"] == "MMcf/d"
    assert fc["as_of"] == "2026-09" and fc["unplaced"] == 0
    assert [f["properties"]["id"] for f in fc["features"]] == ["F00", "F01"]          # ceil(10 × 0.2) = 2
    p = fc["features"][1]["properties"]
    assert (p["rank"], p["name"], p["operator"], p["kind"], p["latest"]) == (2, "Plant F01", "Op", "facility", 999.0)
    assert p["series"] == "petrinex.f01.sales_gas"
    assert len(p["history"]["t"]) == 24 and p["history"]["t"][0] == "2024-10" and p["history"]["t"][-1] == "2026-09"
    assert p["history"]["v"][-1] == 999.0 and p["history"]["v"][-2] is None       # no Aug report → null
    assert fc["features"][0]["properties"]["history"]["v"][-2] == 900.0
    lon, lat = fc["features"][0]["geometry"]["coordinates"]
    assert 55.4 < lat < 55.6 and -119.1 < lon < -118.9
    assert fc["basis"].startswith("2 of 10 reporting facilities")


def test_capped_at_max_points(st, tmp_path):
    seed(st, tmp_path, n=600)
    fc = tp.build(st, "ab", home=tmp_path, use_cache=False)
    assert len(fc["features"]) == tp.MAX_POINTS == 100                           # 120 at ≥ P80, capped
    assert [f["properties"]["rank"] for f in fc["features"]] == list(range(1, 101))
    assert "capped at the top 100" in fc["basis"]


def test_unplaced_keeps_its_rank_but_gets_no_point(st, tmp_path):
    seed(st, tmp_path, n=10, unplaced=("F00",))
    fc = tp.build(st, "ab", home=tmp_path, use_cache=False)
    assert [f["properties"]["rank"] for f in fc["features"]] == [2] and fc["unplaced"] == 1


def test_empty_cases_say_why(st, tmp_path):
    fc = tp.build(st, "bc", home=tmp_path, use_cache=False)
    assert fc["features"] == [] and "gas_bcer_production" in fc["reason"]          # no registry yet
    tp.save_registry("bc", {"BCGP1": {"name": "x", "location": "01-01-080-15W6"}}, tmp_path)
    fc = tp.build(st, "bc", home=tmp_path, use_cache=False)
    assert fc["features"] == [] and "production.bc.monthly" in fc["reason"]        # no total month yet
    with pytest.raises(ValueError):
        tp.build(st, "sk", home=tmp_path)


def test_route(st, tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    seed(st, tmp_path, n=5, province="bc")
    tp._cache.clear()
    with TestClient(create_app(store=st, schedule=False)) as c:
        r = c.get("/api/top-producers", params={"province": "bc"})
        assert r.status_code == 200
        body = r.json()
        assert body["province"] == "bc" and [f["properties"]["id"] for f in body["features"]] == ["F00"]
        assert body["features"][0]["properties"]["series"] == "bcer.f00.plant_outlet"
        assert c.get("/api/top-producers", params={"province": "sk"}).status_code == 422
