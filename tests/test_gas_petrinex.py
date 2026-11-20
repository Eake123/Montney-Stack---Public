"""Petrinex AB plant sales gas (PLAN 5a): the masked-DISP filter, units, the zip-in-zip payloads, guards.

The CSVs copy Petrinex's column names and row shapes; the facilities and volumes are made up. The payloads are built here,
and no Petrinex file is committed.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_petrinex as gp
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 1, 18, 0)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """fetch() writes the top-producers facility registry (L-075): keep it out of the user's data dir."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
VOL_HDR = ("ProductionMonth,OperatorBAID,OperatorName,ReportingFacilityID,ReportingFacilityProvinceState,"
           "ReportingFacilityType,ReportingFacilityIdentifier,ReportingFacilityName,ReportingFacilitySubType,"
           "ReportingFacilitySubTypeDesc,ReportingFacilityLocation,FacilityLegalSubdivision,FacilitySection,"
           "FacilityTownship,FacilityRange,FacilityMeridian,SubmissionDate,ActivityID,ProductID,FromToID,"
           "FromToIDProvinceState,FromToIDType,FromToIDIdentifier,Volume,Energy,Hours,CCICode,ProrationProduct,"
           "ProrationFactor,Heat")
INFRA_HDR = ("FacilityID,FacilityProvinceState,FacilityType,FacilityIdentifier,FacilityName,FacilitySubType,"
             "FacilitySubTypeDesc,FacilityLocation,FacilityOperationalStatus,OperatorBAID,OperatorName")
OVINTIV_2 = "ABGP2000000,AB,GP,2000000,SYNTHETIC TWO,401,GAS PLANT,01-01-070-05W6,ACTIVE,0026,OVINTIV CANADA ULC"


def vol_row(month, fac, sub, activity, product, fromto, volume):
    return (f"{month},0026,OPERATOR,{fac},AB,GP,{fac[4:]},PLANT {fac},{sub},DESC,04-08-075-07W6,4,8,75,7,6,2026-09-21,"
            f"{activity},{product},{fromto},,,{fromto},{volume},,,,,,")


def zip_in_zip(name: str, text: str) -> bytes:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr(name, text)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr(name + ".zip", inner.getvalue())
    return outer.getvalue()


BIG = 300_000.0   # 10³m³/month: with the 30-day month, 30 such plants clear the 6,000 MMcf/d plausibility floor


def vol_csv(month="2026-09", extra=()):
    rows = [VOL_HDR,
            vol_row(month, "ABGP0001892", "405", "DISP", "GAS", "***", 153058.0),      # counted: masked sales gas
            vol_row(month, "ABGP0001892", "405", "DISP", "GAS", "ABBT0085284", 389.5), # unmasked edge: not sales
            vol_row(month, "ABGP0001892", "405", "REC", "GAS", "***", 999.0),          # a receipt, not a disposition
            vol_row(month, "ABGP0001892", "405", "DISP", "C3-MX", "***", 50.0),        # not gas
            vol_row(month, "ABGP0009999", "406", "DISP", "GAS", "***", 999999.0),      # straddle plant: excluded
            vol_row(month, "ABGP0007777", "401", "DISP", "GAS", "***", "***")]         # masked volume: skipped, warned
    rows += [vol_row(month, f"ABGP{2000000 + k:07d}", "401", "DISP", "GAS", "***", BIG) for k in range(30)]
    rows += list(extra)
    return "\n".join(rows)


INFRA = "\n".join([INFRA_HDR, "ABGP0001892,AB,GP,0001892,AEC SADDLE HILLS,405,GAS PLANT SULPHUR RECOVERY,"
                              "04-08-075-07W6,ACTIVE,0026,OVINTIV CANADA ULC", OVINTIV_2])


def run(vols: dict[str, str], params=None, today=date(2026, 11, 1)):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        url = str(req.url)
        if "/Infra/" in url:
            return httpx.Response(200, content=zip_in_zip("Facility Infrastructure-AB.CSV", INFRA))
        for month, text in vols.items():
            if f"/Vol/{month}/CSV" in url:
                return httpx.Response(200, content=zip_in_zip(f"Vol_{month}-AB.CSV", text))
        return httpx.Response(404, json={"Message": "Requested resource doesn't exist."})
    spec = load_specs(include_disabled=True)["gas_petrinex"]
    if params:
        spec = {**spec, "params": {**spec["params"], **params}}
    real, gp.local_today = gp.local_today, (lambda: today)
    try:
        return build(spec).run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)), calls
    finally:
        gp.local_today = real


