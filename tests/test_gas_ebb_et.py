"""Energy Transfer EBB engine (ebb.py, `gas_ebb_et`): CSV payloads copy the iPost layouts (operationally-available /
by-location / NAESB location file); the points and volumes are made up (1,037 Dth = 1 MMcf)."""

from __future__ import annotations

import csv
import io
from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_ebb_et as ebb
from montney.connectors.base import Context
from montney.connectors.gas_ebb_et import CATS, EbbEnergyTransfer, Row, classify, state_regions
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 13, 18, 0)          # 12:00 CT: today + yesterday (after 11:00 CT)

OA_HEAD = ["Loc", "Loc Zn", "Loc Name", "Loc Purp Desc", "Loc/QTI", "Flow Ind", "DC", "OPC", "TSQ", "OAC", "IT",
           "Auth Overrun Ind", "Nom Cap Exceed Ind", "All Qty Avail", "Qty Reason"]
BY_HEAD = ["Loc", "Loc Name", "Loc Purp Desc", "Loc/QTI", "DC", "OPC", "TSQ", "OAC", "Loc Zn", "IT", "Flow Ind",
           "State", "County", "Operator", "OBA", "G/T", "Miles", "EGM", "Flw Cntl", "All Qty Avail", "Qty Reason"]
LOC_HEAD = ["Dir Flo", "Eff Date", "Inact Date", "Loc", "Loc Name", "Loc Cnty", "Loc St Abbrev", "Loc Stat Ind",
            "Loc Type Ind", "Loc Zone", "TSP", "TSP Name", "Up/Dn Name"]


def table(head, rows):
    f = io.StringIO()
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(head)
    for r in rows:
        w.writerow([r.get(h, "") for h in head])
    return f.getvalue()


# (loc, name, flow, Dth, state, NAESB type, counterparty)
TW = [("1", "TARGA PLANT", "R", 103_700, "NM", "OTH", "Targa Northern Delaware LLC"),
      ("2", "GILA RIVER PLANT", "D", 103_700, "AZ", "END", "SRP"),
      ("3", "SOCAL NEEDLES", "D", 207_400, "AZ", "INT", "SOUTHERN CALIFORNIA GAS COMPANY"),
      ("4", "NMG BLOOMFIELD", "D", 51_850, "NM", "INT", "NEW MEXICO GAS COMPANY"),
      ("5", "RED CEDAR", "R", 20_740, "CO", "INT", "RED CEDAR GATHERING COMPANY"),
      ("6", "TRANS PECOS I/C", "D", 31_110, "TX", "", "TRANS-PECOS PIPELINE LLC"),
      ("7", "TW POOL", "R", 999_999, "NM", "VIR", "")]
PEPL = [("MGS", "CONSUMERS ENERGY", "D", 103_700, "MI", "LDC", "Consumers Energy Company"),
        ("UNION", "UNION OJIBWAY - WAYNE COUNTY", "D", 51_850, "MI", "LDC", "Enbridge Gas Inc."),
        ("09859", "LANTIS 1-29", "R", 10_370, "MI", "WHD", "West Bay Exploration Company"),
        ("00425", "CITY OF GRUVER", "D", 0, "TX", "LDC", "City of Gruver")]

SPEC = {"id": "t_ebb_et", "type": "python", "params": {"pipes": {
    "tw": {"host": "https://tw.test", "asset": "TW", "layout": "oa", "locations": True, "receipt_default": "production",
           "emit": {"sunbelt": ["production", "power", "ldc", "int_out", "net"], "tx": ["export_mexico", "net"]},
           "points": {"3": {"cat": "int_out"}, "6": {"cat": "export_mexico", "series": "ebb.tw.tx.mexico_trans_pecos"}},
           "borders": {"az_ca": {"points": ["3"], "flow": "D"}, "co_nm": {"state_net": ["CO"]},
                       "nm_tx": {"state_net": ["TX"], "sign": -1}}},
    "pepl": {"host": "https://pepl.test", "asset": "PEPL", "layout": "by_location", "locations": True,
             "exclude": ["UNION"], "emit": {"umw": ["production", "ldc", "net"]},
             "borders": {"oh_mi": {"state_net": ["MI"], "sign": -1}}}}}}


def tw_oa(rows=TW, head=OA_HEAD):
    return table(head, [{"Loc": r[0], "Loc Name": r[1], "Flow Ind": r[2], "TSQ": f"{r[3]}"} for r in rows])


def pepl_oa(rows=PEPL):
    return table(BY_HEAD, [{"Loc": f"{r[0]:<8}", "Loc Name": r[1], "Flow Ind": r[2], "TSQ": f"{r[3]}",
                            "State": f"{r[4]:<8}", "County": "X", "Operator": r[6]} for r in rows])


def locs(rows):
    return table(LOC_HEAD, [{"Loc": r[0], "Loc Name": r[1], "Loc St Abbrev": r[4], "Loc Type Ind": r[5],
                             "Up/Dn Name": r[6]} for r in rows])


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(ebb, "PACE", 0)


