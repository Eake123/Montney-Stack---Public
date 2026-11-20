"""ERCOT generation outages (PLAN 9e): hourly means of the dashboard's 5-min readings; fail closed on drift."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from montney import regions
from montney.connectors import build, load_specs
from montney.connectors import power_ercot_outages as eo
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)


def row(ts_utc: datetime, offset_h: int = -6, planned=14000.0, unplanned=22000.0):
    local = ts_utc.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=offset_h)))
    stamp = local.strftime("%Y-%m-%d %H:%M:%S") + f"{offset_h:+03d}00"
    d = {"planned": planned * 0.9, "unplanned": unplanned * 0.8}
    r = {"planned": planned * 0.1, "unplanned": unplanned * 0.2}
    key = str(int(ts_utc.replace(tzinfo=timezone.utc).timestamp() * 1000))
    return key, {"deliveryTime": stamp, "dstFlag": "N",
                 "Combined": {"planned": planned, "unplanned": unplanned, "total": planned + unplanned},
                 "Dispatchable": {**d, "total": d["planned"] + d["unplanned"]},
                 "Renewable": {**r, "total": r["planned"] + r["unplanned"]}}


def payload(start: datetime, n: int, **kw):
    cur = dict(row(start + timedelta(minutes=5 * i), **kw) for i in range(n))
    return {"lastUpdated": "x", "currentOutages": 1, "days": {}, "types": ["Dispatchable", "Renewable"],
            "current": cur, "previous": {}}


def run(body, now=datetime(2026, 11, 13, 22)):
    def handler(req):
        return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(200, text=body)
    return build(SPECS["power_ercot_outages"]).run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)),
                                                           now=now))


def test_hourly_means_of_complete_hours_only():
    start = datetime(2026, 11, 13, 18, 1)                     # readings at :01, :06, ... as on the dashboard
    res = run(payload(start, 12 + 6))                         # one full hour + half of the next
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    h = datetime(2026, 11, 13, 18)
    assert v[("ercot.outage.combined.total", h)] == 36000 and v[("ercot.outage.combined.planned", h)] == 14000
    assert v[("ercot.outage.dispatchable.unplanned", h)] == pytest.approx(17600)
    assert v[("ercot.outage.renewable.total", h)] == pytest.approx(1400 + 4400)
    assert not any(t == h + timedelta(hours=1) for _, t in v)          # 6 of 12 readings: not written
    parts = v[("ercot.outage.dispatchable.total", h)] + v[("ercot.outage.renewable.total", h)]
    assert parts == pytest.approx(v[("ercot.outage.combined.total", h)])   # Combined = the two groups


def test_fall_back_hour_is_two_distinct_utc_hours():
    a = [row(datetime(2026, 11, 1, 6) + timedelta(minutes=5 * i), offset_h=-5) for i in range(12)]    # 01:xx CDT
    b = [row(datetime(2026, 11, 1, 7) + timedelta(minutes=5 * i), offset_h=-6) for i in range(12)]    # 01:xx CST
    body = {"types": ["Dispatchable", "Renewable"], "current": dict(a + b), "previous": {}}
    hours = eo.hourly(eo.parse(body))
    assert sorted(hours) == [datetime(2026, 11, 1, 6), datetime(2026, 11, 1, 7)]


def test_fails_closed():
    good = payload(datetime(2026, 11, 13, 18), 12)
    with pytest.raises(FormatDrift, match="unknown outage groups"):
        eo.parse({**good, "types": ["Dispatchable", "Renewable", "Storage"]})
    k, r = next(iter(good["current"].items()))
    with pytest.raises(FormatDrift, match="disagrees"):
        eo.parse({**good, "current": {str(int(k) + 60000): r}})
    bad = {**r, "Combined": {"planned": -5, "unplanned": 1, "total": 1}}
    with pytest.raises(FormatDrift, match="outside"):
        eo.parse({**good, "current": {k: bad}})
    with pytest.raises(FormatDrift, match="isn't JSON"):
        build(SPECS["power_ercot_outages"]).fetch(Context(client=httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="<html>blocked</html>"))), now=datetime(2026, 11, 13, 22)))


def test_tx_outage_group_uses_declared_series():
    declared = {m.series_id for m in build(SPECS["power_ercot_outages"]).declared_series()}
    tx = regions.load()["tx"]
    groups = {g.id: g for g in tx.detail["power"]}
    used = {r.series for r in groups["outages"].rows if r.series}
    assert used and used <= declared
    assert SPECS["power_ercot_outages"]["region"] == "tx" and SPECS["power_ercot_outages"]["interval"] == "1h"
