"""Alberta supply cushion, est. (L-106 8c): power_ab_cushion reads AESO CSD + realized outages back from the store."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_ab_cushion as cu
from montney.connectors.base import Context

SPECS = load_specs(include_disabled=True)
AT = datetime(2026, 11, 7, 19, 37)
CSD = {"cogeneration": (6125, 4445), "combined_cycle": (3974, 2077), "simple_cycle": (1014, 14),
       "gas_fired_steam": (3078, 239), "hydro": (896, 203), "energy_storage": (270, 0), "other": (485, 219)}
OUT = {"gas_total": 3269, "mbo": 817, "hydro": 326, "energy_storage": 24, "biomass_and_other": 261}   # live 2026-11-07


def stored(csd=CSD, out=OUT, day=datetime(2026, 11, 7)):
    s = {}
    for f, (cap, net) in csd.items():
        s[f"aeso.gen.{f}.cap"] = {AT - (datetime(2026, 11, 7, 19, 37) - datetime(2026, 11, 6, 9, 0)): cap}   # old: unchanged
        s[f"aeso.gen.{f}.net"] = {AT: net}
    for o, v in out.items():
        s[f"aeso.outage.{o}.realized"] = {day: v}
    return s


def run(data):
    spec = SPECS["power_ab_cushion"]
    conn = build(spec)
    conn.stored = lambda ids, start, end: data
    return conn.run(Context(client=httpx.Client(), now=datetime(2026, 11, 7, 19, 40)))


def test_cushion_is_dispatchable_headroom_minus_todays_outages():
    res = run(stored())
    (o,) = res.obs
    headroom = sum(c - n for c, n in CSD.values())                       # 8,645 live
    assert o.series_id == "power.ab.supply_cushion_est" and o.ts == AT
    assert o.value == pytest.approx(headroom - sum(OUT.values()))         # 8,645 - 4,697 = 3,948
    assert headroom == 8645 and res.meta[0].extra["estimate"] is True


def test_missing_input_writes_nothing():
    no_out = stored(out={k: v for k, v in OUT.items() if k != "mbo"})
    assert run(no_out).obs == [] and any("missing" in w for w in run(no_out).warnings)
    yesterday = stored(day=datetime(2026, 11, 6))                         # today's outage figures not published yet
    assert run(yesterday).obs == []


def test_seeded_red_forgetting_the_outages_is_caught(monkeypatch):
    """The check can fail: without the outage subtraction the 'cushion' is the 8,645 MW headroom, ~4.7 GW too big."""
    monkeypatch.setattr(cu, "OUTAGES", ())
    (o,) = run(stored()).obs
    assert o.value == pytest.approx(8645) and o.value != pytest.approx(8645 - 4697)
