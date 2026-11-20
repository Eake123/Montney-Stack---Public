"""AESO ETS connector (montney/connectors/aeso_ets.py) and the implied gas burn derive.

All fixtures in tests/fixtures/power_*.csv are synthetic, with the same layout as the real reports.
"""

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.aeso_ets import CsdValidationError, LocalClock, parse_csd
from montney.connectors.base import Context

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 31, 20, 10)  # UTC = 14:10 MDT


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def ctx_serving(routes: dict[str, str]) -> Context:
    def handler(req: httpx.Request) -> httpx.Response:
        for key, body in routes.items():
            if key in str(req.url):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)


def by_id(res):
    out = {}
    for o in res.obs:
        out.setdefault(o.series_id, []).append(o)
    return out


SPECS = load_specs(include_disabled=True)


# ------------------------------------------------------------------ CSD units
def test_csd_blocks_classified_and_validated():
    updated, units = parse_csd(fixture("power_csd_sample.csv"))
    assert updated == datetime(2026, 10, 31, 14, 5)
    fuel = {u["id"]: u["fuel"] for u in units}
    assert fuel["TPA1"] == fuel["TPB1"] == "simple_cycle"
    assert fuel["TCG1"] == "cogeneration"
    assert fuel["TCC1"] == "combined_cycle"
    assert fuel["TGS1"] == "gas_fired_steam"
    # headerless blocks, by order
    assert fuel["THY1"] == "hydro" and fuel["TBT1"] == "energy_storage"
    assert fuel["TSO1"] == "solar" and fuel["TWA1"] == fuel["TWB1"] == "wind"
    assert fuel["TBM1"] == "other"
    tpb = next(u for u in units if u["id"] == "TPB1")
    assert tpb["name"] == "Test Peaker B" and tpb["flag"] == "*" and tpb["mc"] == 50


def test_csd_swapped_block_fails_loudly():
    text = fixture("power_csd_sample.csv")
    # swap the solar and wind blocks: MC sums no longer match the fuel table
    solar = '"Test Solar (TSO1)","100","60","0"'
    wind = '"Test Wind A (TWA1)*","120","30","0"\n"Test Wind B (TWB1)","80","20","0"'
    swapped = text.replace(solar, "@@S@@").replace(wind, solar).replace("@@S@@", wind)
    with pytest.raises(CsdValidationError, match="MC sums"):
        parse_csd(swapped)


def test_csd_missing_block_fails_loudly():
    text = fixture("power_csd_sample.csv").replace('\n"Test Battery (TBT1)","20","0","5"\n', "\n")
    with pytest.raises(CsdValidationError):
        parse_csd(text)


def test_units_spec_keeps_gas_plus_extras(monkeypatch):
    spec = {**SPECS["aeso_ets_units"], "params": {**SPECS["aeso_ets_units"]["params"],
                                                  "extra_units": ["TBM1"]}}
    res = build(spec).run(ctx_serving({"CSDReportServlet": fixture("power_csd_sample.csv")}))
    s = by_id(res)
    assert s["aeso.unit.tcc1.tng"][0].value == 300
    assert s["aeso.unit.tcc1.mc"][0].value == 400
    assert s["aeso.unit.tbm1.tng"][0].value == 20       # extra non-gas unit kept
    assert s["aeso.unit.twa1.tng"][0].value == 30       # wind and solar stored for the map's farms (P4c)
    assert s["aeso.unit.tso1.mc"][0].value == 100
    assert s["aeso.unit.tcc1.tng"][0].ts == datetime(2026, 10, 31, 20, 5)   # 14:05 MDT
    meta = {m.series_id: m for m in res.meta}
    assert meta["aeso.unit.tcc1.tng"].extra == {"asset_id": "TCC1", "fuel": "combined_cycle"}
    assert meta["aeso.unit.tbm1.tng"].extra["fuel"] == "other"
    assert meta["aeso.unit.twa1.tng"].extra["fuel"] == "wind" and meta["aeso.unit.tso1.tng"].extra["fuel"] == "solar"
    assert any(m.extra.get("fuel") == "hydro" for m in res.meta)            # hydro stored for the AB dams (P5b)
    assert not any(m.extra.get("fuel") == "energy_storage" for m in res.meta)   # still not stored


