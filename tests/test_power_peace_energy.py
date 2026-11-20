"""Peace dams annual-energy bound (montney/power_report.py, L-051): report-only, with NO-RESULT and a linear chain band.

A DuckDB store is seeded with hourly power.hydro.<plant>.mw_{est,lo,hi} points (the shape Weather's connector writes)
and read back through the same store.series() the CLI uses. Ground truth for the bound: BC Hydro's published average
annual generation (GMS 15, Peace Canyon 3.5, Site C 5.1 TWh).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from montney import power_report as pr
from montney.store import Obs, SeriesMeta, Store

NOW = datetime(2026, 11, 20, 12, 0)
DAYS = 30
MEAN_MW = {p: twh * 1e6 / 8760 for p, twh in {"wac_bennett": 15.0, "peace_canyon": 3.5, "site_c": 5.1}.items()}
LO, HI = 0.88, 1.12


def seed(tmp_path, days_covered=DAYS, scale=None, drop_lo_hours=0):
    st = Store(tmp_path / "p.duckdb")
    scale = scale or {}
    metas, obs = [], []
    for p, mw in MEAN_MW.items():
        for k in ("est", "lo", "hi"):
            sid = f"power.hydro.{p}.mw_{k}"
            metas.append(SeriesMeta(sid, "weather_peace_mw", sid, "MW", "1h", "Power / Peace hydro (estimated)"))
            f = {"est": 1.0, "lo": LO, "hi": HI}[k] * scale.get(p, 1.0)
            for h in range(days_covered * 24):
                if k == "lo" and h < drop_lo_hours:
                    continue
                t = NOW - timedelta(hours=h)
                v = mw * f * (1 + 0.2 * ((h % 24) - 11.5) / 11.5)   # a daily shape that averages out exactly
                obs.append(Obs(sid, t, v, t))
    st.upsert_meta(metas)
    st.write(obs)
    return st


def check_full_year_shape(rep):
    """The assertions a correct report must pass on full, on-published data (also the target of the seeded red)."""
    for p, r in rep["plants"].items():
        assert r["status"] == "within" and r["ratio"] == pytest.approx(1.0, abs=0.002), (p, r)
        assert r["published_in_band"] and r["n"] == DAYS * 24 and r["coverage"] == 1.0
    c, plants = rep["chain"], rep["plants"].values()
    for k in ("est", "lo", "hi"):
        assert c["twh_per_year"][k] == pytest.approx(sum(r["twh_per_year"][k] for r in plants), rel=1e-9), k
    assert c["published_twh"] == pytest.approx(23.6) and c["status"] == "within"


def test_full_window_on_published_energy(tmp_path):
    st = seed(tmp_path)
    rep = pr.peace_energy(st, NOW, days=DAYS)
    st.close()
    check_full_year_shape(rep)
    assert rep["plants"]["wac_bennett"]["twh_per_year"]["est"] == pytest.approx(15.0, rel=2e-3)
    assert "linear" in rep["chain"]["band_rule"]


def test_seeded_red_quadrature_chain_is_caught(tmp_path, monkeypatch):
    st = seed(tmp_path)
    monkeypatch.setattr(pr, "_CHAIN_ADD", "quadrature")
    rep = pr.peace_energy(st, NOW, days=DAYS)
    st.close()
    with pytest.raises(AssertionError):
        check_full_year_shape(rep)                       # the narrower quadrature band no longer equals sum of lo/hi
    assert rep["chain"]["twh_per_year"]["hi"] < sum(r["twh_per_year"]["hi"] for r in rep["plants"].values())


def test_thin_coverage_is_no_result(tmp_path):
    st = seed(tmp_path, days_covered=20)                 # 20 of 30 days = 67 % < 75 %
    rep = pr.peace_energy(st, NOW, days=DAYS)
    st.close()
    for r in list(rep["plants"].values()) + [rep["chain"]]:
        assert r["status"] == "NO-RESULT" and "to date" in r["reason"] and r["ratio"] is not None, r
        assert "published_in_band" not in r


def test_outside_bound_is_reported(tmp_path):
    st = seed(tmp_path, scale={"wac_bennett": 1.3})
    rep = pr.peace_energy(st, NOW, days=DAYS)
    st.close()
    assert rep["plants"]["wac_bennett"]["status"] == "outside"
    assert rep["plants"]["peace_canyon"]["status"] == "within"


def test_hours_without_the_band_are_not_counted(tmp_path):
    st = seed(tmp_path, drop_lo_hours=48)                # the estimate without its low band: not counted
    rep = pr.peace_energy(st, NOW, days=DAYS)
    st.close()
    assert all(r["n"] == DAYS * 24 - 48 for r in rep["plants"].values()) and rep["chain"]["n"] == DAYS * 24 - 48


def test_empty_store_fails_closed(tmp_path):
    st = Store(tmp_path / "e.duckdb")
    rep = pr.peace_energy(st, NOW, days=DAYS)
    st.close()
    for r in list(rep["plants"].values()) + [rep["chain"]]:
        assert r["status"] == "NO-RESULT" and r["n"] == 0 and r["twh_per_year"]["est"] is None


def test_published_figures_and_format(tmp_path):
    assert pr.PUBLISHED_TWH == {"wac_bennett": 15.0, "peace_canyon": 3.5, "site_c": 5.1}
    st = seed(tmp_path)
    txt = pr.format_peace_energy(pr.peace_energy(st, NOW, days=DAYS))
    st.close()
    assert "Peace chain" in txt and "every term linear" in txt and "NO-RESULT" not in txt
    txt.encode("cp1252")                                  # the Windows console: no Greek letters or plus-minus


def test_cli_report_parses():
    from montney import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["report", "peace-energy", "--help"])
    assert e.value.code == 0
