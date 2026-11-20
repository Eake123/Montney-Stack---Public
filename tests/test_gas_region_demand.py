"""gas_region_demand (US regions' daily gas demand, est.) and gas_cheniere_ccl (Corpus Christi feed gas, scheduled).
Stored inputs are faked (the connector reads the store only); the numbers are made up."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_cheniere_ccl as ccl
from montney.connectors import gas_region_demand as grd
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPEC = load_specs(include_disabled=True)["gas_region_demand"]
NOW = datetime(2026, 11, 13, 18, 0)          # 12:00 in Chicago
D = datetime(2026, 11, 11)                   # a complete local day for every input


def conn(regions: list[str], industrial: dict | None = None):
    spec = copy.deepcopy(SPEC)
    spec["params"]["regions"] = {r: spec["params"]["regions"][r] for r in regions}
    if industrial is not None:
        spec["params"]["industrial"] = industrial
    return build(spec)


def hourly(mw: float, day: datetime, tz: str, days: int = 1) -> dict[datetime, float]:
    from zoneinfo import ZoneInfo
    return {h: mw for i in range(days) for h in grd.local_day_hours(day + timedelta(days=i), ZoneInfo(tz))}


def stored_for(c, mw: dict[str, float], temp: float | dict[str, float], ind: dict[str, float], days=(D,)):
    """Every BA at `mw[ba]` for each hour of `days`, every city at `temp` (realized), industrial last year's month."""
    out: dict[str, dict[datetime, float]] = {}
    for r, cfg in c.regions.items():
        for d in days:
            for g in cfg["power"]:
                for ba in g["bas"]:
                    out.setdefault(f"eia930.{ba}.gen.gas", {}).update(hourly(mw[ba], d, cfg["tz"]))
            for city in c.weights(r, cfg):
                t = temp[city] if isinstance(temp, dict) else temp
                out.setdefault(f"wx.{city}.tmean.realized", {})[d] = t
    for st, v in ind.items():
        out[f"eia.{st.lower()}.deliveries.industrial"] = {datetime(2025, 11, 1): v}
    return out


def run(c, stored, now=NOW):
    c.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    return c.run(Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), now=now))


def values(res):
    return {(o.series_id, o.ts): o.value for o in res.obs}


def test_texas_sum_of_the_three_terms():
    c = conn(["tx"])
    res = run(c, stored_for(c, {"erco": 1000.0, "epe": 100.0}, 8.0, {"TX": 5000.0}))
    v = values(res)
    power = 747.1 + 9.113 * 24 * 1100 / 1037          # base + HR x MWh / 1037
    rescom = 601.9 + 236.5 * (18 - 8)                  # a + b x HDD at base 18
    assert v[("gas.tx.power_burn_est", D)] == pytest.approx(power)
    assert v[("gas.tx.rescom_est", D)] == pytest.approx(rescom)
    assert v[("gas.tx.industrial_est", D)] == pytest.approx(5000.0)      # the store's Nov 2025, not the table
    assert v[("gas.tx.demand_est", D)] == pytest.approx(power + rescom + 5000.0)
    o = next(o for o in res.obs if o.series_id == "gas.tx.demand_est")
    assert o.as_of == o.ts                                                 # a daily actual (retention: daily)
    meta = {m.series_id: m for m in res.meta}
    assert meta["gas.tx.demand_est"].extra["band"] == pytest.approx(0.057)
    assert meta["gas.tx.demand_est"].unit == "MMcf/d" and meta["gas.tx.demand_est"].freq == "D"
    assert meta["gas.tx.demand_est"].day == ""                             # calendar day (the region's zone)


