"""TC Mainline areas (GCTS gas day summary, one request) and the bulletin capability table (L-077).

Payload shapes copy TC's: the GCTS JSON (with its `)]}',` first line) and the maintenance bulletin's HTML table. The
points and volumes are made up.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import tc_mainline as tm
from montney.connectors.base import Context
from montney.connectors.gas_common import E3M3_TO_MMCF as K, FormatDrift

NOW = datetime(2026, 11, 2, 18)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """L-081 caches (<home>/cache): each test starts with none, and never touches the user's data dir."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
D1 = "2026-11-01"


def mm(gj: float) -> float:
    return gj / 39.2 * K


@pytest.mark.parametrize("name, area", [
    ("Empress", "ab"), ("Suffield 2", "ab"), ("Transgas SSDA", "sk"), ("Success", "sk"), ("Centram MDA", "mb"),
    ("Spruce", "mb"), ("Emerson 2", "emerson"), ("Union SSMDA", "on"), ("Union NDA", "on"), ("TCPL WDA", "on"),
    ("SS. Marie", "on"), ("Energir EDA", "eda"), ("Iroquois", "eda"), ("Union NCDA", "east"), ("Kirkwall", "east"),
    ("Brand New Point", None)])
def test_points_fall_in_their_province_area(name, area):
    assert tm.area_of(name) == area           # SSMDA is not MDA, NCDA is not NDA


ROWS = [  # GJ/d
    ("Empress", "Receipt", 4_000_000), ("Suffield 2", "Receipt", 100_000), ("Empress", "Delivery", 10_000),
    ("Success", "Receipt", 20_000), ("Transgas SSDA", "Delivery", 300_000), ("Centram SSDA", "Delivery", 5_000),
    ("Centram MDA", "Delivery", 200_000), ("Emerson 1", "Delivery", 400_000), ("Emerson 2", "Delivery", 1_600_000),
    ("Emerson 2", "Receipt", 30_000), ("Union NDA", "Delivery", 80_000), ("Union SSMDA", "Delivery", 70_000),
    ("SS. Marie", "Receipt", 60_000), ("Energir EDA", "Delivery", 600_000), ("Iroquois", "Delivery", 500_000),
    ("Lachenaie", "Receipt", 7_000), ("Union Parkway Belt", "Receipt", 1_500_000), ("Mystery Point", "Delivery", 9_000)]


def gcts(rows=ROWS, day=D1) -> str:
    body = {"rows": [{"gasDay": day, "flowDirection": f, "locationName": n, "drnNumber": 1, "scheduledQty": q}
                     for n, f, q in rows]}
    return ")]}',\n" + json.dumps(body)


def run_flows(text: str, seen: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req.url)
        return httpx.Response(200, text=text, headers={"content-type": "application/json"})
    conn = build(load_specs(include_disabled=True)["tc_mainline"])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))


def test_one_request_sums_points_into_areas_and_through_flows():
    seen: list = []
    res = run_flows(gcts(), seen)
    assert len(seen) == 1 and "fromGasDay" in str(seen[0]) and "toGasDay" in str(seen[0])
    v = {o.series_id: o.value for o in res.obs}
    assert v["tcm.area.ab.receipts"] == pytest.approx(mm(4_100_000))
    assert v["tcm.prairies.receipts"] == pytest.approx(mm(4_120_000))                   # Empress + Suffield + SK
    assert v["tcm.area.sk.deliveries"] == pytest.approx(mm(305_000))
    sk_out = 4_100_000 - 10_000 + 20_000 - 305_000
    assert v["tcm.area.sk.outflow_est"] == pytest.approx(mm(sk_out))
    nol = sk_out - 200_000 - 2_000_000 + 30_000                                        # − Centra − Emerson + imports
    assert v["tcm.area.mb.outflow_est"] == pytest.approx(mm(nol))
    assert v["tcm.area.on.outflow_est"] == pytest.approx(mm(nol + 60_000 - 150_000))   # arriving at NBJ
    assert v["tcm.emerson.deliveries"] == pytest.approx(mm(2_000_000))
    assert v["tcm.eda.deliveries"] == pytest.approx(mm(1_100_000 - 7_000))
    assert not any(s.startswith("tcm.area.") and "parkway" in s for s in v)           # east of NBJ: no area series
    assert v["tcm.parkway.net_deliveries"] == pytest.approx(mm(-1_500_000))           # L-091: its own point group
    assert {o.ts for o in res.obs} == {datetime(2026, 11, 1)}
    assert any("Mystery Point 9 TJ" in w for w in res.warnings)                       # unknown: warned, not counted
    assert mm(1_000_000) == pytest.approx(900.9, abs=0.1)                               # 1 PJ/d ≈ 901 MMcf/d at 39.2


