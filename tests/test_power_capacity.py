"""Installed capacity by fuel (PLAN 9, region page `capacity_by_fuel`): EIA-860M for the US regions
(power_capacity_us) and StatCan 25-10-0022-01 for the Canadian ones (power_capacity_ca), on mocked payloads in the live
shapes (EIA's 860M page and workbook of 2026-11-13; StatCan WDS rows of the 2025 release, 2026-10-19)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

import httpx
import pytest

from montney import eia860m
from montney.connectors import build, load_specs
from montney.connectors import power_capacity_ca as ca
from montney.connectors import power_capacity_us as us
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift
from montney.regions import FUELS

SPECS = load_specs(include_disabled=True)
NOW = datetime(2026, 11, 13, 18)

# ------------------------------------------------------------------ EIA-860M
INDEX = """<div class="basic-table"><table><tbody>
<!--<tr><td>October 2026</td>
<td>
<a href="/electricity/data/eia860m/xls/october_generator2026.xlsx" title="EIA 860M October 2026">XLS</a></td></tr>-->
<tr><td>September 2026</td>
<td>
<a href="/electricity/data/eia860m/xls/september_generator2026.xlsx" title="EIA 860M September 2026">XLS</a></td></tr>
<tr><td>August 2026</td>
<td>
<a href="/electricity/data/eia860m/archive/xls/august_generator2026.xlsx" title="EIA 860M August 2026">XLS</a></td></tr>
<tr><td>July 2026</td>
<td>
<a href="/electricity/data/eia860m/archive/xls/july_generator2026.xlsx" title="EIA 860M July 2026">XLS</a></td></tr>
</tbody></table></div>"""
HEADER = ["Entity ID", "Entity Name", "Plant ID", "Plant Name", "Google Map", "Bing Map", "Plant State", "County",
          "Balancing Authority Code", "Sector", "Generator ID", "Unit Code", "Nameplate Capacity (MW)",
          "Net Summer Capacity (MW)", "Net Winter Capacity (MW)", "Technology", "Energy Source Code",
          "Prime Mover Code", "Operating Month", "Operating Year", "Planned Retirement Month",
          "Planned Retirement Year", "Status", "Nameplate Energy Capacity (MWh)", "DC Net Capacity (MW)",
          "Planned Derate Year", "Planned Derate Month", "Planned Derate of Summer Capacity (MW)",
          "Planned Uprate Year", "Planned Uprate Month", "Planned Uprate of Summer Capacity (MW)",
          "Planned Repower Year", "Planned Repower Month", "Other Modification Year", "Other Modification Month",
          "Latitude", "Longitude"]
OP, SB = "(OP) Operating", "(SB) Standby/Backup: available for service but not normally used"
OS = "(OS) Out of service and NOT expected to return to service in next calendar year"
# one big plant per region, so every region total is in range (state, lat, lon)
SITES = {"cal": ("CA", 34.05, -118.25), "pnw": ("WA", 47.6, -122.3), "bakken": ("ND", 47.0, -101.0),
         "mtnw": ("CO", 39.7, -105.0), "sunbelt": ("AZ", 33.4, -112.0), "tx": ("TX", 31.0, -97.0),
         "nyc": ("NY", 42.6, -75.8), "upne": ("MA", 42.4, -71.1), "umw": ("MN", 45.0, -93.3)}


def gen(pid, state, tech, status=OP, nameplate="100", summer="90", lat="34.05", lon="-118.25"):
    r = dict.fromkeys(HEADER, " ")
    r.update({"Entity ID": "1", "Entity Name": "Utility", "Plant ID": str(pid), "Plant Name": f"Plant {pid}",
              "Plant State": state, "Sector": "Electric Utility", "Generator ID": "1", "Technology": tech,
              "Status": status, "Nameplate Capacity (MW)": nameplate, "Net Summer Capacity (MW)": summer,
              "Latitude": lat, "Longitude": lon})
    return r


def base_rows():
    rows = [gen(100 + i, st, "Natural Gas Fired Combined Cycle", nameplate="1600", summer="1500", lat=str(la),
                lon=str(lo)) for i, (st, la, lo) in enumerate(SITES.values())]
    return rows + [
        gen(1, "CA", "Solar Photovoltaic", nameplate="250", summer="240"),
        gen(2, "CA", "Batteries", SB, nameplate="100", summer="100"),                     # standby: counted
        gen(3, "CA", "Conventional Steam Coal", OS, nameplate="500", summer="480"),      # out of service: not
        gen(4, "CA", "Onshore Wind Turbine", nameplate="50", summer=" "),                 # no net summer: nameplate
        gen(5, "NY", "", nameplate="60.5", summer="75.7", lat="40.61", lon="-73.76"),     # blank Technology: other
        gen(6, "OH", "Nuclear", nameplate="900", summer="890", lat="41.6", lon="-83.1"),  # Ohio: no region
        gen(7, "MA", "Offshore Wind Turbine", nameplate="132", summer="130", lat="41.0", lon="-70.4"),  # offshore
    ]


def xlsx(rows, month="September 2026", sheet="Operating"):
    """A workbook in the 860M layout: title row, blank row, header row, generators, notes; text as shared strings."""
    strings: list[str] = []

    def s(text):
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    def col(i):
        out = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            out = chr(65 + r) + out
        return out

    header = list(rows[0]) if rows else HEADER
    grid = [[f"Inventory of Operating Generators as of {month}"], [""], header]
    grid += [[r[h] for h in header] for r in rows]
    notes = ("NOTES:\nCapacity from facilities with a total generator nameplate capacity less than 1 MW are "
             "excluded from this report.")
    grid += [[""], [notes]]
    xml_rows = []
    for n, cells in enumerate(grid, 1):
        cs = "".join(f'<c r="{col(i)}{n}" t="s"><v>{s(v)}</v></c>' for i, v in enumerate(cells))
        xml_rows.append(f'<row r="{n}">{cs}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rns}><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/>'
                                      f'<sheet name="Planned" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        body = "".join(xml_rows)
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")
        z.writestr("xl/worksheets/sheet2.xml", f"<worksheet {ns}><sheetData/></worksheet>")
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{escape(t)}</t></si>" for t in strings)
                   + "</sst>")
    return buf.getvalue()


def test_index_skips_commented_months_and_resolves_archive_links():
    got = eia860m.index(INDEX)
    assert sorted(got) == [datetime(2026, 7, 1), datetime(2026, 8, 1), datetime(2026, 9, 1)]    # no October
    assert got[datetime(2026, 9, 1)] == "https://www.eia.gov/electricity/data/eia860m/xls/september_generator2026.xlsx"
    assert got[datetime(2026, 8, 1)].endswith("/eia860m/archive/xls/august_generator2026.xlsx")


def test_reader_header_title_and_rows():
    content = xlsx(base_rows())
    assert eia860m.as_of(content) == datetime(2026, 9, 1)
    rows = list(eia860m.operating(content))
    assert len(rows) == len(base_rows()) and rows[0]["Plant ID"] == "100"
    assert rows[-1]["Technology"] == "Offshore Wind Turbine"
    with pytest.raises(eia860m.Eia860mError, match="no 'Operating' sheet"):
        list(eia860m.operating(xlsx(base_rows(), sheet="Generators")))
    with pytest.raises(eia860m.Eia860mError, match="readable xlsx"):
        list(eia860m.operating(b"<html>not found</html>"))


@pytest.fixture(scope="module")
def region_fn():
    return us.plant_region_fn()


def test_capacity_counts_operating_and_standby_at_net_summer(region_fn):
    cap, stats = us.capacity(xlsx(base_rows()), region_fn)
    assert set(cap) == set(us.REGIONS) and all(set(f) == set(FUELS) for f in cap.values())
    assert cap["cal"]["solar"] == 240 and cap["cal"]["battery"] == 100            # net summer; standby counted
    assert cap["cal"]["coal"] == 0                                                 # (OS) left out
    assert cap["cal"]["wind"] == 50 and stats["nameplate_fallback"] == 1           # blank net summer: nameplate
    assert cap["nyc"]["other"] == 75.7 and stats["blank_technology"] == 1          # blank Technology: other
    assert cap["upne"]["wind"] == 130                                              # offshore: its state's region
    assert stats["outside"] == 1 and all(c["nuclear"] == 0 for c in cap.values())  # Ohio: no region
    assert cap["tx"]["gas"] == 1500 and sum(cap["cal"].values()) == 1500 + 240 + 100 + 50


def test_capacity_fails_closed(region_fn):
    with pytest.raises(FormatDrift, match="unmapped Technology 'Fusion'"):
        us.capacity(xlsx(base_rows() + [gen(8, "TX", "Fusion")]), region_fn)
    with pytest.raises(FormatDrift, match="isn't a number"):
        us.capacity(xlsx(base_rows() + [gen(8, "TX", "Nuclear", summer="n/a")]), region_fn)
    with pytest.raises(FormatDrift, match="negative"):
        us.capacity(xlsx(base_rows() + [gen(8, "TX", "Nuclear", summer="-5")]), region_fn)
    with pytest.raises(FormatDrift, match="bakken total"):                       # a region with no plants at all
        us.capacity(xlsx([r for r in base_rows() if r["Plant State"] != "ND"]), region_fn)
    rows = base_rows()
    for r in rows:
        del r["Net Summer Capacity (MW)"]
    with pytest.raises(FormatDrift, match="missing columns"):
        us.capacity(xlsx(rows), region_fn)
    blank = [gen(20 + i, "TX", "", summer="500") for i in range(5)]
    with pytest.raises(FormatDrift, match="blank Technology"):
        us.capacity(xlsx(base_rows() + blank), region_fn)


def us_run(tmp_path, monkeypatch, books, spec_params=None, log=None):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def handler(req):
        if log is not None:
            log.append(str(req.url))
        if str(req.url) == eia860m.INDEX_URL:
            return httpx.Response(200, text=INDEX)
        name = str(req.url).rsplit("/", 1)[1]
        return httpx.Response(200, content=books[name]) if name in books else httpx.Response(404)
    spec = {**SPECS["power_capacity_us"], "params": {**SPECS["power_capacity_us"]["params"], **(spec_params or {})}}
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    return build(spec).run(c)


BOOKS = {"september_generator2026.xlsx": xlsx(base_rows()),
         "august_generator2026.xlsx": xlsx(base_rows()[:-1], "August 2026"),
         "july_generator2026.xlsx": xlsx(base_rows()[:-2], "July 2026")}


def test_fetch_newest_first_backfill_then_cache(tmp_path, monkeypatch):
    log: list[str] = []
    res = us_run(tmp_path, monkeypatch, BOOKS, log=log)                 # max_downloads 2: September, August
    assert [u.rsplit("/", 1)[1] for u in log] == ["", "september_generator2026.xlsx", "august_generator2026.xlsx"]
    assert any("1 older months still to download" in w for w in res.warnings)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    sep, aug = datetime(2026, 9, 1), datetime(2026, 8, 1)
    assert v[("cap.upne.wind", sep)] == 130 and v[("cap.upne.wind", aug)] == 0        # August lacks the offshore unit
    assert v[("cap.cal.total", sep)] == sum(v[(f"cap.cal.{f}", sep)] for f in FUELS) == 1890
    assert all(o.as_of == o.ts for o in res.obs) and {o.ts for o in res.obs} == {sep, aug}
    assert len(res.obs) == 2 * len(us.REGIONS) * (len(FUELS) + 1)

    log.clear()                                                          # next day: July, then nothing new
    res = us_run(tmp_path, monkeypatch, BOOKS, log=log)
    assert [u.rsplit("/", 1)[1] for u in log] == ["", "july_generator2026.xlsx"]
    assert {o.ts for o in res.obs} == {sep, aug, datetime(2026, 7, 1)}
    assert not any("back-fill" in w for w in res.warnings)
    log.clear()
    res = us_run(tmp_path, monkeypatch, BOOKS, log=log)
    assert log == [eia860m.INDEX_URL] and len({o.ts for o in res.obs}) == 3        # the index only; cache re-written


def test_fetch_window_drops_old_months(tmp_path, monkeypatch):
    us_run(tmp_path, monkeypatch, BOOKS, {"max_downloads": 3})
    res = us_run(tmp_path, monkeypatch, BOOKS, {"backfill_months": 1})
    assert {o.ts for o in res.obs} == {datetime(2026, 9, 1)}


def test_fetch_fails_closed_on_wrong_month_or_not_xlsx(tmp_path, monkeypatch):
    with pytest.raises(FormatDrift, match="the workbook says 2026-08"):
        us_run(tmp_path, monkeypatch, {**BOOKS, "september_generator2026.xlsx": BOOKS["august_generator2026.xlsx"]})
    with pytest.raises(FormatDrift, match="not an xlsx"):
        us_run(tmp_path, monkeypatch, {**BOOKS, "september_generator2026.xlsx": b"<html>maintenance</html>"})
    assert not (tmp_path / "cache" / "eia860m_capacity.json").exists()         # nothing cached from a failed run
    with pytest.raises(FormatDrift, match="2026-08: not an xlsx"):              # a failed back-fill month...
        us_run(tmp_path, monkeypatch, {**BOOKS, "august_generator2026.xlsx": b"<html>maintenance</html>"})
    log: list[str] = []
    res = us_run(tmp_path, monkeypatch, BOOKS, log=log)                        # ...keeps the newest month
    assert [u.rsplit("/", 1)[1] for u in log] == ["", "august_generator2026.xlsx", "july_generator2026.xlsx"]
    assert len({o.ts for o in res.obs}) == 3


def test_us_declares_every_region_and_fuel():
    declared = {m.series_id: m for m in build(SPECS["power_capacity_us"]).declared_series()}
    assert set(declared) == {f"cap.{r}.{f}" for r in us.REGIONS for f in (*FUELS, "total")}
    m = declared["cap.tx.gas"]
    assert m.unit == "MW" and m.freq == "M" and m.extra["region"] == "tx" and "net summer" in m.label


# ------------------------------------------------------------------ StatCan 25-10-0022-01
# 2025 values (kW) from the live WDS answer of 2026-11-13; one province per region is enough to test the sums.
WDS_2025 = {
    10: {1: 21926635, 2: 914000, 3: 5684000, 5: 2348428, 6: 12958207, 7: 6856400, 9: 5710059, 10: 391748,
         11: 16000, 12: 6000},
    11: {1: 18717252, 2: 16377578, 3: 745400, 5: 54918, 6: 1528856, 7: 1135040, 9: 348400, 10: 45416, 11: 10500},
    9: {1: 5781900, 2: 862600, 3: 812360, 5: 110800, 6: 3976140, 7: 2235380, 9: 1726300, 10: 14460, 11: 20000},
    8: {1: 6775914, 2: 6163480, 3: 258450, 5: 40149, 6: 313835, 7: 22000, 9: 280000, 10: 11835},
    7: {1: 39672458, 2: 8919451, 3: 5527000, 5: 2886490, 6: 22339517, 7: 5348412, 8: 11247000, 9: 5610918,
        10: 133187, 11: 0},
    6: {1: 46281214, 2: 40737559, 3: 4035063, 5: 16374, 6: 1492218, 7: 523020, 8: None, 9: 778800, 10: 190398},
}


def wds(data=WDS_2025, year="2025-01-01"):
    out = []
    for g, _ in ca.PROVINCES.values():
        for t in ca.TYPES.values():
            coord = f"{g}.1.{t}.0.0.0.0.0.0.0"
            if t not in data[g]:
                out.append({"status": "FAILED", "object": {"responseStatusCode": 2, "productId": 25100022,
                                                           "coordinate": coord, "vectorId": 0, "vectorDataPoint": []}})
                continue
            v = data[g][t]
            out.append({"status": "SUCCESS", "object": {
                "responseStatusCode": 0, "productId": 25100022, "coordinate": coord, "vectorId": 53517905,
                "vectorDataPoint": [{"refPer": year, "value": None if v is None else float(v), "decimals": 0,
                                     "scalarFactorCode": 0, "symbolCode": 0, "statusCode": 1 if v is None else 0,
                                     "releaseTime": "2026-10-19T08:30", "frequencyCode": 12}]}})
    return out


def ca_run(payload, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(json.loads(req.content))
        return httpx.Response(200, json=payload)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    return build(SPECS["power_capacity_ca"]).run(c)


def test_statcan_provinces_regions_and_types():
    seen: list = []
    res = ca_run(wds(), seen)
    assert len(seen) == 1 and len(seen[0]) == 72 and seen[0][0]["latestN"] == 2026 - 2015 + 1
    y = datetime(2025, 1, 1)
    v = {o.series_id: o.value for o in res.obs if o.ts == y}
    assert v["cap.ab.total"] == pytest.approx(21926.635) and v["cap.ab.wind"] == 5684.0     # kW -> MW
    assert v["cap.ab.nuclear"] == 0.0                                                  # no vector: none installed
    assert v["cap.ab.combustion"] == pytest.approx(6856.4 + 5710.059 + 391.748)
    assert v["cap.ab.other"] == 16.0 and v["cap.ab.geothermal"] == 6.0
    assert v["cap.on.nuclear"] == 11247.0 and "cap.qc.nuclear" not in v                # suppressed: left out
    assert v["cap.wcan.total"] == pytest.approx(21926.635 + 18717.252 + 5781.9)
    assert v["cap.sont.hydro"] == v["cap.on.hydro"] and v["cap.mbnwo.total"] == v["cap.mb.total"]
    assert v["cap.qc.hydro"] == pytest.approx(40737.559)
    for p in ca.PROVINCES:                                                             # identity: parts = total
        parts = sum(v.get(f"cap.{p}.{k}", 0) for k in ("hydro", "wind", "solar", "geothermal", "nuclear", "other",
                                                         "combustion"))
        assert parts == pytest.approx(v[f"cap.{p}.total"], abs=0.01), p
    assert all(o.as_of == o.ts for o in res.obs) and not res.warnings


def test_statcan_region_sum_needs_every_member():
    data = {g: dict(t) for g, t in WDS_2025.items()}
    data[11][11] = None                                    # BC "other" suppressed (as in 2023)
    v = {o.series_id: o.value for o in ca_run(wds(data)).obs}
    assert "cap.bc.other" not in v and "cap.wcan.other" not in v and v["cap.wcan.hydro"] > 0


def test_statcan_fails_closed():
    data = {g: dict(t) for g, t in WDS_2025.items()}
    del data[10][3]                                        # AB wind vanishes: AB parts no longer add up
    with pytest.raises(FormatDrift, match="ab 2025: total parts add to"):
        ca_run(wds(data))
    data = {g: {t: (v / 1000 if v else v) for t, v in ts.items()} for g, ts in WDS_2025.items()}   # MW, not kW
    with pytest.raises(FormatDrift, match="out of range"):
        ca_run(wds(data))
    bad = wds()
    bad[0]["status"] = "ERROR"
    with pytest.raises(FormatDrift, match="ERROR"):
        ca_run(bad)
    bad = wds()
    bad[0]["object"]["vectorDataPoint"][0]["scalarFactorCode"] = 3
    with pytest.raises(FormatDrift, match="scalar factor"):
        ca_run(bad)
    with pytest.raises(FormatDrift, match="no data points"):
        ca_run([x for x in wds() if x["status"] == "FAILED"])


def test_statcan_older_year_mismatch_is_a_warning():
    old = {g: dict(t) for g, t in WDS_2025.items()}
    old[10][3] = 1000000                                   # 2024 AB wind off: an older year only warns
    payload = wds(old, "2024-01-01")
    new = wds()
    for a, b in zip(payload, new):
        if a["status"] == "SUCCESS" and b["status"] == "SUCCESS":
            a["object"]["vectorDataPoint"] += b["object"]["vectorDataPoint"]
    res = ca_run(payload)
    assert any("ab 2024" in w for w in res.warnings)
    assert {o.ts for o in res.obs if o.series_id == "cap.ab.total"} == {datetime(2024, 1, 1), datetime(2025, 1, 1)}


def test_ca_declares_provinces_and_regions():
    declared = {m.series_id: m for m in build(SPECS["power_capacity_ca"]).declared_series()}
    want = {f"cap.{p}.{k}" for p in (*ca.PROVINCES, *ca.REGIONS) for k in ca.STORED}
    assert set(declared) == want
    assert declared["cap.ab.total"].extra["region"] == "wcan" and declared["cap.on.wind"].extra["region"] == "sont"
    assert declared["cap.qc.hydro"].freq == "A" and declared["cap.mbnwo.total"].extra["region"] == "mbnwo"
    assert {k for k in ca.STORED if k in FUELS} == {"hydro", "wind", "solar", "geothermal", "nuclear", "other"}


def test_specs_contract():
    for sid, region, tier in (("power_capacity_us", "rest", "daily"), ("power_capacity_ca", "ab_nebc", "daily")):
        s = SPECS[sid]
        assert s["region"] == region and s["retention"] == tier and s["mode"] == "cached" and s.get("stale_after")