def serve(seen, tw=None, pepl=None):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        host, path = req.url.host, req.url.path
        if path.endswith("/locations/index"):
            return httpx.Response(200, text=locs(TW if host == "tw.test" else PEPL))
        if host == "tw.test":
            return httpx.Response(200, text=tw if tw is not None else tw_oa())
        return httpx.Response(200, text=pepl if pepl is not None else pepl_oa())
    return handler


def run(spec=SPEC, now=NOW, seen=None, **kw):
    seen = [] if seen is None else seen
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(serve(seen, **kw))), now=now)
    return EbbEnergyTransfer(spec).run(ctx), ctx, seen


def test_sums_overrides_borders_and_requests():
    res, ctx, seen = run()
    v = {(o.series_id, o.ts): round(o.value, 3) for o in res.obs}
    d = datetime(2026, 11, 13)
    assert v[("ebb.tw.sunbelt.production", d)] == 100                # the VIR pool is dropped
    assert v[("ebb.tw.sunbelt.power", d)] == 100                     # END + SRP
    assert v[("ebb.tw.sunbelt.int_out", d)] == 200                   # Needles: override, not "LDC" by name
    assert v[("ebb.tw.sunbelt.ldc", d)] == 50                        # INT, counterparty a gas utility
    assert v[("ebb.tw.sunbelt.net", d)] == 100 - 350
    assert v[("ebb.tw.border.az_ca", d)] == 200
    assert v[("ebb.tw.border.co_nm", d)] == 20
    assert v[("ebb.tw.border.nm_tx", d)] == 30
    assert v[("ebb.tw.tx.export_mexico", d)] == v[("ebb.tw.tx.mexico_trans_pecos", d)] == 30
    assert v[("ebb.pepl.umw.ldc", d)] == 100                         # Ojibway excluded from the region sums ...
    assert v[("ebb.pepl.umw.net", d)] == 10 - 100
    assert v[("ebb.pepl.border.oh_mi", d)] == 150 - 10               # ... but it does enter Michigan
    assert {o.ts for o in res.obs} == {d, datetime(2026, 11, 12)}    # today + yesterday's final
    assert {o.as_of for o in res.obs} == {NOW}
    # 2 pipes x 2 days + 2 location files; the CSVs always name the gas day
    assert ctx.n_requests == 6
    days = sorted({r.url.params["gasDay"] for r in seen if "gasDay" in r.url.params and r.url.params["gasDay"]})
    assert days == ["11/12/2026", "11/13/2026"]
    tw_req = next(r for r in seen if r.url.host == "tw.test" and "operationally-available" in r.url.path)
    assert tw_req.url.params["searchType"] == "ALL" and tw_req.url.params["f"] == "csv"
    # next run: yesterday done, locations cached -> one request per pipe
    res2, ctx2, _ = run(now=datetime(2026, 11, 13, 20, 0))
    assert ctx2.n_requests == 2 and {o.ts for o in res2.obs} == {d}


def test_before_11_ct_only_today():
    res, ctx, _ = run(now=datetime(2026, 11, 13, 15, 0))            # 09:00 CT
    assert {o.ts for o in res.obs} == {datetime(2026, 11, 13)} and ctx.n_requests == 4


def test_missing_override_point_is_drift_for_that_pipe_day_only():
    res, _, _ = run(tw=tw_oa([r for r in TW if r[0] != "3"]))
    assert any("FormatDrift" in w and "['3']" in w for w in res.warnings)
    assert not any(o.series_id.startswith("ebb.tw.") for o in res.obs)
    assert any(o.series_id.startswith("ebb.pepl.") for o in res.obs)            # the other pipe still writes
    with pytest.raises(FormatDrift, match=r"\['3'\]"):                         # every pipe-day failing: the run fails
        run(spec={**SPEC, "params": {"pipes": {"tw": SPEC["params"]["pipes"]["tw"]}}},
            tw=tw_oa([r for r in TW if r[0] != "3"]))


def test_changed_header_or_html_is_drift():
    with pytest.raises(FormatDrift, match="header lacks"):
        run(tw=tw_oa(head=[h if h != "Flow Ind" else "Flow" for h in OA_HEAD]))
    with pytest.raises(FormatDrift, match="HTML"):
        run(pepl="<html><body>error</body></html>")


def test_header_only_csv_is_a_warning():
    res, _, _ = run(tw=table(OA_HEAD, []))
    assert not [o for o in res.obs if o.series_id.startswith("ebb.tw.")]
    assert any("not posted yet" in w for w in res.warnings)