# ------------------------------------------------------------------ outages
def test_outage_daily_and_gas_total():
    res = build(SPECS["aeso_ets_outages"]).run(
        ctx_serving({"DailyOutageReportServlet": fixture("power_outage_daily.csv")}))
    s = by_id(res)
    d0 = datetime(2026, 10, 31)
    assert [o.value for o in s["aeso.outage.cogen"] if o.ts == d0] == [20]
    assert [o.value for o in s["aeso.outage.gas_total"] if o.ts == d0] == [100]  # 10+20+30+40
    assert s["aeso.outage.mbo"][0].value == 100
    vintages = [o for o in res.obs if not o.series_id.endswith(".realized")]
    assert vintages and all(o.as_of == NOW for o in vintages)  # as_of: fetch keeps revisions
    # NOW is 14:10 on 2026-10-31 in Alberta, so only that day's row is also written as realized (as_of = ts)
    real = [o for o in res.obs if o.series_id.endswith(".realized")]
    assert {o.ts for o in real} == {d0} and all(o.as_of == o.ts for o in real)
    assert [o.value for o in s["aeso.outage.cogen.realized"]] == [20]
    assert [o.value for o in s["aeso.outage.gas_total.realized"]] == [100]


def test_outage_monthly():
    res = build(SPECS["aeso_ets_outages_monthly"]).run(
        ctx_serving({"MonthlyOutageForecastReportServlet": fixture("power_outage_monthly.csv")}))
    s = by_id(res)
    assert {o.ts: o.value for o in s["aeso.outage.monthly.cc"]} == {
        datetime(2026, 11, 1): 3, datetime(2026, 12, 1): 13}
    assert [o.value for o in s["aeso.outage.monthly.gas_total"] if o.ts == datetime(2026, 11, 1)] == [10]
    # realized: only the earliest listed month (the live report starts at next month), as_of = ts
    assert {o.ts: o.value for o in s["aeso.outage.monthly.cc.realized"]} == {datetime(2026, 11, 1): 3}
    assert all(o.as_of == o.ts for o in res.obs if o.series_id.endswith(".realized"))


# ------------------------------------------------------------------ wind/solar
def test_wind_solar_forecast_future_only_and_actuals():
    spec = {**SPECS["aeso_ets_wind_solar"], "params": {"report": "wind_solar", "horizons": ["st"]}}
    body = fixture("power_wind_st.csv")
    res = build(spec).run(ctx_serving({"wind_rpt_shortterm": body, "solar_rpt_shortterm": body}))
    s = by_id(res)
    # 13:50 MDT = 19:50 UTC is older than now-10min: actual kept, forecast skipped
    assert [(o.ts, o.value) for o in s["aeso.wind.actual"]] == [(datetime(2026, 10, 31, 19, 50), 210)]
    likely = {o.ts: o.value for o in s["aeso.wind.st.forecast.likely"]}
    assert likely == {datetime(2026, 10, 31, 20, 10): 210, datetime(2026, 10, 31, 20, 20): 220}
    assert all(o.as_of == NOW for o in s["aeso.wind.st.forecast.likely"])


# ------------------------------------------------------------------ event log
def test_event_log_alerts():
    res = build(SPECS["aeso_ets_events"]).run(ctx_serving({"RealTimeShiftReportServlet": fixture("power_events.csv")}))
    ev = {e.title: e for e in res.events}
    eea = ev["Test grid: EEA Level 1 declared."]
    assert eea.kind == "alert" and eea.severity == "critical"
    assert "power.region.ab" in eea.refs
    assert eea.starts == datetime(2026, 10, 31, 19, 10)
    kh = ev["Keephills 2 offline."]
    assert kh.severity == "warn" and kh.refs == ["power.plant.keephills"]
    assert ev["Test unit online"].severity == "info"
    again = build(SPECS["aeso_ets_events"]).run(ctx_serving({"RealTimeShiftReportServlet": fixture("power_events.csv")}))
    assert sorted(e.id for e in again.events) == sorted(e.id for e in res.events)  # stable ids


