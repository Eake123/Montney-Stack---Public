"""NRC reactor power status (PLAN 9): mocked PowerReactorStatusForLast365Days.txt in the live shape (2026-11-13)."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_nrc as nrc
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
# Real rows (2026-11-12/13), newest day first, as NRC serves them (CRLF).
BODY = ("ReportDt|Unit|Power\r\n"
        "11/13/2026 12:00:00 AM|Diablo Canyon 1|100\r\n"
        "11/13/2026 12:00:00 AM|Palo Verde 1|0\r\n"
        "11/13/2026 12:00:00 AM|Point Beach 2|49\r\n"
        "11/13/2026 12:00:00 AM|Vogtle 3|100\r\n"
        "11/12/2026 12:00:00 AM|Diablo Canyon 1|100\r\n"
        "11/12/2026 12:00:00 AM|Palo Verde 1|0\r\n"
        "11/12/2026 12:00:00 AM|Point Beach 2|52\r\n"
        "11/12/2026 12:00:00 AM|Vogtle 3|100\r\n")


def run(text, now=datetime(2026, 11, 13, 16), source="", status=200, headers=None, seen=None):
    seen = seen if seen is not None else []

    def handler(r):
        seen.append(r)
        return httpx.Response(status, text=text, headers=headers or {})

    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now, source=source)
    return build(SPECS["power_nrc"]).run(c), c


def test_rows_to_daily_series_and_mw_estimate():
    res, _ = run(BODY)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    d13, d12 = datetime(2026, 11, 13), datetime(2026, 11, 12)                 # report date at 00:00, naive
    assert v[("nrc.diablo_canyon_1.power_pct", d13)] == 100
    assert v[("nrc.diablo_canyon_1.mw_est", d13)] == 1122.0                   # EIA-860M net summer
    assert v[("nrc.point_beach_2.power_pct", d12)] == 52
    assert v[("nrc.point_beach_2.mw_est", d13)] == round(0.49 * 598.2, 1)
    assert v[("nrc.palo_verde_1.mw_est", d13)] == 0                           # an outage is a real 0
    assert all(o.as_of == o.ts and o.ts.hour == 0 for o in res.obs)
    assert len(res.obs) == 16
    assert "Diablo Canyon 2" in res.warnings[0]                               # units absent from the newest day


def test_meta_regions_and_units():
    metas = {m.series_id: m for m in build(SPECS["power_nrc"]).declared_series()}
    assert len(metas) == 2 * len(nrc.UNITS) + 2 * len(nrc.PLANTS) and len(nrc.UNITS) == 95
    assert metas["nrc.diablo_canyon_1.mw_est"].extra["region"] == "cal"
    assert metas["nrc.fermi_2.power_pct"].extra["region"] == "umw"             # EIA's MI, not NRC's "of Toledo, OH"
    assert "region" not in metas["nrc.vogtle_3.mw_est"].extra                 # Georgia: outside every region
    assert all(m.freq == "D" and m.extra["tier"] == "daily" for m in metas.values())
    assert metas["nrc.seabrook_1.power_pct"].unit == "%" and metas["nrc.seabrook_1.mw_est"].unit == "MW"
    assert len({u.slug for u in nrc.UNITS.values()}) == len(nrc.UNITS) == 95


def test_region_detail_units():
    """The units behind each region page's nuclear_units group (a moved outline or a new unit shows up here)."""
    by: dict[str, set[str]] = {}
    for n in nrc.UNITS:
        if (r := nrc.region(n)):
            by.setdefault(r, set()).add(nrc.UNITS[n].slug)
    assert by == {
        "cal": {"diablo_canyon_1", "diablo_canyon_2"},
        "pnw": {"columbia"},
        "sunbelt": {"palo_verde_1", "palo_verde_2", "palo_verde_3"},
        "tx": {"comanche_peak_1", "comanche_peak_2", "south_texas_1", "south_texas_2"},
        "nyc": {"fitzpatrick", "ginna", "nine_mile_point_1", "nine_mile_point_2"},
        "upne": {"millstone_2", "millstone_3", "seabrook_1"},
        "umw": {"dc_cook_1", "dc_cook_2", "fermi_2", "monticello", "palisades", "point_beach_1", "point_beach_2",
                "prairie_island_1", "prairie_island_2"},
    }


