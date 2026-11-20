"""Alliance (PLAN 4b item 3): the Mcf → MMcf/d parse and the receipt-point location formats (L-058: small checks only)."""

from __future__ import annotations

import importlib.util
from datetime import datetime

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors.base import Context

_spec = importlib.util.spec_from_file_location("build_alliance", network.NETWORK_DIR / "gas" / "build_alliance.py")
ba = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ba)


def row(zone, cap, sched, day="10/31/2026"):
    return {"rdtGasDay": day, "rdtRecType": "1R", "rdtDescription": zone, "rdtCapacityMcf": cap, "rdtSchedMcf": sched,
            "rdtDataCreateDtm": "11/01/2026 8:55:46 AM"}


def test_zones_in_mmcfd_and_total_only_with_both_zones():
    def handler(req: httpx.Request) -> httpx.Response:
        day = req.read().decode()
        rows = [row("Zone 1", 1298440.0, 1183147.0), row("Zone 2", 485565.0, 311460.0)] if "2026-10-31" in day else []
        return httpx.Response(200, json={"data": rows})
    res = build(load_specs(include_disabled=True)["gas_alliance"]).run(
        Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 1, 18)))
    pts = {(o.series_id, o.ts): o for o in res.obs}
    z1 = pts[("alliance.zone1.receipts", datetime(2026, 10, 31))]
    assert z1.value == pytest.approx(1183.147) and z1.as_of == datetime(2026, 11, 1, 14, 55, 46)   # 8:55 CST (US DST ended 02:00 Nov 1) → UTC
    assert pts[("alliance.receipts.total", datetime(2026, 10, 31))].value == pytest.approx(1494.607)


def test_rolling_backfill_covers_the_window_within_a_day():
    from montney.connectors import gas_alliance as ga
    start = datetime(2026, 11, 1)
    seen = set()
    for h in range(25):                                   # 25 hourly runs
        seen |= set(ga.backfill_days(datetime(2026, 11, 1, h) if h < 24 else datetime(2026, 11, 2, 0), 3, 100, 4))
    assert seen == set(range(4, 101))                     # every older day in the 100-day window
    assert len(ga.backfill_days(start, 3, 100, 4)) == 4


@pytest.mark.parametrize("loc, kind", [("02-13-063-01 W6", "dls_approx"), ("15-30-71-8 W6M", "dls_approx"),
                                       ("14-28-73-7-W6", "dls_approx"), ("SE 24-068-07 W6M", "dls_approx"),
                                       ("NW 35-80-16W6", "dls_approx"), ("D 44-L / 094 A 13", "nts_approx")])
def test_location_formats(loc, kind):
    lat, lon, k = ba.locate(loc)
    assert k == kind and 53 < lat < 58 and -123 < lon < -117
