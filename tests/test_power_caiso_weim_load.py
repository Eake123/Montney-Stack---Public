"""CAISO OASIS WEIM actual hourly load (PLAN 9, wcan's BC load): mocked SingleZip payloads in the live shape.

The rows are cut from the real 2026-11-13 response (SLD_FCST / ACTUAL, 2026-11-01 07:00Z to 2026-11-13 19:00Z).
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import httpx
import pytest

from montney import regions
from montney.connectors import build, load_specs
from montney.connectors import power_caiso_weim as weim
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
HEAD = ("INTERVALSTARTTIME_GMT,INTERVALENDTIME_GMT,LOAD_TYPE,OPR_DT,OPR_HR,OPR_INTERVAL,MARKET_RUN_ID,TAC_AREA_NAME,"
        "LABEL,XML_DATA_ITEM,POS,MW,EXECUTION_TYPE,GROUP\n")
L = "Total Actual Hourly Integrated Load,SYS_FCST_ACT_MW"
# The US fall-back day: 08:00Z is 01:00 PDT (OPR_HR 2), 09:00Z the repeated 01:00 PST (OPR_HR 25). GMT stamps keep them
# apart; nothing is read from OPR_DT / OPR_HR.
ROWS = f"""2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,AVRN,{L},7.8,0,ACTUAL,2
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,AVRN,{L},7.8,0,ACTUAL,2
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,BCHA,{L},7.8,6736,ACTUAL,10
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,BCHA,{L},7.8,6812,ACTUAL,10
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,BPAT,{L},7.8,6463,ACTUAL,12
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,BPAT,{L},7.8,6491,ACTUAL,12
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,CA ISO-TAC,{L},7,19325,ACTUAL,13
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,CA ISO-TAC,{L},7,20116,ACTUAL,13
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,MWD-TAC,{L},7.3,88,ACTUAL,18
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,MWD-TAC,{L},7.3,88,ACTUAL,18
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,PGE-TAC,{L},7.1,8524,ACTUAL,24
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,PGE-TAC,{L},7.1,8870,ACTUAL,24
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,SCE-TAC,{L},7.2,8674,ACTUAL,27
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,SCE-TAC,{L},7.2,9010,ACTUAL,27
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,SDGE-TAC,{L},7.4,2106,ACTUAL,29
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,SDGE-TAC,{L},7.4,1998,ACTUAL,29
2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,VEA-TAC,{L},7.5,42,ACTUAL,34
2026-11-01T09:00:00-00:00,2026-11-01T10:00:00-00:00,0,2026-11-01,25,0,ACTUAL,VEA-TAC,{L},7.5,41,ACTUAL,34
"""
NOW = datetime(2026, 11, 1, 10, 40)
ERROR_XML = ("<?xml version='1.0' encoding='UTF-8'?><m:OASISReport xmlns:m='http://www.caiso.com/soa/OASISReport_v1.xsd'>"
             "<m:MessagePayload><m:RTO><m:ERROR><m:ERR_CODE>1001</m:ERR_CODE><m:ERR_DESC>Invalid Request</m:ERR_DESC>"
             "</m:ERROR></m:RTO></m:MessagePayload></m:OASISReport>")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """The one-off back-fill keeps a marker under <home>/cache: never the developer's real home."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))


def zipped(text: str, name: str = "20261101_20261101_SLD_FCST_ACTUAL_20261113_08_40_54_v1.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def run(body: bytes, now: datetime = NOW, seen: list | None = None, status: int = 200):
    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, content=body)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)
    return build(SPECS["power_caiso_weim_load"]).run(c)


def test_gmt_hours_across_the_fall_back_and_one_request():
    seen: list = []
    res = run(zipped(HEAD + ROWS), seen=seen)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v[("weim.bcha.load", datetime(2026, 11, 1, 8))] == 6812                 # OPR_HR 2 (01:00 PDT)
    assert v[("weim.bcha.load", datetime(2026, 11, 1, 9))] == 6736                 # OPR_HR 25 (the repeated 01:00 PST)
    assert v[("weim.bpat.load", datetime(2026, 11, 1, 9))] == 6463
    assert not any(s.startswith("weim.avrn") for s, _ in v)                       # generation-only area: not stored
    assert len(seen) == 1                                                         # one request for every area
    q = seen[0].url.params
    assert (q["queryname"], q["market_run_id"], q["resultformat"]) == ("SLD_FCST", "ACTUAL", "6")
    assert (q["startdatetime"], q["enddatetime"]) == ("20261030T10:00-0000", "20261101T10:00-0000")   # 48 h window


def test_one_off_backfill_from_the_spec_date_then_48_h(tmp_path):
    """Reviewer on #485: the first run reads from params.backfill_from in its single request; later runs, 48 h."""
    now = datetime(2026, 11, 13, 18, 20)
    for expect in ("20261101T00:00-0000", "20261111T18:00-0000"):
        seen: list = []
        run(zipped(HEAD + ROWS), now=now, seen=seen)
        assert len(seen) == 1 and seen[0].url.params["startdatetime"] == expect
    assert (tmp_path / "cache" / "caiso_weim" / "backfilled_from_2026-11-01").exists()


