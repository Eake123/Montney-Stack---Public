"""LNG Canada 30-day feed from Pacific Pilotage cargo departures (PLAN 5c item 1).

Synthetic jobs, shaped like PPA's GetCurrentVesselTraffic records; vessel names and times are made up. History is
injected through GasLngPpa.history, the hook the shipped connector reads stored points through.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import gas_lng_ppa as ppa
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

NOW = datetime(2026, 11, 1, 21, 0)              # 15:00 Alberta → the latest complete day is Oct 31
END = datetime(2026, 11, 1, 6, 0)               # end of Alberta Oct 31 in UTC (UTC-6)
START = END - timedelta(days=30)


def job(jid, frm="KLG2", to="NTS", status="Dispatched", draught=11.5, vtype="Tanker", t="2026-11-01T11:30:00Z"):
    return {"jobOrderId": jid, "vessel": {"name": f"V{jid}", "type": vtype, "imo": str(9000000 + jid), "mmsi": 1},
            "orderTime": t, "fromLocationShortCode": frm, "toLocationShortCode": to, "status": status,
            "actualDraught": draught}


def run(jobs, history=None, body=None):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body if body is not None else {"result": "Succeed", "data": jobs})
    conn = build(load_specs(include_disabled=True)["gas_lng_ppa"])
    conn.history = lambda start, end: history or {}
    return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW))


def full_history(cargo_times, skip_hours=()):
    polls = {START + timedelta(hours=h): 120.0 for h in range(30 * 24) if h not in skip_hours}
    return {ppa.POLL: polls, ppa.CARGO: {t: 1.0 for t in cargo_times}}


# ------------------------------------------------------------------ the loaded-cargo rule
@pytest.mark.parametrize("j, loaded", [
    (job(1), True),                                              # under way from the berth at 11.5 m
    (job(2, status="Order"), False),                             # only planned: its draught is a placeholder
    (job(3, to="KLG2"), False),                                  # a shift at the berth
    (job(4, frm="NTS", to="KLG2", draught=9.65), False),         # an arrival
    (job(5, draught=9.6), False),                                # a light (ballast) departure
    (job(6, vtype="Cargo", draught=6.6), False),                 # not a tanker
    (job(7, status="Completed", draught=11.9), True),
])
def test_loaded_departure_rule(j, loaded):
    assert ppa.is_loaded_departure(j) is loaded


# ------------------------------------------------------------------ the shipped path
def test_poll_writes_heartbeat_and_one_point_per_cargo_and_no_feed_without_history():
    res = run([job(1), job(2, status="Order", t="2026-11-03T10:00:00Z"), job(4, frm="NTS", to="KLG2")])
    pts = {(o.series_id, o.ts): o for o in res.obs}
    assert pts[(ppa.POLL, datetime(2026, 11, 1, 21))].value == 3.0
    cargo = pts[(ppa.CARGO, datetime(2026, 11, 1, 11, 30))]
    assert cargo.value == 1.0 and cargo.as_of == cargo.ts
    assert not any(o.series_id == ppa.FEED for o in res.obs)
    assert any(w.startswith("NO-RESULT") for w in res.warnings)


def test_feed_from_a_fully_covered_window_against_a_hand_value():
    cargoes = [START + timedelta(days=2.3 * k) for k in range(13)]
    res = run([job(1)], history=full_history(cargoes))
    feed = [o for o in res.obs if o.series_id == ppa.FEED]
    assert len(feed) == 1
    assert feed[0].ts == datetime(2026, 10, 31) and feed[0].as_of == END
    assert feed[0].value == pytest.approx(1696.3, abs=0.1)       # 13 × 3.523 Bcf / 0.90 / 30 d = 1,696.3 MMcf/d


def test_this_polls_cargo_inside_the_window_is_counted_once():
    t = datetime(2026, 10, 31, 20, 0)
    hist = full_history([t])                                     # already stored ...
    res = run([job(1, t="2026-10-31T20:00:00Z")], history=hist)  # ... and seen again now
    feed = next(o for o in res.obs if o.series_id == ppa.FEED)
    assert feed.value == pytest.approx(ppa.feed_mmcfd(1))


@pytest.mark.parametrize("skip, why", [
    (range(100, 106), "a 6 h gap can hide a whole dispatched departure"),
    (range(0, 720, 15), "48 scattered missing hours: coverage 93 % < 95 %"),
])
def test_thin_or_gappy_polling_is_no_result(skip, why):
    res = run([job(1)], history=full_history([START + timedelta(days=1)], skip_hours=set(skip)))
    assert not any(o.series_id == ppa.FEED for o in res.obs), why
    assert any(w.startswith("NO-RESULT") for w in res.warnings)


def test_a_short_gap_is_tolerated():
    res = run([job(1)], history=full_history([START + timedelta(days=1)], skip_hours={200, 201, 202}))
    assert any(o.series_id == ppa.FEED for o in res.obs)


def test_history_read_failure_is_no_result_not_a_crash():
    conn = build(load_specs(include_disabled=True)["gas_lng_ppa"])

    def boom(start, end):
        raise OSError("db down")
    conn.history = boom
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"result": "Succeed",
                                                                                           "data": [job(1)]})))
    res = conn.run(Context(client=client, now=NOW))
    assert not any(o.series_id == ppa.FEED for o in res.obs) and any("NO-RESULT" in w for w in res.warnings)


@pytest.mark.parametrize("body, needle", [
    ({"result": "Failed", "data": []}, "Succeed"),
    ({"result": "Succeed", "data": [{"jobOrderId": 1}]}, "fields missing"),
])
def test_format_drift_fails_loudly(body, needle):
    with pytest.raises(FormatDrift, match=needle):
        run([], body=body)


def test_coverage_helper():
    polls = {START + timedelta(hours=h) for h in range(720) if h not in (10, 11)}
    share, gap = ppa.coverage(polls, START, END)
    assert share == pytest.approx(718 / 720) and gap == timedelta(hours=2)
    assert ppa.coverage(set(), START, END) == (0.0, timedelta(days=30))


def test_reads_history_back_from_a_duckdb_store_read_only(tmp_path, monkeypatch):
    """DuckDB mode (the test default): the shipped read path, read-only, never via open_store() (ruling on #162)."""
    import duckdb

    from montney.store import Obs, Store
    monkeypatch.delenv("MONTNEY_DB", raising=False)
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    hist = full_history([START + timedelta(days=2.3 * k) for k in range(13)])
    st = Store(tmp_path / "montney.duckdb")
    st.write([Obs(sid, t, v, t) for sid, pts in hist.items() for t, v in pts.items()])
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

    conn = build(load_specs(include_disabled=True)["gas_lng_ppa"])        # no injected history: the shipped path
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"result": "Succeed", "data": [job(1)]})))
    res = conn.run(Context(client=client, now=NOW))
    feed = [o for o in res.obs if o.series_id == ppa.FEED]
    assert len(feed) == 1 and feed[0].value == pytest.approx(1696.3, abs=0.1)
    assert opened == [True]
