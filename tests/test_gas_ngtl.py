"""NGTL CSR / GDSR: specs, Alberta wall-clock timestamps (DST), and the stored-row repair.

All payloads are small synthetic samples shaped like TC's responses.
"""

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors.base import Context
from montney.importers import ngtl_dst_fix
from montney.store import Obs, Store


def ctx_for(handler) -> Context:
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)),
                   now=datetime(2026, 10, 31, 19, 30))


def by_id(res):
    out = {}
    for o in res.obs:
        out.setdefault(o.series_id, []).append(o)
    return out


def csr_row(stamp, empress=129598.0):
    return {"totalReceipts": 441976.17, "fieldReceipts": 404918.4, "groundBirchEastReceipt": 18529.3,
            "gordondaleReceipt": 1343.4, "netStorageFlow": 16944.6, "currentLinepack": 557599.6,
            "linepack4Hr": -6084.4, "totalDeliveries": 395241.6, "intraprovincialDemand": 163356.9,
            "empressBorderFlow": empress, "mcneilBorderFlow": 13195.5, "albertaBorderFlow": 71456.9,
            "willowValleyInterconnect": 3952.8, "flowDifferential": 46734.5, "linepackTarget": 585000.0,
            "lastUpdated": stamp}


def run_csr(rows):
    spec = load_specs()["ngtl_csr"]
    return build(spec).run(ctx_for(lambda r: httpx.Response(200, json={"message": "Success", "data": rows})))


# ------------------------------------------------------------------ CSR spec
def test_ngtl_csr_spec_units_and_derive():
    s = by_id(run_csr([csr_row("2026-10-31 12:29:44")]))
    assert s["ngtl.csr.linepack"][0].value == pytest.approx(19691.4, abs=0.1)
    assert s["ngtl.csr.empress"][0].value == pytest.approx(4576.7, abs=0.1)
    exp = sum(s[f"ngtl.csr.{k}"][0].value for k in ("empress", "mcneill", "alberta_bc", "willow_valley"))
    assert s["ngtl.csr.total_export"][0].value == pytest.approx(exp)


def test_ngtl_csr_daylight_and_standard_time():
    # Pinned against the live feed: 14:29:44 appeared at 20:29 UTC on 2026-10-31 (MDT, UTC-6).
    # Alberta moves to permanent UTC-6 on 2026-11-01 (tzdata 2026e; TC bulletin "Alberta Time
    # Change Effective November 1, 2026"), so the last standard-time (UTC-7) winter is 2025/26.
    s = by_id(run_csr([csr_row("2026-11-03 12:00:00"), csr_row("2026-10-31 14:29:44"),
                       csr_row("2026-01-15 12:00:00")]))
    ts = sorted(o.ts for o in s["ngtl.csr.empress"])
    assert ts == [datetime(2026, 1, 15, 19, 0, 0),      # standard time: UTC-7
                  datetime(2026, 10, 31, 20, 29, 44),   # daylight: UTC-6
                  datetime(2026, 11, 3, 18, 0, 0)]      # permanent UTC-6 from 2026-11-01
    assert all(o.as_of == o.ts for o in s["ngtl.csr.empress"])


def test_ngtl_csr_no_repeated_hour_on_2026_11_01():
    # No fall-back in 2026: 01:00-02:59 local each occur once, 30 min apart in UTC too.
    stamps = ["2026-11-01 02:29:44", "2026-11-01 02:00:50", "2026-11-01 01:29:44", "2026-11-01 01:00:56"]
    s = by_id(run_csr([csr_row(st, empress=1.0 + i) for i, st in enumerate(stamps)]))
    ts = sorted(o.ts for o in s["ngtl.csr.empress"])
    assert ts[0] == datetime(2026, 11, 1, 7, 0, 56) and ts[-1] == datetime(2026, 11, 1, 8, 29, 44)


def test_ngtl_csr_rejects_unexpected_timestamp_format():
    with pytest.raises(ValueError, match="isoformat"):  # the engine fails, not silently
        run_csr([csr_row("31/10/2026 2:29 PM")])


# ------------------------------------------------------------------ GDSR spec
def test_ngtl_gdsr_spec_offsets_abs_and_dst_as_of():
    spec = load_specs()["ngtl_gdsr"]

    def handler(req):
        d = req.url.params["date"]
        return httpx.Response(200, json={"data": {
            "gasDay": f"{d} 08:00:00", "asofDay": "2026-10-31 10:02:02.583000", "linepackChange": -5302.1,
            "volume": {"empressBorder": -130346.9, "netStorage": 5689.9, "endDayLinepack": 558826.6},
            "prorated": {"empressBorder": -128970.2},
            "eight": {"empressBorder": -128437.2},
        }})

    s = by_id(build(spec).run(ctx_for(handler)))
    flow = {o.ts: o.value for o in s["ngtl.gdsr.empress.flow"]}
    nom = {o.ts: o.value for o in s["ngtl.gdsr.empress.nom"]}
    assert all(v > 0 for v in flow.values())  # deliveries stored positive
    assert min(nom) == min(flow) + timedelta(days=1)  # 8:00 nom is for D+1
    assert s["ngtl.gdsr.net_storage.flow"][0].value > 0
    assert s["ngtl.gdsr.linepack_change"][0].value == pytest.approx(-187.24, abs=0.01)
    assert s["ngtl.gdsr.empress.flow"][0].as_of == datetime(2026, 10, 31, 16, 2, 2, 583000)  # MDT


