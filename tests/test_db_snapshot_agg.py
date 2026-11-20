"""Window means in the snapshot (#150 contract, L-033): `agg: "1h" | "24h"` on a feature, or `?extra=<series>@<window>`.

No database: the window is (now - W, now] exactly, thin data is a NO-RESULT with a reason (never a mean of a few points
dressed as a window), and a daily series has no window mean. Seeded reds: the window shifted an hour early, and the
n floor ignored. Database (CI): DuckDB's v1 Store and PostgresStore, given the same writes (including a revised point),
give the same window means through snapshot.build (computed from the same latest-vintage points).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from montney import snapshot

NOW = datetime(2026, 11, 2, 18, 0, 0)
NOW_E = (NOW - datetime(1970, 1, 1)).total_seconds()
M5 = {"freq": "5min", "unit": "MMcf/d"}


def _series(points: list[tuple[float, float]]) -> dict:
    """points: (seconds before NOW, value)"""
    pts = sorted((NOW_E - back, v) for back, v in points)
    return {"ts": [t for t, _ in pts], "v": [v for _, v in pts]}


def _hour_of_5min(value_at=lambda i: float(i)) -> dict:
    # 12 points with ts in (now-1h, now]: now-55m ... now, plus one EXACTLY at now-1h (outside) and one before it
    pts = [(300 * k, value_at(k)) for k in range(0, 12)]
    pts += [(3600, 1000.0), (3900, 2000.0)]                            # at the window start (excluded) and before
    return _series(pts)


def test_the_window_is_now_minus_w_exclusive_to_now_inclusive():
    a = snapshot.window_mean(_hour_of_5min(), M5, "1h", NOW_E)
    print(a)
    assert a["n"] == 12 and a["n_min"] == 9 and a["reason"] is None
    assert a["mean"] == pytest.approx(sum(range(12)) / 12)                 # the 1000 at now-1h is NOT in
    assert (a["start"], a["end"]) == (NOW_E - 3600, NOW_E)


@pytest.mark.parametrize("shift", [0, 3600], ids=["window-ends-now", "RED-window-an-hour-early"])
def test_RED_a_window_off_by_an_hour_is_caught(shift, monkeypatch):
    """Seeded red: the same check on a window ending an hour early must fail (it averages the previous hour)."""
    monkeypatch.setattr(snapshot, "_AGG_END_SHIFT_S", shift)
    pts = [(300 * k, 10.0) for k in range(12)] + [(3600 + 300 * k, 50.0) for k in range(12)]   # last hour 10, the one before 50
    a = snapshot.window_mean(_series(pts), M5, "1h", NOW_E)
    print(shift, a["mean"], a["start"], a["end"])
    ok = a["mean"] == pytest.approx(10.0) and a["end"] == NOW_E
    assert ok if shift == 0 else not ok


@pytest.mark.parametrize("floor", [0.75, 0.0], ids=["n-floor", "RED-n-floor-ignored"])
def test_RED_thin_data_is_a_no_result_not_a_dressed_up_mean(floor, monkeypatch):
    """5 points in an hour of a 5-min series (floor 9), and 2 h of data for a 24 h window (floor 216): mean None with a
    reason. Seeded red: with the floor ignored, a mean comes back and the check fails."""
    monkeypatch.setattr(snapshot, "AGG_MIN_COVERAGE", floor)
    thin_hour = _series([(300 * k, 5.0) for k in range(5)])
    two_hours = _series([(300 * k, 5.0) for k in range(24)])
    results = [snapshot.window_mean(thin_hour, M5, "1h", NOW_E), snapshot.window_mean(two_hours, M5, "24h", NOW_E)]
    print(floor, [(r["mean"], r["n"], r["n_min"], r["reason"]) for r in results])
    no_result = all(r["mean"] is None and r["reason"].startswith("thin: ") for r in results)
    assert no_result if floor else not no_result
    if floor:
        assert [(r["n"], r["n_min"]) for r in results] == [(5, 9), (24, 216)]


@pytest.mark.parametrize("freq, agg", [("D", "1h"), ("D", "24h"), ("1h", "1h")])
def test_no_window_mean_for_a_series_not_finer_than_the_window(freq, agg):
    a = snapshot.window_mean(_series([(0, 1.0), (3600, 2.0)]), {"freq": freq}, agg, NOW_E)
    assert a["mean"] is None and "not finer" in a["reason"]


def test_an_unknown_window_is_a_reason_not_a_crash():
    a = snapshot.window_mean(_hour_of_5min(), M5, "7d", NOW_E)
    assert a["mean"] is None and "unknown window" in a["reason"]


def test_an_hourly_series_has_a_24h_mean():
    s = _series([(3600 * k, float(k)) for k in range(24)])
    a = snapshot.window_mean(s, {"freq": "1h"}, "24h", NOW_E)
    assert (a["n"], a["n_min"]) == (24, 18) and a["mean"] == pytest.approx(sum(range(24)) / 24)


# ------------------------------------------------------------------ both stores through snapshot.build (CI)
def _write(stores, sid, src):
    from montney.db import pgstore as pgmod
    from montney.db import store as dbstore
    from montney import store as v1mod
    from montney.store import Obs, SeriesMeta

    for st in stores:
        st.upsert_meta([SeriesMeta(sid, src, unit="MMcf/d", freq="5min")])
    pts = [NOW - timedelta(minutes=5 * k) for k in range(0, 30 * 12)]          # 30 h of 5-min points
    first = [Obs(sid, t, 100.0 + (k % 17), t) for k, t in enumerate(pts)]
    revised = [Obs(sid, pts[3], 555.0, pts[3] + timedelta(minutes=7))]      # a revision inside the last hour
    for st in stores:
        st.write(first)
        st.write(revised)
    return v1mod, dbstore, pgmod


def test_duckdb_and_postgres_give_the_same_window_means(tmp_path, monkeypatch):
    from montney.db.pgstore import PostgresStore
    from montney.db.testing import scratch_db
    from montney.store import Store

    with scratch_db() as conn:
        v1, pg = Store(tmp_path / "v1.duckdb"), PostgresStore(conn)
        sid = "test.agg.flow"
        v1mod, dbstore, pgmod = _write([v1, pg], sid, "test_agg")
        for m in (v1mod, dbstore, pgmod):
            monkeypatch.setattr(m, "_utcnow", lambda: NOW)
        monkeypatch.setattr(snapshot, "utcnow", lambda: NOW)
        args = dict(refs={"f.flow": {"value": sid}}, compares={}, extra=[sid, f"{sid}@1h", f"{sid}@24h"],
                    aggs={"f.flow": "24h"})
        a, b = snapshot.build(v1, **args), snapshot.build(pg, **args)
        v1.close()
    got = {k: (a["series"][k].get("agg"), b["series"][k].get("agg")) for k in (f"{sid}@1h", f"{sid}@24h")}
    got["feature"] = (a["values"]["f.flow"]["agg"], b["values"]["f.flow"]["agg"])
    for k, (x, y) in got.items():
        print(k, x["mean"], x["n"], "|", y["mean"], y["n"])
        assert x["reason"] is None and x["n"] >= x["n_min"]
        assert (x["n"], x["n_min"], x["start"], x["end"]) == (y["n"], y["n_min"], y["start"], y["end"])
        assert x["mean"] == pytest.approx(y["mean"], rel=1e-12)
    # the revision counts once, with its latest value (555), in both: the 1 h mean moves by (555 - original) / 12
    assert got[f"{sid}@1h"][0]["n"] == 12 and got[f"{sid}@24h"][0]["n"] == 288
    assert a["series"][sid].get("agg") is None                                   # a bare id has no window mean


def test_build_carries_agg_for_feature_refs_and_extra_tokens_duckdb(tmp_path, monkeypatch):
    """End to end on the code default (DuckDB), no Postgres needed: a feature `agg` and `?extra=` tokens come back
    with the window mean; a bare id keeps the plain entry; a daily series' token says why there is no mean."""
    from montney import store as v1mod
    from montney.store import Obs, SeriesMeta, Store

    monkeypatch.setattr(v1mod, "_utcnow", lambda: NOW)
    monkeypatch.setattr(snapshot, "utcnow", lambda: NOW)
    st = Store(tmp_path / "v1.duckdb")
    st.upsert_meta([SeriesMeta("t.fast", "src", freq="5min"), SeriesMeta("t.daily", "src", freq="D")])
    st.write([Obs("t.fast", NOW - timedelta(minutes=5 * k), float(k % 4), NOW - timedelta(minutes=5 * k))
              for k in range(0, 300)])
    st.write([Obs("t.daily", datetime(2026, 11, 1), 7.0, datetime(2026, 11, 2))])
    out = snapshot.build(st, {"f": {"value": "t.fast"}}, {}, extra=["t.fast", "t.fast@1h", "t.daily@24h"],
                         aggs={"f": "1h"})
    st.close()
    assert out["values"]["f"]["agg"]["mean"] == pytest.approx(sum(k % 4 for k in range(12)) / 12)
    assert out["series"]["t.fast@1h"]["agg"] == out["values"]["f"]["agg"]
    assert out["series"]["t.fast@1h"]["v"] == out["series"]["t.fast"]["v"]   # the token keeps the latest-point fields
    assert "agg" not in out["series"]["t.fast"]
    d = out["series"]["t.daily@24h"]["agg"]
    assert d["mean"] is None and "not finer" in d["reason"]
