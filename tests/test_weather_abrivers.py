"""Alberta Rivers reservoirs (montney/connectors/weather_abrivers.py): timestamps per L-034, the per-fetch clock check.

Fixtures copy the real 5-day JSON shape and values (Ghost 05BE005 and Bighorn TAU-001, fetched 2026-11-01 18:21Z:
filecreate 10:56:00 local with Last-Modified 17:56:00 GMT, i.e. the site on UTC-7). Expected hour-ends come from
power.md §A.1's table (2024 transitions, checked against AESO) and the P2 live observation.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import weather_abrivers as ab
from montney.connectors.base import Context

SPECS = load_specs(include_disabled=True)
NOW = datetime(2026, 11, 1, 18, 22)
LM = "Sun, 01 Nov 2026 17:56:00 GMT"


def doc(stn="05BE005", rows=None, filecreate="2026-11-01 10:56:00", cols=None, units=None):
    return {"station_no": stn, "station_name": "Ghost Lake Reservoir near Cochrane - TAU",
            "filecreate_timestamp": filecreate,
            "columnarray": cols or ["Timestamp", "Level", "Outflow", "Capacity", "% Full"],
            "ts_unitsymbols": units or ["m", "m³/s", "dam³", "%"],
            "data": rows if rows is not None else GHOST_ROWS}


# Ghost, 2026-10-31 22:00 .. 2026-11-01 07:00 local, as published (outflow column). The repeated 01:00 is from the
# Download feed (the 5-day JSON keeps only the first); including both exercises the fold handling.
GHOST_ROWS = [[t, 1191.12, q, 58387.9, 88.8] for t, q in [
    ("2026-10-31 22:00:00", 60.303), ("2026-10-31 23:00:00", 60.763), ("2026-11-01 00:00:00", 58.677),
    ("2026-11-01 01:00:00", 51.133), ("2026-11-01 01:00:00", 44.708), ("2026-11-01 02:00:00", 45.508),
    ("2026-11-01 03:00:00", 44.931), ("2026-11-01 07:00:00", 45.102)]]


# ---------------------------------------------------------------- the timestamp rule
@pytest.mark.parametrize("stamp,fold,want", [
    ("2026-10-31 19:00", 0, "2026-11-01 00:00"),     # P2: the "19:00" value seen at 19:41 MDT is the 17-18 MDT mean
    ("2024-11-03 00:00", 0, "2024-11-03 05:00"),     # power.md §A.1 fall: DST, -1 h
    ("2024-11-03 01:00", 0, "2024-11-03 06:00"),     # first 01:00 (DST)
    ("2024-11-03 01:00", 1, "2024-11-03 08:00"),     # second 01:00 (standard); 07:00Z has no stamp: a gap
    ("2024-11-03 02:00", 0, "2024-11-03 09:00"),
    ("2024-03-10 01:00", 0, "2024-03-10 08:00"),     # spring: standard
    ("2024-03-10 04:00", 0, "2024-03-10 09:00"),     # spring: DST, -1 h; no gap, no collision
    ("2026-11-01 07:00", 0, "2026-11-01 14:00"),     # after the switch: the site's standard time, UTC-7 hour-end
])
def test_hour_end_rule(stamp, fold, want):
    assert ab.hour_end_utc(datetime.fromisoformat(stamp), fold) == datetime.fromisoformat(want)


def test_seeded_red_without_the_dst_shift_the_rule_fails(monkeypatch):
    """Store-as-is (Weather's pre-L-034 interim rule) puts the P2 value an hour late: the known value must catch it."""
    as_is = lambda s, fold=0: s.replace(tzinfo=ab.WISKI_CLOCK, fold=fold).astimezone(ab.timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(ab, "hour_end_utc", as_is)
    assert ab.hour_end_utc(datetime(2026, 10, 31, 19)) != datetime(2026, 11, 1, 0)


def test_seeded_red_edmonton_clock_would_shift_post_switch_stamps():
    """Reading stamps on America/Edmonton (permanent UTC-6 now) instead of the site's real clock puts every post-switch
    hour-end 1 h early. That's why WISKI_CLOCK isn't America/Edmonton."""
    edm = datetime(2026, 11, 1, 7).replace(tzinfo=ab.ZoneInfo("America/Edmonton")).astimezone(ab.timezone.utc)
    assert edm.replace(tzinfo=None) != ab.hour_end_utc(datetime(2026, 11, 1, 7))


# ---------------------------------------------------------------- parsing one file
def test_parse_ghost_across_the_switch_values():
    obs, warns = ab.parse("05BE005", doc(), LM, NOW)
    assert warns == []
    q = {o.ts: o.value for o in obs if o.series_id == "abrivers.05be005.outflow"}
    assert q == {datetime(2026, 11, 1, 3): 60.303,     # 22:00 MDT, -1 h -> 21:00 MDT = 03:00Z
                 datetime(2026, 11, 1, 4): 60.763,     # 23:00
                 datetime(2026, 11, 1, 5): 58.677,     # 00:00
                 datetime(2026, 11, 1, 6): 51.133,     # first 01:00 (DST); not a copy of 00:00 this year: kept
                 datetime(2026, 11, 1, 8): 44.708,     # second 01:00 (standard)
                 datetime(2026, 11, 1, 9): 45.508,     # 02:00 MST
                 datetime(2026, 11, 1, 10): 44.931,
                 datetime(2026, 11, 1, 14): 45.102}    # 07:00 MST, the newest
    assert datetime(2026, 11, 1, 7) not in q          # the fall gap: stored as missing, never interpolated
    assert all(o.as_of == o.ts for o in obs)
    assert {o.series_id for o in obs} == {"abrivers.05be005.outflow", "abrivers.05be005.level", "abrivers.05be005.storage"}


def test_a_dst_copy_at_the_first_0100_is_dropped():
    """2024 pattern (power.md §A.1): the first 01:00 repeats 00:00. Drop it; keep the second 01:00."""
    rows = [["2024-11-03 00:00:00", 1.0, 34.21, 1.0, 1.0], ["2024-11-03 01:00:00", 1.0, 34.21, 1.0, 1.0],
            ["2024-11-03 01:00:00", 1.0, 30.0, 1.0, 1.0], ["2024-11-03 02:00:00", 1.0, 31.0, 1.0, 1.0]]
    obs, _ = ab.parse("05BE005", doc(rows=rows, filecreate="2024-11-04 10:56:00"), "Mon, 04 Nov 2024 17:56:00 GMT",
                      datetime(2024, 11, 4, 18))
    q = {o.ts: o.value for o in obs if o.series_id == "abrivers.05be005.outflow"}
    assert q == {datetime(2024, 11, 3, 5): 34.21, datetime(2024, 11, 3, 8): 30.0, datetime(2024, 11, 3, 9): 31.0}


def test_seeded_red_site_clock_fixed_to_utc6_writes_nothing():
    """If WISKI starts stamping on UTC-6 (filecreate 11:56 at 17:56Z), the rule would be 1 h off: fail closed."""
    obs, warns = ab.parse("05BE005", doc(filecreate="2026-11-01 11:56:00"), LM, NOW)
    assert obs == [] and "NO-RESULT" in warns[0] and "UTC-6.00" in warns[0]


def test_missing_clock_evidence_writes_nothing():
    obs, warns = ab.parse("05BE005", doc(filecreate=None), LM, NOW)
    assert obs == [] and "site clock" in warns[0]
    assert ab.parse("05BE005", doc(), None, NOW)[0] == []


def test_seeded_red_future_stamp_writes_nothing():
    rows = GHOST_ROWS + [["2026-11-01 12:00:00", 1191.1, 45.0, 58000.0, 88.0]]       # 19:00Z > now 18:22Z
    obs, warns = ab.parse("05BE005", doc(rows=rows), LM, NOW)
    assert obs == [] and "future" in warns[0]


def test_columns_by_name_units_and_storage():
    spray = doc("05BC006", rows=[["2026-11-01 07:00:00", 1695.74, 142979.9, 80.6]],
                cols=["Timestamp", "Level", "Capacity", "% Full"], units=["m", "dam³", "%"])
    obs, warns = ab.parse("05BC006", spray, LM, NOW)
    assert {o.series_id for o in obs} == {"abrivers.05bc006.level", "abrivers.05bc006.storage"} and warns == []
    horse = doc("TAU-004", rows=[["2026-11-01 07:00:00", 1255.02, 38.059, -53.6, None]])
    obs, _ = ab.parse("TAU-004", horse, LM, NOW)
    assert {o.series_id for o in obs} == {"abrivers.tau_004.level", "abrivers.tau_004.outflow"}   # no storage
    cfs = doc(units=["m", "ft³/s", "dam³", "%"])
    obs, warns = ab.parse("05BE005", cfs, LM, NOW)
    assert not any(o.series_id.endswith(".outflow") for o in obs)
    assert any("unit 'ft³/s'" in w for w in warns)                 # a unit change never passes silently
    assert ab.parse("05BE005", doc(stn="05BH010"), LM, NOW)[0] == []    # wrong station in the file


# ---------------------------------------------------------------- the connector
def handler(files, fail=()):
    def h(req):
        stn = req.url.path.rsplit("/", 1)[1][len("L_HG_"):-len("_table.json")]
        if stn in fail or stn not in files:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json=[files[stn]], headers={"Last-Modified": LM})
    return h


def run(files, stations, fail=()):
    conn = ab.ABRiversReservoirs({"id": "weather_abrivers", "type": "python", "params": {"stations": stations}})
    return conn.fetch(Context(client=httpx.Client(transport=httpx.MockTransport(handler(files, fail))), now=NOW))


def test_connector_one_failure_is_a_warning_all_failing_raises():
    res = run({"05BE005": doc()}, ["05BE005", "TAU-001"])
    assert any(w.startswith("TAU-001:") for w in res.warnings) and res.obs
    with pytest.raises(RuntimeError, match="no Alberta Rivers reservoir gave data"):
        run({}, ["05BE005"])


def test_shipped_spec():
    conn = build(SPECS["weather_abrivers"])
    ids = {m.series_id for m in conn.declared_series()}
    assert len(ab.STATIONS) == 10 and len(ids) == 28
    assert {"abrivers.05be005.outflow", "abrivers.tau_001.outflow", "abrivers.05dd006.storage",
            "abrivers.05bc006.level"} <= ids
    assert "abrivers.05bc006.outflow" not in ids and "abrivers.tau_004.storage" not in ids
    assert all(m.freq == "1h" and "P5 check pending" in m.extra["ts_convention"] for m in conn.declared_series())
    assert SPECS["weather_abrivers"]["retention"] == "hourly"
    with pytest.raises(ValueError, match="unknown Alberta Rivers station"):
        ab.ABRiversReservoirs({"id": "x", "params": {"stations": ["TAU-999"]}})