# ------------------------------------------------------------ stored-row repair
def test_dst_fix_moves_daylight_rows_only(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    old = [
        # Stored by the old fixed-MST spec: 14:29:44 MDT landed at 21:29:44 instead of 20:29:44.
        Obs("ngtl.csr.empress", datetime(2026, 10, 31, 21, 29, 44), 1.0, datetime(2026, 10, 31, 21, 29, 44)),
        # Standard time was already right: 12:00 MST -> 19:00 UTC.
        Obs("ngtl.csr.empress", datetime(2026, 1, 15, 19, 0), 2.0, datetime(2026, 1, 15, 19, 0)),
        # GDSR as_of 10:02 MDT stored as 17:02; should be 16:02.
        Obs("ngtl.gdsr.empress.flow", datetime(2026, 10, 30), 3.0, datetime(2026, 10, 31, 17, 2, 2)),
        # CSV importer row (as_of = day + 1 at 00:00): never touched.
        Obs("ngtl.gdsr.empress.flow", datetime(2026, 7, 1), 4.0, datetime(2026, 7, 2)),
        # Another source: never touched.
        Obs("aeso.load.ail", datetime(2026, 7, 1, 12), 5.0, datetime(2026, 7, 1, 12)),
    ]
    st.write(old)
    st.con.execute("UPDATE observations SET fetched_at = TIMESTAMP '2026-10-01 00:00'")
    before = datetime(2026, 10, 15)  # "upgraded" after these rows were written

    moves = ngtl_dst_fix.plan(st, before)
    assert {(m[0], m[3], m[4]) for m in moves} == {
        ("ngtl.csr.empress", datetime(2026, 10, 31, 20, 29, 44), datetime(2026, 10, 31, 20, 29, 44)),
        ("ngtl.gdsr.empress.flow", datetime(2026, 10, 30), datetime(2026, 10, 31, 16, 2, 2)),
    }
    assert ngtl_dst_fix.apply(st, moves) == (2, 0)
    keys = {(r[0], r[1], r[3]) for r in st.con.execute(
        "SELECT series_id, ts, value, as_of FROM observations").fetchall()}
    assert ("ngtl.csr.empress", datetime(2026, 10, 31, 20, 29, 44), datetime(2026, 10, 31, 20, 29, 44)) in keys
    assert ("ngtl.csr.empress", datetime(2026, 1, 15, 19, 0), datetime(2026, 1, 15, 19, 0)) in keys
    assert len(keys) == 5
    # Idempotent: the corrected rows carry a fresh fetched_at, so a re-run finds nothing.
    assert ngtl_dst_fix.plan(st, before) == []


def test_dst_fix_drops_stale_row_when_refetched_copy_exists(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    wrong = Obs("ngtl.csr.empress", datetime(2026, 10, 31, 21, 29, 44), 1.0, datetime(2026, 10, 31, 21, 29, 44))
    st.write([wrong])
    st.con.execute("UPDATE observations SET fetched_at = TIMESTAMP '2026-10-01 00:00'")
    before = datetime(2026, 10, 15)
    moves = ngtl_dst_fix.plan(st, before)
    # The fixed connector already re-fetched this reading at the right time.
    st.write([Obs("ngtl.csr.empress", datetime(2026, 10, 31, 20, 29, 44), 1.0, datetime(2026, 10, 31, 20, 29, 44))])
    assert ngtl_dst_fix.apply(st, moves) == (0, 1)
    rows = st.con.execute("SELECT ts FROM observations").fetchall()
    assert rows == [(datetime(2026, 10, 31, 20, 29, 44),)]


@pytest.mark.parametrize("newest_first", [True, False])
def test_dst_fix_consecutive_hourly_rows_lose_nothing(tmp_path, newest_first):
    # 24 half-hourly MDT readings with the CSR's repeating seconds (:00:56 / :29:44). Under the old
    # spec each sat 1 h late, so most corrected keys collide with another stale row's current key.
    st = Store(tmp_path / "t.duckdb")
    true_ts = [datetime(2026, 10, 30, 12, 0, 56) + timedelta(minutes=30 * i) - timedelta(seconds=72 * (i % 2))
               for i in range(24)]
    rows = [Obs("ngtl.csr.empress", t + timedelta(hours=1), float(i), t + timedelta(hours=1))
            for i, t in enumerate(true_ts)]
    st.write(list(reversed(rows)) if newest_first else rows)
    gdsr = [Obs("ngtl.gdsr.empress.flow", datetime(2026, 10, d), float(d), datetime(2026, 10, d + 1, 17, 2))
            for d in range(1, 8)]
    st.write(gdsr)
    st.con.execute("UPDATE observations SET fetched_at = TIMESTAMP '2026-10-31 00:00'")
    moves = ngtl_dst_fix.plan(st, datetime(2026, 10, 31, 12))
    assert ngtl_dst_fix.apply(st, moves) == (31, 0)
    got = dict(st.con.execute(
        "SELECT ts, value FROM observations WHERE series_id = 'ngtl.csr.empress'").fetchall())
    assert got == {t: float(i) for i, t in enumerate(true_ts)}  # every reading kept, at its true time
    as_of = dict(st.con.execute(
        "SELECT ts, as_of FROM observations WHERE series_id = 'ngtl.gdsr.empress.flow'").fetchall())
    assert all(a.hour == 16 for a in as_of.values()) and len(as_of) == 7