def test_event_log_alerts_end(tmp_path):
    from datetime import timedelta
    from montney.store import Store
    res = build(SPECS["aeso_ets_events"]).run(ctx_serving({"RealTimeShiftReportServlet": fixture("power_events.csv")}))
    ev = {e.title: e for e in res.events}
    eea, done = ev["Test grid: EEA Level 1 declared."], ev["Test grid: EEA Level 1 terminated."]
    assert eea.ends == done.starts == datetime(2026, 10, 31, 19, 40)   # closed by the all-clear
    assert done.severity == "info" and "power.region.ab" in done.refs   # all-clear isn't red
    off, on = ev["Keephills 2 offline."], ev["Keephills #2 Online"]
    assert off.ends == on.starts == datetime(2026, 10, 31, 18, 0)        # closed by "<unit> online"
    assert off.extra["unit"] == on.extra["unit"] == "keephills 2"
    assert on.ends == on.starts + timedelta(hours=24)
    assert all(e.ends is not None for e in res.events)

    st = Store(tmp_path / "t.duckdb")
    st.upsert_events(res.events)
    ongoing = {e["title"] for e in st.events(state="ongoing", at=NOW)}
    assert ongoing == {"Test grid: EEA Level 1 terminated.", "Keephills #2 Online"}
    assert "Test unit online" in {e["title"] for e in st.events(state="past", at=NOW)}  # 2 days old


def test_eea_without_all_clear_falls_back_to_12h():
    body = fixture("power_events.csv").replace('"10/31/2026 13:40","Test grid: EEA Level 1 terminated."\n', "")
    res = build(SPECS["aeso_ets_events"]).run(ctx_serving({"RealTimeShiftReportServlet": body}))
    eea = next(e for e in res.events if "declared" in e.title)
    assert eea.severity == "critical"
    assert eea.ends == datetime(2026, 11, 1, 7, 10)  # 19:10Z + 12 h


# ------------------------------------------------------------------ time zones
def test_edmonton_2026_11_01_has_no_repeated_hour():
    # Recent tzdata (Alberta change in 2026c): UTC-6 from 2026-11-01; AESO files run 01:50 -> 02:00.
    c = LocalClock("America/Edmonton")
    stamps = [datetime(2026, 11, 1, 1, 50), datetime(2026, 11, 1, 2, 0)]
    assert c.series(stamps) == [datetime(2026, 11, 1, 7, 50), datetime(2026, 11, 1, 8, 0)]


def test_fold_resolved_for_a_zone_that_falls_back():
    c = LocalClock("America/Denver")  # still observes DST: 2026-11-01 01:00-02:00 repeats
    stamps = [datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 1, 50),
              datetime(2026, 11, 1, 1, 0), datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 2, 0)]
    assert c.series(stamps) == [datetime(2026, 11, 1, 7, 30), datetime(2026, 11, 1, 7, 50),
                                datetime(2026, 11, 1, 8, 0), datetime(2026, 11, 1, 8, 30),
                                datetime(2026, 11, 1, 9, 0)]
    # single stamp: pick the occurrence closest to now that isn't in the future
    assert c.near(datetime(2026, 11, 1, 1, 30), now=datetime(2026, 11, 1, 7, 40)) == datetime(2026, 11, 1, 7, 30)
    assert c.near(datetime(2026, 11, 1, 1, 30), now=datetime(2026, 11, 1, 8, 40)) == datetime(2026, 11, 1, 8, 30)


