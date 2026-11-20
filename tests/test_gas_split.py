"""NGTL production by area / segment (PLAN 5a): rd parsing, field shares, the k rescale and the daily estimate.

Synthetic workbooks shaped like TC's (station utilization, System Receipts & Deliveries); the numbers are made up.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta

import httpx
import pytest

from montney import paths
from montney.connectors import build, load_specs
from montney.connectors import ngtl_split as ns
from montney.connectors import ngtl_station_util as su
from montney.connectors.base import Context
from montney.connectors.gas_common import E3M3_TO_MMCF as K, FormatDrift
from test_gas_station_util import _cell, _ref, era_c_rows, make_xlsx, page_html

NOW = datetime(2026, 11, 1, 18, 0)
RD_HDR = ["Gas Day", "Measured Receipts Volume (103M3)", "Storage Receipts Volume (103M3)",
          "Gordondale Receipts Volume (103M3)", "Groundbirch East Receipts Volume (103M3)"]


def serial(d: date) -> float:
    return float((d - date(1899, 12, 30)).days)


def make_book(sheets: dict[str, list[list]]) -> bytes:
    ns_ = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns_} {rel_ns}><sheets>' + "".join(
            f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(sheets)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
                       f'<Relationship Id="rId{i + 1}" Type="worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                       for i in range(len(sheets))) + "</Relationships>")
        for i, rows in enumerate(sheets.values()):
            body = "".join(f'<row r="{r + 1}">' + "".join(_cell(_ref(r, c), v) for c, v in enumerate(row)) + "</row>"
                           for r, row in enumerate(rows))
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", f"<worksheet {ns_}><sheetData>{body}</sheetData></worksheet>")
    return buf.getvalue()


def rd_sheet(month: date, field_mmcfd: float) -> list[list]:
    """Daily rows whose measured − storage − Gordondale − Groundbirch = field_mmcfd."""
    rows = [["NGTL System Summary of Receipts and Deliveries"], [], RD_HDR]
    d = month
    while d.month == month.month:
        rows.append([serial(d), field_mmcfd / K + 1300.0, 1000.0, 200.0, 100.0])
        d += timedelta(days=1)
    return rows


def station_rows():
    rows = era_c_rows()                                      # 5106 GRDL + 60 WAEX (USJR) + 5242 nothing
    rest = [7001, "SOUTH STATION", None, "BLEG", None] + [100, 200000, 0] * 9
    border = [3886, "GORDONDALE BORDER", None, "GRDL", None] + [100, 999999, 0] * 9    # non-field: excluded
    return rows[:-2] + [rest, border] + rows[-2:]


def run(gdsr: dict[datetime, float], rd_book: bytes | None = None, stored: dict | None = None, client=None, now=NOW,
        calls: list | None = None):
    rd_book = rd_book or make_book({"Sep": rd_sheet(date(2026, 9, 1), 102.6)})

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if calls is not None:
            calls.append(url)
        if url == su.PAGE:
            return httpx.Response(200, text=page_html(years=(2026, 2025)))
        if "2026-09" in url:
            return httpx.Response(200, content=make_xlsx(station_rows()))
        if "12-2025" in url:
            return httpx.Response(200, content=make_xlsx(era_c_rows(2025, 12, as_of="As of: 1/20/2026 2:55:41 PM")))
        if "2026-NGTL-System-Receipts" in url:
            return httpx.Response(200, content=rd_book)
        return httpx.Response(404)
    conn = build(load_specs(include_disabled=True)["ngtl_split"])
    conn.gdsr = lambda start, end: gdsr
    conn.stored = lambda ids, start, end: {i: v for i, v in (stored or {}).items() if i in ids}
    real, ns.local_today = ns.local_today, (lambda: date(2026, 11, 1))
    real_su, su.local_today = su.local_today, (lambda: date(2026, 11, 1))
    try:
        if client is not None:
            client._transport = httpx.MockTransport(handler)          # one client across runs, as in the runner
        return conn.run(Context(client=client or httpx.Client(transport=httpx.MockTransport(handler)), now=now))
    finally:
        ns.local_today, su.local_today = real, real_su


def gdsr_days(first: date, last: date, v: float = 100.0) -> dict[datetime, float]:
    out, d = {}, first
    while d <= last:
        out[datetime(d.year, d.month, d.day)] = v
        d += timedelta(days=1)
    return out


def test_rd_rows_and_header_guard():
    rows = rd_sheet(date(2026, 9, 1), 50.0)
    got = ns._rd_rows(rows)
    assert len(got) == 30 and ns.rd_field(got[date(2026, 9, 1)]) * K == pytest.approx(50.0)
    bad = [r[:] for r in rows]
    bad[2] = ["Gas Day", "Measured", "Storage Receipts", "Gordondale Receipts", "Groundbirch East Receipts"]
    with pytest.raises(FormatDrift, match="Measured Receipts"):
        ns._rd_rows(bad)


def test_estimate_is_gdsr_times_last_months_share():
    """L-088 (Erik): on TC's GDSR basis, so USJR + outside USJR = GDSR field receipts exactly; k stays for the measured
    total and the remainders."""
    res = run(gdsr_days(date(2026, 9, 1), date(2026, 10, 5)))
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    # Sep shares (field stations only; 3886 is a border): USJR = 6,000 + 60 × 100,000; rest = 200,000 (10³m³/month)
    share_rest = 200_000 / (6_000 + 6_000_000 + 200_000)
    t = datetime(2026, 10, 3)
    assert pts[("ngtl.area.rest.receipt_est", t)] == pytest.approx(100.0 * share_rest, rel=1e-6)
    assert pts[("ngtl.area.usjr.receipt_est", t)] == pytest.approx(100.0 * (1 - share_rest), rel=1e-6)
    assert pts[("ngtl.area.usjr.receipt_est", t)] + pts[("ngtl.area.rest.receipt_est", t)] == pytest.approx(100.0)
    assert pts[("ngtl.segment.bleg.receipt_est", t)] == pytest.approx(100.0 * share_rest, rel=1e-6)
    assert not any(t.month == 9 and sid.endswith("_est") for sid, t in pts)      # rd has measured values for Sep
    assert pts[("ngtl.area.rest.receipt", datetime(2026, 9, 1))] == pytest.approx(su.to_mmcfd(200_000, date(2026, 9, 1)))
    assert pts[("ngtl.rd.measured_receipts", datetime(2026, 9, 15))] == pytest.approx(102.6 + 1300.0 * K)
    assert any("k = 1.0260" in w for w in res.warnings)


def test_thin_gdsr_in_the_rd_month_is_no_result():
    res = run(gdsr_days(date(2026, 9, 5), date(2026, 10, 5)))        # 26 of 30 Sep days < 90 %
    assert not any(o.series_id.endswith("_est") for o in res.obs)
    assert any(w.startswith("NO-RESULT") for w in res.warnings)


def test_bands_follow_the_p2_back_test():
    meta = {m.series_id: m for m in build(load_specs(include_disabled=True)["ngtl_split"]).declared_series()}
    assert meta["ngtl.area.usjr.receipt_est"].extra["band"] == 0.007
    assert meta["ngtl.area.rest.receipt_est"].extra["band"] == 0.06
    assert meta["ngtl.segment.gpml.receipt_est"].extra["band"] == 0.038
    assert meta["ngtl.segment.redl.receipt_est"].extra["band"] == 0.25


def test_station_estimate_is_todays_gdsr_times_its_share():
    res = run(gdsr_days(date(2026, 9, 1), date(2026, 10, 5)))
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    share_5106 = 6_000 / (6_000 + 6_000_000 + 200_000)        # Sep field receipts, 10³m³ (3886 border excluded)
    assert pts[("ngtl.station.5106.receipt_est", datetime(2026, 10, 5))] == pytest.approx(100.0 * share_5106)
    days = {t for sid, t in pts if sid == "ngtl.station.5106.receipt_est"}
    assert days == {datetime(2026, 10, 3), datetime(2026, 10, 4), datetime(2026, 10, 5)}   # the latest 3 gas days
    assert "ngtl.station.3886.receipt_est" not in {sid for sid, _ in pts}                    # border, not a field station


def test_province_totals_are_ngtl_today_plus_the_regulator_months_remainder():
    gd = {**gdsr_days(date(2026, 9, 1), date(2026, 9, 30)), **gdsr_days(date(2026, 10, 1), date(2026, 10, 5), 110.0)}
    sep = datetime(2026, 9, 1)
    border = gdsr_days(date(2026, 9, 1), date(2026, 10, 5), 5.0)
    oct3 = datetime(2026, 10, 3)
    res = run(gd, stored={"production.ab.monthly": {sep: 150.0}, "production.bc.monthly": {sep: 60.0},
                          "cgl.producer_direct": {sep: 20.0}, ns.GD: border, ns.GBE: border,
                          "ngtl.gdsr.total_export.flow": {oct3: 70.0}, "alliance.receipts.total": {oct3: 15.0},
                          "lng.export.direct": {oct3: 20.0}, "alliance.zone1.receipts": {oct3: 15.0}})
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    k, bc = 1.026, 6_000 / (6_000 + 6_000_000 + 200_000)        # station 5106 is the one BC field station
    ab = 1 - bc
    assert pts[("ngtl.province.bc.receipt_est", oct3)] == pytest.approx(110 * bc)
    # AB: NGTL AB today + (Petrinex Sep − NGTL AB Sep); NGTL in Sep = GDSR Sep mean × Sep share (GDSR basis, L-088)
    assert pts[("production.ab.daily_est", oct3)] == pytest.approx(110 * ab + 150 - 100 * ab)
    assert pts[("production.ab.other_est", oct3)] == pytest.approx(150 - 100 * ab)
    # the remainder absorbs the basis: its part (k − 1) × Sep's NGTL field receipts is written for the tie-out
    assert pts[("production.ab.other_ngtl_basis_est", oct3)] == pytest.approx((k - 1) * 100 * ab)
    # vs the old measured basis the day's total moves only by (k − 1) × (GDSR today − GDSR Sep) × share
    old_ab = 110 * k * ab + 150 - 100 * k * ab
    assert pts[("production.ab.daily_est", oct3)] - old_ab == pytest.approx(-(k - 1) * (110 - 100) * ab)
    # BC: NGTL BC today + Gordondale + Groundbirch East (measured) + (BCER Sep − the same parts in Sep)
    r_bc = 60 - (100 * bc + 10)
    assert pts[("production.bc.daily_est", oct3)] == pytest.approx(110 * bc + 10 + r_bc)
    assert pts[("production.bc.cgl_direct_est", oct3)] == 20.0
    assert pts[("production.bc.other_est", oct3)] == pytest.approx(r_bc - 20)
    assert pts[("export.total.flow_est", oct3)] == pytest.approx(105.0)


def test_province_totals_without_a_regulator_month_are_no_result():
    res = run(gdsr_days(date(2026, 9, 1), date(2026, 10, 5)))
    assert not any(o.series_id.startswith("production.") for o in res.obs)
    assert any("NO-RESULT production.ab" in w for w in res.warnings)


def test_province_totals_are_a_visible_sum_with_alliance_daily():
    gd = {**gdsr_days(date(2026, 9, 1), date(2026, 9, 30)), **gdsr_days(date(2026, 10, 1), date(2026, 10, 5), 110.0)}
    sep, oct3 = datetime(2026, 9, 1), datetime(2026, 10, 3)
    border = gdsr_days(date(2026, 9, 1), date(2026, 10, 5), 5.0)
    z1 = {**gdsr_days(date(2026, 9, 1), date(2026, 9, 30), 30.0), oct3: 33.0}
    z2 = {**gdsr_days(date(2026, 9, 1), date(2026, 9, 30), 8.0), oct3: 9.0}
    res = run(gd, stored={"production.ab.monthly": {sep: 150.0}, "production.bc.monthly": {sep: 60.0},
                          "cgl.producer_direct": {sep: 20.0}, ns.GD: border, ns.GBE: border,
                          "alliance.zone1.receipts": z1, "alliance.zone2.receipts": z2})
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    k, bc = 1.026, 6_000 / (6_000 + 6_000_000 + 200_000)
    ab = 1 - bc
    r_ab = 150 - 100 * ab - 8                           # Petrinex Sep − NGTL AB Sep − Zone 2 Sep mean
    assert pts[("production.ab.other_est", oct3)] == pytest.approx(r_ab)
    assert pts[("production.ab.alliance_est", oct3)] == 9.0
    assert pts[("production.ab.daily_est", oct3)] == pytest.approx(110 * ab + 9 + r_ab)
    r_bc = 60 - (100 * bc + 10) - 30 - 20               # BCER Sep − NGTL BC − borders − Zone 1 − CGL (all Sep)
    assert pts[("production.bc.other_est", oct3)] == pytest.approx(r_bc)
    assert pts[("production.bc.daily_est", oct3)] == pytest.approx(110 * bc + 10 + 33 + 20 + r_bc)
    assert pts[("production.bc.alliance_est", oct3)] == 33.0
    # step 5: AB + BC, and the NGTL part (field receipts by province + the T-North borders)
    assert pts[("production.total.daily_est", oct3)] == pytest.approx(pts[("production.ab.daily_est", oct3)]
                                                                      + pts[("production.bc.daily_est", oct3)])
    assert pts[("production.total.ngtl_est", oct3)] == pytest.approx(110 + 10)
    # L-073 nested rows: NGTL field receipts (AB + BC) is the NGTL row less the T-North borders; L-088: = GDSR's figure
    assert pts[("production.total.ngtl_field_est", oct3)] == pytest.approx(110)
    assert pts[("production.total.ngtl_field_measured_est", oct3)] == pytest.approx(110 * k)     # TC's measured basis


def test_bc_adds_tnorth_south_net_of_aitken_and_takes_it_out_of_the_remainder():
    """6B: BC = … + (Station 2 South − Aitken storage est.) + (R − the same in the regulator month)."""
    gd = {**gdsr_days(date(2026, 9, 1), date(2026, 9, 30)), **gdsr_days(date(2026, 10, 1), date(2026, 10, 5), 110.0)}
    sep, oct3 = datetime(2026, 9, 1), datetime(2026, 10, 3)
    border = gdsr_days(date(2026, 9, 1), date(2026, 10, 5), 5.0)
    s2s = {**gdsr_days(date(2026, 9, 1), date(2026, 9, 30), 40.0), oct3: 45.0}
    base = {"production.ab.monthly": {sep: 150.0}, "production.bc.monthly": {sep: 60.0},
            "cgl.producer_direct": {sep: 20.0}, ns.GD: border, ns.GBE: border}
    res_old = run(gd, stored=base)
    res_new = run(gd, stored={**base, ns.WC_S2S: s2s, ns.AITKEN_NET: {sep: -3.0}, ns.WC_AITKEN_EST: {oct3: 2.0}})
    old = {(o.series_id, o.ts): o.value for o in res_old.obs}
    new = {(o.series_id, o.ts): o.value for o in res_new.obs}
    ts_sep, ts_today = 40.0 - (-3.0), 45.0 - 2.0
    assert new[("production.bc.tnorth_south_est", oct3)] == pytest.approx(ts_today)
    assert new[("production.bc.other_est", oct3)] == pytest.approx(old[("production.bc.other_est", oct3)] - ts_sep)
    assert new[("production.bc.daily_est", oct3)] == pytest.approx(old[("production.bc.daily_est", oct3)] + ts_today - ts_sep)


def test_gas_leaving_the_production_area_is_ngtl_borders_alliance_and_tsouth():
    """L-071: NGTL total export − Willow Valley + Alliance + Westcoast Station 2 South; no estimated term."""
    from montney.connectors.base import Context, FetchResult
    conn = build(load_specs(include_disabled=True)["ngtl_split"])
    t = datetime(2026, 11, 1)
    stored = {"ngtl.gdsr.total_export.flow": {t: 7700.0}, "ngtl.gdsr.willow_valley.flow": {t: 70.0},
              "alliance.receipts.total": {t: 1500.0}, "westcoast.oa.station2_south.scheduled": {t: 1640.0}}
    conn.stored = lambda ids, lo, hi: {i: v for i, v in stored.items() if i in ids}
    res = FetchResult()
    conn._pipeline_export(Context(client=None, now=datetime(2026, 11, 2, 18)), res, datetime(2026, 11, 2))
    pts = {o.series_id: o.value for o in res.obs}
    assert pts[ns.PIPE_NGTL] == pytest.approx(7630.0) and pts[ns.PIPE_TSOUTH] == 1640.0
    assert pts[ns.PIPE_TOTAL] == pytest.approx(7630 + 1500 + 1640)


def test_bc_everything_else_band_is_absolute():
    """Reviewer on #298: R_BC sits near 0 (the NW-AB part of Zone 1), so a relative band would collapse (at −50,
    ±58 against a real ±300). BC's "everything else" carries an absolute ±300 MMcf/d; AB's stays relative."""
    meta = {m.series_id: m for m in build(load_specs(include_disabled=True)["ngtl_split"]).declared_series()}
    bc, ab = meta["production.bc.other_est"], meta["production.ab.other_est"]
    assert "band" not in bc.extra and bc.extra["band_mmcfd"] == 300.0 and "±300 MMcf/d" in bc.label
    assert ab.extra["band"] == 0.30                     # L-088: 401 ÷ 1,360 (R_AB holds the NGTL basis)
    rows = [r for f in json.loads((paths.REPO_ROOT / "network/gas/nodes.geojson").read_text(encoding="utf-8"))["features"]
            for r in f["properties"].get("callouts_extra", []) if r.get("series") == "production.bc.other_est"]
    assert rows and all("band" not in r and "±300" in r["label"] for r in rows)


def test_hourly_runs_follow_the_gdsr_without_re_downloading_tc_files():
    """PLAN 9: hourly runs on the runner's one client download TC's page and workbooks at most every FILE_MAX_AGE; a
    gas day the GDSR adds in between still gets its estimate on the next run."""
    client, calls = httpx.Client(trust_env=False), []
    first = run(gdsr_days(date(2026, 9, 1), date(2026, 10, 30)), client=client, calls=calls)
    n = len(calls)
    assert n >= 4 and calls.count(su.PAGE) == 1
    later = run(gdsr_days(date(2026, 9, 1), date(2026, 10, 31)), client=client, calls=calls, now=NOW + timedelta(hours=1))
    assert len(calls) == n                                               # nothing downloaded again
    day = datetime(2026, 10, 31)
    assert not any(o.ts == day for o in first.obs if o.series_id.endswith("_est"))
    assert any(o.ts == day for o in later.obs if o.series_id.endswith("receipt_est"))
    run(gdsr_days(date(2026, 9, 1), date(2026, 10, 31)), client=client, calls=calls, now=NOW + timedelta(hours=12))
    assert len(calls) == 2 * n                                           # 12 h on: one fresh download of each


def test_the_split_spec_runs_hourly():
    assert load_specs(include_disabled=True)["ngtl_split"]["interval"] == "1h"
    assert ns.FILE_MAX_AGE == timedelta(hours=12)
