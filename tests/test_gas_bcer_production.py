"""BC production from the BCER facility report (PLAN 4b item 4): the plant/battery identity, units and guards.

Rows copy the BCER CSV's header; facilities and volumes are made up.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_bcer_production as bp
from montney.connectors.base import Context
from montney.connectors.gas_common import E3M3_TO_MMCF as K, FormatDrift


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """fetch() writes the top-producers facility registry (L-075): keep it out of the user's data dir."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

HDR = ("Facilityid,Px Facility Id,Organizationname,Fac Name,Legal Description,Facility Type,Prod Period,"
       "Ss Pgr Patr Productid,Flaredgas,Ventedgas,Fuelgas,Deliverablevolumes,Productionvolumes,Receiptvolumes")


def row(typ, period, deliv, rec, product="GAS"):
    return f"00000001,BCGP0000001,Op,Fac,01-01-080-15W6,{typ},{period},{product},,,1.0,{deliv},,{rec}"


def csv_text(scale=20.0):        # ×20: ~5,900 MMcf/d, inside the 4,000-14,000 guard
    return "\n".join([HDR,
                      row("Gas Processing Plant", "September 2026", 250_000 * scale, 265_000 * scale),
                      row("Gas Processing Plant", "September 2026", 10_000 * scale, 0),      # its feed: another plant
                      row("Battery", "September 2026", 262_000 * scale, 7_000 * scale),
                      row("Battery", "September 2026", 999, 0, product="OIL"),               # not gas
                      row("LNG Plant", "September 2026", 0, 50_000),                         # takes pipeline gas: out
                      row("Gas Processing Plant", "August    2026", 240_000 * scale, 250_000 * scale),
                      row("Battery", "August    2026", 255_000 * scale, 5_000 * scale)])


def run(text):
    def handler(req: httpx.Request) -> httpx.Response:
        if "request=CSV" in str(req.url):
            return httpx.Response(200, text=text, headers={"content-type": "text/csv; charset=utf-8"})
        return httpx.Response(200, text="<html/>")
    conn = build(load_specs(include_disabled=True)["gas_bcer_production"])
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 1, 18)))


def test_identity_plant_net_plus_battery_net():
    res = run(csv_text())
    pts = {o.ts: o.value for o in res.obs if o.series_id == bp.SERIES}
    # Sep: (250,000 + 10,000 − 265,000) + (262,000 − 7,000) = 250,000 10³m³ → ÷ 30 d
    assert pts[datetime(2026, 9, 1)] == pytest.approx(20 * 250_000 * K / 30)
    assert pts[datetime(2026, 8, 1)] == pytest.approx(20 * (240_000 - 250_000 + 255_000 - 5_000) * K / 31)
    assert {o.series_id for o in res.obs} == {bp.SERIES, bp.plant_series_id("BCGP0000001")}
    assert res.obs[0].as_of == datetime(2026, 11, 1)


def test_guards():
    with pytest.raises(FormatDrift, match="outside"):
        run(csv_text(scale=20_000))                                # 10³m³ read as m³, or a filter break
    with pytest.raises(FormatDrift, match="expected columns"):
        run(csv_text().replace("Deliverablevolumes", "Deliveries"))


def test_plant_outlets_and_the_bc_registry(tmp_path):
    """L-075: each gas plant's deliveries, MMcf/d, and its name/operator/location in <home>/facilities/bc.json."""
    from montney import top_producers as tp
    res = run(csv_text())
    sid = bp.plant_series_id("BCGP0000001")
    pts = {o.ts: o.value for o in res.obs if o.series_id == sid}
    # the fixture's two Sep plant rows share one id: 250,000 + 10,000 delivered (×20)
    assert pts[datetime(2026, 9, 1)] == pytest.approx(20 * 260_000 * K / 30)
    assert pts[datetime(2026, 8, 1)] == pytest.approx(20 * 240_000 * K / 31)
    assert sid in {m.series_id for m in res.meta}
    reg = tp.load_registry("bc", tmp_path)
    assert set(reg) == {"BCGP0000001"}
    f = reg["BCGP0000001"]
    assert (f["name"], f["operator"], f["location"]) == ("Fac", "Op", "01-01-080-15W6")
    assert 55.85 < f["lat"] < 55.95 and -120.25 < f["lon"] < -120.1