@pytest.mark.parametrize("bad, match", [
    (BODY.replace("ReportDt|Unit|Power", "ReportDate|Unit|Power"), "header"),
    ("", "header"),
    (BODY.replace("Vogtle 3|100\r\n11/12", "Vogtle 5|100\r\n11/12"), "unknown unit 'Vogtle 5'"),
    (BODY.replace("Diablo Canyon 1|100", "Diablo Canyon 1|106", 1), "outside 0..105"),
    (BODY.replace("Point Beach 2|49", "Point Beach 2|-3"), "isn't an integer"),
    (BODY.replace("Point Beach 2|49", "Point Beach 2|"), "isn't an integer"),
    (BODY.replace("11/13/2026 12:00:00 AM|Vogtle", "2026-11-13|Vogtle"), "doesn't parse"),
    (BODY.replace("11/13/2026 12:00:00 AM|Vogtle", "11/13/2026 06:00:00 AM|Vogtle"), "isn't at 12:00:00 AM"),
    (BODY.replace("|Vogtle 3|100\r\n11/12", "|Vogtle 3|100|x\r\n11/12"), "isn't date|unit|power"),
    (BODY + "11/13/2026 12:00:00 AM|Diablo Canyon 1|90\r\n", "two values"),
    ("ReportDt|Unit|Power\r\n", "no rows"),
])
def test_fails_closed(bad, match):
    with pytest.raises(FormatDrift, match=match):
        run(bad)


def test_capacity_table_plausible():
    for u in nrc.UNITS.values():
        assert 400 <= u.mw <= 1500, u
        assert len(u.state) == 2 and len(u.docket) == 8 and u.slug == u.slug.lower()
        assert -125 < u.lon < -67 and 24 < u.lat < 49, u


def test_date_gate_one_download_per_day(monkeypatch):
    monkeypatch.setattr(nrc, "_newest", {})
    seen: list = []
    first, _ = run(BODY, now=datetime(2026, 11, 13, 16), source="t_nrc_gate", seen=seen)   # 11:00 EST
    assert len(seen) == 1 and len(first.obs) == 16                            # first run: everything
    again, _ = run(BODY, now=datetime(2026, 11, 13, 17), source="t_nrc_gate", seen=seen)
    early, _ = run(BODY, now=datetime(2026, 11, 14, 13, 59), source="t_nrc_gate", seen=seen)  # 08:59 EST
    assert len(seen) == 1 and again.obs == [] == early.obs                    # nothing newer can exist: no request
    assert {m.series_id for m in again.meta} == {m.series_id for m in first.meta}
    # Next day after 09:00 ET: download; later downloads re-emit only the last `recent_days`.
    rows = ["ReportDt|Unit|Power"] + [f"{(datetime(2026, 11, 14) - timedelta(days=i)):%m/%d/%Y} 12:00:00 AM|Ginna|100"
                                      for i in range(30)]
    later, _ = run("\r\n".join(rows), now=datetime(2026, 11, 14, 14, 30), source="t_nrc_gate", seen=seen)
    assert len(seen) == 2
    days = sorted({o.ts for o in later.obs})
    assert len(days) == 14 and days[-1] == datetime(2026, 11, 14)
    # The file hasn't moved on yet: keep polling (one request per run) until the day's rows appear.
    run("\r\n".join(rows), now=datetime(2026, 11, 15, 15), source="t_nrc_gate", seen=seen)
    run("\r\n".join(rows), now=datetime(2026, 11, 15, 16), source="t_nrc_gate", seen=seen)
    assert len(seen) == 4


