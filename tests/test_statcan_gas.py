"""Provincial gas monthly (Ontario + Québec L-091; AB, BC, SK, MB L-106): StatCan 25-10-0086 by vector, one WDS request. Values made up; the payload shape
copies WDS's getDataFromVectorsAndLatestNPeriods (one object per vector, any order)."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import statcan_gas as sg
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

AUG = "2026-08-01"


def payload(skip=()):
    out = []
    for (pv, m), vec in sg.VECTORS.items():
        if vec in skip:
            continue
        val = {"deliveries": 2_071_620.0, "storage": 8_026_573.4, "withdrawals": 0.0, "injections": 846_000.0,
               "production": 146_096.0, "residential": 310.0, "commercial": 620.0, "industrial": 31_000.0}[m]
        out.append({"status": "SUCCESS", "object": {"vectorId": vec, "vectorDataPoint": [
            {"refPer": AUG, "value": val, "releaseTime": "2026-10-23T08:30"}]}})
    return list(reversed(out))


def run(body):
    conn = build(load_specs(include_disabled=True)["gas_statcan_onqc"])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=body))), now=datetime(2026, 11, 3, 18)))


def test_provincial_demand_storage_and_net():
    res = run(payload())
    v = {(o.series_id, o.ts): o for o in res.obs}
    t = datetime(2026, 8, 1)
    assert v[("statcan.on.deliveries", t)].value == pytest.approx(2_071_620 * 35.3147 / 1000 / 31)   # ≈ 2,360
    assert v[("statcan.on.storage", t)].value == pytest.approx(8_026_573.4 * 35.3147 / 1e6)          # ≈ 283 Bcf
    assert v[("statcan.qc.storage_net_withdrawal", t)].value == pytest.approx(-846_000 * 35.3147 / 1000 / 31)
    assert v[("statcan.on.deliveries", t)].as_of == datetime(2026, 10, 23, 12, 30)                  # 08:30 ET -> UTC


def test_a_missing_vector_is_loud():
    with pytest.raises(FormatDrift, match="no data"):
        run(payload(skip={sg.VECTORS[("qc", "deliveries")]}))


def test_western_provinces_production_and_manitoba_deliveries_only():
    res = run(payload())
    v = {(o.series_id, o.ts): o for o in res.obs}
    t = datetime(2026, 8, 1)
    assert v[("statcan.sk.production", t)].value == pytest.approx(146_096 * 35.3147 / 1000 / 31)     # ≈ 166
    assert ("statcan.ab.storage", t) in v and ("statcan.bc.storage_net_withdrawal", t) in v
    assert ("statcan.mb.deliveries", t) in v
    assert not any(o.series_id.startswith("statcan.mb.") and ".deliveries" not in o.series_id for o in res.obs)
    declared = {m.series_id for m in res.meta}
    assert "statcan.mb.storage" not in declared and "statcan.on.production" not in declared


def test_end_use_split_for_the_demand_model():
    res = run(payload())
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    t = datetime(2026, 8, 1)
    for pv in ("sk", "mb", "qc", "on"):
        assert v[(f"statcan.{pv}.deliveries.industrial", t)] == pytest.approx(31_000 * 35.3147 / 1000 / 31)
        assert v[(f"statcan.{pv}.deliveries.residential", t)] == pytest.approx(310 * 35.3147 / 1000 / 31)
        assert (f"statcan.{pv}.deliveries.commercial", t) in v
    assert not any(o.series_id.startswith(("statcan.ab.deliveries.", "statcan.bc.deliveries.")) for o in res.obs)
    with pytest.raises(FormatDrift, match="industrial"):
        run(payload(skip={sg.VECTORS[("sk", "industrial")]}))