@pytest.mark.parametrize("row, cat", [
    (Row("x", "WILLIAMS FIELD MILAGRO PLANT", "R", 1, "NM", "OTH", "HARVEST FOUR CORNERS LLC"), "production"),
    (Row("x", "VALVERDE PLANT", "R", 1, "NM", "INT", "HARVEST"), "production"),
    (Row("x", "SHERMAN (FROM ENTERPRISE)", "R", 1, "TX", "INT", "Enterprise Texas Pipeline LLC"), "int_in"),
    (Row("x", "ENSTOR KATY STORAGE", "D", 1, "TX", "INT", "Katy Storage"), "storage_inj"),
    (Row("x", "STRATTON RIDGE (TO FREEPORT LNG)", "D", 1, "TX", "LNG", "Freeport"), "lng"),
    (Row("x", "GUARDIAN GLASS", "D", 1, "MI", "END", "Guardian Industries, LLC"), "industrial"),
    (Row("x", "BEPC-PIONEER STATION", "D", 1, "ND", "END", "Basin Electric Power Cooperative"), "power"),
    (Row("x", "GRAND AVENUE DELIVERY", "D", 1, "AZ", "END", "SOUTHWEST GAS CORPORATION"), "ldc"),
    (Row("x", "EL PASO BLANCO I/C", "D", 1, "NM", "INT", "EL PASO NATURAL GAS COMPANY, L.L.C."), "int_out"),
    (Row("x", "ETC RED HILLS FUEL", "D", 1, "NM", "", "ET Gathering & Processing LLC"), "other"),
    (Row("x", "IMBALANCE -PRIOR MONTH", "R", 1, "MU", "", ""), "skip"),
])
def test_classify_rules(row, cat):
    assert classify(row, {}) == cat


def test_real_spec_series_and_overrides():
    spec = load_specs(include_disabled=True)["gas_ebb_et"]
    ids = {m.series_id for m in build(spec).declared_series()}
    for want in ("ebb.tw.border.az_ca", "ebb.tw.border.co_nm", "ebb.tw.border.nm_tx", "ebb.tw.sunbelt.production",
                 "ebb.tw.sunbelt.power", "ebb.tw.sunbelt.ldc", "ebb.tw.tx.mexico_trans_pecos", "ebb.pepl.umw.ldc",
                 "ebb.pepl.border.oh_mi", "ebb.rover.border.oh_mi", "ebb.tgc.border.in_mi", "ebb.fgt.tx.power"):
        assert want in ids
    regions = set(state_regions().values())
    for pipe, cfg in spec["params"]["pipes"].items():
        assert set(cfg["emit"]) <= regions, pipe
        assert all(c in CATS for cats in cfg["emit"].values() for c in cats), pipe
        assert all(ov.get("cat", "net") in CATS + ("skip",) for ov in (cfg.get("points") or {}).values()), pipe
        assert "UNION" not in (cfg.get("exclude") or []) or pipe == "pepl"
    assert spec["params"]["pipes"]["pepl"]["exclude"] == ["UNION"]     # Ojibway lives in gas_pepl_ojibway
    assert state_regions()["NM"] == "sunbelt" and state_regions()["MI"] == "umw" and state_regions()["ND"] == "bakken"


def test_a_row_with_gas_and_no_state_fails_the_pipe_day():
    """Reviewer on #478: a TSQ > 0 row the location file doesn't know would drop out of every total (and a group or
    state border would read 0): the pipe-day writes nothing instead. Zero-TSQ and `unlocated_ok` rows pass."""
    extra = ("99", "NEW METER", "D", 41_480, "", "", "")
    rows, _ = ebb.et_rows([{"Loc": r[0], "Loc Name": r[1], "Flow Ind": r[2], "TSQ": f"{r[3]}"} for r in TW + [extra]],
                          {r[0]: [r[4], "", r[5], r[6]] for r in TW})
    cfg = SPEC["params"]["pipes"]["tw"]
    with pytest.raises(FormatDrift, match=r"no state.*\['99'\]"):
        ebb.require_states(rows, cfg, "TW")
    ebb.require_states(rows, {**cfg, "unlocated_ok": "NEW METER"}, "TW")
    ebb.require_states([r for r in rows if r.loc != "99"] + [ebb.Row("98", "IDLE", "D", 0.0, "")], cfg, "TW")
    ebb.require_states(rows, {**cfg, "points": {**cfg["points"], "99": {"region": "sunbelt"}}}, "TW")


def test_groups_and_state_borders_need_a_matching_row():
    """Reviewer on #478: no Colorado row -> no co_nm border (not 0); no matching counterparty -> no group (not 0)."""
    cfg = {**SPEC["params"]["pipes"]["tw"],
           "groups": {"to_x": {"counterparty": "NO SUCH PIPE", "region": "sunbelt", "flow": "D"}}}
    rows = [ebb.Row("2", "GILA RIVER PLANT", "D", 103_700, "AZ", "END", "SRP"),
            ebb.Row("3", "SOCAL NEEDLES", "D", 207_400, "AZ", "INT", "SOUTHERN CALIFORNIA GAS COMPANY")]
    out = ebb.aggregate("tw", rows, cfg)
    assert "ebb.tw.border.co_nm" not in out and "ebb.tw.border.nm_tx" not in out
    assert "ebb.tw.sunbelt.to_x" not in out
    assert out["ebb.tw.border.az_ca"] == 207_400
