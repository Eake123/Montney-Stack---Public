"""Westcoast notices: Station 2 South / Huntingdon capacity parser, series and events.

The notice texts below copy Enbridge's phrasing; the numbers are made up.
"""

from datetime import date, datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift
from montney.connectors.gas_westcoast import gj_to_mmcf, parse_capacity, parse_days


def notice(nid, title, text, posted="10/30/2026 07:51:26 AM", descr="Capacity Constraint"):
    d, t, ampm = posted.split(" ")
    return {"@Noticeid": str(nid), "NoticeType": "CAPC", "NoticeTypeDescr": descr, "NoticeTitle": title,
            "PostedDate": d, "PostedTime": f"{t} {ampm}", "NoticeEffFromDate": d,
            "NoticeEffFromTime": f"{t} {ampm}", "PriorNoticeId": "None",
            "NoticeText": {"rawText": text, "html": text, "isHtml": True}}


S2_NOV = ("Further to notice #184597, Westcoast will be allowing the following flow on T South:<br><br>"
          "Huntingdon Delivery Area Capacity<br><br>November 1 - 2, 2026: 2,000,000 GJ<br><br>"
          "Station 2 South Capacity<br><br>November 1 - 2, 2026: 1,800,000 GJ<br><br>"
          "The constraint is below Firm Contracted Capacity.")
S2_SPLIT = ("As per notice #183333, Westcoast will be allowing the following flow on T South: "
            "Huntingdon Delivery Area Capacity: October 17-19, 2026: 1,900,000 GJ "
            "Station 2 South Capacity: October 17-18, 2026: 1,200,000 GJ October 19, 2026: 1,600,000 GJ "
            "The constraint is below Firm Contracted Capacity.")
LINEPACK = ("Linepack Status - Critical System Linepack - 2,900 MMCF <br>Target Linepack - 3,100 MMCF<br>"
            "Physical Operations Notes : Linepack is expected to decrease today.")


def ctx_for(notices):
    def handler(req):
        assert req.url.params["businessUnit"] == "WE" and req.url.params["noticeType"] == "CRI"
        return httpx.Response(200, json={"Notices": {"Notice": notices}})
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 10, 31, 21))


def run(notices):
    return build(load_specs()["gas_westcoast"]).run(ctx_for(notices))


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("spec,days", [
    ("November 1 - 2, 2026", [date(2026, 11, 1), date(2026, 11, 2)]),
    ("October 17-18, 2026", [date(2026, 10, 17), date(2026, 10, 18)]),
    ("October 26, 2026 ID2", [date(2026, 10, 26)]),
    ("October 25 ID1, 2026", [date(2026, 10, 25)]),
    ("October 21 EVE, 2026", [date(2026, 10, 21)]),
    ("October 19 ID2 - 20, 2026", [date(2026, 10, 19), date(2026, 10, 20)]),
    ("September 8, 2026 (Revised)", [date(2026, 9, 8)]),
    ("October 30 - November 1, 2026", [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)]),
    ("December 31 - January 1, 2027", [date(2026, 12, 31), date(2027, 1, 1)]),
])
def test_parse_days(spec, days):
    assert parse_days(spec) == days


def test_parse_days_rejects_garbage():
    for bad in ("Until Further Notice", "Smarch 3, 2026", "October 30 - 2, 2026"):
        with pytest.raises(ValueError):
            parse_days(bad)


def test_parse_capacity_both_layouts():
    from montney.connectors.gas_westcoast import notice_text
    a = parse_capacity(notice_text(notice(1, "t", S2_NOV)))
    assert a["Station 2 South"] == [(date(2026, 11, 1), 1_800_000.0), (date(2026, 11, 2), 1_800_000.0)]
    assert a["Huntingdon Delivery Area"][0] == (date(2026, 11, 1), 2_000_000.0)
    b = parse_capacity(notice_text(notice(2, "t", S2_SPLIT)))
    assert dict(b["Station 2 South"])[date(2026, 10, 19)] == 1_600_000.0
    assert len(b["Huntingdon Delivery Area"]) == 3