def test_flows_fail_loudly():
    with pytest.raises(FormatDrift, match="expected JSON"):
        run_flows("<html>maintenance</html>")
    with pytest.raises(FormatDrift, match="no rows"):
        run_flows(gcts(rows=[]))
    with pytest.raises(FormatDrift, match="no gas day with Empress"):
        run_flows(gcts(rows=[("Centram MDA", "Delivery", 1)]))


TABLE = ("<p>Below is a summary of forecasted system capabilities for the posting period beginning December 26, 2025."
         "</p><table border=1><tr><th></th><th width=180>Prairies Line<sup>1</sup><br/>(TJ)</th><th>Emerson Extension"
         "<sup>2</sup><br/>(TJ)</th><th>Northern Ontario Line<sup>3</sup><br/>(TJ)</th><th>Eastern Delivery Area<sup>4"
         "</sup><br/>(TJ)</th><th>Downstream STN 148<sup>5</sup><br/>(TJ)</th></tr><tr><th width=180>December 31</th>"
         "<td align=center>5065</td><td align=center>3455</td><td align=center>2485</td><td align=center>2555</td>"
         "<td align=center>1200</td></tr><tr><th>January 1</th><td>5180</td><td>3445</td><td>2505</td><td>3105</td>"
         "<td>1205</td></tr></table><sup>4</sup>Eastern Delivery Area represents …")


def bulletin(i, subject, provider="Mainline", published="2025-12-23 20:45:52"):
    return {"id": i, "subject": subject, "publishedDateTimeUtc": published,
            "subscription": {"name": "Maintenance", "serviceProvider": {"name": provider}}}


def run_cap(rows, bodies):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/bulletin/sent"):
            return httpx.Response(200, json={"data": {"data": rows}})
        return httpx.Response(200, json={"data": {"body": bodies[int(req.url.path.rsplit("/", 1)[1])]}})
    conn = build(load_specs(include_disabled=True)["tc_mainline_capability"])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))


def test_capability_table_by_day_across_the_new_year():
    rows = [bulletin(1, "TC Energy Planned Maintenance Dec 26, 2025 - Jan 5, 2026"),
            bulletin(2, "Prairies Pipeline System - Unplanned Maintenance"),                  # no table: never read
            bulletin(3, "TC Energy Planned Maintenance Dec 26 - Jan 5", provider="NGTL")]    # not Mainline
    res = run_cap(rows, {1: TABLE})
    v = {(o.series_id, o.ts): o for o in res.obs}
    jan1 = datetime(2026, 1, 1)
    assert v[("tcm.prairies.capability", jan1)].value == pytest.approx(5180 * 1000 / 38.5 * K)
    assert v[("tcm.nol.capability", datetime(2025, 12, 31))].value == pytest.approx(2485 * 1000 / 38.5 * K)
    assert v[("tcm.stn148.capability", jan1)].value == pytest.approx(1205 * 1000 / 38.5 * K)
    assert v[("tcm.eda.capability", jan1)].as_of == datetime(2025, 12, 23, 20, 45, 52)      # vintage = publication
    assert len(res.obs) == 10 and not res.warnings