def test_masked_sales_gas_filter_units_and_meta():
    res, calls = run({"2026-09": vol_csv()})
    pts = {o.series_id: o for o in res.obs}
    sh = pts["petrinex.abgp0001892.sales_gas"]
    assert sh.value == pytest.approx(180.17, abs=0.01)          # 153,058 10³m³ × 0.0353147 / 30 d (only the *** DISP GAS row)
    assert sh.ts == datetime(2026, 9, 1) and sh.as_of == datetime(2026, 11, 1)
    assert "petrinex.abgp0009999.sales_gas" not in pts           # straddle (406) excluded
    assert "petrinex.abgp0007777.sales_gas" not in pts           # masked volume
    assert any("1 sales-gas rows have a masked volume" in w for w in res.warnings)
    m = {x.series_id: x for x in res.meta}["petrinex.abgp0001892.sales_gas"]
    assert m.unit == "MMcf/d" and m.freq == "M"
    assert m.extra["name"] == "AEC SADDLE HILLS" and m.extra["operator"] == "OVINTIV CANADA ULC"
    assert any("not in the facility list" in w for w in res.warnings)   # the 30 synthetic plants have no infra row


def test_trailing_window_skips_unpublished_months():
    res, calls = run({"2026-08": vol_csv("2026-08"), "2026-09": vol_csv()})
    vols = [c for c in calls if "/Vol/" in c]
    assert [c.split("/Vol/")[1][:7] for c in vols] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    assert {o.ts for o in res.obs} == {datetime(2026, 8, 1), datetime(2026, 9, 1)}


def test_history_from_month():
    res, calls = run({"2022-01": vol_csv("2022-01")}, params={"from_month": "2022-01"})
    assert sum("/Vol/" in c for c in calls) == 59                  # 2022-01 .. 2026-11
    assert {o.ts for o in res.obs} == {datetime(2022, 1, 1)}


@pytest.mark.parametrize("vols, needle", [
    ({}, "no published Petrinex month"),
    ({"2026-09": vol_csv().replace("FromToID", "FromTo")}, "expected columns"),
    ({"2026-09": "\n".join([VOL_HDR, vol_row("2026-09", "ABGP0001892", "405", "DISP", "GAS", "***", 153058.0)])},
     "outside"),                                                   # 180 MMcf/d for all of AB: a broken filter
])
def test_guards_fail_loudly(vols, needle):
    with pytest.raises(FormatDrift, match=needle):
        run(vols)


def test_history_importer_overrides_only_from_month():
    from montney.importers.gas_petrinex import history_spec
    spec = history_spec("2022-01")
    assert spec["params"]["from_month"] == "2022-01" and spec["params"]["months_back"] == 4


def test_operator_totals_sum_the_operators_facilities_with_the_measured_band():
    res, _ = run({"2026-09": vol_csv()})
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    plant_a = pts[("petrinex.abgp0001892.sales_gas", datetime(2026, 9, 1))]
    plant_b = pts[("petrinex.abgp2000000.sales_gas", datetime(2026, 9, 1))]
    assert pts[("petrinex.operator.0026.sales_gas", datetime(2026, 9, 1))] == pytest.approx(plant_a + plant_b)
    meta = {m.series_id: m for m in res.meta}
    op = meta["petrinex.operator.0026.sales_gas"]
    assert op.extra["band"] == gp.OPERATOR_BAND == 0.23 and "last-month context" in op.label
    assert op.label.startswith("Sales gas through OVINTIV CANADA ULC-operated")       # the operator, not the gas owner
    assert op.extra["operator"] == "OVINTIV CANADA ULC"
    assert meta["petrinex.abgp0001892.sales_gas"].extra["band"] == gp.PLANT_BAND
    assert gp.SERIES_PATTERN.match("petrinex.operator.0026.sales_gas")


