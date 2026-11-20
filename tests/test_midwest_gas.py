"""Midwest gas (L-095): Northern Border and ANR from TC eConnects' SSRS CSV export, EIA monthly state pages, the Chicago
drop-off. Payload shapes copy the live ones (the CSV's columns, the viewer's ExportUrlBase); the volumes are made up."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import midwest_gas as mw
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 3, 21, 0)
VIEWER = ('<html><script>{"ExportUrlBase":"/infopost/Reserved.ReportViewerWebControl.axd?ReportSession=abc\\u0026'
          'OpType=Export\\u0026Format="}</script></html>')
NB_COLS = ["TSPName", "EffGasDay", "Cycle", "PostingDate", "LocationName", "Location", "LocPurpDesc",
           "OperatingCapacity", "TotalScheduledQuantity", "LocQTI", "Direction", "RateSchedule"]


def csv_text(cols, rows):
    out = [",".join(cols)]
    for r in rows:
        out.append(",".join(f'"{r.get(c, "")}"' for c in cols))
    return "﻿" + "\r\n".join(out)


def nb_row(name, qti, tsq, direction="", rs="FT1", qty_col="TotalScheduledQuantity"):
    return {"TSPName": "Northern Border Pipeline Company", "EffGasDay": "11/3/2026", "Cycle": "TIM",
            "PostingDate": "11/2/2026 4:59 PM", "LocationName": name, "LocQTI": qti, "Direction": direction,
            "RateSchedule": rs, qty_col: f"{tsq}"}


def run(sid, viewer=VIEWER, body="", stored=None):
    conn = build(load_specs(include_disabled=True)[sid])
    if stored is not None:
        conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    seen = []

    def handler(r):
        seen.append(str(r.url))
        if "ReportViewerWebControl" in str(r.url):
            assert str(r.url).endswith("Format=CSV")
            return httpx.Response(200, text=body, headers={"content-type": "text/csv; charset=utf-8"})
        return httpx.Response(200, text=viewer if "ReportViewer.aspx" in str(r.url) else body)
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    return conn.run(ctx), seen


NB_ROWS = [nb_row("Flow Past Port Of Morgan", "SGQ", 259_250, "Forwardhaul"),
           nb_row("Flow Past Glen Ullin", "SGQ", 2_333_250, "Forwardhaul"),
           nb_row("Flow Past Glen Ullin", "SGQ", 2_333_250, "Forwardhaul", rs="IT"),      # a rate-schedule copy
           nb_row("Flow Past Harper", "SGQ", 414_800, "Forwardhaul"),
           nb_row("Port of Morgan", "RPQ", 207_400), nb_row("Ventura", "DPQ", 1_659_200),
           nb_row("Ventura", "RPQ", 103_700), nb_row("Will County", "DPQ", 103_700),
           nb_row("Will County", "DPQ", 103_700, rs="IT"), nb_row("North Hayden", "DPQ", 207_400),
           nb_row("Channahon", "RPQ", 103_700), nb_row("Iowa City", "DPQ", 51_850)]


def test_nbpl_two_requests_bakken_and_chicago():
    res, seen = run("gas_nbpl", body=csv_text(NB_COLS, NB_ROWS))
    assert len(seen) == 2                                         # the viewer, then its CSV export: no postback
    v = {o.series_id: o.value for o in res.obs}
    assert v["nbpl.bakken.net_receipts"] == pytest.approx(2_000)  # (2,333,250 − 259,250) / 1,037
    assert v["nbpl.glen_ullin.flow"] == pytest.approx(2_250)      # counted once despite its rate-schedule copy
    assert v["nbpl.port_of_morgan.receipts"] == pytest.approx(200)
    assert v["nbpl.ventura.net_deliveries"] == pytest.approx(1_500)
    assert v["nbpl.harper.flow"] == pytest.approx(400)
    assert v[mw.NBPL_CHICAGO] == pytest.approx(200)              # Will County 100 + North Hayden 200 − Channahon 100
    o = res.obs[0]
    assert o.ts == datetime(2026, 11, 3) and o.as_of == datetime(2026, 11, 2, 22, 59)   # 4:59 PM CST -> UTC


def test_nbpl_fails_closed():
    with pytest.raises(FormatDrift, match="ExportUrlBase"):
        run("gas_nbpl", viewer="<html>maintenance</html>")
    with pytest.raises(FormatDrift, match="Flow Past Glen Ullin"):
        run("gas_nbpl", body=csv_text(NB_COLS, [r for r in NB_ROWS if r["LocationName"] != "Flow Past Glen Ullin"]))
    two_days = NB_ROWS + [{**NB_ROWS[0], "EffGasDay": "11/4/2026"}]
    with pytest.raises(FormatDrift, match="2 gas days"):
        run("gas_nbpl", body=csv_text(NB_COLS, two_days))


def test_anr_wisconsin_alliance_viking():
    cols = [c if c != "TotalScheduledQuantity" else "TotalSchedQty" for c in NB_COLS]
    q = "TotalSchedQty"
    rows = [nb_row("WISCONSIN               ", "DPQ", 829_600, qty_col=q), nb_row("WISCONSIN", "RPQ", 207_400, qty_col=q),
            nb_row("ALLIANCE/ANR INT", "RPQ", 933_300, qty_col=q), nb_row("ALLIANCE/ANR INT", "DPQ", 103_700, qty_col=q),
            nb_row("MARSHFIELD/VIKING INT", "RPQ", 155_550, qty_col=q),
            nb_row("MARSHFIELD/VIKING INT", "DPQ", 0, qty_col=q)]
    res, seen = run("gas_anr", body=csv_text(cols, rows))
    v = {o.series_id: o.value for o in res.obs}
    assert len(seen) == 2 and "pAssetNbr=3005" in seen[0]
    assert v == pytest.approx({"anr.wisconsin.deliveries": 800, "anr.alliance.net_receipts": 800,
                               "anr.marshfield.viking_net_receipts": 150})


def test_chicago_dropoff_sums_alliance_and_nbpl():
    t, t2 = datetime(2026, 11, 2), datetime(2026, 11, 3)
    stored = {mw.ALLIANCE: {t: 1_550.0, t2: 1_560.0}, mw.NBPL_CHICAGO: {t: 310.0}}
    res, seen = run("gas_midwest_balance", stored=stored)
    assert not seen
    assert [(o.ts, o.value) for o in res.obs] == [(t, pytest.approx(1_860))]
    assert any("2026-11-03: no nbpl.chicago" in w for w in res.warnings)
    assert res.meta[0].extra == {"band": mw.DROPOFF_BAND}       # Alliance receipts standing in: an estimate (#381)


def test_chicago_box_carries_the_band():
    import json
    import pathlib
    nodes = json.loads((pathlib.Path(__file__).parents[1] / "network/gas/nodes.geojson").read_text(encoding="utf-8"))
    f = next(x["properties"] for x in nodes["features"] if x["properties"]["id"] == "gas.midwest.chicago")
    assert f["band"] == mw.DROPOFF_BAND and "est." in f["callout"]["label"]
    alliance = next(r for r in f["callouts_extra"] if r["series"] == mw.ALLIANCE)
    assert alliance["band"] == mw.ALLIANCE_BAND and "est." in alliance["label"]