def test_try_without_source_always_downloads(monkeypatch):
    monkeypatch.setattr(nrc, "_newest", {})
    seen: list = []
    run(BODY, seen=seen)
    run(BODY, seen=seen)
    assert len(seen) == 2 and nrc._newest == {}


def test_not_modified_is_nothing_new(monkeypatch):
    monkeypatch.setattr(nrc, "_newest", {})
    seen: list = []
    _, ctx = run(BODY, now=datetime(2026, 11, 13, 16), source="t_nrc_304", headers={"etag": '"abc"'}, seen=seen)
    ctx.commit_validators()                                                   # the runner, after the write
    res, _ = run("", now=datetime(2026, 11, 14, 15), source="t_nrc_304", status=304, seen=seen)
    assert res.obs == [] and seen[-1].headers["if-none-match"] == '"abc"'


def test_plant_series_follow_their_own_units():
    """PLAN 10b (L-108): Comanche Peak 1 at 0 % while unit 2 runs must read ~half the plant, not ERCOT's fleet ~75 %.
    A plant gets a day only when every one of its units has a row (a missing unit is never a 0)."""
    body = ("ReportDt|Unit|Power\r\n"
            "11/13/2026 12:00:00 AM|Comanche Peak 1|0\r\n"
            "11/13/2026 12:00:00 AM|Comanche Peak 2|100\r\n"
            "11/13/2026 12:00:00 AM|Diablo Canyon 1|100\r\n")              # Diablo Canyon 2 missing
    res, _ = run(body)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    d = datetime(2026, 11, 13)
    cp = nrc.PLANTS[6145]
    assert {u.nrc for u in cp} == {"Comanche Peak 1", "Comanche Peak 2"}
    u2 = next(u for u in cp if u.nrc == "Comanche Peak 2")
    assert v[("nrc.plant.eia_6145.mw", d)] == u2.mw
    assert v[("nrc.plant.eia_6145.capacity", d)] == round(sum(u.mw for u in cp), 1)
    assert ("nrc.plant.eia_6099.mw", d) not in v and ("nrc.plant.eia_6099.capacity", d) not in v
    declared = {m.series_id for m in res.meta}
    assert {nrc.plant_sid(pid, k) for pid in nrc.PLANTS for k in ("mw", "capacity")} <= declared


def test_every_us_nuclear_plant_on_the_map_reads_its_own_reactors():
    """Each inventory nuclear plant binds value/capacity to its NRC plant series, with no fleet estimate; build_plants'
    feature() writes exactly that, so a rebuild keeps it."""
    import glob
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "network" / "power"))
    import build_plants as bp

    declared = {m.series_id for m in build(SPECS["power_nrc"]).declared_series()}
    n = 0
    for f in glob.glob(str(root / "network" / "power" / "plants_*.geojson")):
        for ft in json.loads(Path(f).read_text(encoding="utf-8"))["features"]:
            p = ft["properties"]
            if p.get("fuel_group") != "nuclear" or not p["id"].startswith("power.plant.eia_"):
                continue
            n += 1
            pid = int(p["id"].split("eia_")[1])
            assert p["value"] == f"nrc.plant.eia_{pid}.mw" and p["capacity"] == f"nrc.plant.eia_{pid}.capacity"
            assert p["value"] in declared and p["capacity"] in declared
            assert "util_est" not in p and "quality" not in p and "daily reading" in p["basis"]
            rec = {"source": "eia", "key": str(pid), "name": p["name"], "fuel": p["fuel"], "group": "nuclear",
                   "mw": p["nameplate_mw"], "lonlat": tuple(ft["geometry"]["coordinates"]), "ba": p.get("ba")}
            built = bp.feature(rec, "x", {str(p.get("ba", "")).lower(): {"nuclear": 9e9}})["properties"]
            assert {k: built[k] for k in ("value", "capacity", "basis")} == {k: p[k] for k in ("value", "capacity", "basis")}
            assert "util_est" not in built
    assert n == 15
