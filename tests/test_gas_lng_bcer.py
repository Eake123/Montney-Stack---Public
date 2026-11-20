"""LNG Canada monthly receipts (BCER) and CGL producer-direct (PLAN 5c item 2).

The CSV below copies BCER's Facility Petrinex Reporting header and row shape; the volumes are made up. Willow Valley
history is injected through GasLngBcer.willow, the hook the shipped connector reads stored GDSR points through.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_lng_bcer as bc
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 1, 18, 30)
HDR = ("Facilityid,Px Facility Id,Organizationname,Fac Name,Legal Description,Facility Type,Prod Period,"
       "Ss Pgr Patr Productid,Flaredgas,Ventedgas,Fuelgas,Deliverablevolumes,Productionvolumes,Receiptvolumes")


def row(period, receipts, fuel=1000.0, flare=300.0, product="GAS", fac="00018315", px="BCLN3165257"):
    return (f"{fac},{px},LNG Canada Development Inc.,LNG Canada,A-005-B/103-I-02,LNG Plant,{period},{product},"
            f"{flare},,{fuel},,,{receipts}")


CSV = "\n".join([HDR, row("August    2026", 31000.0), row("September 2026", 30000.0),
                 row("September 2026", "", product="OIL")])


def run(text=CSV, willow=None, ctype="text/csv; charset=utf-8", real_history=False, sunset=None):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.params.get("request") == "CSV":
            return httpx.Response(200, text=text, headers={"content-type": ctype})
        return httpx.Response(200, text="<html>apex</html>", headers={"set-cookie": "ORA_WWV_APP_200=x; Path=/ogc/"})
    conn = build(load_specs(include_disabled=True)["gas_lng_bcer"])
    if not real_history:
        conn.willow = lambda start, end: willow or {}
        conn.sunset = lambda start, end: sunset or {}
    res = conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))
    return res, calls


def days(year, month, n, value):
    return {datetime(year, month, 1) + timedelta(days=i): value for i in range(n)}


def test_receipts_units_and_the_apex_request_sequence():
    res, calls = run()
    assert [c.url.params.get("request") for c in calls] == [None, "CSV"]          # session page, then the CSV
    assert calls[1].url.params["ir_facilityid"] == "00018315"
    pts = {(o.series_id, o.ts): o for o in res.obs}
    sep = pts[(bc.RECEIPTS, datetime(2026, 9, 1))]
    assert sep.value == pytest.approx(35.3147, abs=1e-3)        # 30,000 10³m³ × 0.0353147 / 30 d
    assert pts[(bc.RECEIPTS, datetime(2026, 8, 1))].value == pytest.approx(35.3147, abs=1e-3)   # 31,000 / 31 d
    assert pts[(bc.FUEL, datetime(2026, 9, 1))].value == pytest.approx(1.1772, abs=1e-3)
    assert sep.as_of == datetime(2026, 11, 1)                   # the fetch day: a report vintage, after ts
    assert not any(o.series_id == bc.DIRECT for o in res.obs)   # no Willow Valley history → NO-RESULT
    assert any("NO-RESULT" in w for w in res.warnings)


def test_producer_direct_is_receipts_minus_the_month_mean_of_willow_valley():
    willow = {**days(2026, 9, 30, 5.0), **days(2026, 8, 31, 10.0)}
    res, _ = run(willow=willow)
    d = {o.ts: o.value for o in res.obs if o.series_id == bc.DIRECT}
    assert d[datetime(2026, 9, 1)] == pytest.approx(35.3147 - 5.0, abs=1e-3)
    assert d[datetime(2026, 8, 1)] == pytest.approx(35.3147 - 10.0, abs=1e-3)


def test_thin_willow_month_is_no_result():
    willow = {**days(2026, 9, 26, 5.0), **days(2026, 8, 31, 10.0)}     # 26/30 < 90 %
    res, _ = run(willow=willow)
    d = {o.ts for o in res.obs if o.series_id == bc.DIRECT}
    assert d == {datetime(2026, 8, 1)}
    assert any("2026-09 (26/30 d)" in w for w in res.warnings)


@pytest.mark.parametrize("text, ctype, needle", [
    (CSV.replace("Receiptvolumes", "Receipts"), "text/csv", "expected columns"),
    ("\n".join([HDR, row("September 2026", 1.0, fac="00099999")]), "text/csv", "unexpected facility"),
    (CSV, "text/html; charset=utf-8", "not CSV"),
    ("\n".join([HDR, row("September 2026", 4_000_000.0)]), "text/csv", "a unit change"),
])
def test_format_drift_fails_loudly(text, ctype, needle):
    with pytest.raises(FormatDrift, match=needle):
        run(text=text, ctype=ctype)


# ------------------------------------------------------------------ the real read-back path (orchestrator ruling on #162)
def test_reads_willow_back_from_a_duckdb_store_read_only(tmp_path, monkeypatch):
    """DuckDB mode (the test default): the shipped read path reads stored points, read-only, never via open_store()."""
    import duckdb

    from montney.store import Obs, Store
    monkeypatch.delenv("MONTNEY_DB", raising=False)
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    st = Store(tmp_path / "montney.duckdb")
    st.write([Obs(bc.WILLOW, t, v, t) for t, v in days(2026, 9, 30, 5.0).items()])
    st.close()

    opened = []
    real_connect = duckdb.connect

    def spy(path, *a, **kw):
        opened.append(kw.get("read_only"))
        return real_connect(path, *a, **kw)
    monkeypatch.setattr(duckdb, "connect", spy)

    def no_writer(*a, **kw):
        raise AssertionError("open_store() must not be used for the DuckDB read-back")
    monkeypatch.setattr("montney.store.open_store", no_writer)

    res, _ = run(real_history=True)
    d = {o.ts: o.value for o in res.obs if o.series_id == bc.DIRECT}
    assert d == {datetime(2026, 9, 1): pytest.approx(35.3147 - 5.0, abs=1e-3)}   # Aug has no Willow Valley → NO-RESULT
    assert opened and all(opened)                     # Willow Valley and Sunset Creek: both read-only


def test_no_store_file_is_no_result(tmp_path, monkeypatch):
    monkeypatch.delenv("MONTNEY_DB", raising=False)
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path / "empty"))
    res, _ = run(real_history=True)
    assert not any(o.series_id == bc.DIRECT for o in res.obs) and any("NO-RESULT" in w for w in res.warnings)


def test_daily_lng_export_is_willow_valley_plus_the_carried_direct():
    willow = {**days(2026, 9, 30, 5.0), **days(2026, 8, 31, 10.0), **days(2026, 10, 31, 7.0)}
    res, _ = run(willow=willow)
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    direct_sep = 35.3147 - 5.0                                   # Sep receipts − Sep mean Willow Valley
    assert pts[(bc.EXPORT, datetime(2026, 10, 15))] == pytest.approx(7.0 + direct_sep, abs=1e-3)
    assert pts[(bc.EXPORT_WV, datetime(2026, 10, 15))] == 7.0
    assert pts[(bc.EXPORT_DIRECT, datetime(2026, 10, 15))] == pytest.approx(direct_sep, abs=1e-3)
    assert not any(sid == bc.EXPORT and t <= datetime(2026, 9, 30) for sid, t in pts)   # only after the BCER month


def test_direct_connect_is_a_daily_regression_once_there_are_enough_months():
    """4c: direct(d) = a + b × Willow Valley(d) + c × Sunset Creek(d), fitted on the BCER months since TRAIN_FROM."""
    k = 0.0353147
    wv = {4: 5.0, 5: 10.0, 6: 15.0, 7: 20.0, 8: 25.0, 9: 30.0}
    ss = {4: 40.0, 5: 20.0, 6: 60.0, 7: 10.0, 8: 50.0, 9: 30.0}
    direct = {m: 100.0 + 2.0 * wv[m] + 0.5 * ss[m] for m in wv}
    names = {4: "April     2026", 5: "May       2026", 6: "June      2026", 7: "July      2026", 8: "August    2026",
             9: "September 2026"}
    ndays = {4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30}
    text = "\n".join([HDR] + [row(names[m], (direct[m] + wv[m]) * ndays[m] / k) for m in wv])
    willow, sunset = {}, {}
    for m in wv:
        willow.update(days(2026, m, ndays[m], wv[m])); sunset.update(days(2026, m, ndays[m], ss[m]))
    willow.update(days(2026, 10, 31, 7.0)); sunset.update(days(2026, 10, 31, 40.0))
    res, _ = run(text=text, willow=willow, sunset=sunset)
    pts = {(o.series_id, o.ts): o.value for o in res.obs}
    # Oct 31: the trailing 30 days (Oct 2-31) are all Oct, so the inputs are 7 and 40
    assert pts[(bc.EXPORT_DIRECT, datetime(2026, 10, 31))] == pytest.approx(100 + 2 * 7 + 0.5 * 40, abs=1e-2)
    assert pts[(bc.EXPORT, datetime(2026, 10, 31))] == pytest.approx(7 + 134, abs=1e-2)
    # Oct 15: the window mixes Sep 16-30 (30, 30) and Oct 1-15 (7, 40), a level, not the single day
    w30, s30 = (15 * 30 + 15 * 7) / 30, (15 * 30 + 15 * 40) / 30
    # Erik (L-066): export = WV(d) + Sunset(d) + D/C, D/C = the fit with Sunset on the measured side (coef − 1)
    dc = 100 + 2 * w30 + (0.5 - 1.0) * s30
    assert pts[(bc.EXPORT_DC, datetime(2026, 10, 15))] == pytest.approx(dc, abs=1e-2)
    assert pts[(bc.EXPORT_SUNSET, datetime(2026, 10, 15))] == 40.0
    assert pts[(bc.EXPORT, datetime(2026, 10, 15))] == pytest.approx(7 + 40 + dc, abs=1e-2)
    assert pts[(bc.EXPORT_DIRECT, datetime(2026, 10, 15))] == pytest.approx(40 + dc, abs=1e-2)
    assert {m.series_id: m for m in res.meta}[bc.EXPORT_DIRECT].extra["band"] == bc.BAND_EXPORT_DIRECT == 0.11


def test_hourly_runs_reuse_the_monthly_report_for_12_hours():
    """PLAN 9: the source runs hourly (the daily LNG export follows Willow Valley), but the BCER report is read at most
    every CSV_MAX_AGE on one client (the runner shares one); a fresh client (a test, a restart) reads it again."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.params.get("request") == "CSV":
            return httpx.Response(200, text=CSV, headers={"content-type": "text/csv"})
        return httpx.Response(200, text="<html>apex</html>")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    conn = build(load_specs(include_disabled=True)["gas_lng_bcer"])
    conn.willow = conn.sunset = lambda start, end: {}
    first = conn.run(Context(client=client, now=NOW))
    assert len(calls) == 2 and first.obs
    ctx = Context(client=client, now=NOW + timedelta(hours=11))
    again = build(load_specs(include_disabled=True)["gas_lng_bcer"])
    again.willow = again.sunset = lambda start, end: {}
    res = again.run(ctx)
    assert len(calls) == 2 and ctx.n_requests == 0                     # reused: no session page, no CSV
    assert {(o.series_id, o.ts, o.value) for o in res.obs} >= {(o.series_id, o.ts, o.value) for o in first.obs
                                                              if o.series_id == bc.RECEIPTS}
    conn.run(Context(client=client, now=NOW + timedelta(hours=12)))
    assert len(calls) == 4                                             # 12 h on: read again (session + CSV)
    conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW + timedelta(hours=12)))
    assert len(calls) == 6                                             # another client never sees the cache


def test_the_spec_runs_hourly():
    spec = load_specs(include_disabled=True)["gas_lng_bcer"]
    assert spec["interval"] == "1h" and bc.CSV_MAX_AGE == timedelta(hours=12)


def test_a_report_that_fails_to_parse_is_not_reused():
    """Reviewer on #462: a 200 the run can't use (an APEX HTML page) is dropped, so the next hourly run reads again."""
    calls, bodies = [], [("<html>error</html>", "text/html"), (CSV, "text/csv")]

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.params.get("request") == "CSV":
            text, ctype = bodies.pop(0)
            return httpx.Response(200, text=text, headers={"content-type": ctype})
        return httpx.Response(200, text="<html>apex</html>")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    conn = build(load_specs(include_disabled=True)["gas_lng_bcer"])
    conn.willow = conn.sunset = lambda start, end: {}
    with pytest.raises(FormatDrift):
        conn.fetch(Context(client=client, now=NOW))
    res = conn.run(Context(client=client, now=NOW + timedelta(hours=1)))
    assert len(calls) == 4 and any(o.series_id == bc.RECEIPTS for o in res.obs)      # read again, and it parsed
