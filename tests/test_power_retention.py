"""Retention tiers of the AESO specs (docs/db/RETENTION.md spec contract), pinned by power.

The backfill (#64) classes a series `forecast` if any row was known before its time (as_of < ts).
Forecast-tier rows are thinned and dropped after a year. These tests make sure every actual series in
sources/aeso_*.yaml is pinned to a non-forecast tier and every true forecast to `forecast`. They check
both the declared mapping and the shipped connector path (real parse of the synthetic fixtures).
"""

from __future__ import annotations

import fnmatch
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import Context

SPECS = load_specs(include_disabled=True)
AESO = sorted(s for s in SPECS if s.startswith("aeso_"))
TIERS = {"fast", "hourly", "daily", "forecast", "live", "events", "grid"}
FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 31, 20, 10)

# Power's ruling (owner of sources/aeso_*.yaml). Concrete ids, so a renamed series shows up here.
ACTUALS = {
    "aeso.price.pool": "hourly", "aeso.price.avg30": "hourly",           # as_of: ts
    "aeso.load.ail": "hourly",
    "aeso.gen.combined_cycle.net": "fast", "aeso.gen.gas_total.net": "fast",   # CSD and its derives
    "aeso.csd.ail": "fast", "aeso.intertie.bc": "fast",
    "aeso.unit.egc1.tng": "fast", "aeso.unit.egc1.mc": "fast", "aeso.plant.genesee.tng": "fast",
    "aeso.wind.actual": "fast", "aeso.solar.actual": "fast",
    # L-036: the realized outage value (last value known on the day, as_of = ts) is actual history.
    "aeso.outage.cc.realized": "daily", "aeso.outage.gas_total.realized": "daily",
    "aeso.outage.monthly.cc.realized": "daily", "aeso.outage.monthly.gas_total.realized": "daily",
}
FORECASTS = {
    "aeso.price.forecast", "aeso.load.ail_forecast",
    "aeso.wind.st.forecast.likely", "aeso.wind.lt.forecast.max", "aeso.solar.lt.forecast.min",
    "aeso.outage.monthly.combined_cycle", "aeso.outage.monthly.gas_total",
    # L-036: outage revisions, daily and monthly alike, are forecast-tier; Database keeps the realized value.
    "aeso.outage.gas_total", "aeso.outage.cc", "aeso.outage.mbo",
}


def _source_rule(spec: dict, series_id: str) -> str | None:
    r = spec.get("retention")
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        hits = [t for pat, t in r.items() if pat != "default" and fnmatch.fnmatchcase(series_id, pat)]
        assert len(hits) <= 1, f"{spec['id']}: {series_id} matches {len(hits)} retention patterns (L-036)"
        return hits[0] if hits else r.get("default")
    return None


def tier_of(series_id: str, specs: dict = SPECS) -> str:
    """Tier for a series: the extract block that declares it, else the source rule (globs, then default)."""
    for spec in specs.values():
        for block in spec.get("extract", []) or []:
            ids = {f.get("id") for f in (block.get("fields") or {}).values() if isinstance(f, dict)}
            if series_id in ids:
                return block.get("retention") or _source_rule(spec, series_id)
        if any(d.get("id") == series_id for d in spec.get("derive", []) or []):
            return _source_rule(spec, series_id)
    prefix = {"aeso.price.": "aeso_price", "aeso.load.": "aeso_load", "aeso.gen.": "aeso_csd",
              "aeso.csd.": "aeso_csd", "aeso.intertie.": "aeso_csd",
              "aeso.unit.": "aeso_ets_units", "aeso.plant.": "aeso_ets_units",
              "aeso.outage.monthly.": "aeso_ets_outages_monthly", "aeso.outage.": "aeso_ets_outages",
              "aeso.wind.": "aeso_ets_wind_solar", "aeso.solar.": "aeso_ets_wind_solar"}
    for p, sid in prefix.items():  # python connectors: series come from the fetch, so map by family
        if series_id.startswith(p):
            return _source_rule(specs[sid], series_id)
    raise KeyError(series_id)


def misclassified(specs: dict = SPECS) -> list[str]:
    bad = [f"{s}: actual pinned to {tier_of(s, specs)!r}, want {want!r}"
           for s, want in ACTUALS.items() if tier_of(s, specs) != want]
    bad += [f"{s}: forecast pinned to {tier_of(s, specs)!r}" for s in FORECASTS if tier_of(s, specs) != "forecast"]
    return bad