def test_a_missing_hour_or_city_gives_no_value_never_a_partial_sum():
    c = conn(["tx"])
    days = (D - timedelta(days=1), D)
    st = stored_for(c, {"erco": 1000.0, "epe": 100.0}, 8.0, {"TX": 5000.0}, days=days)
    st["eia930.epe.gen.gas"].pop(grd.local_day_hours(D, __import__("zoneinfo").ZoneInfo("America/Chicago"))[5])
    st["wx.lubbock.tmean.realized"].pop(D - timedelta(days=1))
    v = values(run(c, st))
    assert ("gas.tx.power_burn_est", D) not in v and ("gas.tx.demand_est", D) not in v   # one EPE hour short
    assert ("gas.tx.rescom_est", D) in v                                                   # weather complete on D
    assert ("gas.tx.rescom_est", D - timedelta(days=1)) not in v                           # Lubbock missing
    assert ("gas.tx.power_burn_est", D - timedelta(days=1)) in v
    assert ("gas.tx.demand_est", D - timedelta(days=1)) not in v


def test_hdd_at_the_region_base_and_weighted():
    c = conn(["nyc"])
    w = c.weights("nyc", c.regions["nyc"])
    assert set(w) == {"new_york", "buffalo", "rochester", "albany", "syracuse"}    # the Weather agent's composite
    temps = {k: (10.0 if k == "new_york" else 20.0) for k in w}                    # 20 °C: 0 HDD at base 15, not −5
    v = values(run(c, stored_for(c, {"nyis": 0.0}, temps, {"NY": 0.0})))
    hdd = 5.0 * w["new_york"] / sum(w.values())
    assert v[("gas.nyc.rescom_est", D)] == pytest.approx(826.7 + 208.0 * hdd)
    assert grd.hdd({"a": 1.0}, {"a": 10.0}, 15.0) == 5.0
    assert grd.hdd({"a": 1.0, "b": 1.0}, {"a": 10.0, "b": None}, 15.0) is None


def test_forecast_temperature_stands_in_for_today_realized_wins_for_ended_days():
    c = conn(["nyc"])
    today = datetime(2026, 11, 13)
    st = stored_for(c, {"nyis": 0.0}, 10.0, {"NY": 0.0})
    for city in c.weights("nyc", c.regions["nyc"]):
        st[f"wx.{city}.tmean"] = {today: 5.0, D: 0.0}       # D's forecast vintage is ignored: realized wins
    v = values(run(c, st))
    assert v[("gas.nyc.rescom_est", today)] == pytest.approx(826.7 + 208.0 * 10)
    assert v[("gas.nyc.rescom_est", D)] == pytest.approx(826.7 + 208.0 * 5)


def test_umw_takes_the_fitted_miso_share():
    c = conn(["umw"])
    v = values(run(c, stored_for(c, {"miso": 30000.0}, 16.0, {"MN": 1.0, "WI": 2.0, "MI": 3.0})))
    assert v[("gas.umw.power_burn_est", D)] == pytest.approx(2.838 * 30000 * 24 / 1037)
    assert v[("gas.umw.industrial_est", D)] == pytest.approx(6.0)
    assert v[("gas.umw.rescom_est", D)] == pytest.approx(704.7)                     # 16 °C at base 16: no HDD


def test_bakken_two_groups_both_required():
    c = conn(["bakken"])
    st = stored_for(c, {"nwmt": 100.0, "miso": 30000.0, "swpp": 20000.0}, 14.0, {"MT": 1.0, "ND": 1.0})
    v = values(run(c, st))
    assert v[("gas.bakken.power_burn_est", D)] == pytest.approx((12.268 * 2400 + 0.088 * 50000 * 24) / 1037)
    del st["eia930.swpp.gen.gas"]
    assert ("gas.bakken.power_burn_est", D) not in values(run(c, st))


