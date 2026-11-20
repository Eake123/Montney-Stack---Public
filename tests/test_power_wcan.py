"""wcan power (L-106, PLAN 8c): BC Hydro load + interties, SaskPower's daily mix, and the region's power: lines.

BC Hydro fixtures are SYNTHETIC .xls (made with xlwt) in the live files' shape (checked 2026-11-07); the SaskPower JSON
is the live payload's shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from montney import regions
from montney.connectors import build, load_specs
from montney.connectors import power_bch as bch
from montney.connectors import power_saskpower as sk
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
FIX = Path(__file__).parent / "fixtures"
SK = {"PowerCacheData": {"asOfDate": "/Date(1793944799000)/", "generation": 2958, "netInterchange": -61,
                         "systemDemand": 2897, "generationByType": [
                             {"type": "Wind", "totalGenerationForType": 477}, {"type": "Hydro", "totalGenerationForType": 334},
                             {"type": "Solar", "totalGenerationForType": 2}, {"type": "Coal", "totalGenerationForType": 1115},
                             {"type": "Gas", "totalGenerationForType": 959}, {"type": "Other", "totalGenerationForType": 70}]}}


def ctx(body, binary=True):
    def handler(req):
        return httpx.Response(200, content=body) if binary else httpx.Response(200, json=body)
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 7, 20, 30))


def test_bch_flows_complete_hours_pst_to_utc():
    res = build(SPECS["power_bch_interties"]).run(ctx((FIX / "power_bch_flow.xls").read_bytes()))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v == {("bch.intertie.bc_us", datetime(2026, 11, 7, 18)): -300, ("bch.intertie.bc_ab", datetime(2026, 11, 7, 18)): -150,
                 ("bch.intertie.bc_us", datetime(2026, 11, 7, 19)): -200, ("bch.intertie.bc_ab", datetime(2026, 11, 7, 19)): -150}
    # 10:00 PST = 18:00 UTC (fixed UTC-8 all year); 12:00 PST has 3 of 12 samples: not stored


def test_bch_load_hour_ending_bc_local_time():
    """The load file is BC local time (America/Vancouver, UTC-7 since 2026-11-01), not PST like data1.xls: HE1 of
    11/05 = 00:00-01:00 UTC-7 = 07:00 UTC. Read as PST (before PLAN 9) every hour sat one hour late against CAISO
    OASIS's BCHA load (explicit GMT stamps)."""
    res = build(SPECS["power_bch_load"]).run(ctx((FIX / "power_bch_load.xls").read_bytes()))
    v = {o.ts: o.value for o in res.obs}
    assert v[datetime(2026, 11, 5, 7)] == 7001 and v[datetime(2026, 11, 6, 6)] == 7024 and len(v) == 24   # HE1/HE24
    assert datetime(2026, 11, 6, 7) not in v                                       # the old PST stamp of HE24


def test_bch_load_matches_oasis_bcha_on_the_same_utc_hour():
    """Real numbers, 2026-11-08 (a Sunday): BC Hydro's file (HE, BC local) against OASIS SLD_FCST ACTUAL BCHA
    (INTERVALSTARTTIME_GMT). Same UTC hour after the fix: within 2 %. The old PST reading put them 4-5 % apart."""
    from datetime import timedelta
    bch_he = {7: 6360, 8: 6699, 9: 7152, 17: 7723, 18: 8050, 19: 8236, 24: 6967}      # morning ramp, evening peak
    oasis = {datetime(2026, 11, 8, 13): 6269, datetime(2026, 11, 8, 14): 6613, datetime(2026, 11, 8, 15): 7064,
             datetime(2026, 11, 8, 23): 7669, datetime(2026, 11, 9, 0): 8002, datetime(2026, 11, 9, 1): 8198,
             datetime(2026, 11, 9, 6): 6946}
    fixed = {bch._utc(datetime(2026, 11, 8) + timedelta(hours=he - 1), bch.LOAD_TZ): mw for he, mw in bch_he.items()}
    old = {bch._utc(datetime(2026, 11, 8) + timedelta(hours=he - 1), bch.BCH_TZ): mw for he, mw in bch_he.items()}
    assert set(fixed) == set(oasis)
    assert all(abs(fixed[t] / oasis[t] - 1) < 0.02 for t in oasis)
    assert max(abs(old[t] / oasis[t] - 1) for t in old if t in oasis) > 0.04          # seeded red: the old reading


