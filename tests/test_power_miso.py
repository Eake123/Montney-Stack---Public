"""MISO headline (L-096): mocked /api/FuelMix + /api/Snapshot in the live shapes (public-api.misoenergy.org, 2026-11-03)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import power_miso as miso
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
ACT = {"Coal": "19859", "Natural Gas": "24949", "Nuclear": "7045", "Wind": "19571", "Solar": "4",
       "Battery Storage": "-187", "Other": "1337", "Imports": "4427"}


def fuelmix(act=ACT, total="77192", stamp="2026-11-03 9:30:00 PM") -> dict:
    return {"RefId": "03-Nov-2026 - Interval 21:30 EST", "TotalMW": total, "Fuel": {"Type": [
        {"INTERVALEST": stamp, "CATEGORY": k, "ACT": v, "FUEL_CATEGORY": f"{k} ({v} MW)"} for k, v in act.items()]}}


SNAP = [{"t": "Forecasted Peak Demand (MW)", "v": "81,746", "d": "11/03/2026 12:00:00 AM EST", "id": "0"},
        {"t": "Current Demand (MW)", "v": "79,473", "d": "11/03/2026 9:30:00 PM EST", "id": "1"},
        {"t": "Marginal Energy Cost ($/MWh)", "v": "$21.76", "d": "11/03/2026 9:30:00 PM EST", "id": "2"},
        {"t": "Scheduled Imports (-) / Exports (+) (MW)", "v": "-4,513", "d": "11/03/2026 9:28:00 PM EST", "id": "3"}]


def run(fm=None, snap=None):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=fm or fuelmix()) if req.url.path.endswith("FuelMix") else httpx.Response(
            200, json=snap if snap is not None else SNAP)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 4, 2, 40))
    return build(SPECS["power_miso"]).run(c), seen


def test_headline_values_units_and_est():
    res, seen = run()
    v = {o.series_id: (o.ts, o.value) for o in res.obs}
    t = datetime(2026, 11, 4, 2, 30)                                   # 21:30 EST = 02:30 UTC
    assert v["miso.demand"] == (t, 79473) and v["miso.price.mec"] == (t, 21.76)
    assert v["miso.demand.peak_forecast"] == (datetime(2026, 11, 3), 81746)   # the day's forecast peak
    assert v["miso.gen.gas"] == (t, 24949) and v["miso.gen.storage"] == (t, -187) and v["miso.gen.total"] == (t, 77192)
    # TotalMW as published = every category but battery storage
    assert sum(v[f"miso.gen.{k}"][1] for k in miso.FUELS if k != "storage") == v["miso.gen.total"][1]
    assert seen == ["/api/FuelMix", "/api/Snapshot"]                    # 2 requests per run
    assert SPECS["power_miso"]["mode"] == "live" and SPECS["power_miso"]["region"] == "midwest"
    assert {m.extra["tier"] for m in res.meta} == {"live"}


def test_unknown_fuel_or_renamed_snapshot_fails_closed():
    with pytest.raises(FormatDrift, match="unknown category"):
        run(fm=fuelmix({**ACT, "Hydro": "900"}))
    with pytest.raises(FormatDrift, match="Current Demand"):
        run(snap=[x for x in SNAP if x["id"] != "1"])
    with pytest.raises(FormatDrift, match="not a number"):
        run(fm=fuelmix({**ACT, "Coal": "n/a"}))


def test_all_regions_box_is_zoomed_in_and_its_mix_is_labelled_miso_wide():
    """L-100: the real-time MISO-wide box drops to priority 2 ("MISO (all regions)"); the Midwest box is the overview."""
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    root = Path(network.__file__).resolve().parents[1]
    feats = json.loads((root / "network/power/miso.geojson").read_text(encoding="utf-8"))["features"]
    p = next(f["properties"] for f in feats if f["properties"]["id"] == "power.region.miso")
    declared = {m.series_id for m in build(SPECS["power_miso"]).declared_series()}
    assert p["value"] == "miso.demand" and p["callout"]["priority"] == 2 and "all regions" in p["callout"]["label"]
    assert {r["series"] for r in p["callouts_extra"]} | set(p["panel"]["series"]) <= declared
    mix = {r["series"] for r in p["callouts_extra"] if r["priority"] == 1}
    assert mix == {f"miso.gen.{k}" for k in miso.FUELS if k != "total"}                     # load + mix
    assert all("MISO-wide" in r["label"] for r in p["callouts_extra"] if r["series"] in mix)
    assert {r["series"] for r in p["callouts_extra"] if r["priority"] == 2} == {"miso.demand.peak_forecast",
                                                                                 "miso.price.mec"}
