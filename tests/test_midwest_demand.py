"""Midwest demand (L-101 part 2): the weather model and NGPL's Chicago citygate. Payload shapes copy Open-Meteo's
multi-location JSON and NGPL's OA page + Excel download; the numbers are made up."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import midwest_demand as md
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 4, 18, 0)


def run(sid, handler):
    conn = build(load_specs(include_disabled=True)[sid])
    seen = []

    def h(r):
        seen.append((r.method, str(r.url)))
        return handler(r)
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(h)), now=NOW)), seen


def om(temps):
    return [{"daily": {"time": ["2026-11-03", "2026-11-04"], "temperature_2m_mean": t}} for t in temps]


def test_weather_demand_formula_and_stamping():
    res, seen = run("gas_midwest_weather", lambda r: httpx.Response(200, json=om([[8.3, -1.7], [18.3, 28.3],
                                                                                   [None, 3.3]])))
    assert len(seen) == 1 and "latitude=41.837045%2C43.0642%2C42.3834" in seen[0][1]
    v = {(o.series_id, o.ts.day): o for o in res.obs}
    _, _, _, base, h, _c, *_ = md.FIT["il"]
    assert v[("demand.il.weather_est", 3)].value == pytest.approx(base + 10 * h)        # 10 °C-days of heating
    assert v[("demand.il.weather_est", 4)].value == pytest.approx(base + 20 * h)
    assert v[("demand.wi.weather_est", 3)].value == pytest.approx(md.FIT["wi"][3])      # 18.3 °C: base only
    assert v[("demand.wi.weather_est", 4)].value == pytest.approx(md.FIT["wi"][3] + 10 * md.FIT["wi"][5])  # cooling
    assert ("demand.mi.weather_est", 3) not in v and any("Detroit 2026-11-03" in w for w in res.warnings)
    assert all(o.as_of == o.ts for o in res.obs)                  # a daily actual, re-stamped until the day is over
    assert {m.series_id: m.extra["band"] for m in res.meta} == {"demand.il.weather_est": 0.14, "demand.wi.weather_est": 0.12,
                                                         "demand.mi.weather_est": 0.14}


def test_weather_fails_closed():
    with pytest.raises(FormatDrift, match="1 locations"):
        run("gas_midwest_weather", lambda r: httpx.Response(200, json=om([[1.0, 2.0]])[0]))
    with pytest.raises(FormatDrift, match="temperature_2m_mean"):
        run("gas_midwest_weather", lambda r: httpx.Response(200, json=[{"hourly": {}}] * 3))


def test_the_fit_is_winter_peaking():
    """Sanity on the fitted coefficients: a -10 °C day is 2.5-6.5x a mild one in every state (heating dominates)."""
    for st in md.FIT:
        assert 2.5 < md.weather_demand(st, -10.0) / md.weather_demand(st, 18.3) < 6.5


PAGE = ('<form><input type="hidden" name="__VIEWSTATE" value="vs" /><input type="hidden" name="__EVENTVALIDATION" '
        'value="ev" /><input type="radio" name="ctl00$x$location" value="rbDelivery" checked="checked" />'
        '<input type="radio" name="ctl00$x$location" value="rbReceipt" />'
        '<input type="image" name="ctl00$x$HeaderBTN1$btnDownload" src="d.png" />'
        '<select name="ctl00$x$DownloadDDL"><option selected="selected" value="EXCEL">EXCEL</option></select></form>')


def xlsx(rows):
    strings, sheet = [], []
    for i, row in enumerate(rows, 1):
        cells = []
        for col, val in zip("ABCDEFGH", row):
            strings.append(val)
            cells.append(f'<c r="{col}{i}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet.append(f"<row r=\"{i}\">{''.join(cells)}</row>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", "<sst>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{''.join(sheet)}</sheetData></worksheet>")
    return buf.getvalue()


HEAD = [["TSP", "TSP Name", "Eff Gas Day/Eff Time", "CycleDesc", "Loc Purp Desc"],
        ["6931794", "NATURAL GAS PIPELINE CO.", "11/4/2026 09:00 AM CCT", "EVENING", "Delivery Location", "Dth",
         "11/03/2026 08:45 PM"],
        ["Loc", "Loc Name", "Loc Zn", "Loc (Segment)", "Design Capacity", "Operating Capacity",
         "Total Scheduled Quantity"]]


def ngpl(rows):
    def handler(r):
        if r.method == "POST":
            body = r.content.decode()
            assert "rbDelivery" in body and "rbReceipt" not in body and "btnDownload.x" in body
            return httpx.Response(200, content=xlsx(HEAD + rows), headers={"content-type": "application/ms-excel"})
        return httpx.Response(200, text=PAGE)
    return handler


def test_ngpl_chicago_citygate():
    rows = [["9258", "NICORGAS/NGPL CDP", "06", "28", "0", "2,763,444", "1,037,000"],
            ["9285", "PGLC/NGPL CDP", "06", "30", "0", "974,565", "311,100"],
            ["9254", "NO SHORE/NGPL CDP", "06", "30", "0", "284,222", "62,220"],
            ["9260", "NIPSCO/NGPL CDP", "06", "36", "0", "991,794", "417,102"],
            ["4131", "NICORGAS/NGPL PARK RIDGE COOK", "06", "30", "545,963", "545,963", "0"]]
    res, seen = run("gas_ngpl_chicago", ngpl(rows))
    assert [m for m, _ in seen] == ["GET", "POST"]
    o = res.obs[0]
    assert o.series_id == md.CITYGATE and o.value == pytest.approx(1_360)       # (1,037,000 + 311,100 + 62,220) / 1,037
    assert o.ts == datetime(2026, 11, 4) and o.as_of == datetime(2026, 11, 4, 2, 45)   # 8:45 PM CST -> UTC


def test_ngpl_fails_closed():
    with pytest.raises(FormatDrift, match="NICORGAS/NGPL CDP"):
        run("gas_ngpl_chicago", ngpl([["9285", "PGLC/NGPL CDP", "06", "30", "0", "1", "1"],
                                      ["9254", "NO SHORE/NGPL CDP", "06", "30", "0", "1", "1"]]))
    with pytest.raises(FormatDrift, match="Download button"):
        run("gas_ngpl_chicago", lambda r: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(FormatDrift, match=".xlsx"):
        run("gas_ngpl_chicago", lambda r: httpx.Response(200, text=PAGE if r.method == "GET" else "<html/>"))
    assert json.dumps(md.NGPL_LDC)                   # the three LDC aggregates are named, not pattern-matched
