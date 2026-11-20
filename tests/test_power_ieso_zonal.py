"""IESO demand by zone (L-106 8c sont + mbnwo): mocked PUB_DemandZonal CSV in the live shape (2026-11-07)."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_ieso_zonal as iz
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
HEAD = ("\\\\Hourly Zonal Demand Report,,,,,,,,,,,,,,\n\\\\Created at 2026-11-07 08:00:14,,,,,,,,,,,,,,\n"
        "\\\\For 2026,,,,,,,,,,,,,,\n"
        "Date,Hour,Ontario Demand,Northwest,Northeast,Ottawa,East,Toronto,Essa,Bruce,Southwest,Niagara,West,Zone Total,Diff\n")
ROW = "2026-11-06,{he},15500,471,1378,948,895,5405,1031,136,3297,454,1638,15652,152\n"


def run(text, now=datetime(2026, 11, 7, 20)):
    spec = {**SPECS["power_ieso_zonal"], "params": {"from_year": 2026}}
    c = Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=text))), now=now)
    return build(spec).run(c)


def test_zones_hour_ending_est_and_north_split():
    res = run(HEAD + ROW.format(he=1) + ROW.format(he=24))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    t1 = datetime(2026, 11, 6, 5)                                     # HE1 EST = 05:00 UTC
    assert v[("ieso.zone.northwest.demand", t1)] == 471 and v[("ieso.zone.toronto.demand", t1)] == 5405
    assert v[("ieso.demand.ontario", datetime(2026, 11, 7, 4))] == 15500                     # HE24
    zones = sum(v[(f"ieso.zone.{z.lower()}.demand", t1)] for z in iz.ZONES)
    assert abs(zones - 15652) <= 1                                     # = the file's own Zone Total (IESO rounds)
    north = {m.series_id for m in res.meta if m.extra.get("region") == "mbnwo"}
    assert north == {"ieso.zone.northwest.demand", "ieso.zone.northeast.demand", "ieso.zone.north.demand"}
    assert v[("ieso.zone.north.demand", t1)] == 471 + 1378                       # the mbnwo detail box


def test_missing_zone_or_bad_value_fails_closed():
    with pytest.raises(FormatDrift, match="missing columns"):
        iz.parse(HEAD.replace("Northwest,", "") + ROW.format(he=1))
    with pytest.raises(FormatDrift, match="isn't a number"):
        iz.parse(HEAD + ROW.format(he=1).replace("5405", "n/a"))


def test_north_box_uses_declared_series():
    import json
    from pathlib import Path

    from montney import network
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    p = json.loads((Path(network.__file__).resolve().parents[1] / "network/power/ontario_north.geojson")
                   .read_text(encoding="utf-8"))["features"][0]["properties"]
    declared = {m.series_id for m in build(SPECS["power_ieso_zonal"]).declared_series()}
    assert p["value"] == "ieso.zone.north.demand" and p["callout"]["priority"] == 2
    assert {r["series"] for r in p["callouts_extra"]} | set(p["panel"]["series"]) <= declared
