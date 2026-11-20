"""City gas demand (PLAN 4b item 6): the district mapping and the GJ → MMcf/d unit (L-058: small checks only)."""

from __future__ import annotations

import pytest

from montney.connectors import ngtl_city as nc


@pytest.mark.parametrize("station, name, group", [
    (32209, "AG SP 44-003 APS", "calgary"), (31944, "AG SP 21-094 APN", "edmonton"),
    (32447, "AG SP 51-002 APS", "lethbridge"), (32001, "AG SP 31-003 APN", "red_deer"),
    (31800, "AG SP 22-010 APN", None),                      # Edmonton suburbs district: deliberately not Edmonton
    (31004, "AGRIUM REDWATER SALES APN", "fort_sask"), (31016, "CALGARY ENERGY CENTRE SALES APS", None),
])
def test_district_mapping(station, name, group):
    assert nc.group_of(station, name) == group


def test_gj_to_mmcfd_at_tc_area_heat_value():
    # Calgary Sep 2026: 144.2 TJ/d at WGAT 38.5 MJ/m3 → 144,200 / 38.5 × 0.0353147 = 132.3 MMcf/d
    assert nc.to_mmcfd(144_200.0, 38.5) == pytest.approx(132.27, abs=0.01)


def test_industry_is_intra_minus_population_and_its_parts():
    """Step 5: industry = intra-AB − population; other = industry − oil sands − Heartland − power (outside cogen)."""
    from datetime import date, datetime, timedelta

    from montney.connectors import build, load_specs
    from montney.connectors.base import Context, FetchResult
    from montney.store import Obs

    conn = build(load_specs(include_disabled=True)["ngtl_city"])
    d = date(2026, 11, 1)
    start = datetime(2026, 11, 1, nc.GAS_DAY_START_UTC)
    five = [start + timedelta(minutes=5 * i) for i in range(288)]
    stored = {nc.INTRA: {datetime(2026, 11, 1): 7000.0}, nc.OSDA: {datetime(2026, 10, 31): 3300.0},
              nc.POWER_TOTAL: {t: 2000.0 for t in five},
              **{f"power.plant.{p}.gas_burn.est": {t: 100.0 for t in five} for p in nc.POWER_OUTSIDE}}
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    res = FetchResult(obs=[Obs(nc.FS_DEMAND, datetime(2026, 11, 1), 460.0, datetime(2026, 11, 1))])
    conn._industry(Context(client=None, now=datetime(2026, 11, 2, 18)), res, {d: 900.0}, datetime(2026, 11, 2))
    pts = {o.series_id: o.value for o in res.obs if o.ts == datetime(2026, 11, 1)}
    assert pts["demand.ab.industry_est"] == pytest.approx(6100.0)
    assert pts["demand.ab.power_burn_est"] == pytest.approx(2000.0 - 5 * 100.0)       # less the five cogen/BC plants
    assert pts["demand.ab.other_industry_est"] == pytest.approx(6100.0 - 3300.0 - 460.0 - 1500.0)   # OSDA carried 1 day


def test_archive_outage_uses_the_data_dir_cache_and_the_forecast_hosts_past_days(tmp_path, monkeypatch):
    """ngtl_city survives an Open-Meteo archive outage: fit on cached archive days + the forecast host's recent days."""
    from datetime import date, datetime

    import httpx

    from montney.connectors.base import Context, FetchResult
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    up = {"archive": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if "archive-api" in str(req.url):
            if not up["archive"]:
                return httpx.Response(503, json={"reason": "The service is overloaded", "error": True})
            return httpx.Response(200, json={"daily": {"time": ["2026-09-01", "2026-09-02"],
                                                       "temperature_2m_mean": [10.0, 12.0]}})
        return httpx.Response(200, json={"daily": {"time": ["2026-09-02", "2026-10-30", "2026-11-02"],
                                                   "temperature_2m_mean": [99.0, 3.0, -5.0]}})
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 2, 18))
    got = nc.daily_temps(ctx, FetchResult(), "calgary", 51.05, -114.07, date(2026, 9, 1), date(2026, 11, 2))
    assert got == {date(2026, 9, 1): 10.0, date(2026, 9, 2): 12.0}                  # archive up: and it's cached
    up["archive"] = False
    res = FetchResult()
    got = nc.daily_temps(ctx, res, "calgary", 51.05, -114.07, date(2026, 9, 1), date(2026, 11, 2))
    assert got == {date(2026, 9, 1): 10.0, date(2026, 9, 2): 12.0, date(2026, 10, 30): 3.0}   # cache wins; no today
    assert any("using the cache (2 days)" in w for w in res.warnings)


def test_rest_of_ab_is_population_minus_the_four_cities():
    """L-072: the population row's parts (four cities + Rest of AB) sum exactly to it."""
    from datetime import date, datetime

    from montney.connectors import build, load_specs
    from montney.connectors.base import Context, FetchResult
    from montney.store import Obs

    conn = build(load_specs(include_disabled=True)["ngtl_city"])
    months = [date(2025, m, 1) for m in range(1, 13)] + [date(2026, 1, 1)]
    hdd = {m: [10.0 + i] * 28 for i, m in enumerate(months)}
    by_m = {m: (100.0 + 40.0 * (10.0 + i)) * 1000.0 for i, m in enumerate(months)}       # GJ/d, exact a + b × HDD
    d = date(2026, 11, 1)
    ts = datetime(2026, 11, 1)
    res = FetchResult(obs=[Obs(f"ngtl.city.{c}.demand_est", ts, v, ts) for c, v in
                           zip(nc.CITIES, (300.0, 250.0, 30.0, 20.0))])
    pop = conn._population(Context(client=None, now=datetime(2026, 11, 2)), res, by_m,
                           {"calgary": hdd, "edmonton": hdd}, {"calgary": {d: 15.0}, "edmonton": {d: 15.0}}, 39.0, ts)
    pts = {o.series_id: o.value for o in res.obs}
    assert pts["demand.ab.population_rest_est"] == pytest.approx(pop[d] - 600.0)
