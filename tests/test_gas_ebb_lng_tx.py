"""Texas LNG feedgas (gas_ebb_lng_tx.py): Gulf South's GasQuest posting list + base64 CSV, and Golden Pass Pipeline's
gasnom OA table. Layouts copy the sites'; volumes are made up (1,037 Dth = 1 MMcf)."""

from __future__ import annotations

import base64
import json
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_et as ebb
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 18, 0)          # 12:00 CT

GS_HEAD = ("TSP Name,TSP,Post Date/Time,Effective Gas Day,Effective Time,LineCode,Loc,Loc Name,Loc Zn,Loc Purp Desc,"
           "Loc/QTI,Flow Ind,Design Capacity,Operating Capacity,Total Scheduled Quantity,Operationally  Available "
           "Capacity,IT,All Qty Avail,Quantity Not Available Reason,Meas Basis Desc")


def gs_csv(day, posted, tsq, loc="24329"):
    rows = [GS_HEAD,
            f'"Gulf South Pipeline Company, LLC",078444247,{posted},{day},10:00:00,325,6,Raceland,SYS,Delivery Location,'
            f'DPQ,D,8618,8618,1,8617,,Y,,MMBtu',
            f'"Gulf South Pipeline Company, LLC",078444247,{posted},{day},10:00:00,625,{loc},Stratton Ridge (To Freeport '
            f'Lng),SYS,Delivery Location,DPQ,D,1779129,1779129,{tsq},1,,Y,,MMBtu']
    return base64.b64encode(("﻿" + "\r\n".join(rows)).encode())


POSTINGS = {"totalPostingsCount": "1128", "postings": [
    {"infoPostName": "Operational Capacity", "tspId": 1, "datetimePostingEffective": "2026-11-13T09:38:24+00:00",
     "cycleCode": "TD1", "description": "11/13/2026 TD1-10:00",
     "reportFiles": [{"infoPostTrackerID": 101, "fileName": "OACY-1-20261113-TD1.csv"},
                     {"infoPostTrackerID": 102, "fileName": "OACY-1-20261113-TD1.pdf"}]},
    {"infoPostName": "Operational Capacity", "tspId": 1, "datetimePostingEffective": "2026-11-13T07:08:37+00:00",
     "cycleCode": "TD11", "description": "11/12/2026 TD11-08:00",
     "reportFiles": [{"infoPostTrackerID": 99, "fileName": "OACY-1-20261112-TD11.csv"}]},
    {"infoPostName": "Operational Capacity", "tspId": 1, "datetimePostingEffective": "2026-11-12T20:38:37+00:00",
     "cycleCode": "EVE", "description": "11/13/2026 Evening",
     "reportFiles": [{"infoPostTrackerID": 90, "fileName": "OACY-1-20261113-EVE.csv"}]}]}

GP_PAGE = """<html><body>TSP: 809259778 Posting Date/Time: November 12, 2026 08:29 PM CT &nbsp; Eff Gas Day/Time:
November 13, 2026 09:00 AM CT Cycle Indicator Description: Evening Capacity Type Description Operationally available
<table><tr><th>Location Name</th><th>Loc</th><th>Loc Zn</th><th>Loc Purp Desc</th><th>Loc/QTI</th><th>Flow Ind</th>
<th>All Qty Avail</th><th>Design Capacity</th><th>Operating Capacity</th><th>TSQ</th><th>OAC</th><th>IT Indicator</th>
<th>Qty Reason</th></tr>
<tr><td>Gulf Run</td><td>805469</td><td>1</td><td>Receipt Location</td><td>RDQ</td><td>R</td><td>Y</td><td>1,250,000</td>
<td>1,250,000</td><td>868,434</td><td>381,566</td><td>&nbsp;</td><td></td></tr>
<tr><td>Terminal</td><td>1097217</td><td>1</td><td>Delivery Location</td><td>RDQ</td><td>D</td><td>Y</td><td>2,600,910</td>
<td>2,600,910</td><td>829,600</td><td>1</td><td>&nbsp;</td><td></td></tr>
<tr><td></td><td>1097217</td><td>1</td><td>Receipt Location</td><td>RDQ</td><td>R</td><td>Y</td><td>2,600,910</td>
<td>2,500,000</td><td>0</td><td>2,500,000</td><td>&nbsp;</td><td></td></tr></table></body></html>"""


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(ebb, "PACE", 0)


def handler(seen, files=None, page=GP_PAGE):
    files = files or {101: gs_csv("20261113", "20261113 09:38:00", 1_555_500),
                      99: gs_csv("20261112", "20261113 07:08:00", 1_451_800)}

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "www.gasnom.com":
            return httpx.Response(200, text=page)
        if req.method == "POST":
            return httpx.Response(200, json=POSTINGS)
        return httpx.Response(200, content=files[int(req.url.params["postingsDocumentId"])])
    return h


def run(seen=None, now=NOW, **kw):
    seen = [] if seen is None else seen
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler(seen, **kw))), now=now)
    return build(load_specs(include_disabled=True)["gas_ebb_lng_tx"]).run(ctx), ctx, seen


def test_freeport_and_golden_pass():
    res, ctx, seen = run()
    v = {(o.series_id, o.ts): (round(o.value, 3), o.as_of) for o in res.obs}
    assert v[("lng.tx.freeport.feedgas", datetime(2026, 11, 13))] == (1500, datetime(2026, 11, 13, 15, 38))  # TD1, CT->UTC
    assert v[("lng.tx.freeport.feedgas", datetime(2026, 11, 12))] == (1400, datetime(2026, 11, 13, 13, 8))   # TD11
    assert v[("lng.tx.golden_pass.feedgas", datetime(2026, 11, 13))] == (800, datetime(2026, 11, 13, 2, 29))
    body = json.loads(next(r for r in seen if r.method == "POST").content)
    assert body["infoPostID"] == 1 and body["tspId"] == 1 and body["sortDescending"] is True
    assert ctx.n_requests == 1 + 2 + 1                       # list + today's TD1 + yesterday's TD11 + Golden Pass
    # same postings an hour later: nothing new to download from Gulf South
    res2, ctx2, _ = run(now=datetime(2026, 11, 13, 19, 0))
    assert ctx2.n_requests == 2 and [o.series_id for o in res2.obs] == ["lng.tx.golden_pass.feedgas"]


def test_drift():
    with pytest.raises(FormatDrift, match="24329"):
        run(files={101: gs_csv("20261113", "20261113 09:38:00", 1, loc="11111"), 99: gs_csv("20261112", "x", 1)})
    with pytest.raises(FormatDrift, match="header changed"):
        run(page=GP_PAGE.replace("<th>TSQ</th>", "<th>Sched</th>"))
    with pytest.raises(FormatDrift, match="Terminal"):
        run(page=GP_PAGE.replace("1097217", "1"))
