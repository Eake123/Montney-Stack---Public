"""Realized outage values (L-036, precisions in PR #71).

* Daily: `aeso.outage.<fuel>.realized` for day D = the latest vintage of `aeso.outage.<fuel>` (ts = D) with
  as_of before the end of Alberta day D (America/Edmonton). On the 2026-10/11 dates used here that is
  06:00Z on D+1; the oracle's +30 h is only valid for them. The pre-switch test shows the zone decides.
* Monthly: `aeso.outage.monthly.<fuel>.realized` for month M = the latest vintage known by the end of M
  (06:00Z on the 1st of M+1).

Synthetic fetches run through the real connector and the real DuckDB store. The oracle is recomputed from
the stored forecast-tier vintages with exactly that definition, so it is independent of the connector's
"today" logic. A vintage at exactly 06:00Z belongs to the new day and must not count for the previous one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import aeso_ets, build, load_specs
from montney.connectors.base import Context
from montney.store import Store

SPECS = load_specs(include_disabled=True)
FUELS = ("SC", "Cogen", "CC", "GFS", "Hydro", "Wind", "Solar", "Energy Storage", "Biomass and Other", "MBO")
DAILY_HEAD = ('Daily Outage Report\n\n"Last Updated:"," x"\n\n"   Date   ",'
              + ",".join(f'"   {f}   "' for f in FUELS) + ',"   Load   "\n')
MONTHLY_HEAD = "Monthly Outage\n\nMonth," + ",".join(FUELS) + "\n"


def daily_report(cc_by_day: dict[str, int]) -> str:
    return DAILY_HEAD + "".join(f'"{d}","10","20","{cc}","40","5","6","7","1","2","100","0"\n'
                                for d, cc in cc_by_day.items())


def monthly_report(cc_by_month: dict[str, int]) -> str:
    return MONTHLY_HEAD + "".join(f'"{m}","1","2","{cc}","4","5","6","7","8","9","10"\n'
                                  for m, cc in cc_by_month.items())


DAILY_FETCHES = [  # (fetch time, naive UTC; CC MW by outage day). Alberta is UTC-6 on these dates.
    (datetime(2026, 10, 31, 15, 0), {"31-Oct-2026": 300, "01-Nov-2026": 900}),   # 09:00 Oct 31
    (datetime(2026, 11, 1, 4, 0), {"31-Oct-2026": 350, "01-Nov-2026": 910}),     # 22:00 Oct 31: last of Oct 31
    (datetime(2026, 11, 1, 6, 0), {"31-Oct-2026": 777, "01-Nov-2026": 915}),     # 00:00 Nov 1: NOT Oct 31's
    (datetime(2026, 11, 1, 7, 0), {"31-Oct-2026": 999, "01-Nov-2026": 920}),     # late revision of Oct 31
    (datetime(2026, 11, 1, 20, 0), {"31-Oct-2026": 999, "01-Nov-2026": 930}),    # 14:00 Nov 1: last of Nov 1
]
MONTHLY_FETCHES = [  # the live report starts at NEXT month (checked 2026-10-31 23:04 local)
    (datetime(2026, 10, 15, 12, 0), {"Nov 2026": 60, "Dec 2026": 80}),
    (datetime(2026, 11, 1, 5, 59), {"Nov 2026": 61, "Dec 2026": 81}),   # 23:59 Oct 31: last vintage of Nov
    (datetime(2026, 11, 1, 6, 0), {"Dec 2026": 82, "Jan 2027": 90}),    # 00:00 Nov 1: Nov no longer listed
    (datetime(2026, 11, 15, 12, 0), {"Dec 2026": 85, "Jan 2027": 91}),
    (datetime(2026, 12, 1, 5, 0), {"Dec 2026": 86, "Jan 2027": 92}),    # 23:00 Nov 30: last vintage of Dec
    (datetime(2026, 12, 1, 6, 0), {"Jan 2027": 93, "Feb 2027": 95}),
    (datetime(2026, 12, 1, 6, 30), {"Nov 2026": 99, "Jan 2027": 94}),   # late roll: Nov ended; Jan still realized
    (datetime(2027, 1, 1, 6, 0), {"Feb 2027": 96, "Mar 2027": 97}),     # Dec and Jan have both ended
]


def _run(sid: str, at: datetime, body: str):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)
    return build(SPECS[sid]).run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=at))


def day_end(d: datetime) -> datetime:
    return d + timedelta(days=1, hours=6)


def month_end(m: datetime) -> datetime:
    nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt + timedelta(hours=6)


def realized_mismatches(vintages, realized: dict, end, fetched: list[datetime]) -> list[str]:
    """vintages: (ts, as_of, value); realized: ts -> value; end(ts) -> period end (exclusive).
    Every period that had a fetch inside it must have a realized value equal to the latest vintage with
    as_of < end(ts); a period with no fetch inside it must have none. Fails closed on empty input."""
    if not vintages or not fetched:
        return ["NO-RESULT: no vintages to compare"]
    expect = {}
    for ts, as_of, v in vintages:
        if as_of < end(ts) and (ts not in expect or as_of > expect[ts][0]):
            expect[ts] = (as_of, v)
    bad = []
    for ts in sorted({t for t, _, _ in vintages} | set(realized)):
        had_fetch = any(ts <= f - timedelta(hours=6) < end(ts) - timedelta(hours=6) for f in fetched)
        if had_fetch:
            want = expect[ts][1]
            if realized.get(ts) != want:
                bad.append(f"{ts:%Y-%m-%d}: realized {realized.get(ts)!r} != latest vintage before period end {want!r}")
        elif ts in realized:
            bad.append(f"{ts:%Y-%m-%d}: realized written without a fetch inside the period")
    return bad


def _store(tmp_path, sid, fetches, report, series):
    tmp_path.mkdir(parents=True, exist_ok=True)
    st = Store(tmp_path / f"{sid}.duckdb")
    for at, rows in fetches:
        res = _run(sid, at, report(rows))
        st.upsert_meta(res.meta)
        st.write(res.obs)
    vint = st._q("SELECT ts, as_of, value FROM observations WHERE series_id = ? ORDER BY as_of", [series])
    real = {ts: v for ts, _, v in st._q("SELECT ts, as_of, value FROM observations WHERE series_id = ?",
                                       [series + ".realized"])}
    st.close()
    return vint, real


def daily(tmp_path):
    return _store(tmp_path, "aeso_ets_outages", DAILY_FETCHES, daily_report, "aeso.outage.cc")


def monthly(tmp_path):
    return _store(tmp_path, "aeso_ets_outages_monthly", MONTHLY_FETCHES, monthly_report, "aeso.outage.monthly.cc")


DAILY_AT = [f for f, _ in DAILY_FETCHES]
MONTHLY_AT = [f for f, _ in MONTHLY_FETCHES]


def test_daily_realized_is_latest_vintage_before_day_end(tmp_path):
    vint, real = daily(tmp_path)
    assert len(vint) == 10                                                    # every fetch kept as a vintage
    assert real == {datetime(2026, 10, 31): 350, datetime(2026, 11, 1): 930}  # not 777 (06:00Z), not 999
    assert realized_mismatches(vint, real, day_end, DAILY_AT) == []


def ended_mismatches(vintages, realized: dict, end, now: datetime) -> list[str]:
    """Monthly check: every period that has ENDED by `now` and has a vintage before its end must have
    realized == the latest such vintage. Fails closed when nothing has ended."""
    expect = {}
    for ts, as_of, v in vintages:
        if as_of < end(ts) and (ts not in expect or as_of > expect[ts][0]):
            expect[ts] = (as_of, v)
    ended = {ts: v for ts, (_, v) in expect.items() if end(ts) <= now}
    if not ended:
        return ["NO-RESULT: no ended period to compare"]
    return [f"{ts:%Y-%m}: realized {realized.get(ts)!r} != latest vintage before month end {v!r}"
            for ts, v in sorted(ended.items()) if realized.get(ts) != v]


def test_monthly_realized_is_latest_vintage_by_month_end(tmp_path):
    vint, real = monthly(tmp_path)
    # Nov: 61 (not 60, and not the late-roll 99 after it ended); Dec: 86 (not 82); Jan: 94 (the last listing).
    assert {ts: real[ts] for ts in (datetime(2026, 11, 1), datetime(2026, 12, 1), datetime(2027, 1, 1))} == {
        datetime(2026, 11, 1): 61, datetime(2026, 12, 1): 86, datetime(2027, 1, 1): 94}
    assert ended_mismatches(vint, real, month_end, MONTHLY_AT[-1]) == []
    assert datetime(2027, 3, 1) not in real          # listed, but never the earliest month: no realized yet


def test_seeded_red_a_0600z_vintage_counted_for_the_previous_day(tmp_path, monkeypatch):
    """Break the boundary on purpose (06:00Z treated as the old day): the check must catch it."""
    monkeypatch.setattr(aeso_ets, "alberta_day", lambda now, tz: (now - timedelta(hours=6, microseconds=1)).date())
    vint, real = daily(tmp_path)
    assert real[datetime(2026, 10, 31)] == 777
    assert any(b.startswith("2026-10-31") for b in realized_mismatches(vint, real, day_end, DAILY_AT))


def test_seeded_red_monthly_boundary(tmp_path, monkeypatch):
    """A vintage AT the month end (06:00Z on the 1st) must not count for the month that just ended, and
    realizing the LAST listed month instead of the earliest is caught."""
    vint, real = monthly(tmp_path)
    late = {**real, datetime(2026, 11, 1): 82}   # pretend the 06:00Z Nov 1 fetch counted for November
    assert any(b.startswith("2026-11") for b in ended_mismatches(vint, real | late, month_end, MONTHLY_AT[-1]))
    monkeypatch.setattr(aeso_ets, "min", max, raising=False)   # the connector's "earliest" becomes "latest"
    vint2, real2 = _store(tmp_path / "red", "aeso_ets_outages_monthly", MONTHLY_FETCHES, monthly_report,
                          "aeso.outage.monthly.cc")
    assert ended_mismatches(vint2, real2, month_end, MONTHLY_AT[-1]) != []
    assert ended_mismatches([], {}, month_end, MONTHLY_AT[-1]) == ["NO-RESULT: no ended period to compare"]


def test_seeded_red_wrong_values_and_empty_input():
    vint = [(datetime(2026, 10, 31), datetime(2026, 10, 31, 15), 300.0),
            (datetime(2026, 10, 31), datetime(2026, 11, 1, 4), 350.0),
            (datetime(2026, 10, 31), datetime(2026, 11, 1, 7), 999.0)]
    at = [datetime(2026, 10, 31, 15)]
    assert realized_mismatches(vint, {datetime(2026, 10, 31): 350.0}, day_end, at) == []
    assert realized_mismatches(vint, {datetime(2026, 10, 31): 999.0}, day_end, at)   # post-day revision
    assert realized_mismatches(vint, {datetime(2026, 10, 31): 300.0}, day_end, at)   # first, not last
    assert realized_mismatches(vint, {}, day_end, at)                                # missing
    assert realized_mismatches([], {}, day_end, []) == ["NO-RESULT: no vintages to compare"]


def test_no_fetch_in_a_day_means_no_realized_row(tmp_path):
    """The connector can't know a past day's value without a fetch inside it: missing, never post-day."""
    st = Store(tmp_path / "gap.duckdb")
    for at, rows in [(datetime(2026, 11, 3, 15), {"01-Nov-2026": 1, "02-Nov-2026": 2, "03-Nov-2026": 3})]:
        res = _run("aeso_ets_outages", at, daily_report(rows))
        st.write(res.obs)
    real = {ts for (ts,) in st._q("SELECT ts FROM observations WHERE series_id = 'aeso.outage.cc.realized'")}
    st.close()
    assert real == {datetime(2026, 11, 3)}


# ---- the Alberta day comes from the zone (America/Edmonton), not a fixed offset (AGENTS.md; L-029 is SQL-only)
WINTER_2026 = datetime(2026, 1, 15, 6, 30)   # MST era (UTC-7): 23:30 on Jan 14 local, while a fixed UTC-6 says Jan 15


def test_alberta_day_uses_the_zone_pre_switch():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Edmonton")
    assert aeso_ets.alberta_day(WINTER_2026, tz).isoformat() == "2026-01-14"
    assert aeso_ets.alberta_day(datetime(2026, 11, 2, 5, 59), tz).isoformat() == "2026-11-01"   # post-switch:
    assert aeso_ets.alberta_day(datetime(2026, 11, 2, 6, 0), tz).isoformat() == "2026-11-02"    # 06:00Z day end


def _winter_realized():
    res = _run("aeso_ets_outages", WINTER_2026, daily_report({"14-Jan-2026": 14, "15-Jan-2026": 15}))
    return {o.ts: o.value for o in res.obs if o.series_id == "aeso.outage.cc.realized"}


def test_daily_realized_pre_switch_follows_the_zone():
    assert _winter_realized() == {datetime(2026, 1, 14): 14}


def test_seeded_red_fixed_offset_day_is_caught(monkeypatch):
    monkeypatch.setattr(aeso_ets, "alberta_day", lambda now, tz: (now - timedelta(hours=6)).date())
    assert _winter_realized() != {datetime(2026, 1, 14): 14}   # a hard-coded UTC-6 picks Jan 15