def test_ab_production_total_leaves_storage_out():
    storage = vol_row("2026-09", "ABGS0003410", "621", "DISP", "GAS", "***", 500_000.0)    # AEC Suffield (storage)
    res, _ = run({"2026-09": vol_csv(extra=[storage])})
    pts = {o.series_id: o.value for o in res.obs}
    plants = sum(v for s, v in pts.items() if s.startswith("petrinex.ab") and s != "petrinex.abgs0003410.sales_gas"
                 and ".sales_to." not in s)                                # the destination split re-sums the same gas
    assert pts["petrinex.abgs0003410.sales_gas"] > 0                     # still on its own facility series
    assert pts[gp.AB_TOTAL] == pytest.approx(plants)                     # but not in AB production
    assert gp.AB_TOTAL in {m.series_id for m in res.meta}


def test_operator_totals_leave_storage_out():
    global INFRA
    infra = INFRA + "\nABGS0003410,AB,GS,0003410,AEC SUFFIELD,621,GAS STORAGE,01-01-015-10W4,ACTIVE,0026,OVINTIV CANADA ULC"
    storage = vol_row("2026-09", "ABGS0003410", "621", "DISP", "GAS", "***", 500_000.0)
    real, INFRA = INFRA, infra
    try:
        res, _ = run({"2026-09": vol_csv(extra=[storage])})
    finally:
        INFRA = real
    pts = {o.series_id: o.value for o in res.obs}
    ovintiv = pts["petrinex.abgp0001892.sales_gas"] + pts["petrinex.abgp2000000.sales_gas"]
    assert pts["petrinex.operator.0026.sales_gas"] == pytest.approx(ovintiv)        # Suffield's withdrawals not added
    assert "storage withdrawals" in {m.series_id: m for m in res.meta}["petrinex.abgs0003410.sales_gas"].label


def test_ab_registry_has_names_operators_locations_and_no_storage(tmp_path):
    """L-075: the top-producers registry (<home>/facilities/ab.json), written on fetch; storage sites left out."""
    from montney import top_producers as tp
    run({"2026-09": vol_csv()})
    reg = tp.load_registry("ab", tmp_path)
    f = reg["ABGP0001892"]
    assert (f["name"], f["operator"], f["location"]) == ("AEC SADDLE HILLS", "OVINTIV CANADA ULC", "04-08-075-07W6")
    assert 55.4 < f["lat"] < 55.6 and -119.1 < f["lon"] < -118.9
    assert not set(reg) & gp.STORAGE


def test_destinations_by_nearest_meter_station(monkeypatch):
    """L-080 item 4: each facility's sales gas goes to its nearest active meter station's pipeline system; an ATCO
    station that TC counts as an NGTL receipt is atco_in_ngtl; storage stays out; no station near → unattributed."""
    from montney.connectors import petrinex_destination as pdst
    ms = [("ABMS0001001", "NOVA (MAIN DIVISION)", 55.0, -118.05), ("ABMS0007001", "ATCO PIPELINES NORTH SYSTEM", 53.0, -114.0),
          ("ABMS0007002", "ATCO PIPELINES NORTH SYSTEM", 52.0, -113.0), ("ABMS0008001", "GAS ALBERTA", 51.0, -112.0)]
    assert pdst.group_of("ATCO PIPELINES NORTH SYSTEM", "ABMS0007001", {7001}) == "atco_in_ngtl"
    assert pdst.group_of("ATCO PIPELINES NORTH SYSTEM", "ABMS0007002", {7001}) == "atco_pipelines"
    assert pdst.group_of("AEC SUFFIELD GAS PIPLINE", "ABMS0009001", set()) == "tc_mainline_suffield"
    assert pdst.group_of("CITY OF MEDICINE HAT", "ABMS0009002", set()) == "other_systems"
    near = pdst.nearest({"A": "01-01-070-01W6", "B": "01-01-001-01W4"}, ms[:1])
    assert set(near) == {"A"} and near["A"][1] == "NOVA (MAIN DIVISION)"        # B: no station within MAX_KM
    res, _ = run({"2026-09": vol_csv()})
    to = {o.series_id.split(".")[-1]: o.value for o in res.obs if ".sales_to." in o.series_id}
    assert set(to) == set(pdst.ALL_GROUPS)                                       # every group, every month
    total = next(o.value for o in res.obs if o.series_id == gp.AB_TOTAL)
    assert sum(to.values()) == pytest.approx(total)                               # the split re-sums AB production
