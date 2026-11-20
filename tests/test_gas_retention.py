"""Retention tiers of the gas specs (docs/db/RETENTION.md spec contract), pinned by gas.

Two layers, like Power's tests/test_power_retention.py:
1. the declared mapping: every gas spec declares a valid tier, and concrete series ids resolve to the tier
   gas ruled for them (so a renamed series or a flipped tier shows up here);
2. the shipped path: the python connectors run on the synthetic payloads from test_gas_tc.py /
   test_gas_westcoast.py, and every emitted series is classified by its real as_of.

The as_of rule for gas (stricter than "as_of != ts means forecast"):
- as_of == fetch time (ctx.now) is a fetch vintage: it must be `forecast`;
- as_of == ts is an actual: never `forecast`;
- as_of is a *publication* time (DOP publication, Westcoast notice posting) for a value published ahead of its gas
  day: allowed as `daily` only for the series in PUBLISHED_AHEAD, which gas pinned daily on measured size (see the
  comments in sources/ngtl_dop.yaml and sources/gas_westcoast.yaml). Any other series stamped that way fails.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from montney.connectors import load_specs
from montney.db.backfill import spec_tier
from test_gas_tc import CHART, bulletin, run_bulletins, run_dop
from test_gas_station_util import live_files, run as run_station_util
from test_gas_westcoast import S2_NOV, notice, run as run_westcoast

SPECS = load_specs(include_disabled=True)
GAS = sorted(s for s in SPECS if s.startswith(("ngtl_", "gas_", "oil_")))
TIERS = {"fast", "hourly", "daily", "forecast", "live", "events", "grid"}

# Gas's ruling (owner of sources/ngtl_*.yaml, sources/gas_*.yaml). Concrete ids.
PINNED = {
    "ngtl_csr": {"ngtl.csr.willow_valley": "fast", "ngtl.csr.linepack": "fast", "ngtl.csr.total_export": "fast"},
    "ngtl_gdsr": {"ngtl.gdsr.field_receipts.flow": "daily", "ngtl.gdsr.linepack_change": "daily",
                  "ngtl.gdsr.empress.prorated": "daily", "ngtl.gdsr.empress.nom": "daily"},
    "ngtl_dop": {"ngtl.dop.egat.capability": "daily", "ngtl.dop.egat.flow": "daily",
                 "ngtl.dop.egat.flow_yago": "daily"},
    "gas_westcoast": {"westcoast.station2_south.capacity": "daily", "westcoast.huntingdon.capacity": "daily"},
    "ngtl_city": {"ngtl.city.calgary.demand_est": "daily", "ngtl.city.calgary.monthly": "daily"},
    "gas_alliance": {"alliance.zone1.receipts": "daily", "alliance.receipts.total": "daily"},
    "gas_bcer_production": {"production.bc.monthly": "daily"},
    "gas_westcoast_oa": {"westcoast.oa.station2_south.scheduled": "daily", "westcoast.oa.tnorth.out": "daily"},
    "gas_cer_keypoints": {"cer.mainline.prairies.throughput": "daily", "cer.westcoast.fortisbc_lm.capacity": "daily"},
    "ngtl_station_util": {"ngtl.station.5106.receipt": "daily"},
    "gas_petrinex": {"petrinex.abgp0001892.sales_gas": "daily", "production.ab.monthly": "daily"},
    "ngtl_split": {"ngtl.area.usjr.receipt_est": "daily", "ngtl.rd.measured_receipts": "daily",
                   "production.ab.daily_est": "daily", "export.total.flow_est": "daily", "export.pipeline.total_est": "daily"},
    "gas_lng_ppa": {"lng.kitimat.cargo": "daily", "lng.kitimat.feed_30d": "daily", "lng.kitimat.ppa_poll": "hourly"},
    "gas_lng_bcer": {"lng.lng_canada.receipts": "daily", "cgl.producer_direct": "daily"},
    "gas_nwp": {"nwp.sumas.receipts": "daily", "nwp.jackson_prairie.inventory": "daily",
                "fortisbc.lower_mainland.demand_est": "daily", "demand.bc.implied_est": "daily"},
    "gas_gtn": {"gtn.malin.deliveries": "daily", "gtn.kingsgate.capacity": "daily"},
    "gas_pnw_balance": {"pnw.demand_est": "daily", "pnw.storage.inventory_est": "daily"},
    "gas_eia_states": {"eia.or.storage.working_gas": "daily", "eia.tx.marketed_production": "daily",
                       "eia.ca.crude_production": "daily", "eia.tx.deliveries.industrial": "daily"},
    "gas_region_demand": {"gas.tx.demand_est": "daily", "gas.umw.power_burn_est": "daily", "gas.sk.demand_est": "daily",
                          "gas.on.industrial_est": "daily"},
    "gas_cheniere_ccl": {"lng.tx.corpus_christi.feedgas": "daily"},
    "oil_statcan_provinces": {"statcan.ab.crude_production": "daily", "statcan.bc.condensate_production": "daily"},
    "oil_eia_wpsr": {"oil.padd3.refinery_crude_input": "daily", "oil.padd1a.gasoline_stocks": "daily"},
    "gas_eia_storage_weekly": {"eia.storage.pacific.working_gas": "daily", "eia.storage.east.net_injection": "daily"},
    "gas_pge": {"pge.demand.total": "daily", "pge.storage.working_gas": "daily"},
    "gas_socalgas": {"socal.demand.sendout": "daily", "socal.storage.inventory": "daily"},
    "gas_cal_balance": {"cal.demand.total": "daily", "cal.imports.south": "daily"},
    "gas_statcan_onqc": {"statcan.on.deliveries": "daily", "statcan.qc.storage": "daily",
                         "statcan.sk.production": "daily", "statcan.ab.storage": "daily",
                         "statcan.sk.deliveries.industrial": "daily"},
    "gas_glgt": {"glgt.st_clair.net_deliveries": "daily"},
    "gas_vector": {"vector.st_clair.deliveries": "daily"},
    "gas_pepl_ojibway": {"pepl.ojibway.deliveries": "daily"},
    "gas_ont_supply": {"supply.on.pipelines_est": "daily"},
    "gas_nbpl": {"nbpl.bakken.net_receipts": "daily", "nbpl.chicago.net_deliveries": "daily"},
    "gas_anr": {"anr.wisconsin.deliveries": "daily"},
    "gas_midwest_balance": {"midwest.chicago.dropoff": "daily"},
    "gas_midwest_weather": {"demand.il.weather_est": "daily", "demand.mi.weather_est": "daily"},
    "gas_ngpl_chicago": {"ngpl.chicago.citygate_deliveries": "daily"},
}
EVENTS_ONLY = {"ngtl_bulletins"}
PUBLISHED_AHEAD = {"ngtl.dop.*.capability", "westcoast.*.capacity",   # daily despite as_of < ts (see docstring)
                   "nwp.*", "gtn.*", "fortisbc.*", "demand.bc.*", "pge.*", "socal.*", "glgt.*", "vector.*",
                   "nbpl.*", "anr.*", "ngpl.*"}   # NAESB cycles: Timely/Evening post the day before (L-085)


def _published_ahead(series_id: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatchcase(series_id, p) for p in PUBLISHED_AHEAD)


def misclassified(specs: dict = SPECS) -> list[str]:
    bad = []
    for sid, ids in PINNED.items():
        for s, want in ids.items():
            got = spec_tier(s, specs[sid])
            if got != want:
                bad.append(f"{s}: pinned to {got!r}, want {want!r}")
    for sid in EVENTS_ONLY:
        if specs[sid].get("retention") != "events":
            bad.append(f"{sid}: events-only source pinned to {specs[sid].get('retention')!r}")
    return bad


def as_of_problems(sid: str, res, now: datetime, specs: dict = SPECS) -> list[str]:
    """Classify every emitted series by its real as_of. Fails closed: no observations is a problem."""
    if sid in EVENTS_ONLY:
        return [f"{sid}: events-only source emitted observations {sorted({o.series_id for o in res.obs})}"] \
            if res.obs else ([] if res.events else [f"{sid}: fixture produced no events (NO-RESULT)"])
    if not res.obs:
        return [f"{sid}: fixture produced no observations (NO-RESULT)"]
    out, series = [], {}
    for o in res.obs:
        series.setdefault(o.series_id, []).append(o)
    for s, obs in series.items():
        tier = spec_tier(s, specs[sid])       # raises RetentionConflict when unresolvable (L-036 fail closed)
        if tier not in TIERS - {"events", "grid"}:
            out.append(f"{s}: observation series resolves to {tier!r}")
        elif any(o.as_of == now for o in obs):
            if tier != "forecast":
                out.append(f"{s}: fetch-time vintage classed {tier!r}")
        elif all(o.as_of == o.ts for o in obs):
            if tier == "forecast":
                out.append(f"{s}: actual (as_of == ts) classed forecast")
        elif all(o.as_of > o.ts for o in obs):           # a report published after its period (a vintage)
            if tier == "forecast":
                out.append(f"{s}: report vintage (as_of after ts) classed forecast")
        elif not (_published_ahead(s) and tier == "daily"):
            out.append(f"{s}: publication-time vintages classed {tier!r} but not in PUBLISHED_AHEAD")
    return out


# ---- 1. declared mapping
@pytest.mark.parametrize("sid", GAS)
def test_every_gas_spec_declares_a_valid_retention(sid):
    r = SPECS[sid].get("retention")
    if isinstance(r, dict):                       # L-036: python specs only, with a mandatory default
        assert SPECS[sid].get("type") == "python" and r.get("default") in TIERS, f"{sid}: retention {r!r}"
        assert set(r.values()) <= TIERS, f"{sid}: retention {r!r}"
    else:
        assert isinstance(r, str) and r in TIERS, f"{sid}: retention {r!r}"


def test_pinned_tiers():
    assert misclassified() == []


def test_seeded_red_flipped_tier_is_caught():
    specs = dict(SPECS)
    specs["ngtl_csr"] = {**SPECS["ngtl_csr"], "retention": "forecast"}
    specs["ngtl_bulletins"] = {**SPECS["ngtl_bulletins"], "retention": "daily"}
    bad = misclassified(specs)
    assert any(b.startswith("ngtl.csr.willow_valley: pinned to 'forecast'") for b in bad), bad
    assert any(b.startswith("ngtl_bulletins:") for b in bad), bad


# ---- 2. shipped path
NOW = datetime(2026, 10, 31, 21, 0)       # ctx.now in test_gas_tc.ctx_for / test_gas_westcoast.ctx_for


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Connectors keep small caches in <home>/cache (L-081): each test starts clean, never the user's data dir."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))


def _runs():
    return {
        "ngtl_station_util": run_station_util(live_files()),
        "ngtl_dop": run_dop()[0],
        "gas_westcoast": run_westcoast([notice(1, "Huntingdon/Station 2 South Capacity for November 1 - 2, 2026",
                                               S2_NOV)]),
        "ngtl_bulletins": run_bulletins([bulletin(1, "Weekend Border Capability Forecast (EGAT/WGAT)",
                                                  kind="Capacity Constraint")])[0],
    }


@pytest.mark.parametrize("sid", ["ngtl_dop", "gas_westcoast", "ngtl_bulletins", "ngtl_station_util"])
def test_emitted_series_classify_by_their_as_of(sid):
    res = _runs()[sid]
    assert as_of_problems(sid, res, NOW) == []
    if sid == "ngtl_dop":   # the fixture really exercises all three DOP measures, incl. the published-ahead one
        assert {o.series_id.rsplit(".", 1)[1] for o in res.obs} >= {"capability", "flow", "flow_yago"}
        assert any(o.as_of < o.ts for o in res.obs if o.series_id.endswith(".capability"))


def test_seeded_red_fetch_vintage_on_a_daily_series_is_caught():
    """A connector that stamped as_of = fetch time on a daily-pinned series must be reported."""
    res = _runs()["ngtl_dop"]
    for o in res.obs:
        if o.series_id == "ngtl.dop.egat.flow":
            o.as_of = NOW
    assert any(p.startswith("ngtl.dop.egat.flow: fetch-time vintage") for p in as_of_problems("ngtl_dop", res, NOW))


def test_seeded_red_published_ahead_needs_the_allowlist():
    """Drop capability from PUBLISHED_AHEAD and its publication-time vintages must fail as daily."""
    res = _runs()["ngtl_dop"]
    PUBLISHED_AHEAD.discard("ngtl.dop.*.capability")
    try:
        bad = as_of_problems("ngtl_dop", res, NOW)
    finally:
        PUBLISHED_AHEAD.add("ngtl.dop.*.capability")
    assert any("capability: publication-time vintages" in b for b in bad), bad


def test_seeded_red_empty_run_is_no_result():
    class Empty:
        obs, events = [], []
    assert as_of_problems("ngtl_dop", Empty(), NOW) == ["ngtl_dop: fixture produced no observations (NO-RESULT)"]
    assert as_of_problems("ngtl_bulletins", Empty(), NOW) == ["ngtl_bulletins: fixture produced no events (NO-RESULT)"]


def test_fixture_chart_has_a_future_capability_row():
    """Guard: the shared DOP fixture keeps a capability row for a gas day after its publication."""
    assert any(r["capabilityVolume"] and r["flow"] is None for r in CHART["data"])


def test_seeded_red_report_vintage_pinned_forecast_is_caught():
    """Station receipts are monthly report vintages (as_of after the month): forecast-tiering them must fail."""
    res = _runs()["ngtl_station_util"]
    specs = {**SPECS, "ngtl_station_util": {**SPECS["ngtl_station_util"], "retention": "forecast"}}
    assert any("report vintage (as_of after ts) classed forecast" in p
               for p in as_of_problems("ngtl_station_util", res, NOW, specs))
