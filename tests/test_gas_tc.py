"""NGTL Daily Operating Plan (series + outage events) and TC bulletins (events).

Synthetic payloads shaped like TC's responses; numbers are made up.
"""

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift
from montney.connectors.ngtl_bulletins import area_refs, bulletin_severity
from montney.connectors.ngtl_dop import severity


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """L-081 caches (<home>/cache): each test starts with none, and never touches the user's data dir."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

E3 = 0.0353146667


def ctx_for(routes: dict):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        for path, body in routes.items():
            if req.url.path.endswith(path):
                return httpx.Response(200, json=body(req) if callable(body) else body)
        return httpx.Response(404, json={"message": "nope"})

    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)),
                   now=datetime(2026, 10, 31, 21, 0)), calls


def area_obj(i, acr):
    return {"id": i, "dopAcronym": acr, "acronym": acr, "displayName": acr, "capabilities": [], "locations": []}


CHART = {"message": "Success", "data": [
    {"gasDay": "2026-10-29", "historicalGasDay": "2025-10-29", "areaId": 3, "flow": 148000.0,
     "historicalFlow": 147000.0, "firmContractEnergy": 5668.0, "capabilityVolume": 148000.0},
    {"gasDay": "2026-11-04", "historicalGasDay": "2025-11-04", "areaId": 3, "flow": None,
     "historicalFlow": 149000.0, "firmContractEnergy": 5668.0, "capabilityVolume": 135000.0},
    {"gasDay": "2026-11-20", "historicalGasDay": "2025-11-20", "areaId": 1, "flow": None,
     "historicalFlow": 360000.0, "firmContractEnergy": None, "capabilityVolume": None},
    {"gasDay": "2026-11-04", "historicalGasDay": "2025-11-04", "areaId": 99, "flow": None,
     "historicalFlow": 1.0, "firmContractEnergy": None, "capabilityVolume": 2.0},
]}
OUTAGES = {"message": "Success", "data": [
    {"outageId": 20171456, "area": area_obj(3, "EGAT"), "startDateTime": "2026-11-04 00:00:00",
     "endDateTime": "2026-11-06 00:00:00", "description": " NPS 42 Foothills Pipeline Zone 6 - Pipeline Maintenance",
     "impact": " Partial impact to FT-D; Empress/McNeill Borders ", "areaBaseCapability": "161000.0",
     "flowCapability": "135000", "typicalFlow": None, "localAreaBaseCapability": None,
     "localAreaOutageCapability": None, "publishedDateTimeUtc": "2026-10-30 21:34:43"},
    {"outageId": 20216913, "area": area_obj(3, "EGAT"), "startDateTime": "2026-11-01 00:00:00",
     "endDateTime": "2026-11-07 00:00:00", "description": "Jenner - Compressor Station Maintenance",
     "impact": "No impact to FT-D anticipated", "areaBaseCapability": "161000.0",
     "flowCapability": "154000", "typicalFlow": None, "publishedDateTimeUtc": "2026-10-30 21:34:43"},
    {"outageId": 555, "area": area_obj(8, "LCLR"), "startDateTime": "2026-11-09 00:00:00",
     "endDateTime": "2026-11-13 00:00:00", "description": "Saddle Hills C4 - Compressor Station Maintenance",
     "impact": "Potential impact to FT-R; Segment 4", "areaBaseCapability": None,
     "flowCapability": "157000", "typicalFlow": "135000-165000", "publishedDateTimeUtc": "2026-10-30 21:34:43"},
]}
PUBS = {"message": "Success", "data": ["2026-10-30 21:34:43", "2026-10-29 21:32:48"]}


def run_dop(chart=CHART, outages=OUTAGES):
    ctx, calls = ctx_for({"/outages/publisheddates": PUBS, "/chart": chart, "/outages": outages})
    return build(load_specs()["ngtl_dop"]).run(ctx), calls


# ------------------------------------------------------------------- DOP series
def test_dop_series_units_and_vintages():
    res, calls = run_dop()
    chart_req = next(c for c in calls if c.url.path.endswith("/chart"))
    assert {"start", "end"} <= set(chart_req.url.params)  # TC returns nothing without both
    obs = {(o.series_id, o.ts): o for o in res.obs}
    cap = obs[("ngtl.dop.egat.capability", datetime(2026, 11, 4))]
    assert cap.value == pytest.approx(135000 * E3) and cap.value == pytest.approx(4767.5, abs=0.1)
    assert cap.as_of == datetime(2026, 10, 30, 21, 34, 43)  # publication time, not fetch time
    flow = obs[("ngtl.dop.egat.flow", datetime(2026, 10, 29))]
    assert flow.as_of == flow.ts
    assert ("ngtl.dop.egat.flow", datetime(2026, 11, 4)) not in obs  # future flow is null
    assert obs[("ngtl.dop.egat.flow_yago", datetime(2026, 11, 4))].value == pytest.approx(149000 * E3)
    assert ("ngtl.dop.usjr.capability", datetime(2026, 11, 20)) not in obs  # beyond horizon
    assert not any(".99." in sid or sid.endswith(".flow_yago") and o.value == 1.0 * E3
                   for (sid, _), o in obs.items())  # unknown areas skipped
    units = {m.series_id: m.unit for m in res.meta}
    assert units["ngtl.dop.osda.capability"] == "MMcf/d"


def test_dop_fails_loudly_on_drift():
    with pytest.raises(FormatDrift):
        run_dop(chart={"message": "Success"})  # what TC returns without start/end
    bad = {"data": [{**OUTAGES["data"][0], "area": area_obj(3, "WGAT")}]}
    with pytest.raises(FormatDrift, match="areaId 3"):
        run_dop(outages=bad)


# ------------------------------------------------------------------ DOP events
def test_dop_outage_events():
    res, _ = run_dop()
    assert res.event_scopes == [("ngtl_dop", "outage")]  # complete set -> cancelled ones withdrawn
    ev = {e.id: e for e in res.events}
    nps = ev["ngtl_dop:20171456:EGAT"]
    assert nps.kind == "outage" and nps.severity == "critical" and nps.refs == ["gas.ngtl.egat"]
    assert nps.title.startswith("NPS 42 Foothills") and "(EGAT)" in nps.title
    # NGTL gas day starts 08:00 MST = 15:00 UTC (TC bulletin 3374725064); ends after gas day 11-06.
    assert nps.starts == datetime(2026, 11, 4, 15) and nps.ends == datetime(2026, 11, 7, 15)
    assert nps.extra["cut_mmcfd"] == pytest.approx(26000 * E3, abs=0.1)
    assert ev["ngtl_dop:20216913:EGAT"].severity == "info"
    local = ev["ngtl_dop:555:LCLR"]
    assert local.severity == "warn" and local.refs == [] and local.extra["local"] is True


def test_dop_severity_rules():
    assert severity("Partial impact to FT-D", 161000, 135000) == "critical"
    assert severity("Potential impact to FT-R", None, 157000) == "warn"
    assert severity("No impact to FT-D anticipated", 161000, 140000) == "warn"  # >= 10% cut
    assert severity("No impact to FT-D anticipated", 161000, 154000) == "info"


# ------------------------------------------------------------------- bulletins
def bulletin(i, subject, provider="NGTL", kind="Other", critical=False):
    return {"id": i, "subject": subject, "isCritical": critical, "contactName": "Jane Doe",
            "contactPhone": "403-555-0100", "publishedDateTimeUtc": "2026-10-30 14:49:22",
            "startDateTimeUtc": "2026-10-30 14:49:22",
            "subscription": {"name": kind, "serviceProvider": {"name": provider}}}


def run_bulletins(rows, body="<p>EGAT forecast total capability:</p><p>Sunday – 145 E6</p>"):
    def detail(req):
        return {"data": {"id": req.url.path.rsplit("/", 1)[-1], "body": body,
                         "publishedBy": "someone@example.com", "contactName": "Jane Doe"}}
    routes = {"/bulletin/sent": lambda req: {"data": {"data": rows}}}
    ctx, calls = ctx_for({**routes, "/bulletin/1": detail, "/bulletin/2": detail, "/bulletin/3": detail})
    return build(load_specs()["ngtl_bulletins"]).run(ctx), calls


def test_bulletins_events_refs_and_privacy():
    rows = [bulletin(1, "Weekend Border Capability Forecast (EGAT/WGAT)", kind="Capacity Constraint"),
            bulletin(2, "Notice of Upstream NGTL EGAT Service Restriction", provider="Foothills Saskatchewan"),
            bulletin(3, "TC Energy Planned Maintenance", provider="Mainline", kind="Maintenance"),
            bulletin(4, "Invitation to the Operations Meeting", kind="Event")]
    res, calls = run_bulletins(rows)
    sent = next(c for c in calls if c.url.path.endswith("/bulletin/sent"))
    assert sent.url.params["submission_date_start"] == "undefined"  # TC 502s without these
    ev = {e.id: e for e in res.events}
    assert set(ev) == {"ngtl_bulletins:1", "ngtl_bulletins:2", "ngtl_bulletins:4"}  # Mainline dropped
    wk = ev["ngtl_bulletins:1"]
    assert wk.severity == "warn" and wk.refs == ["gas.ngtl.egat", "gas.ngtl.wgat"]
    assert "145 E6" in wk.body and wk.url.endswith("/#/Bulletin/1")
    fh = ev["ngtl_bulletins:2"]
    assert fh.severity == "warn" and fh.refs == ["gas.ngtl.fhsk", "gas.ngtl.egat"]
    assert fh.title.startswith("Foothills Saskatchewan:") and fh.body == ""  # no body for "Other"
    assert ev["ngtl_bulletins:4"].severity == "info"
    flat = repr(res.events)
    assert "Jane Doe" not in flat and "555-0100" not in flat and "someone@example.com" not in flat


def test_bulletin_rules():
    assert bulletin_severity("Curtailment", False) == "critical"
    assert bulletin_severity("Other", "True") == "critical"
    assert bulletin_severity("Tolerance Change", False) == "warn"
    assert bulletin_severity("Other", False, "Force Majeure at Empress") == "warn"
    assert bulletin_severity("News", False, "Interim rates") == "info"
    assert area_refs("West Gate and OSDA") == ["gas.ngtl.wgat", "gas.ngtl.osda"]


def test_bulletins_fail_loudly_on_drift():
    ctx, _ = ctx_for({"/bulletin/sent": {"data": []}})
    with pytest.raises(FormatDrift):
        build(load_specs()["ngtl_bulletins"]).run(ctx)


# ------------------------------------------------------------------ gas day / text
def test_ngtl_gas_day_is_fixed_0800_mst():
    from datetime import date
    from montney.connectors.ngtl_gasday import gas_day_of, gas_day_start_utc
    # Same UTC hour before and after Alberta's 2026-11-01 switch to permanent UTC-6.
    assert gas_day_start_utc(date(2026, 7, 1)) == datetime(2026, 7, 1, 15)
    assert gas_day_start_utc(date(2026, 11, 2)) == datetime(2026, 11, 2, 15)
    assert gas_day_of(datetime(2026, 11, 2, 14, 59)) == date(2026, 11, 1)
    assert gas_day_of(datetime(2026, 11, 2, 15, 0)) == date(2026, 11, 2)
    from montney.connectors.ngtl_gasday import foothills_gas_day_start_utc
    assert foothills_gas_day_start_utc(date(2026, 7, 1)) == datetime(2026, 7, 1, 14)   # CDT
    assert foothills_gas_day_start_utc(date(2026, 12, 1)) == datetime(2026, 12, 1, 15)  # CST


def test_html_text_drops_word_metadata():
    from montney.connectors.gas_common import html_text
    body = ("<!--[if gte mso 9]><xml><o:DocumentProperties><o:Author>Jane Doe</o:Author>"
            "</o:DocumentProperties></xml><![endif]--><style>p{}</style><p>Gas day stays 08:00 MST</p>")
    assert html_text(body) == "Gas day stays 08:00 MST"


def test_shipped_gas_day_series_declare_day_gas():
    """Every shipped NGTL gas-day and Westcoast daily series opts into the gas-day cutoff."""
    import re
    from montney.connectors import build, load_specs
    metas = [m for spec in load_specs().values() for m in build(spec).declared_series()]
    pat = re.compile(r"^(ngtl\.gdsr\..+|ngtl\.dop\..+|westcoast\..+\.capacity)$")
    gas = [m for m in metas if pat.match(m.series_id)]
    assert len(gas) >= 12 + 2 + 20, len(gas)  # dop 12, westcoast 2, gdsr dozens
    assert [m.series_id for m in gas if m.day != "gas"] == []
    # and nothing else is accidentally on the gas day (e.g. intraday CSR)
    assert all(m.day == "" for m in metas if m.series_id.startswith("ngtl.csr."))


def test_dop_rereads_only_on_a_new_publication():
    """L-081: an unchanged DOP publication within 12 h costs one small request (publisheddates) and writes nothing."""
    res, calls = run_dop()
    assert len(calls) == 3 and res.obs                                         # first: publisheddates + chart + outages
    res, calls = run_dop()
    assert [c.url.path.rsplit("/", 1)[-1] for c in calls] == ["publisheddates"]
    assert not res.obs and not res.events and not res.event_scopes             # nothing withdrawn by a skip
    assert any("no new DOP publication" in w for w in res.warnings)
    global PUBS
    old = PUBS
    try:
        PUBS = {"message": "Success", "data": ["2026-10-31 21:30:00"] + old["data"]}   # TC publishes again
        res, calls = run_dop()
        assert len(calls) == 3 and res.obs
    finally:
        PUBS = old


def test_bulletin_bodies_are_fetched_once():
    """L-081: a sent bulletin's body doesn't change; the second run reads it from the cache."""
    rows = [bulletin(1, "EGAT capability", kind="Capacity Constraint")]
    res, calls = run_bulletins(rows)
    assert sum(c.url.path.endswith("/bulletin/1") for c in calls) == 1
    first = {e.id: e.body for e in res.events}
    res, calls = run_bulletins(rows)
    assert not any(c.url.path.endswith("/bulletin/1") for c in calls)
    assert {e.id: e.body for e in res.events} == first and first["ngtl_bulletins:1"]