def test_no_plant_columns_still_gives_bc_production():
    res = run(csv_text().replace("Legal Description", "Legal"))
    assert {o.series_id for o in res.obs} == {bp.SERIES}
    assert any("no plant series" in w for w in res.warnings)


def test_aitken_storage_withdrawals_are_not_production():
    """L-079: Aitken Creek's storage-well battery (BCBT0000160) feeds its plant. Without it the plant's outlet gas from
    storage cancels against its receipts, so a winter withdrawal no longer counts as BC production."""
    storage = ("00000160,BCBT0000160,Aitken Creek Gas Storage,STORAGE,D-044-L/094-A-13,Battery,September 2026,GAS,,,,"
               "4000000,4000000,")
    plant = ("00000559,BCGP0000559,Aitken Creek Gas Storage,AITKENCREEK,D-044-L/094-A-13,Gas Processing Plant,"
             "September 2026,GAS,,,,4000000,,4000000")
    base = {o.ts: o.value for o in run(csv_text()).obs if o.series_id == bp.SERIES}
    with_storage = {o.ts: o.value for o in run(csv_text() + "\n" + storage + "\n" + plant).obs
                    if o.series_id == bp.SERIES}
    assert with_storage[datetime(2026, 9, 1)] == pytest.approx(base[datetime(2026, 9, 1)])


# ---- L-102: the Prod Period filter (the last WINDOW_MONTHS calendar months) and its unfiltered fallback
def test_window_labels_pad_like_oracle_month():
    labels = bp.window_labels(datetime(2026, 11, 4).date(), 30)
    assert len(labels) == 30 and labels[-1] == "November  2026" and labels[0] == "June      2024"
    assert "May       2026" in labels and "September 2026" in labels       # padded to 9, as the report's text


def test_filtered_request_then_unfiltered_fallback():
    seen = []
    months = [datetime(2026, 9, 1).replace(month=9 - i) if i < 9 else datetime(2025, 21 - i, 1) for i in range(12)]
    rows = [row("Gas Processing Plant", f"{m:%B} {m:%Y}".replace(f"{m:%B} ", f"{m:%B}".ljust(9) + " "), 250_000 * 20,
                10_000 * 20) for m in months]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("irin_prod_period") or req.url.params.get("request") or "plain")
        if req.url.params.get("request") == "CSV":
            return httpx.Response(200, text="\n".join([HDR, *rows]), headers={"content-type": "text/csv"})
        return httpx.Response(200, text="<html/>")
    conn = build(load_specs(include_disabled=True)["gas_bcer_production"])
    res = conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 1, 18)))
    assert seen[0].split(",")[-1] == bp.window_labels(bp.local_today(), bp.WINDOW_MONTHS)[-1]   # the IN filter
    # 12 months < PLANT_MONTHS: re-read without the filter, so nothing stored is lost
    assert seen[1:] == ["CSV", "plain", "CSV"] and any("unfiltered report" in w for w in res.warnings)