def test_withheld_states_fall_back_to_an_older_year_labelled():
    c = conn(["sunbelt"])
    dec = datetime(2026, 12, 10)
    st = stored_for(c, {b: 10.0 for g in c.regions["sunbelt"]["power"] for b in g["bas"]}, 10.0, {}, days=(dec,))
    st["eia.az.deliveries.industrial"] = {datetime(2025, 12, 1): 70.0}            # AZ stored; NV / NM from the table
    res = run(c, st, now=datetime(2026, 12, 12, 18))
    v = values(res)
    nv_dec, nm_dec = c.industrial["NV"][11], c.industrial["NM"][11]
    assert nv_dec[1] == 2024 and nm_dec[1] == 2025                                  # NV: EIA withheld Dec 2025
    assert v[("gas.sunbelt.industrial_est", dec)] == pytest.approx(70.0 + nv_dec[0] + nm_dec[0])
    meta = {m.series_id: m for m in res.meta}
    assert meta["gas.sunbelt.industrial_est"].extra["older_year"] == {"NV": 2024}
    assert "NV 2024" in meta["gas.sunbelt.industrial_est"].label
    assert any("older year" in w and "NV (2024)" in w for w in res.warnings)
    # a state with neither a stored month nor a table cell: no industrial (and no demand), never a 0
    c2 = conn(["sunbelt"], industrial={**c.industrial, "NM": [None] * 12})
    v2 = values(run(c2, st, now=datetime(2026, 12, 12, 18)))
    assert ("gas.sunbelt.industrial_est", dec) not in v2 and ("gas.sunbelt.demand_est", dec) not in v2


def test_local_day_hours_follow_dst():
    from zoneinfo import ZoneInfo
    chi = ZoneInfo("America/Chicago")
    assert len(grd.local_day_hours(datetime(2026, 11, 1), chi)) == 25              # fall back
    assert len(grd.local_day_hours(datetime(2026, 3, 8), chi)) == 23               # spring forward
    assert grd.local_day_hours(datetime(2026, 11, 11), chi)[0] == datetime(2026, 11, 11, 6)
    assert len(grd.local_day_hours(datetime(2026, 7, 1), ZoneInfo("America/Phoenix"))) == 24


def test_every_region_declared_and_cross_checks_labelled():
    c = build(SPEC)
    ids = {m.series_id for m in c.declared_series()}
    for r in ("tx", "mtnw", "sunbelt", "nyc", "upne", "umw", "bakken", "pnw", "cal"):
        assert {f"gas.{r}.{t}" for t in grd.TERMS} <= ids
        assert len(c.weights(r, c.regions[r])) >= 5
        assert set(c.regions[r]["states"]) <= set(c.industrial)
        assert all(len(c.industrial[s]) == 12 and all(c.industrial[s]) for s in c.regions[r]["states"])
    labels = {m.series_id: m.label for m in c.declared_series()}
    assert "cross-check" in labels["gas.cal.demand_est"] and "cross-check" in labels["gas.pnw.demand_est"]
    assert "cross-check" not in labels["gas.tx.demand_est"]
    assert "drift" in labels["gas.mtnw.demand_est"] and "dual-fuel" in labels["gas.nyc.power_burn_est"]
    assert "dual-fuel" in labels["gas.upne.power_burn_est"] and "MISO" in labels["gas.umw.power_burn_est"]


# ------------------------------------------------------------------ Canadian provinces (L-107)
def test_saskatchewan_no_power_term_statcan_industrial_two_cities():
    c = conn(["sk"])
    w = c.weights("sk", c.regions["sk"])
    assert set(w) == {"regina", "saskatoon"}                                   # filtered from region.wcan
    st = {f"wx.{k}.tmean.realized": {D: 5.0} for k in w}
    st["statcan.sk.deliveries.industrial"] = {datetime(2025, 11, 1): 700.0}
    res = run(c, st)
    v = values(res)
    assert v[("gas.sk.rescom_est", D)] == pytest.approx(38.9 + 10.89 * 10)     # base 15
    assert v[("gas.sk.industrial_est", D)] == pytest.approx(700.0)
    assert v[("gas.sk.demand_est", D)] == pytest.approx(38.9 + 108.9 + 700.0)
    assert not any(o.series_id == "gas.sk.power_burn_est" for o in res.obs)
    meta = {m.series_id: m for m in res.meta}
    assert "gas.sk.power_burn_est" not in meta
    assert "StatCan" in meta["gas.sk.industrial_est"].label and "power generation" in meta["gas.sk.industrial_est"].label
    # nothing stored for last year's month: the fitted table (CA-SK, Nov 2025)
    del st["statcan.sk.deliveries.industrial"]
    assert values(run(c, st))[("gas.sk.industrial_est", D)] == pytest.approx(c.industrial["CA-SK"][10][0])