# ------------------------------------------------------------------ implied gas burn
def test_implied_gas_burn_derive(monkeypatch):
    monkeypatch.setenv("AESO_API_KEY", "test-key")
    gen = [("COGENERATION", 4908), ("COMBINED CYCLE", 3419), ("SIMPLE CYCLE", 97), ("GAS FIRED STEAM", 766)]

    def handler(req):
        return httpx.Response(200, json={"return": {
            "effective_datetime_utc": "2026-10-31 20:03", "alberta_internal_load": 1,
            "total_net_generation": 1, "total_max_generation_capability": 1,
            "net_to_grid_generation": 1, "net_actual_interchange": 0,
            "generation_data_list": [{"fuel_type": f, "aggregated_net_generation": mw,
                                      "aggregated_maximum_capability": mw} for f, mw in gen],
            "interchange_list": []}})

    res = build(SPECS["aeso_csd"]).run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))
    s = by_id(res)
    mid = s["power.ab.gas_burn.implied"][0].value
    lo = s["power.ab.gas_burn.implied_low"][0].value
    hi = s["power.ab.gas_burn.implied_high"][0].value
    # P5b written-down defaults (docs/research/v2/power-heat-rates.md): HR GJ/MWh (central, lo, hi), HHV 1.104 (1.116, 1.093);
    # cogen = SITE FUEL (ruling after #177)
    hr = {"COGENERATION": (11.3, 8.5, 15.0), "COMBINED CYCLE": (7.5, 7.0, 8.6), "SIMPLE CYCLE": (10.8, 9.9, 12.5),
          "GAS FIRED STEAM": (11.3, 10.5, 12.6)}
    want = [sum(mw * hr[f][i] * 24 / hhv / 1000 for f, mw in gen) for i, hhv in ((0, 1.104), (1, 1.116), (2, 1.093))]
    assert mid == pytest.approx(want[0], abs=0.5) and mid == pytest.approx(1974.0, abs=0.5)   # power.md sample MW
    assert lo == pytest.approx(want[1], abs=0.5) and hi == pytest.approx(want[2], abs=0.5)
    assert lo < mid < hi
    units = {m.series_id: m.unit for m in res.meta}
    assert units["power.ab.gas_burn.implied"] == "MMcf/d"


# ------------------------------------------------------------------ load by fuel (PLAN step 5, L-061)
def _csd_run(monkeypatch, ail, interchange, gen):
    monkeypatch.setenv("AESO_API_KEY", "test-key")
    body = {"return": {"effective_datetime_utc": "2026-11-02 04:10", "alberta_internal_load": ail,
                       "total_net_generation": sum(gen.values()), "total_max_generation_capability": 1,
                       "net_to_grid_generation": 1, "net_actual_interchange": interchange,
                       "generation_data_list": [{"fuel_type": f, "aggregated_net_generation": mw,
                                                 "aggregated_maximum_capability": mw} for f, mw in gen.items()],
                       "interchange_list": []}}
    res = build(SPECS["aeso_csd"]).run(Context(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=body))), now=NOW))
    return {k: v[0].value for k, v in by_id(res).items()}