# ---- #394 review: APEX keeps an IR filter in the server session and the runner shares one client across sources
class FakeApex:
    """A stateful stand-in for BCER's APEX report: a session cookie per new visitor; ir_facilityid / irin_prod_period
    set that session's filter (sticky, like APEX); request=CSV returns the rows the session's filter lets through."""

    def __init__(self, rows):
        self.rows, self.sessions, self.csv_filters = rows, {}, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        sid = req.headers.get("cookie", "").partition("ORA_WWV_APP_101=")[2].split(";")[0]
        headers = {}
        if sid not in self.sessions:
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = {}
            headers["set-cookie"] = f"ORA_WWV_APP_101={sid}; Path=/ogc/"
        flt = self.sessions[sid]
        for k in ("ir_facilityid", "irin_prod_period"):
            if k in req.url.params:
                flt[k] = req.url.params[k]
        if req.url.params.get("request") != "CSV":
            return httpx.Response(200, text="<html>apex</html>", headers=headers)
        self.csv_filters.append(dict(flt))
        keep = [r for r in self.rows
                if ("ir_facilityid" not in flt or r.split(",")[0] == flt["ir_facilityid"])
                and ("irin_prod_period" not in flt or r.split(",")[6] in flt["irin_prod_period"].split(","))]
        return httpx.Response(200, text="\n".join([HDR, *keep]), headers={**headers, "content-type": "text/csv"})


def apex_rows(n_months):
    labels = bp.window_labels(bp.local_today(), n_months + 2)[:-2]          # the newest two months not reported yet
    plant = [row("Gas Processing Plant", m, 250_000 * 20, 10_000 * 20) for m in labels]
    lng = [f"00018315,BCLN3165257,LNG Canada,LNG Canada,A-005-B/103-I-02,LNG Plant,{m},GAS,300,,1000,,,30000"
           for m in labels[-2:]]
    return plant + lng


def test_lng_filter_does_not_leak_into_production(monkeypatch):
    from montney.connectors import gas_lng_bcer as lng
    server = FakeApex(apex_rows(28))
    client = httpx.Client(transport=httpx.MockTransport(server))            # ONE client, as in the runner
    lng_conn = build(load_specs(include_disabled=True)["gas_lng_bcer"])
    lng_conn.willow = lambda start, end: {}
    lng_conn.sunset = lambda start, end: {}
    lng_conn.run(Context(client=client, now=datetime(2026, 11, 1, 18)))
    assert server.csv_filters[-1] == {"ir_facilityid": lng.FACILITY}
    prod = build(load_specs(include_disabled=True)["gas_bcer_production"])
    res = prod.run(Context(client=client, now=datetime(2026, 11, 1, 18)))
    assert "ir_facilityid" not in server.csv_filters[-1]                  # a fresh session: lng's filter is gone
    assert len([o for o in res.obs if o.series_id == bp.SERIES]) == bp.MONTHS_BACK
    assert not any("unfiltered" in w for w in res.warnings)


def test_seeded_red_without_dropping_cookies_the_filter_leaks(monkeypatch):
    """The 11-04 17:51 failure: keep the cookies and production reads lng's one-facility CSV."""
    from montney.connectors import gas_lng_bcer as lng
    monkeypatch.setattr(bp, "drop_cookies", lambda ctx, host: 0)
    monkeypatch.setattr(lng, "drop_cookies", lambda ctx, host: 0)
    server = FakeApex(apex_rows(28))
    client = httpx.Client(transport=httpx.MockTransport(server))
    lng_conn = build(load_specs(include_disabled=True)["gas_lng_bcer"])
    lng_conn.willow = lambda start, end: {}
    lng_conn.sunset = lambda start, end: {}
    lng_conn.run(Context(client=client, now=datetime(2026, 11, 1, 18)))
    with pytest.raises(FormatDrift, match="no month with gas plants"):
        build(load_specs(include_disabled=True)["gas_bcer_production"]).run(
            Context(client=client, now=datetime(2026, 11, 1, 18)))


def test_fallback_is_really_unfiltered():
    server = FakeApex(apex_rows(12))                     # 12 months < PLANT_MONTHS: the window read is short
    res = build(load_specs(include_disabled=True)["gas_bcer_production"]).run(
        Context(client=httpx.Client(transport=httpx.MockTransport(server)), now=datetime(2026, 11, 1, 18)))
    assert [set(f) for f in server.csv_filters] == [{"irin_prod_period"}, set()]   # the re-read has no filter
    assert any("unfiltered report" in w for w in res.warnings)