@pytest.mark.parametrize("sid", AESO)
def test_every_aeso_spec_declares_a_valid_retention(sid):
    spec = SPECS[sid]
    r = spec.get("retention")
    assert r is not None, f"{sid}: no retention: (RETENTION.md spec contract)"
    vals = [r] if isinstance(r, str) else list(r.values())
    vals += [b["retention"] for b in spec.get("extract", []) or [] if "retention" in b]
    assert vals and set(vals) <= TIERS, f"{sid}: {vals}"
    if isinstance(r, dict):
        assert "default" in r, f"{sid}: a retention mapping needs a default"
        assert spec.get("type") == "python", f"{sid}: glob retention mappings are for type: python specs only (L-036)"


def test_actuals_never_forecast_and_forecasts_pinned():
    assert misclassified() == []


def test_http_blocks_match_their_as_of():
    """as_of: ts is an actual, never forecast; as_of: fetch is a forecast (http specs)."""
    for sid in AESO:
        for block in SPECS[sid].get("extract", []) or []:
            ids = [f.get("id") or block.get("id", "?") for f in block["fields"].values()]  # csd: templated ids
            tier = block.get("retention") or _source_rule(SPECS[sid], ids[0])
            if block.get("as_of") == "ts":
                assert tier != "forecast", (sid, ids)
            if block.get("as_of") == "fetch":
                assert tier == "forecast", (sid, ids)


def test_seeded_red_an_actual_marked_forecast_is_caught():
    """The check can fail: flip aeso_price's actual block to forecast and it must be reported."""
    specs = {k: dict(v) for k, v in SPECS.items()}
    price = specs["aeso_price"]
    price["extract"] = [dict(b) for b in price["extract"]]
    price["extract"][0]["retention"] = "forecast"
    bad = misclassified(specs)
    assert any(b.startswith("aeso.price.pool: actual pinned to 'forecast'") for b in bad), bad
    specs["aeso_ets_wind_solar"] = {**SPECS["aeso_ets_wind_solar"], "retention": "forecast"}  # drop the glob
    assert any(b.startswith("aeso.wind.actual:") for b in misclassified(specs))


def test_seeded_red_overlapping_patterns_are_rejected():
    spec = {**SPECS["aeso_ets_wind_solar"]}
    spec["retention"] = {**spec["retention"], "aeso.wind.*": "fast"}  # overlaps "aeso.*.actual" for aeso.wind.actual
    with pytest.raises(AssertionError, match="matches 2 retention patterns"):
        _source_rule(spec, "aeso.wind.actual")


# ---- shipped path: run the python connectors on the synthetic fixtures and classify what they emit
def _ctx(routes: dict[str, str]) -> Context:
    def handler(req: httpx.Request) -> httpx.Response:
        for key, body in routes.items():
            if key in str(req.url):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)


def _fix(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


CASES = [
    ("aeso_ets_wind_solar", {"report": "wind_solar", "horizons": ["st"]},
     lambda: {"wind_rpt_shortterm": _fix("power_wind_st.csv"), "solar_rpt_shortterm": _fix("power_wind_st.csv")}),
    ("aeso_ets_outages", None, lambda: {"DailyOutageReportServlet": _fix("power_outage_daily.csv")}),
    ("aeso_ets_outages_monthly", None, lambda: {"MonthlyOutageForecastReportServlet": _fix("power_outage_monthly.csv")}),
    ("aeso_ets_units", None, lambda: {"CSDReportServlet": _fix("power_csd_sample.csv")}),
]


@pytest.mark.parametrize("sid,params,routes", CASES, ids=[c[0] for c in CASES])
def test_emitted_series_classify_by_their_as_of(sid, params, routes):
    spec = SPECS[sid] if params is None else {**SPECS[sid], "params": params}
    res = build(spec).run(_ctx(routes()))
    series = {}
    for o in res.obs:
        series.setdefault(o.series_id, []).append(o)
    assert series, f"{sid}: the fixture produced no observations (NO-RESULT)"
    for s, obs in series.items():
        tier = tier_of(s)
        if all(o.as_of == o.ts for o in obs):           # stamped at its own time: an actual
            assert tier != "forecast", f"{s}: actual (as_of == ts) classed forecast"
        else:                                            # a fetch-time vintage is a forecast run (L-036)
            assert tier == "forecast", f"{s}: fetch-vintage series classed {tier!r}"