def test_load_box_rows_add_up_to_aeso_load(monkeypatch):
    """The AESO load box's rows (network/power/context.geojson) sum to AIL. Live CSD, 2026-11-02 04:10Z: an import."""
    gen = {"COGENERATION": 4971, "COMBINED CYCLE": 3293, "SIMPLE CYCLE": 417, "GAS FIRED STEAM": 1371, "WIND": 99,
           "SOLAR": 0, "HYDRO": 168, "OTHER": 283, "ENERGY STORAGE": 0}
    v = _csd_run(monkeypatch, 10747, -145, gen)
    fc = json.loads((Path(__file__).resolve().parents[1] / "network/power/context.geojson").read_text("utf-8"))
    box = next(f["properties"] for f in fc["features"] if f["properties"]["id"] == "power.region.aeso_load")
    rows = [r for r in box["callouts_extra"] if "share" in r]     # the fuel rows (the cushion row has no share)
    assert [r["label"] for r in rows] == ["Gas", "Wind", "Solar", "Hydro", "Other (incl. storage)", "Net imports"]
    assert all(r["share"]["of"] == box["value"] == "aeso.csd.ail" and r["flow"] == "supply" for r in rows)
    assert v["aeso.csd.net_imports"] == 145                        # CSD interchange is + export
    assert sum(v[r["series"]] for r in rows) == v["aeso.csd.ail"] == 10747
    exp = _csd_run(monkeypatch, 10302, 300, gen)                   # the same fleet with a net export of 300 MW
    assert exp["aeso.csd.net_imports"] == -300 and sum(exp[r["series"]] for r in rows) == exp["aeso.csd.ail"]


# ------------------------------------------------------------------ intertie transmission outages
def tx_routes(csv7=None, csv24=None):
    return {"qryOpPlanTransmissionTable_1.html": fixture("power_tx_7day.html"),
            "_qryOpPlanTransmissionTable_1.csv": csv7 if csv7 is not None else fixture("power_tx_7day.csv"),
            "Longterm_Critical_Outages.html": fixture("power_tx_24m.html"),
            "_Longterm_Critical_outages.csv": csv24 if csv24 is not None else fixture("power_tx_24m.csv")}


def test_tx_outages_intertie_rows_only_deduped():
    res = build(SPECS["aeso_ets_tx_outages"]).run(ctx_serving(tx_routes()))
    ev = {e.extra["element"]: e for e in res.events}
    assert set(ev) == {"9L1 (1s Test - 2s BCH Test)", "SaskPower T1", "2375 (NWE Test)"}  # no internal lines
    bc = ev["9L1 (1s Test - 2s BCH Test)"]
    assert bc.kind == "outage" and bc.refs == ["power.intertie.bc"]
    assert bc.extra["report"] == "7day" and bc.extra["also_in"] == "24month"   # same outage, kept once
    assert bc.severity == "info"                                              # "does not affect the ... ATC"
    assert bc.starts == datetime(2026, 11, 2, 15, 0)   # 09:00 local, UTC-6 after 2026-11-01
    assert bc.ends == datetime(2026, 11, 6, 23, 0)
    assert "not a confirmed ATC change" in bc.body
    assert ev["SaskPower T1"].refs == ["power.intertie.sk"] and ev["SaskPower T1"].severity == "warn"
    assert ev["2375 (NWE Test)"].refs == ["power.intertie.matl"] and ev["2375 (NWE Test)"].extra["report"] == "24month"
    assert res.event_scopes == [("aeso_ets_tx_outages", "outage")]


def test_tx_outages_dropped_row_is_withdrawn(tmp_path):
    from montney.store import Store
    st = Store(tmp_path / "t.duckdb")
    first = build(SPECS["aeso_ets_tx_outages"]).run(ctx_serving(tx_routes()))
    st.upsert_events(first.events, complete_scopes=first.event_scopes)
    # AESO republishes without the SaskPower outage
    csv7 = "\n".join(l for l in fixture("power_tx_7day.csv").splitlines() if "SaskPower" not in l) + "\n"
    second = build(SPECS["aeso_ets_tx_outages"]).run(ctx_serving(tx_routes(csv7=csv7)))
    st.upsert_events(second.events, complete_scopes=second.event_scopes)
    active = {e["title"] for e in st.events(kinds=["outage"])}
    assert not any("SaskPower" in t for t in active) and len(active) == 2


def test_tx_outages_partial_fetch_raises_not_withdraws():
    routes = tx_routes()
    del routes["_Longterm_Critical_outages.csv"]  # 404 -> must not return a partial "complete" set
    with pytest.raises(Exception):
        build(SPECS["aeso_ets_tx_outages"]).run(ctx_serving(routes))