def test_ontario_power_daily_from_ieso_and_out_of_last_years_industrial():
    c = conn(["on"])
    w = c.weights("on", c.regions["on"])
    st = {f"wx.{k}.tmean.realized": {D: 15.0} for k in w}                      # base 15: no HDD
    st["ieso.gen.gas.daily"] = {D: 120_000.0, **{datetime(2025, 11, 1) + timedelta(days=i): 90_000.0
                                                 for i in range(30)}}
    st["statcan.on.deliveries.industrial"] = {datetime(2025, 11, 1): 2_000.0}
    v = values(run(c, st))
    assert v[("gas.on.power_burn_est", D)] == pytest.approx(6.66 * 120_000 / 1037)
    assert v[("gas.on.industrial_est", D)] == pytest.approx(2_000.0 - 6.66 * 90_000 / 1037)
    assert v[("gas.on.demand_est", D)] == pytest.approx(6.66 * 120_000 / 1037 + 446.9 + 2_000 - 6.66 * 90_000 / 1037)
    # last year's IESO month not stored: the spec's power_ly table (Nov 2025)
    st["ieso.gen.gas.daily"] = {D: 120_000.0}
    ly = c.regions["on"]["power"][0]["power_ly"][10][0]
    assert values(run(c, st))[("gas.on.industrial_est", D)] == pytest.approx(2_000.0 - 6.66 * ly / 1037)
    # the day's IESO value missing: no power, no demand (never a partial sum)
    st["ieso.gen.gas.daily"] = {}
    v = values(run(c, st))
    assert ("gas.on.power_burn_est", D) not in v and ("gas.on.demand_est", D) not in v


# ------------------------------------------------------------------ current days: ISO real time + small-BA hold
CHI = "America/Chicago"
Y = datetime(2026, 11, 12)                   # yesterday in Chicago at NOW: EIA-930 hasn't posted it


def tx_store(erco_rt=1000.0, eia=1010.0, epe=50.0, rt_days=(datetime(2026, 11, 6), 7), eia_end=datetime(2026, 11, 11)):
    """ERCO and EPE in EIA-930 for 10 local days up to (not incl.) eia_end; ERCO's RT feed from rt_days[0] for
    rt_days[1] days (through yesterday); weather and industrial for every day so only power decides."""
    c = conn(["tx"])
    st = {"eia930.erco.gen.gas": {}, "eia930.epe.gen.gas": {}, "ercot.gen.gas": {}}
    for i in range(10):
        d = eia_end - timedelta(days=10 - i)
        st["eia930.erco.gen.gas"].update(hourly(eia, d, CHI))
        st["eia930.epe.gen.gas"].update(hourly(epe, d, CHI))
    for i in range(rt_days[1]):
        st["ercot.gen.gas"].update(hourly(erco_rt(i) if callable(erco_rt) else erco_rt, rt_days[0] + timedelta(days=i), CHI))
    for d in (Y, datetime(2026, 11, 13)):
        for city in c.weights("tx", c.regions["tx"]):
            st.setdefault(f"wx.{city}.tmean.realized", {})[d] = 18.0
    st["eia.tx.deliveries.industrial"] = {datetime(2025, 11, 1): 5000.0}
    return c, st


