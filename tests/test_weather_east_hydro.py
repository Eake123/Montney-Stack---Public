"""L-091 east hydro: the parts that could put a wrong number on the map: the CEHQ fixed-width history (a missing value
must not shift columns), the vigilance CSV's UTC stamp, and Lake Winnipeg's ft operating range in metres."""

from datetime import date, datetime, timedelta

import httpx

from montney import snapshot
from montney.connectors import weather_mb_hydro as mb, weather_qc_hydro as qc
from montney.connectors.base import Context


def _epoch(d: datetime) -> float:
    return (d - datetime(1970, 1, 1)).total_seconds()


def test_fresh_reading_is_the_snapshots_current_value(tmp_path, monkeypatch):
    """Daily (freq D) hydro values are dated 00:00 of the reading's Alberta day, so the snapshot shows a fresh reading as
    its current `v`, never as a future `next`. The hard case: a reading at 03:00Z on Nov 4 is still Nov 3 in Alberta."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    now = datetime(2026, 11, 4, 3, 30)
    csv = "station,dern_valeur_deb,dern_date_prise_valeur_utc\n" + "".join(
        f"{s},500,2026/11/04 03:00:00\n" for s in qc.RIVERS)

    def history(stn):          # 12 years of 1,000 m3/s around the date -> normal 1,000, today 50 %
        rows = [f"{stn}  {d:%Y/%m/%d}  1000.0  MJ" for y in range(2010, 2022)
                for d in (date(y, 10, 25) + timedelta(days=i) for i in range(20))]
        return "\n".join(rows)

    def handler(req):
        if "vigilance" in str(req.url):
            return httpx.Response(200, content=csv.encode())
        return httpx.Response(200, content=history(req.url.path.rsplit("/", 1)[-1][:6]).encode("latin-1"))

    res = qc.QuebecHydro({"id": "weather_qc_hydro"}).fetch(
        Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now))
    o = next(o for o in res.obs if o.series_id == "qc.hydro.inflow.pct_normal")
    assert o.ts == datetime(2026, 11, 3) and o.value == 50.0
    meta = {"freq": "D"}
    cut = snapshot._cutoff(meta, _epoch(now), 0, _epoch(datetime(2026, 11, 3)))   # today's Alberta date: Nov 3
    out = snapshot.summarize({"ts": [_epoch(o.ts)], "v": [o.value]}, meta, None, _epoch(now), cut)
    assert out["v"] == 50.0 and not out.get("next")


def test_cehq_history_skips_missing_values():
    text = ("Station        Date                Debit (m3/s)   Remarque\n"
            "093801         2026/10/28          603.1          MJ\n"
            "093801         2026/10/29                         \n"
            "093801         2026/10/30          583.1          P\n")
    assert qc.parse_history(text, "093801") == {date(2026, 10, 28): 603.1, date(2026, 10, 30): 583.1}


def test_vigilance_reads_flow_and_utc_stamp():
    text = ("station,description,dern_valeur_deb,dern_valeur_niv,dern_date_prise_valeur_utc\n"
            "093801,x,544.3,29.459,2026/11/03 21:00:00\n"
            "999999,y,1.0,1.0,2026/11/03 21:00:00\n"
            "072301,z,,26.6,2026/11/03 21:00:00\n")
    assert qc.parse_vigilance(text) == {"093801": (datetime(2026, 11, 3, 21), 544.3)}


def test_lake_winnipeg_range_is_711_to_715_ft():
    assert round(mb.pct_range(711 * 0.3048), 6) == 0.0 and round(mb.pct_range(715 * 0.3048), 6) == 100.0
    assert round(mb.pct_range(217.52), 1) == 66.2          # 713.65 ft


# ---- 2026-11-13: the station layer down (WFS ExceptionReport, HTTP 200) -> CEHQ provisional daily means, never a failure
WFS_ERROR = ('<?xml version="1.0" encoding="UTF-8"?><ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows">'
             '<ows:Exception exceptionCode="NoApplicableCode" locator="mapserv"><ows:ExceptionText>'
             'msPostGISLayerGetItems(): Query error. Error executing SQL. Check server logs</ows:ExceptionText>'
             '</ows:Exception></ows:ExceptionReport>')


def _hist(stn, last: date, value=1000.0, today=500.0):
    """12 years of `value` around Nov, plus provisional days up to `last` at `today` m3/s."""
    rows = [f"{stn}  {d:%Y/%m/%d}  {value}  MJ" for y in range(2010, 2022)
            for d in (date(y, 10, 25) + timedelta(days=i) for i in range(30))]
    rows += [f"{stn}  {last - timedelta(days=i):%Y/%m/%d}  {today}  P" for i in range(5)]
    return "\n".join(rows)


def _run_down(tmp_path, monkeypatch, last: date, now=datetime(2026, 11, 13, 18, 0), calls=None):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def handler(req):
        if calls is not None:
            calls.append((req.url.host, req.headers.get("if-modified-since")))
        if "vigilance" in str(req.url):
            return httpx.Response(200, content=WFS_ERROR.encode())
        stn = req.url.path.rsplit("/", 1)[-1][:6]
        return httpx.Response(200, content=_hist(stn, last).encode("latin-1"),
                              headers={"last-modified": "Thu, 12 Nov 2026 10:54:53 GMT"})
    return qc.QuebecHydro({"id": "weather_qc_hydro"}).fetch(
        Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now))


def test_station_layer_down_uses_the_provisional_daily_means(tmp_path, monkeypatch):
    res = _run_down(tmp_path, monkeypatch, last=date(2026, 11, 11))
    o = {x.series_id: x for x in res.obs}
    assert o["qc.hydro.inflow.pct_normal"].ts == datetime(2026, 11, 11) and o["qc.hydro.inflow.pct_normal"].value == 50.0
    assert o["qc.hydro.093801.discharge"].value == 500.0
    assert any("WFS answered an error: msPostGISLayerGetItems()" in w for w in res.warnings), res.warnings
    assert any("provisional daily means of 2026-11-11" in w for w in res.warnings)


def test_seeded_red_both_down_writes_nothing_and_never_raises(tmp_path, monkeypatch):
    """Before the fix this raised every run (61 failures on kraken). Now: a warning, no rows, and the box goes stale."""
    res = _run_down(tmp_path, monkeypatch, last=date(2026, 11, 1))          # the newest daily mean is 12 days old
    assert res.obs == [] and any("nothing written" in w for w in res.warnings), res.warnings


def test_history_files_are_rechecked_with_if_modified_since(tmp_path, monkeypatch):
    calls = []
    _run_down(tmp_path, monkeypatch, last=date(2026, 11, 11), calls=calls)
    import os, time
    for f in (tmp_path / "cache").glob("cehq_*_q.txt"):                    # age the cache past the 12 h re-check
        os.utime(f, (time.time() - 13 * 3600, time.time() - 13 * 3600))
    calls.clear()
    _run_down(tmp_path, monkeypatch, last=date(2026, 11, 11), calls=calls)
    cehq = [ims for host, ims in calls if "cehq" in host]
    assert len(cehq) == 9 and all(ims == "Thu, 12 Nov 2026 10:54:53 GMT" for ims in cehq)