def test_saskpower_day_held_hourly_and_identity():
    res = build(SPECS["power_saskpower"]).run(ctx(SK, binary=False))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    day = [datetime(2026, 11, 5, 6 + h) if h < 18 else datetime(2026, 11, 6, h - 18) for h in range(24)]  # CST day
    assert all(v[("saskpower.demand", t)] == 2897 for t in day) and ("saskpower.demand", datetime(2026, 11, 6, 6)) not in v
    assert v[("saskpower.net_imports", day[0])] == -61 and v[("saskpower.gen.coal", day[0])] == 1115
    # *_best: the day, then held forward to the fetch hour (2026-11-07 20:00 UTC), est.; never past HOLD_MAX
    best = sorted(t for s, t in v if s == "saskpower.demand_best")
    assert best[0] == day[0] and best[-1] == datetime(2026, 11, 7, 20) and len(best) == 24 + 39
    assert all(v[("saskpower.demand_best", t)] == 2897 for t in best)
    late = Context(client=ctx(SK, binary=False).client, now=datetime(2026, 11, 20, 0))
    tail = max(o.ts for o in build(SPECS["power_saskpower"]).run(late).obs if o.series_id == "saskpower.demand_best")
    assert tail == day[-1] + timedelta(hours=1) + sk.HOLD_MAX                       # a 2-week-old day isn't "now"
    bad = {"PowerCacheData": {**SK["PowerCacheData"], "systemDemand": 3200}}
    with pytest.raises(FormatDrift, match="!= demand"):
        sk.parse(bad)
    with pytest.raises(FormatDrift, match="unknown generation type"):
        sk.parse({"PowerCacheData": {**SK["PowerCacheData"], "generationByType": [{"type": "Nuclear",
                                                                                  "totalGenerationForType": 1}]}})


def _sum(line, vals):
    return sum(p.sign * p.scale * vals[p.series] for p in line.parts)


def test_wcan_lines_cancel_the_internal_ties_and_balance():
    """AB imports 300 from BC and 100 from SK and 50 from Montana; BC exports 300 to AB and 400 to the US. The region's
    net imports are only the external borders: 50 (MATL) - 400 (BC-US) + SK's non-AB balance."""
    w = regions.load()["wcan"].power
    vals = {"aeso.csd.ail": 10000, "aeso.csd.net_generation": 9550, "aeso.csd.net_imports": 450,     # 300+100+50
            "weim.bcha.load": 7000, "bch.intertie.bc_us": 400, "bch.intertie.bc_ab": 300,               # + = BC export
            "saskpower.demand_best": 2900, "saskpower.generation_best": 3080, "saskpower.net_imports_best": -180}  # -100 to AB, -80 MB
    ic = _sum(w["interchange"], vals)
    assert ic == pytest.approx(50 - 400 - 80)                                       # MATL in, BC-US out, SK-MB out
    assert _sum(w["generation"], vals) + ic - _sum(w["load"], vals) == pytest.approx(0)
    assert {"aeso.csd.ail", "weim.bcha.load", "saskpower.demand_best"} == {p.series for p in w["load"].parts}


def test_seeded_red_counting_the_ab_bc_tie_as_external_is_caught():
    """The check can fail: leave out BC's -bc_ab part and the AB-BC flow shows up as a region import."""
    w = regions.load()["wcan"].power
    vals = {"aeso.csd.net_imports": 450, "bch.intertie.bc_us": 400, "bch.intertie.bc_ab": 300, "saskpower.net_imports_best": -180}
    without = sum(p.sign * vals[p.series] for p in w["interchange"].parts if p.series != "bch.intertie.bc_ab")
    assert without != pytest.approx(_sum(w["interchange"], vals))


def test_bad_files_fail_closed():
    with pytest.raises(FormatDrift):
        bch.parse_flows(b"not an xls")
    with pytest.raises(FormatDrift):
        bch.parse_load(b"not an xls")
