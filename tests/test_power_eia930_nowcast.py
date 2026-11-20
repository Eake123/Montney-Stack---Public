"""EIA-930 nowcast (PLAN 9): power_eia930_nowcast fills net_gen_best / interchange_best from stored D, NG and TI."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney import regions as R
from montney.connectors import build, load_specs
from montney.connectors import power_eia930 as e9
from montney.connectors import power_eia930_nowcast as nc
from montney.connectors.base import Context

SPECS = load_specs(include_disabled=True)
NOW = datetime(2026, 11, 13, 16, 20)
H = timedelta(hours=1)
D = timedelta(days=1)
LAST_NG = datetime(2026, 11, 13, 6)       # NG posted through 07:00 UTC hour ending (Pacific local 23:00)
LAST_D = datetime(2026, 11, 13, 14)       # demand ~1-2 h old


def hours(a, b):
    t = a
    while t <= b:
        yield t
        t += H


def solar_ba(ba="ciso", start=datetime(2026, 11, 8)):
    """A BA whose net export swings with the hour of day (solar): NG - D = +2000 (+ day) at 09-15 UTC, else -2000."""
    dem = {t: 20000.0 + 100 * t.hour for t in hours(start, LAST_D)}
    net = {t: 2000.0 * (1 if 9 <= t.hour <= 15 else -1) + t.day for t in hours(start, LAST_D)}   # + day: a ref check
    return {f"eia930.{ba}.demand": dem,
            f"eia930.{ba}.net_gen": {t: dem[t] + net[t] for t in dem if t <= LAST_NG},
            f"eia930.{ba}.interchange": {t: net[t] + 7 for t in dem if t <= LAST_NG}}   # TI != NG - D a little


def run(data, now=NOW):
    conn = build(SPECS["power_eia930_nowcast"])
    conn.stored = lambda ids, start, end: data
    res = conn.run(Context(client=httpx.Client(), now=now))
    return res, {(o.series_id, o.ts): o.value for o in res.obs}


def test_actual_hours_pass_through():
    data = solar_ba()
    res, v = run(data)
    t = datetime(2026, 11, 12, 20)
    assert v[("eia930.ciso.net_gen_best", t)] == data["eia930.ciso.net_gen"][t]
    assert v[("eia930.ciso.interchange_best", t)] == data["eia930.ciso.interchange"][t]       # actual TI, not NG - D
    assert min(ts for s, ts in v if s == "eia930.ciso.net_gen_best") == datetime(2026, 11, 10, 16)   # 72 h window
    assert all(o.as_of == o.ts for o in res.obs)        # the actual overwrites the nowcast in place
    meta = {m.series_id: m for m in res.meta}
    assert meta["eia930.ciso.net_gen_best"].unit == "MW" and meta["eia930.ciso.net_gen_best"].freq == "1h"
    assert meta["eia930.ciso.net_gen_best"].extra["quality"] == "estimate"
    assert meta["eia930.ciso.interchange_best"].extra["tier"] == "hourly"


def test_nowcast_hours_carry_the_same_hour_offset():
    data = solar_ba()
    _, v = run(data)
    dem, ng = data["eia930.ciso.demand"], data["eia930.ciso.net_gen"]
    t, r = datetime(2026, 11, 13, 14), datetime(2026, 11, 12, 14)          # 14 UTC today: yesterday's 14 UTC offset
    assert v[("eia930.ciso.net_gen_best", t)] == pytest.approx(dem[t] + ng[r] - dem[r])
    ti = data["eia930.ciso.interchange"]
    # NG_best - D (+ net export), less the BA's own reported NG - D - TI (-7 here; CISO's is ~ -2.8 GW live) at r
    assert v[("eia930.ciso.interchange_best", t)] == pytest.approx(ng[r] - dem[r] - (ng[r] - dem[r] - ti[r]))
    assert v[("eia930.ciso.interchange_best", t)] == pytest.approx(v[("eia930.ciso.net_gen_best", t)] - dem[t] + 7)
    # hour of day matters: 07 UTC today is the night offset (-2000 + 12), not the last NG hour's carried forward
    assert v[("eia930.ciso.net_gen_best", datetime(2026, 11, 13, 7))] == pytest.approx(dem[datetime(2026, 11, 13, 7)]
                                                                                       - 2000 + 12)
    assert max(ts for s, ts in v if s == "eia930.ciso.net_gen_best") == LAST_D                 # up to newest demand
    # a day missing NG at that hour: the next earlier day is used (<= 3 back)
    del data["eia930.ciso.net_gen"][r]
    _, v = run(data)
    assert v[("eia930.ciso.net_gen_best", t)] == pytest.approx(dem[t] + 2000 + 11)          # 11-11 offset


def test_gen_only_ba_holds_the_same_hour_up_to_now():
    data = solar_ba()
    data["eia930.avrn.net_gen"] = {t: 100.0 + t.hour for t in hours(datetime(2026, 11, 8), LAST_NG)}
    data["eia930.avrn.interchange"] = {t: 90.0 + t.hour for t in hours(datetime(2026, 11, 8), LAST_NG)}
    _, v = run(data)
    t = datetime(2026, 11, 13, 12)
    assert v[("eia930.avrn.net_gen_best", t)] == 112 and v[("eia930.avrn.interchange_best", t)] == 102
    assert max(ts for s, ts in v if s == "eia930.avrn.net_gen_best") == LAST_D      # the newest demand hour of any BA
    assert ("eia930.avrn.net_gen_best", LAST_D + H) not in v
    # no demand anywhere: no "now", so no gen-only nowcast past the actuals
    _, v = run({k: s for k, s in data.items() if k.startswith("eia930.avrn")})
    assert max(ts for s, ts in v if s == "eia930.avrn.net_gen_best") == LAST_NG


def test_nothing_beyond_three_days():
    data = solar_ba()
    old = datetime(2026, 11, 9, 23)        # NG/TI stop 4+ days before today's hours
    for k in ("net_gen", "interchange"):
        data[f"eia930.ciso.{k}"] = {t: x for t, x in data[f"eia930.ciso.{k}"].items() if t <= old}
    _, v = run(data)
    assert ("eia930.ciso.net_gen_best", datetime(2026, 11, 12, 20)) in v          # 11-09 20 UTC is 3 days back
    assert ("eia930.ciso.net_gen_best", datetime(2026, 11, 13, 1)) not in v       # 11-10 01: no NG; 11-09 01 is 4 days
    assert ("eia930.ciso.interchange_best", datetime(2026, 11, 13, 1)) not in v
    assert all(x != 0 for x in v.values())


def test_missing_demand_hour_gives_no_value():
    data = solar_ba()
    gap = datetime(2026, 11, 13, 10)
    del data["eia930.ciso.demand"][gap]
    _, v = run(data)
    assert ("eia930.ciso.net_gen_best", gap) not in v and ("eia930.ciso.interchange_best", gap) not in v
    assert ("eia930.ciso.net_gen_best", gap - H) in v and ("eia930.ciso.net_gen_best", gap + H) in v
    for bad in (0.0, 6000.0):     # EIA's newest hour as a placeholder 0 (PNM) or a partial sum (AZPS), 2026-11-13
        data["eia930.ciso.demand"][LAST_D] = bad
        _, v = run(data)
        assert ("eia930.ciso.net_gen_best", LAST_D) not in v and ("eia930.ciso.net_gen_best", LAST_D - H) in v
        assert ("eia930.ciso.demand_best", LAST_D) not in v and v[("eia930.ciso.demand_best", LAST_D - H)] > 0
    assert nc.plausible({LAST_D - H: 4000.0, LAST_D: 4500.0}) == {LAST_D - H: 4000.0, LAST_D: 4500.0}
    assert nc.plausible({LAST_D - D: 3900.0, LAST_D: 1220.0}) == {LAST_D - D: 3900.0}      # hour before missing: day
    t0 = LAST_D - 2 * H                          # a bad hour doesn't make the good hour after it look like a jump
    assert nc.plausible({t0: 3900.0, t0 + H: 1220.0, t0 + 2 * H: 3950.0}) == {t0: 3900.0, t0 + 2 * H: 3950.0}


def test_jump_filter_tightens_with_the_ba_size():
    """Reviewer on #482: a 70 % partial hour of a big BA must not pass; real solar-hour ramps of mid BAs must."""
    t0 = LAST_D - 2 * H
    assert nc.plausible({t0: 50000.0, t0 + H: 35000.0}) == {t0: 50000.0}                  # ERCO-size, 70 %: dropped
    assert nc.plausible({t0: 50000.0, t0 + H: 53000.0}) == {t0: 50000.0, t0 + H: 53000.0}  # a 6 % ramp: kept
    assert nc.plausible({t0: 4451.0, t0 + H: 3116.0}) == {t0: 4451.0}                     # AZPS-size, 70 %: dropped
    assert nc.plausible({t0: 3868.0, t0 + H: 3129.0}) == {t0: 3868.0, t0 + H: 3129.0}     # real AZPS -19 %: kept
    assert nc.plausible({t0: 900.0, t0 + H: 1300.0}) == {t0: 900.0, t0 + H: 1300.0}       # small BA: 50 % rule