def test_heat_value_conversion():
    # 40.000 MJ/m3 (Westcoast HV 2026/27): 1,858,306 GJ = 46,457.65 10^3 m3 = 1,640.6 MMcf/d
    assert gj_to_mmcf(1_858_306) == pytest.approx(1640.6, abs=0.1)


# ---------------------------------------------------------- series + events
def test_series_vintages_and_events():
    rev = notice(3, "Huntingdon/Station 2 South Capacity for October 19, 2026 ID2 REVISED",
                 "Huntingdon Delivery Area Capacity: October 19, 2026: 1,900,000 GJ "
                 "Station 2 South Capacity: October 19, 2026 ID2: 1,000,000 GJ "
                 "Due to an unplanned compressor station outage, Westcoast will be decreasing flow.",
                 posted="10/19/2026 01:15:00 PM")
    res = run([
        notice(1, "Huntingdon/Station 2 South Capacity for November 1 - 2, 2026", S2_NOV),
        notice(2, "Huntingdon/Station 2 South Capacity for October 17-19, 2026", S2_SPLIT,
               posted="10/16/2026 07:45:00 AM"),
        rev,
        notice(4, "Pipeline Operational Status for 10/24/2026", LINEPACK,
               posted="10/25/2026 07:44:46 AM", descr="Pipeline Conditions"),
        notice(5, "Fort St John Mainline to Station 2 (Station #1 Westbound) Capacity for October 20 - 31, 2026",
               "Station #1 Westbound Capacity October 20 - 31, 2026: 0 GJ", posted="10/19/2026 09:00:00 AM"),
    ])
    s2 = [o for o in res.obs if o.series_id == "westcoast.station2_south.capacity"]
    nov1 = [o for o in s2 if o.ts == datetime(2026, 11, 1)]
    assert len(nov1) == 1 and nov1[0].value == pytest.approx(1_800_000 / 40 * 0.0353146667)
    assert nov1[0].as_of == datetime(2026, 10, 30, 12, 51, 26)  # posted 07:51:26 CCT (CDT, UTC-5)
    oct19 = sorted((o.as_of, o.value) for o in s2 if o.ts == datetime(2026, 10, 19))
    assert len(oct19) == 2 and oct19[-1][1] == pytest.approx(gj_to_mmcf(1_000_000))  # revision wins
    assert {m.unit for m in res.meta} == {"MMcf/d"}

    ev = {e.id: e for e in res.events}
    assert ev["westcoast:1"].severity == "warn" and ev["westcoast:1"].refs == ["gas.station2", "gas.huntingdon"]
    assert ev["westcoast:3"].severity == "critical"  # unplanned outage
    assert ev["westcoast:4"].severity == "critical"  # critical linepack
    assert set(ev["westcoast:4"].refs) == {"gas.westcoast.t_north", "gas.westcoast.t_south"}
    assert "gas.westcoast.t_north" in ev["westcoast:5"].refs
    assert all(e.kind == "notice" for e in res.events)


def test_fails_loudly_when_newest_capacity_notice_drifts():
    drifted = notice(9, "Huntingdon/Station 2 South Capacity for November 3, 2026",
                     "Station 2 South allowable for Nov 3 is 1.8 PJ")  # new wording, no parse
    with pytest.raises(FormatDrift, match="notice 9"):
        run([drifted, notice(1, "Huntingdon/Station 2 South Capacity for November 1 - 2, 2026", S2_NOV,
                             posted="10/29/2026 07:51:26 AM")])
    # An older unparsable notice is only a warning.
    res = run([notice(1, "Huntingdon/Station 2 South Capacity for November 1 - 2, 2026", S2_NOV),
               notice(8, "Huntingdon/Station 2 South Capacity for October 1, 2026", "garbled",
                      posted="10/01/2026 07:00:00 AM")])
    assert any("notice 8" in w for w in res.warnings)


def test_fails_loudly_on_layout_drift():
    def handler(req):
        return httpx.Response(200, json={"notices": []})
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 10, 31))
    with pytest.raises(FormatDrift):
        build(load_specs()["gas_westcoast"]).run(ctx)
