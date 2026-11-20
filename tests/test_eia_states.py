"""US state gas + oil (EIA dnav summary pages), EIA weekly storage regions and StatCan crude oil by province (L-106 8b).
Page shapes copy EIA's summary tables (a nested stub table per row, DataB / Current2 cells) and StatCan's WDS JSON; the
numbers are made up."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import eia_states as es
from montney.connectors import statcan_gas as sg
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 7, 18, 0)
MONTHS = ["Mar-26", "Apr-26", "May-26", "Jun-26", "Jul-26", "Aug-26"]


def summary(heads, rows, release="10/30/2026"):
    th = "".join(f'<th class="Series5">{h}</th>' for h in heads)
    body = ""
    for name, cells in rows.items():
        tds = "".join(f'<td width="76" class="{"Current2" if i == len(cells) - 1 else "DataB"}">{c}</td>'
                      for i, c in enumerate(cells))
        body += ('<tr class="DataRow"><td class="DataStub"><table class="data2"><tr><td width="3"></td>'
                 f'<td class="DataStub1">{name}</td></tr></table></td>{tds}<td class="DataHist">h</td></tr>\n')
    return f"<table><tr>{th}</tr>{body}</table><span>Release Date: {release}</span>"


def all_states(cells, keys=None):
    return {es.STATES[st]: cells for st in (keys or es.STATES)}


def handler_for(pages):
    def h(r):
        for path, page in pages.items():
            if str(r.url).endswith(path):
                return httpx.Response(200, text=page)
        return httpx.Response(404, text="")
    return h


def run(sid, handler):
    conn = build(load_specs(include_disabled=True)[sid])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))


def default_pages():
    cells = ["31,000", "NA", "W", "30,000", "31,000", "62,000"]
    return {path: summary(MONTHS, {"U.S.": cells, **all_states(cells), "Federal Offshore Gulf of America": cells})
            for path, *_ in es.PAGES.values()}


def test_states_units_and_skips():
    res = run("gas_eia_states", handler_for(default_pages()))
    v = {(o.series_id, o.ts): o for o in res.obs}
    aug = datetime(2026, 8, 1)
    assert v[("eia.tx.marketed_production", aug)].value == pytest.approx(2_000)        # 62,000 MMcf / 31 d
    assert v[("eia.ny.deliveries", aug)].value == pytest.approx(2_000)
    assert v[("eia.mi.storage.working_gas", aug)].value == pytest.approx(62.0)          # MMcf -> Bcf
    assert v[("eia.ca.storage.net_withdrawal", datetime(2026, 6, 1))].value == pytest.approx(1_000)  # 30,000 / 30 d
    assert v[("eia.nd.crude_production", aug)].value == pytest.approx(62_000_000)        # thousand b/d -> b/d
    assert ("eia.tx.deliveries", datetime(2026, 4, 1)) not in v and ("eia.tx.deliveries", datetime(2026, 5, 1)) not in v
    assert not any(o.series_id.startswith("eia.u.") for o in res.obs)                    # only STATES
    assert v[("eia.tx.deliveries", aug)].as_of == datetime(2026, 10, 30)
    # the end-use split (gas_region_demand's inputs): one page each, the same units
    for use in ("residential", "commercial", "industrial", "electric"):
        assert v[(f"eia.tx.deliveries.{use}", aug)].value == pytest.approx(2_000)
    assert v[("eia.vt.deliveries.electric", datetime(2026, 6, 1))].value == pytest.approx(1_000)
    assert ("eia.nv.deliveries.industrial", datetime(2026, 5, 1)) not in v               # "W": skipped, never 0


def test_nine_pages_one_request_each():
    seen = []
    pages = default_pages()

    def h(r):
        seen.append(str(r.url))
        return handler_for(pages)(r)
    res = run("gas_eia_states", h)
    assert len(seen) == 9 and len(set(seen)) == 9
    assert {m.series_id for m in res.meta} >= {"eia.ca.deliveries.industrial", "eia.ny.deliveries.residential",
                                                "eia.ct.deliveries.commercial", "eia.az.deliveries.electric"}
    assert all(m.unit == "MMcf/d" and m.freq == "M" for m in res.meta if ".deliveries." in m.series_id)


def test_a_known_state_dropping_off_fails_loudly():
    pages = default_pages()
    path = es.PAGES["production"][0]
    rows = {n: ["1"] * 6 for n in es.STATES.values() if n != "Texas"}
    pages[path] = summary(MONTHS, rows)
    with pytest.raises(FormatDrift, match="TX"):
        run("gas_eia_states", handler_for(pages))
    pages[path] = summary(["bad"], {"Texas": ["1"]})
    with pytest.raises(FormatDrift, match="month headers"):
        run("gas_eia_states", handler_for(pages))


def test_non_producers_get_no_series():
    pages = default_pages()
    producers = {st for st in es.STATES if st not in {"WA", "MN", "WI", "ME", "NH", "VT", "MA", "RI", "CT"}}
    pages[es.PAGES["production"][0]] = summary(MONTHS, all_states(["10"] * 6, producers))
    res = run("gas_eia_states", handler_for(pages))
    assert not any(o.series_id == "eia.wa.marketed_production" for o in res.obs)          # absent, never a 0
    assert any(o.series_id == "eia.or.marketed_production" for o in res.obs)


def test_weekly_storage_regions():
    weeks = ["10/09/26", "10/16/26", "10/23/26", "10/30/26"]
    rows = {"Total Lower 48 States": ["3,500", "3,577", "3,654", "3,731"], "East": ["900", "907", "914", "921"],
            "Midwest": ["1,000", "1,007", "1,014", "1,021"], "Mountain": ["250", "250", "250", "250"],
            "Pacific": ["300", "293", "293", "286"], "South Central": ["1,050", "1,120", "1,183", "1,253"],
            "Salt": ["1", "1", "1", "1"]}
    page = summary(weeks, rows, release="11/5/2026")
    res = run("gas_eia_storage_weekly", lambda r: httpx.Response(200, text=page))
    v = {(o.series_id, o.ts): o for o in res.obs}
    wk = datetime(2026, 10, 30)
    assert v[("eia.storage.pacific.working_gas", wk)].value == 286
    assert v[("eia.storage.pacific.net_injection", wk)].value == pytest.approx(-1_000)    # −7 Bcf / 7 d
    assert v[("eia.storage.lower48.net_injection", wk)].value == pytest.approx(11_000)
    assert ("eia.storage.east.net_injection", datetime(2026, 10, 9)) not in v              # no week before it
    assert v[("eia.storage.east.working_gas", wk)].as_of == datetime(2026, 11, 5)
    with pytest.raises(FormatDrift, match="missing rows"):
        run("gas_eia_storage_weekly", lambda r: httpx.Response(200, text=summary(weeks, {"East": ["1"] * 4})))


def test_statcan_crude_oil_one_request():
    seen = []

    def h(r):
        seen.append(r)
        body = [{"status": "SUCCESS", "object": {"vectorId": v, "vectorDataPoint": [
            {"refPer": "2026-08-01", "value": 3_100_000 if v == 107757710 else None if v == 107757562 else 31_000,
             "releaseTime": "2026-10-30T08:30"}]}} for v in sg.OIL_VECTORS.values()]
        return httpx.Response(200, json=body)
    res = run("oil_statcan_provinces", h)
    assert len(seen) == 1
    v = {o.series_id: o for o in res.obs}
    assert v["statcan.ab.crude_production"].value == pytest.approx(3_100_000 * 6.28981 / 31)
    assert "statcan.mb.crude_production" not in v                                         # suppressed: skipped
    assert v["statcan.ab.crude_production"].as_of == datetime(2026, 10, 30, 12, 30)       # 08:30 EDT -> UTC