def test_gas_best_scales_the_same_hour_by_generation():
    """PLAN 9e: gen.gas_best = actual where posted, else gas(r) x net_gen_best(t) / net_gen(r) (never below 0)."""
    data = solar_ba()
    data["eia930.ciso.gen.gas"] = {t: 0.25 * v for t, v in data["eia930.ciso.net_gen"].items()}
    _, v = run(data)
    t_act, t_now = LAST_NG, LAST_D
    assert v[("eia930.ciso.gen.gas_best", t_act)] == data["eia930.ciso.gen.gas"][t_act]          # actual passes
    r = t_now - D
    want = data["eia930.ciso.gen.gas"][r] * v[("eia930.ciso.net_gen_best", t_now)] / data["eia930.ciso.net_gen"][r]
    assert v[("eia930.ciso.gen.gas_best", t_now)] == pytest.approx(want, abs=0.1)
    assert ("eia930.ciso.gen.gas_best", t_now + H) not in v                                    # no hour past demand


def test_store_unreadable_is_no_result():
    conn = build(SPECS["power_eia930_nowcast"])

    def boom(ids, start, end):
        raise RuntimeError("no server")
    conn.stored = boom
    res = conn.run(Context(client=httpx.Client(), now=NOW))
    assert res.obs == [] and any("not readable" in w for w in res.warnings)


