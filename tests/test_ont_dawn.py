"""Ontario gas in (L-093 part 2): Great Lakes / Vector / Panhandle at the Dawn side, and the aggregate. Payload shapes
copy the EBBs' (tcplus JSON, gasnom's OA table, Energy Transfer's by-location table); the volumes are made up."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import ont_dawn as od
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 3, 21, 0)


def run(sid, body, stored=None):
    conn = build(load_specs(include_disabled=True)[sid])
    if stored is not None:
        conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=body))), now=NOW))


def glgt_rows(day, cycle, posted, pts):
    return [{"GasDay": f"{day} 9:00 AM CCT", "PostedDateTime": posted, "CycleName": cycle, "LocationName": n,
             "LocationID": "1", "LocQti": "DPQ", "OperatingCapacity": "0", "TotalScheduledQuantity": f"{q:,}"}
            for n, q in pts]


def test_glgt_st_clair_net_latest_cycle():
    content = (glgt_rows("11/02/2026", "TIM", "11/1/2026 4:30:59 PM", [("ST CLAIR DELIVERY", 1_037_000)])
               + glgt_rows("11/02/2026", "ID3", "11/2/2026 9:30:00 PM",
                           [("ST CLAIR DELIVERY", 1_037_000), ("ST CLAIR RECEIPT", 518_500)]))
    res = run("gas_glgt", json.dumps({"data": {"Content": content}}))
    assert [round(o.value) for o in res.obs] == [500]                                # ID3: 1,000 − 500
    with pytest.raises(FormatDrift, match="ST CLAIR DELIVERY"):
        run("gas_glgt", json.dumps({"data": {"Content": glgt_rows("11/02/2026", "TIM", "11/1/2026 4:30:59 PM",
                                                                      [("FARWELL DELIVERY", 1)])}}))


VEC_HEAD = ("<tr><th>Location</th><th>Location Name</th><th>Purp</th><th>All</th><th>IT</th><th>DC</th><th>OPC</th>"
            "<th>TSQ</th><th>OAC</th><th>Eff Gas Day/Time</th><th>Cycle Desc</th><th>Posting Date/Time</th></tr>")


def vec_row(name, tsq, eff, cycle, posted):
    return (f"<tr><td>ST101U</td><td>{name}</td><td>Delivery Location / DPQ / D</td><td>Y</td><td>Y</td><td>1</td>"
            f"<td>1</td><td>{tsq:,}</td><td>0</td><td>{eff}</td><td>{cycle}</td><td>{posted}</td></tr>")


def test_vector_st_clair_latest_cycle_per_day():
    page = ("<table>" + VEC_HEAD + vec_row("St. Clair (US)", 1_037_000, "11/02/2026 09:00:00 AM", "Timely",
                                           "11/01/2026 04:22:57 PM")
            + vec_row("St. Clair (US)", 829_600, "11/02/2026 10:00:00 PM", "Intraday 3", "11/02/2026 09:13:01 PM")
            + vec_row("Alliance", 5, "11/02/2026 10:00:00 PM", "Intraday 3", "11/02/2026 09:13:01 PM") + "</table>")
    res = run("gas_vector", page)
    o = {x.ts: x for x in res.obs}[datetime(2026, 11, 2)]
    assert o.value == pytest.approx(800) and o.as_of == datetime(2026, 11, 3, 3, 13, 1)   # ID3, 21:13 CST -> UTC
    with pytest.raises(FormatDrift, match="header"):
        run("gas_vector", "<table><tr><td>moved</td></tr></table>")


def test_panhandle_ojibway():
    page = ('<input type="text" name="gasDay" value="11/03/2026" /><input name="cycleDesc" value="Intraday 3" />'
            "<table><tr><th>Loc</th><th>Loc Name</th><th>Loc Purp Desc</th><th>Loc/QTI</th><th>DC</th><th>OPC</th>"
            "<th>TSQ</th><th>OAC</th></tr>"
            f"<tr><td>UNION</td><td>{od.OJIBWAY}</td><td>Receipt Location</td><td>RPQ</td><td>1</td><td>1</td>"
            "<td>0</td><td>0</td></tr>"
            f"<tr><td>UNION</td><td>{od.OJIBWAY}</td><td>Delivery Location</td><td>DPQ</td><td>1</td><td>1</td>"
            "<td>103,700</td><td>0</td></tr></table>")
    res = run("gas_pepl_ojibway", page)
    assert res.obs[0].ts == datetime(2026, 11, 3) and res.obs[0].value == pytest.approx(100)
    with pytest.raises(FormatDrift, match="OJIBWAY"):
        run("gas_pepl_ojibway", '<input name="gasDay" value="11/03/2026" /><table></table>')


def test_ontario_gas_in_sums_at_the_border():
    t = datetime(2026, 11, 2)
    v = {"tcm.area.mb.outflow_est": 2_100.0, "tcm.area.on.receipts": 60.0, "tcm.niagara.imports": 750.0,
         "glgt.st_clair.net_deliveries": 420.0, "vector.st_clair.deliveries": 850.0, "pepl.ojibway.deliveries": 95.0,
         "tcm.energir.deliveries": 500.0, "tcm.qc.exports": 340.0, "tcm.iroquois.exports": 100.0}
    stored = {k: {t: x} for k, x in v.items()}
    stored["glgt.st_clair.net_deliveries"][datetime(2026, 11, 3)] = 400.0          # a day missing its other inputs
    res = run("gas_ont_supply", "", stored)
    out = {(o.series_id, o.ts): o.value for o in res.obs}
    mainline = 2_100 + 60 + 750 - 500 - 340 - 100
    assert out[("supply.on.mainline_est", t)] == pytest.approx(mainline)
    assert out[("supply.on.dawn_inflows_est", t)] == pytest.approx(420 + 850 + 95)
    assert out[(od.SUPPLY, t)] == pytest.approx(mainline + 1_365)
    assert not any(ts == datetime(2026, 11, 3) for _, ts in out) and any("2026-11-03: no" in w for w in res.warnings)