def test_rt_fills_eia930s_late_days_with_the_fitted_ratio_and_the_small_ba_holds():
    c, st = tx_store()
    res = run(c, st)
    v = values(res)
    ratio = 1010.0 / 1000.0                                                     # Σ EIA / Σ RT on the 5-day overlap
    assert v[("gas.tx.power_burn_est", Y)] == pytest.approx(747.1 + 9.113 * (24 * 1000 * ratio + 24 * 50) / 1037)
    assert ("gas.tx.demand_est", Y) in v
    meta = {m.series_id: m for m in res.meta}["gas.tx.power_burn_est"]
    fit = meta.extra["rt_fit"]["erco"]
    assert fit["accepted"] and fit["ratio"] == pytest.approx(ratio) and fit["hours"] == 5 * 24
    assert meta.extra["held"] == {"epe": ["2026-11-11", "2026-11-12"]}            # EPE: 50 / 1060 = 4.7 % < 10 %
    assert meta.extra["hold"]["epe"]["share"] == pytest.approx(50 / 1060, abs=1e-4)
    assert meta.extra["rt_days"]["2026-11-12"] == {"erco": 24}
    assert "ISO real time" in meta.label and "complete local days only" in meta.label


def test_today_is_never_written_even_with_every_hour_so_far():
    c, st = tx_store(rt_days=(datetime(2026, 11, 6), 8))                         # RT through today's hours too
    st["eia930.epe.gen.gas"].update(hourly(50.0, datetime(2026, 11, 13), CHI))
    v = values(run(c, st))
    today = datetime(2026, 11, 13)
    assert ("gas.tx.power_burn_est", today) not in v and ("gas.tx.demand_est", today) not in v
    assert ("gas.tx.rescom_est", today) in v                                      # weather terms still run today


def test_rt_missing_an_hour_gives_no_value():
    c, st = tx_store()
    st["ercot.gen.gas"].pop(grd.local_day_hours(Y, __import__("zoneinfo").ZoneInfo(CHI))[7])
    v = values(run(c, st))
    assert ("gas.tx.power_burn_est", Y) not in v and ("gas.tx.demand_est", Y) not in v


def test_an_unstable_or_implausible_ratio_is_refused():
    c, st = tx_store(erco_rt=lambda i: 1000.0 if i % 2 else 1150.0)              # days alternate by 15 %
    res = run(c, st)
    assert ("gas.tx.power_burn_est", Y) not in values(res)
    assert not {m.series_id: m for m in res.meta}["gas.tx.power_burn_est"].extra["rt_fit"]["erco"]["accepted"]
    assert any("ERCO real-time gas not used" in w and "unstable" in w for w in res.warnings)
    c, st = tx_store(erco_rt=505.0)                                               # RT half of EIA: not the same plants
    res = run(c, st)
    assert ("gas.tx.power_burn_est", Y) not in values(res)
    assert any("outside" in w for w in res.warnings)
    c, st = tx_store(rt_days=(datetime(2026, 11, 9), 4))                          # 2 overlap days: too short
    res = run(c, st)
    assert ("gas.tx.power_burn_est", Y) not in values(res)
    assert any("needs 72 h" in w for w in res.warnings)


def test_a_big_ba_without_a_feed_blocks_the_day():
    c, st = tx_store(epe=200.0)                                                   # EPE 200 / 1210 = 16.5 %: not small
    res = run(c, st)
    v = values(res)
    assert ("gas.tx.power_burn_est", Y) not in v
    assert "epe" not in {m.series_id: m for m in res.meta}["gas.tx.power_burn_est"].extra["hold"]
    # EIA-930 complete for the day: the plain sum, nothing held, no RT
    c, st = tx_store(eia_end=datetime(2026, 11, 13))
    res = run(c, st)
    assert values(res)[("gas.tx.power_burn_est", Y)] == pytest.approx(747.1 + 9.113 * 24 * 1060 / 1037)
    meta = {m.series_id: m for m in res.meta}["gas.tx.power_burn_est"]
    assert meta.extra["held"] == {} and meta.extra["rt_days"] == {}


def test_a_yaml_boolean_key_fails_loudly():
    spec = copy.deepcopy(SPEC)
    spec["params"]["regions"] = {True: spec["params"]["regions"]["on"]}       # what a bare `on:` key becomes
    with pytest.raises(ValueError, match="quote"):
        build(spec)
    spec["params"]["regions"] = {"on": {**SPEC["params"]["regions"]["on"], "province": True}}
    with pytest.raises(ValueError, match="quote"):
        build(spec)


