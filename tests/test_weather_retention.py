"""Retention tiers of the weather specs (docs/db/RETENTION.md spec contract), pinned by weather.

Two layers, like tests/test_power_retention.py and tests/test_gas_retention.py:
1. the declared mapping: each weather spec declares the tier weather ruled for it, and every declared series
   resolves to that tier (so a flipped tier or a new series under the wrong rule shows up here);
2. the shipped path: the three python connectors run on the synthetic/recorded payloads from
   test_weather_points.py, test_weather_fires.py and test_weather_gfs.py, and what they emit is classified:
   - an observation stamped as_of == ts is an actual: never `forecast`;
   - any other as_of (Open-Meteo stamps the fetch hour) is a run vintage: it must be `forecast` (L-038);
   - an `events` spec emits events and no observations, and a `grid` spec emits grids and no observations
     (those rows would have no observation tier to land in).
   An empty run is NO-RESULT and fails (Wellsite §4.2).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import FetchResult
from montney.db.backfill import spec_tier
from montney.store import Obs
from test_weather_fires import HOTSPOTS, make as make_fires
from test_weather_gfs import make as make_gfs, serve_all
from test_weather_points import run as run_points
from test_weather_wsc import ctx as wsc_ctx, daily_rows, handler_factory as wsc_handler
from test_weather_abrivers import doc as ab_doc, run as run_ab
from test_weather_peace_mw import run as run_peace_mw

SPECS = load_specs(include_disabled=True)
WEATHER = sorted(s for s in SPECS if s.startswith("weather"))

# Weather's ruling (owner of sources/weather*.yaml). Open-Meteo: forecast, except the realized actuals (L-039).
PINNED = {"weather": {"default": "forecast", "wx.*.realized": "daily"},
          "weather_fires": "events", "weather_gfs": "grid", "weather_gfs_east": "grid",
          "weather_wsc": "fast", "weather_wsc_daily": "daily",       # WSC gauges: actuals, as_of = ts
          "weather_abrivers": "hourly",                                # AB Rivers reservoirs: hourly actuals
          "weather_peace_mw": {"default": "hourly", "bchydro.hydromet.*": "fast"},   # 24 h MW estimates; 5-min levels
          "weather_flow_normal": "hourly",
          "weather_usgs": "fast", "weather_usgs_daily": "daily",     # L-085 Columbia hydro: The Dalles + boundary
          "weather_columbia_hydro": "daily",
          # L-090: California + PacNW cities (Open-Meteo, as "weather") and the south grid
          "weather_cal": {"default": "forecast", "wx.*.realized": "daily"},
          "weather_pnw": {"default": "forecast", "wx.*.realized": "daily"},
          "weather_gfs_south": "grid",
          # L-091: Ontario + Quebec cities (Open-Meteo, as "weather")
          "weather_ont": {"default": "forecast", "wx.*.realized": "daily"},
          "weather_qc": {"default": "forecast", "wx.*.realized": "daily"},
          "weather_mb_hydro": "daily", "weather_qc_hydro": "daily",
          # L-106: the US region city specs (Open-Meteo, as "weather")
          **{f"weather_{r}": {"default": "forecast", "wx.*.realized": "daily"}
             for r in ("upne", "nyc", "umw", "bakken", "mtnw", "sunbelt", "tx")}}     # L-091 hydro summaries, as_of = ts                         # storage + runoff forecast, as_of = ts


def expected_tier(sid: str, series_id: str) -> str:
    if sid in ("weather", "weather_cal", "weather_pnw", "weather_ont", "weather_qc", "weather_upne", "weather_nyc",
               "weather_umw", "weather_bakken", "weather_mtnw", "weather_sunbelt", "weather_tx"):
        return "daily" if series_id.endswith(".realized") else "forecast"
    if sid == "weather_peace_mw":
        return "fast" if series_id.startswith("bchydro.hydromet.") else "hourly"
    return PINNED[sid]


def test_pinned_specs_are_all_the_weather_specs():
    assert set(WEATHER) == set(PINNED), "a weather spec was added or renamed: pin its tier here"


@pytest.mark.parametrize("sid", sorted(PINNED))
def test_declared_tier(sid):
    assert SPECS[sid].get("retention") == PINNED[sid]
    for m in build(SPECS[sid]).declared_series():
        assert spec_tier(m.series_id, SPECS[sid]) == expected_tier(sid, m.series_id), m.series_id


def test_realized_series_are_declared_daily():
    ids = [m.series_id for m in build(SPECS["weather"]).declared_series()]
    realized = [i for i in ids if i.endswith(".realized")]
    assert len(realized) == 72, realized      # 20 cities (+5 L-106) + 4 composites (+region.wcan/.mbnwo), tmean/hdd/cdd
    assert {"wx.alberta.hdd.realized", "wx.calgary.tmean.realized", "wx.nebc.hdd.realized"} <= set(realized)


def emitted_problems(sid: str, res: FetchResult, spec: dict) -> list[str]:
    """How a run's output contradicts the spec's tier. Empty = consistent. NO-RESULT is a problem."""
    tier = spec.get("retention") if isinstance(spec.get("retention"), str) else None
    if tier == "events":
        out = [] if res.events else [f"{sid}: no events emitted (NO-RESULT)"]
        return out + ([f"{sid}: events spec emitted {len(res.obs)} observations"] if res.obs else [])
    if tier == "grid":
        out = [] if res.grids else [f"{sid}: no grids emitted (NO-RESULT)"]
        return out + ([f"{sid}: grid spec emitted {len(res.obs)} observations"] if res.obs else [])
    if not res.obs:
        return [f"{sid}: no observations emitted (NO-RESULT)"]
    by_series: dict[str, list[Obs]] = {}
    for o in res.obs:
        by_series.setdefault(o.series_id, []).append(o)
    out = []
    for s, obs in sorted(by_series.items()):
        t = spec_tier(s, spec)
        if all(o.as_of == o.ts for o in obs):
            if t == "forecast":
                out.append(f"{s}: actual (as_of == ts) classed forecast")
        elif t != "forecast":
            out.append(f"{s}: run-vintage series (as_of != ts) classed {t!r}")
    return out


def _run_points(tmp_path, monkeypatch) -> FetchResult:
    res, _ = run_points(tmp_path, monkeypatch)
    return res


def _run_fires(tmp_path, monkeypatch) -> FetchResult:
    conn, ctx, _ = make_fires(HOTSPOTS)
    return conn.fetch(ctx)


def _run_gfs(tmp_path, monkeypatch) -> FetchResult:
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    conn, ctx, _, _ = make_gfs(serve_all)
    return conn.fetch(ctx)


def _run_wsc(tmp_path, monkeypatch) -> FetchResult:
    spec = {**SPECS["weather_wsc"], "params": {**SPECS["weather_wsc"]["params"], "stations": ["07FA004", "05BH004"]}}
    return build(spec).run(wsc_ctx(wsc_handler()))


def _run_wsc_daily(tmp_path, monkeypatch) -> FetchResult:
    spec = {**SPECS["weather_wsc_daily"], "params": {**SPECS["weather_wsc_daily"]["params"], "stations": ["07FD002"]}}
    return build(spec).run(wsc_ctx(wsc_handler(daily={"07FD002": daily_rows("07FD002", "2025-10-20", 5)})))


def _run_abrivers(tmp_path, monkeypatch) -> FetchResult:
    return run_ab({"05BE005": ab_doc()}, ["05BE005"])


RUNS = {"weather": _run_points, "weather_fires": _run_fires, "weather_gfs": _run_gfs,
        "weather_wsc": _run_wsc, "weather_wsc_daily": _run_wsc_daily, "weather_abrivers": _run_abrivers,
        "weather_peace_mw": lambda tmp_path, monkeypatch: run_peace_mw()}


@pytest.mark.parametrize("sid", sorted(RUNS))
def test_emitted_output_fits_the_tier(sid, tmp_path, monkeypatch):
    res = RUNS[sid](tmp_path, monkeypatch)
    assert emitted_problems(sid, res, SPECS[sid]) == []


def test_open_meteo_stamps_every_point_with_the_fetch_hour(tmp_path, monkeypatch):
    """The premise of `forecast` (L-038): no non-realized point is stamped as its own time, past days included.
    Only the realized actuals (L-039) are as_of == ts."""
    res = _run_points(tmp_path, monkeypatch)
    plain = [o for o in res.obs if not o.series_id.endswith(".realized")]
    realized = [o for o in res.obs if o.series_id.endswith(".realized")]
    assert len(plain) > 100 and realized, (len(plain), len(realized))   # a short count would be NO-RESULT
    assert {o.as_of for o in plain} == {datetime(2026, 11, 1, 18, 0)}
    assert any(o.ts < o.as_of for o in plain) and any(o.ts > o.as_of for o in plain)  # past + forecast days
    assert all(o.as_of == o.ts for o in realized)


# ---- seeded reds (Wellsite §4.1): each must be caught
def test_seeded_red_forecast_spec_flipped_to_daily(tmp_path, monkeypatch):
    res = _run_points(tmp_path, monkeypatch)
    bad = emitted_problems("weather", res, {**SPECS["weather"], "retention": "daily"})
    assert any("wx.comp.hdd: run-vintage series" in b and "'daily'" in b for b in bad), bad


def test_seeded_red_actual_classed_forecast():
    ts = datetime(2026, 11, 1)
    res = FetchResult(obs=[Obs("wx.calgary.tmax", ts, 5.0, ts)])
    assert any("actual (as_of == ts) classed forecast" in b
               for b in emitted_problems("weather", res, SPECS["weather"]))


def test_seeded_red_events_and_grid_specs_emitting_observations():
    ts = datetime(2026, 11, 1)
    obs = [Obs("weather.x.y", ts, 1.0, ts)]
    fires = emitted_problems("weather_fires", FetchResult(obs=obs, events=[object()]), SPECS["weather_fires"])
    gfs = emitted_problems("weather_gfs", FetchResult(obs=obs, grids={"t2m": {}}), SPECS["weather_gfs"])
    assert any("events spec emitted 1 observations" in b for b in fires), fires
    assert any("grid spec emitted 1 observations" in b for b in gfs), gfs


@pytest.mark.parametrize("sid", sorted(PINNED))
def test_seeded_red_empty_run_is_no_result(sid):
    assert any("NO-RESULT" in b for b in emitted_problems(sid, FetchResult(), SPECS[sid]))