def test_covers_every_region_ba_and_the_fetch_groups():
    used = {p.series.split(".")[1] for reg in R.load().values()
            for ln in [*reg.power.values(), *reg.fuels.values()] for p in ln.parts if p.series.startswith("eia930.")}
    assert used <= set(nc.BAS)              # CISO stays in the nowcast; cal reads CAISO's own feed for it
    fetched = {b.lower() for g in e9.GROUPS for b in g}
    assert set(nc.BAS) <= fetched and all(len(g) <= e9.MAX_GROUP for g in e9.GROUPS)
    declared = {m.series_id for m in build(SPECS["power_eia930_nowcast"]).declared_series()}
    assert ({f"eia930.{b}.{k}_best" for b in nc.BAS for k in ("net_gen", "interchange", "gen.gas")}
            | {f"eia930.{b}.demand_best" for b in nc.DEMAND_BAS}) == declared
    spec = SPECS["power_eia930_nowcast"]
    assert (spec["interval"], spec["region"], spec["retention"], spec["mode"]) == ("15m", "rest", "hourly", "cached")


def test_seeded_red_a_flat_hold_is_caught(monkeypatch):
    """The check can fail: carrying the last NG hour's offset (night) into midday is off by the solar swing (4 GW)."""
    data = solar_ba()
    t = datetime(2026, 11, 13, 14)
    _, good = run(data)
    monkeypatch.setattr(nc, "_same_hour_before", lambda t, ok: LAST_NG if ok(LAST_NG) else None)
    _, flat = run(data)
    assert abs(flat[("eia930.ciso.net_gen_best", t)] - good[("eia930.ciso.net_gen_best", t)]) > 3900