def test_provinces_declared():
    c = build(SPEC)
    ids = {m.series_id for m in c.declared_series()}
    for pv in ("sk", "mb", "qc", "on"):
        assert {f"gas.{pv}.demand_est", f"gas.{pv}.rescom_est", f"gas.{pv}.industrial_est"} <= ids
        assert f"CA-{pv.upper()}" in c.industrial and len(c.weights(pv, c.regions[pv])) >= 2
    assert "gas.on.power_burn_est" in ids and not {"gas.sk.power_burn_est", "gas.mb.power_burn_est",
                                                    "gas.qc.power_burn_est"} & ids


# ------------------------------------------------------------------ gas_cheniere_ccl
def row(loc, sched, cap=2_750_000.0, flow="D", posted="2026-11-12T22:15:18.907"):
    return {"tsp": "079841991", "cycle": "Intraday 3", "postinG_DT_TIME": posted, "loc": loc,
            "loC_NAME": f"{loc}-X", "opeR_CAP": cap, "scheD_QTY": sched, "floW_IND": flow}


def run_ccl(handler, now=NOW):
    c = build(load_specs(include_disabled=True)["gas_cheniere_ccl"])
    return c.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))      # the backfill cache stays out of the user's data dir
    return tmp_path


def test_ccl_feedgas_one_request(home):
    seen = []

    def h(r):
        seen.append(r.url)
        return httpx.Response(200, json={"report": [row("CC121073", 1_000_000.0, flow="R"),
                                                    row("CC200221", 2_738_687.0)]})
    res = run_ccl(h)
    assert len(seen) == 1
    q = seen[0].params
    assert q["tspNo"] == "400" and q["cycleId"] == "166" and q["locationId"] == "0"
    assert q["beginDate"] == "11/12/2026"                     # yesterday's gas day, Central clock
    o = res.obs[0]
    assert o.series_id == "lng.tx.corpus_christi.feedgas" and o.ts == datetime(2026, 11, 12)
    assert o.value == pytest.approx(2_738_687 / 1037)          # Dth -> MMcf at 1,037 Btu/cf
    assert o.as_of == datetime(2026, 11, 13, 4, 15, 18)        # 22:15 CST -> UTC
    assert res.meta[0].day == "gas" and res.meta[0].extra["capacity"] == pytest.approx(2_750_000 / 1037, rel=1e-3)


def test_ccl_backfills_missed_days_at_most_three(home):
    ok = lambda r: httpx.Response(200, json={"report": [row("CC200221", 2_600_000.0)]})  # noqa: E731
    run_ccl(ok)                                                # remembers 2026-11-12
    seen = []

    def h(r):
        seen.append(r.url.params["beginDate"])
        return ok(r)
    run_ccl(h, now=datetime(2026, 11, 20, 18))                 # an outage: 11-13 .. 11-19 missed
    assert seen == ["11/17/2026", "11/18/2026", "11/19/2026"]
    seen.clear()
    run_ccl(h, now=datetime(2026, 11, 20, 23))                 # same day again: re-read yesterday only
    assert seen == ["11/19/2026"]


def test_ccl_not_posted_and_format_drift(home):
    res = run_ccl(lambda r: httpx.Response(200, json={"report": [], "beginDate": "11/12/2026"}))
    assert not res.obs and "not posted" in res.warnings[0]
    with pytest.raises(FormatDrift, match="CC200221"):
        run_ccl(lambda r: httpx.Response(200, json={"report": [row("CC108011", 1.0)]}))
    with pytest.raises(FormatDrift, match="out of range"):
        run_ccl(lambda r: httpx.Response(200, json={"report": [row("CC200221", 2.7e10)]}))
    with pytest.raises(FormatDrift, match="report"):
        run_ccl(lambda r: httpx.Response(200, json=[1, 2]))
    assert ccl.parse({"report": [row("CC200221", 0.0)]}, D)[0] == 0.0