def test_caiso_tacs_sum_to_ca_iso_tac():
    """Identity (in the test, not in production): CAISO's five TACs add up to CA ISO-TAC, to the MW."""
    data, _ = weim.parse(zipped(HEAD + ROWS), NOW)
    for t in (datetime(2026, 11, 1, 8), datetime(2026, 11, 1, 9)):
        tacs = sum(data[a][t] for a in ("PGE-TAC", "SCE-TAC", "SDGE-TAC", "VEA-TAC", "MWD-TAC"))
        assert tacs == data["CA ISO-TAC"][t]


def test_complete_hours_only():
    late = f"2026-11-01T10:00:00-00:00,2026-11-01T11:00:00-00:00,0,2026-11-01,3,0,ACTUAL,BCHA,{L},7.8,6713,ACTUAL,10\n"
    data, _ = weim.parse(zipped(HEAD + ROWS + late), NOW)                         # 10:40: 10-11 isn't over
    assert datetime(2026, 11, 1, 10) not in data["BCHA"]
    data, _ = weim.parse(zipped(HEAD + ROWS + late), datetime(2026, 11, 1, 11, 0))
    assert data["BCHA"][datetime(2026, 11, 1, 10)] == 6713


def test_blank_is_skipped_never_zero_and_new_area_warns():
    rows = ROWS.replace(",ACTUAL,BPAT,{L},7.8,6463,".format(L=L), ",ACTUAL,BPAT,{L},7.8,,".format(L=L))
    rows += f"2026-11-01T08:00:00-00:00,2026-11-01T09:00:00-00:00,0,2026-11-01,2,0,ACTUAL,NEWBA,{L},7.8,500,ACTUAL,40\n"
    res = run(zipped(HEAD + rows))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert ("weim.bpat.load", datetime(2026, 11, 1, 9)) not in v and v[("weim.bpat.load", datetime(2026, 11, 1, 8))]
    assert any("NEWBA" in w for w in res.warnings) and not any("newba" in s for s, _ in v)


@pytest.mark.parametrize("body, match", [
    (zipped(HEAD.replace("TAC_AREA_NAME", "AREA") + ROWS), "missing columns"),
    (zipped(HEAD + ROWS.replace("SYS_FCST_ACT_MW", "SYS_FCST_5MIN_MW")), "data item"),
    (zipped(HEAD + ROWS.replace("2026-11-01T09:00:00-00:00,2026-11-01T10:00:00", "2026-11-01T09:30:00-00:00,"
                                "2026-11-01T10:00:00")), "one clock hour"),
    (zipped(HEAD + ROWS.replace("2026-11-01T08:00:00-00:00,", "11/01/2026 08:00,")), "doesn't parse"),
    (zipped(HEAD + ROWS.replace(",6812,", ",n/a,")), "isn't a number"),
    (zipped(HEAD + ROWS.replace(",6812,", ",-6812,")), "out of range"),
    (zipped(HEAD + ROWS.replace(",6812,", ",681200,")), "out of range"),
    (zipped(HEAD + ROWS + ROWS.replace(",6812,", ",6900,")), "two values"),
    (zipped(HEAD + "\n".join(r for r in ROWS.splitlines() if ",BCHA," not in r) + "\n"), "no BCHA"),
    (zipped(HEAD), "no complete hour"),
    (zipped(ERROR_XML, "INVALID_REQUEST.xml"), "error reply 1001"),
    (b"<html>maintenance</html>", "not a zip"),
])
def test_fails_closed(body, match):
    with pytest.raises(FormatDrift, match=match):
        run(body)


def test_no_retry_inside_a_run():
    """OASIS asks for >= 5 s between requests; the shared client would retry a 5xx after 2 s. One attempt, then fail."""
    seen: list = []
    with pytest.raises(httpx.HTTPStatusError):
        run(b"", seen=seen, status=503)
    assert len(seen) == 1


def test_series_meta_and_regions():
    meta = {m.series_id: m for m in build(SPECS["power_caiso_weim_load"]).declared_series()}
    assert meta["weim.bcha.load"].extra["region"] == "wcan" and meta["weim.bpat.load"].extra["region"] == "pnw"
    assert meta["weim.ca_iso_tac.load"].extra["region"] == "cal" and meta["weim.pge.load"].extra["region"] == "pnw"
    assert all(m.unit == "MW" and m.freq == "1h" and m.extra["tier"] == "hourly" for m in meta.values())
    assert weim.series_id("CA ISO-TAC") == "weim.ca_iso_tac.load" and weim.series_id("PGE-TAC") == "weim.pge_tac.load"


def test_every_wcan_power_part_is_declared():
    declared = {m.series_id for s in SPECS.values() for m in build(s).declared_series()}
    w = regions.load()["wcan"]
    parts = {p.series for ln in [*w.power.values(), *w.fuels.values()] for p in ln.parts}
    assert "weim.bcha.load" in parts and "bch.load" not in parts
    bc = {p for p in parts if p.startswith(("weim.", "bch."))}       # aeso.gen.* are derived series, not declared
    assert bc == {"weim.bcha.load", "bch.intertie.bc_us", "bch.intertie.bc_ab"} and bc <= declared