def test_capability_without_a_table_fails_loudly():
    with pytest.raises(FormatDrift, match="capability table"):
        run_cap([bulletin(1, "TC Energy Planned Maintenance Oct 30-Nov 9, 2026")], {1: "<p>no table today</p>"})
    res = run_cap([bulletin(1, "Mainline Fuel Ratios")], {})
    assert res.obs == [] and any("no Mainline" in w for w in res.warnings)


def test_capability_parser_reads_the_real_layout():
    t = tm.capability_table(TABLE, datetime(2025, 12, 23))
    assert t == {date(2025, 12, 31): {"prairies": 5065, "emerson": 3455, "nol": 2485, "eda": 2555, "stn148": 1200},
                 date(2026, 1, 1): {"prairies": 5180, "emerson": 3445, "nol": 2505, "eda": 3105, "stn148": 1205}}


def test_capability_bulletin_bodies_are_fetched_once():
    """L-081: the maintenance bulletin's body is cached by id; the second run requests only the list."""
    rows = [bulletin(1, "TC Energy Planned Maintenance Dec 26, 2025 - Jan 5, 2026")]
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path.endswith("/bulletin/sent"):
            return httpx.Response(200, json={"data": {"data": rows}})
        return httpx.Response(200, json={"data": {"body": TABLE}})
    conn = build(load_specs(include_disabled=True)["tc_mainline_capability"])
    a = conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))
    n1 = len(seen)
    b = conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))
    assert n1 == 2 and len(seen) - n1 == 1                                         # list + body, then list only
    assert {(o.series_id, o.ts, o.value) for o in a.obs} == {(o.series_id, o.ts, o.value) for o in b.obs}


def test_ontario_and_quebec_points_from_the_same_request():
    """L-091: points east of North Bay Junction summed into groups (no new request); Québec demand = Énergir."""
    rows = ROWS + [("Enbridge CDA", "Delivery", 200_000), ("Union CDA", "Delivery", 50_000),
                   ("Union Parkway Belt", "Delivery", 1_800_000), ("Union Parkway Belt", "Receipt", 1_100_000),
                   ("Kirkwall", "Delivery", 780_000), ("Kirkwall", "Receipt", 30_000),
                   ("Niagara Falls", "Receipt", 600_000), ("Chippawa", "Receipt", 270_000),
                   ("Chippawa", "Delivery", 40_000),
                   ("Energir NDA", "Delivery", 7_000),
                   ("Philipsburg", "Delivery", 30_000)]
    v = {o.series_id: o.value for o in run_flows(gcts(rows)).obs}
    assert v["tcm.cda.deliveries"] == pytest.approx(mm(250_000))
    assert v["tcm.parkway.net_deliveries"] == pytest.approx(mm(1_800_000 - 1_100_000 - 1_500_000))   # + ROWS'

    assert v["tcm.kirkwall.net_deliveries"] == pytest.approx(mm(750_000))
    assert v["tcm.niagara.imports"] == pytest.approx(mm(600_000 + 270_000 - 40_000))
    assert v["tcm.iroquois.exports"] == pytest.approx(mm(500_000))                # Waddington (ROWS)
    assert v["tcm.energir.deliveries"] == pytest.approx(mm(600_000 + 7_000))       # EDA (ROWS) + NDA
    assert v["demand.qc.implied_est"] == v["tcm.energir.deliveries"]
    assert v["tcm.qc.exports"] == pytest.approx(mm(30_000))
    assert v["tcm.philipsburg.exports"] == pytest.approx(mm(30_000))                # L-106: one by one too
    assert v["tcm.east_hereford.exports"] == 0 and v["tcm.napierville.exports"] == 0
    assert v["tcm.area.on.deliveries"] == pytest.approx(mm(80_000 + 70_000 + 7_000))  # Énergir NDA: still NOL area


def test_iroquois_export_is_net():
    rows = ROWS + [("Iroquois", "Receipt", 100_000)]
    v = {o.series_id: o.value for o in run_flows(gcts(rows)).obs}
    assert v["tcm.iroquois.exports"] == pytest.approx(mm(500_000 - 100_000))
