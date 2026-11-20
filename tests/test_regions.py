"""Regions (L-106, docs/REGIONS.md): the region files, the generated map features and the region_balance engine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from montney import regions as R
from montney.connectors import region_balance as RB


# ------------------------------------------------------------------ the region files
def test_repo_regions_load_and_cover_the_13():
    regs = R.load()
    assert set(regs) == {"wcan", "mbnwo", "sont", "qc", "upne", "nyc", "umw", "pnw", "bakken", "mtnw", "cal",
                         "sunbelt", "tx"}
    members = [m for r in regs.values() for m in r.members]
    assert len(members) == len(set(members))


def test_anchor_inside_its_own_outline_and_every_outline_present():
    regs = R.load()
    outlines = json.loads(R.OUTLINES.read_text(encoding="utf-8"))
    assert {f["properties"]["region"] for f in outlines["features"]} == set(regs)
    for reg in regs.values():
        assert R.region_of(*reg.anchor, country=reg.members[0][:2], outlines=outlines) == reg.id, reg.id


@pytest.mark.parametrize("lon,lat,country,want", [
    (-114.07, 51.05, "CA", "wcan"), (-104.6, 50.45, "CA", "wcan"), (-128.65, 54.05, "CA", "wcan"),  # Calgary, Regina, Kitimat
    (-123.12, 49.28, "CA", "pnw"), (-123.37, 48.43, "CA", "pnw"),                     # Vancouver, Victoria (the clip)
    (-97.14, 49.90, "CA", "mbnwo"), (-89.25, 48.38, "CA", "mbnwo"),                   # Winnipeg, Thunder Bay
    (-79.38, 43.65, "CA", "sont"), (-73.57, 45.50, "CA", "qc"),                       # Toronto, Montreal
    (-74.00, 40.71, "US", "nyc"), (-71.06, 42.36, "US", "upne"), (-93.27, 44.98, "US", "umw"),
    (-100.78, 46.81, "US", "bakken"), (-104.82, 39.74, "US", "mtnw"), (-118.24, 34.05, "US", "cal"),
    (-112.07, 33.45, "US", "sunbelt"), (-95.37, 29.76, "US", "tx"),
    (-87.63, 41.88, "US", None), (-90.07, 29.95, "US", None),                          # Chicago, New Orleans: outside
])
def test_point_in_region(lon, lat, country, want):
    assert R.region_of(lon, lat, country=country) == want


def test_member_keeps_a_point_out_of_a_neighbour_across_a_land_border():
    # Reviewer on #411: Toledo (OH) sits within the coast tolerance of Michigan's outline; Ohio is in no region
    assert R.region_of(-83.54, 41.66, country="US", member="US-OH") is None
    assert R.region_of(-82.12, 42.70, country="CA", member="CA-ON") == "sont"         # Dawn
    assert R.region_of(-123.12, 49.28, country="CA", member="CA-BC") == "pnw"        # Vancouver: pnw holds BC's clip
    with pytest.raises(ValueError):
        R.region_of(-114, 51, country="Canada")


def test_scope_labels_the_box_when_a_line_is_not_the_whole_region():
    reg = _reg({"power": {"load": {"parts": ["l.x"], "scope": "CAISO"},
                          "generation": {"parts": ["g.x"], "scope": "all ON", "quality": "estimate"}}})
    assert R.line_tags(reg, reg.power["load"]) == ["CAISO"]
    assert R.line_tags(reg, reg.power["generation"]) == ["all ON", "est."]
    assert R.box_v2(reg)[-1]["tags"] == ["all ON", "est."]                 # the box's Generation row carries them


def test_committed_boxes_are_current():
    """network/region/boxes.geojson is generated: regenerate it (scripts/build_regions.py) after editing a region."""
    want = json.dumps(R.box_features(R.load()), indent=1, ensure_ascii=False) + "\n"
    assert R.BOXES.read_text(encoding="utf-8") == want


def test_box_layout_is_the_same_rule_for_every_region():
    for reg in R.load().values():
        rows = R.box_v2(reg)
        assert [r["section"] for r in rows] == ["gas"] * 5 + ["power"], reg.id            # gas, then power
        assert all(r["series"].startswith(f"region.{reg.id}.") for r in rows)


def test_bad_region_files_fail_loudly():
    base = {"id": "zz", "name": "Z", "members": ["CA-AB"], "anchor": [-114, 51], "tz": "America/Edmonton",
            "budget_region": "ab_nebc"}
    R.parse(base, "ok")
    for bad, msg in [({"members": ["Alberta"]}, "ISO 3166-2"), ({"gas": {"production": {"parts": []}}}, "non-empty"),
                     ({"gas": {"supply": {"parts": ["a.b"]}}}, "unknown line"),
                     ({"power": {"fuels": {"plasma": {"parts": ["a.b"]}}}}, "unknown fuels"),
                     ({"gas": {"demand": {"parts": ["a.b"], "covers": ["Texas"]}}}, "covers"),
                     ({"colour": "red"}, "unknown keys")]:
        with pytest.raises(R.RegionError, match=msg):
            R.parse({**base, **bad}, "bad")


# ------------------------------------------------------------------ the engine
NOW = datetime(2026, 11, 7, 12)
D = lambda n: datetime(2026, 11, 7) - timedelta(days=n)  # noqa: E731


def _reg(raw: dict) -> R.Region:
    return R.parse({"id": "zz", "name": "Z", "members": ["CA-AB", "CA-BC"], "anchor": [-114, 51],
                    "tz": "America/Edmonton", "budget_region": "ab_nebc", **raw}, "t")


def _by(obs, series):
    return {o.ts: o.value for o in obs if o.series_id == series}


def test_gas_balance_implied_storage_and_residual():
    reg = _reg({"gas": {"production": {"parts": ["p.ab", "p.bc"]}, "imports": {"parts": ["i.x"]},
                        "exports": {"parts": ["e.x", "-e.back"]}, "demand": {"parts": ["d.x"]},
                        "storage": {"parts": [{"series": "s.wd", "sign": -1}]}}})
    got = {"p.ab": {D(1): 100.0, D(2): 110.0}, "p.bc": {D(1): 50.0, D(2): 50.0}, "i.x": {D(1): 10.0, D(2): 10.0},
           "e.x": {D(1): 70.0, D(2): 70.0}, "e.back": {D(1): 5.0, D(2): 5.0}, "d.x": {D(1): 80.0},
           "s.wd": {D(1): -12.0}}
    obs = RB.region_obs(reg, got, NOW, NOW)
    assert _by(obs, "region.zz.gas.production") == {D(1): 150.0, D(2): 160.0}
    assert _by(obs, "region.zz.gas.exports") == {D(1): 65.0, D(2): 65.0}
    assert _by(obs, "region.zz.gas.net_imports") == {D(1): -55.0, D(2): -55.0}
    # implied = 150 + 10 - 65 - 80 = 15; only D(1), where demand exists (a missing part is never a zero)
    assert _by(obs, "region.zz.gas.storage_implied") == {D(1): 15.0}
    assert _by(obs, "region.zz.gas.storage") == {D(1): 12.0}           # withdrawal -12 -> injection +12
    assert _by(obs, "region.zz.gas.residual") == {D(1): 3.0}


def test_none_line_counts_as_zero_and_writes_no_series():
    reg = _reg({"gas": {"production": {"none": True}, "imports": {"parts": ["i.x"]}, "demand": {"parts": ["d.x"]}}})
    obs = RB.region_obs(reg, {"i.x": {D(1): 500.0}, "d.x": {D(1): 480.0}}, NOW, NOW)
    assert "region.zz.gas.production" not in {o.series_id for o in obs}
    assert _by(obs, "region.zz.gas.storage_implied") == {D(1): 20.0}


def test_fill_month_holds_a_monthly_value_across_days():
    reg = _reg({"gas": {"production": {"parts": ["p.daily", {"series": "p.monthly", "fill": "month"}]}}})
    got = {"p.daily": {D(1): 100.0, D(10): 100.0}, "p.monthly": {datetime(2026, 11, 1): 30.0,
                                                                 datetime(2026, 10, 1): 20.0}}
    obs = RB.region_obs(reg, got, NOW, NOW)
    assert _by(obs, "region.zz.gas.production") == {D(1): 130.0, D(10): 120.0}     # Nov 6 -> Nov; Oct 28 -> Oct


def test_power_hourly_means_and_residual():
    reg = _reg({"power": {"load": {"parts": ["l.x"]}, "generation": {"parts": ["g.x"]},
                          "interchange": {"parts": ["n.x"]}, "fuels": {"wind": {"parts": ["w.x"]}}}})
    h = datetime(2026, 11, 7, 10)
    five = {h + timedelta(minutes=5 * i): 1000.0 + 10 * i for i in range(12)}       # mean 1055
    got = {"l.x": {h: 1100.0}, "g.x": five, "n.x": {h: 60.0}, "w.x": {h + timedelta(minutes=30): 300.0}}
    obs = RB.region_obs(reg, got, NOW, NOW)
    assert _by(obs, "region.zz.power.generation") == {h: 1055.0}
    assert _by(obs, "region.zz.power.residual") == {h: 15.0}                       # 1055 + 60 - 1100
    assert _by(obs, "region.zz.power.gen.wind") == {h: 300.0}


def test_subdaily_gas_parts_bucket_into_alberta_days():
    reg = _reg({"gas": {"demand": {"parts": ["d.halfhour"]}}})
    # 05:30 UTC is still the previous Alberta day (UTC-6, L-029); 06:30 UTC is the new one
    got = {"d.halfhour": {datetime(2026, 11, 6, 5, 30): 10.0, datetime(2026, 11, 6, 6, 30): 30.0,
                          datetime(2026, 11, 6, 20, 0): 50.0}}
    obs = RB.region_obs(reg, got, NOW, NOW)
    assert _by(obs, "region.zz.gas.demand") == {datetime(2026, 11, 5): 10.0, datetime(2026, 11, 6): 40.0}


def test_connector_reads_each_family_over_its_own_window():
    reg = _reg({"gas": {"production": {"parts": ["p.x"]}}, "power": {"load": {"parts": ["l.x"]}}})
    calls = []

    class Ctx:
        now = NOW

    c = RB.RegionBalance({"id": "region_balance", "type": "python"})
    c.load_regions = lambda: {"zz": reg}
    c.stored = lambda ids, start, end: calls.append((tuple(ids), NOW - start)) or {}
    res = c.fetch(Ctx())
    spans = dict(calls)
    assert spans[("l.x",)] == RB.WINDOW["power"] + timedelta(days=1)
    assert spans[("p.x",)] == RB.WINDOW["gas"] + timedelta(days=1)
    assert res.warnings and {m.series_id for m in res.meta} == {"region.zz.gas.production", "region.zz.power.load"}


def test_fill_latest_holds_the_newest_month_forward_within_the_cap():
    reg = _reg({"gas": {"demand": {"parts": ["d.daily", {"series": "d.eia", "fill": "latest"}]}}})
    got = {"d.daily": {D(1): 100.0, D(200): 100.0}, "d.eia": {datetime(2026, 8, 1): 40.0}}
    obs = RB.region_obs(reg, got, NOW, NOW)
    # Nov 6: August (the newest month) held forward, 68 days past its end (< 150); April (D(200)) precedes it
    assert _by(obs, "region.zz.gas.demand") == {D(1): 140.0}
    assert reg.gas["demand"].estimate
    stale = {"d.daily": {D(1): 100.0}, "d.eia": {datetime(2026, 4, 1): 40.0}}       # ends Apr 30: 190 days > cap
    assert _by(RB.region_obs(reg, stale, NOW, NOW), "region.zz.gas.demand") == {}


@pytest.mark.parametrize("lon,lat,country,want", [
    # Reviewer on #408: US plants on the St. Clair / Detroit rivers, Lake Erie's Ohio shore and the Rainy River
    (-82.49, 42.77, "US", "umw"), (-82.47, 42.76, "US", "umw"),      # Belle River, St. Clair (MI): Michigan is umw
    (-83.09, 41.60, "US", None),                                    # Davis-Besse (OH): outside every region
    (-93.40, 48.60, "US", "umw"), (-84.35, 46.50, "US", "umw"),     # International Falls (MN), Saint Marys Falls (MI)
    (-82.12, 42.70, "CA", "sont"), (-82.40, 42.97, "CA", "sont"),   # Dawn, Sarnia
    (-83.03, 42.31, "CA", "sont"), (-93.41, 48.61, "CA", "mbnwo"),  # Windsor, Fort Frances
])
def test_point_in_region_with_country(lon, lat, country, want):
    assert R.region_of(lon, lat, country=country) == want


def test_outlines_carry_their_countries():
    fc = json.loads(R.OUTLINES.read_text(encoding="utf-8"))
    by = {f["properties"]["region"]: f["properties"]["countries"] for f in fc["features"]}
    assert by["wcan"] == ["CA"] and by["pnw"] == ["CA", "US"] and by["tx"] == ["US"]    # pnw holds BC's south coast


def test_derived_rows_inherit_est_and_scope_from_their_inputs():
    # Reviewer on #417: storage implied built from an est. demand / a scoped import must say so
    reg = _reg({"gas": {"production": {"parts": ["p.x"], "covers": ["CA-AB"]}, "imports": {"parts": ["i.x"], "scope": "PG&E"},
                        "exports": {"parts": ["e.x"]}, "demand": {"parts": ["d.x"], "quality": "estimate"}}})
    assert R.line_tags(reg, [reg.gas["imports"], reg.gas["exports"]]) == ["PG&E"]            # net imports
    rows = {r["line"]: r for r in R.box_v2(reg)}
    assert rows["gas.storage_implied"]["tags"] == ["AB", "PG&E", "est."]


def test_a_balance_derived_demand_suppresses_implied_storage():
    # pnw: demand = imports - exits + storage withdrawal, so implied == measured by construction (residual 0)
    reg = _reg({"gas": {"production": {"none": True}, "imports": {"parts": ["i.x"]}, "exports": {"parts": ["e.x"]},
                        "demand": {"parts": ["d.x"], "derived": True}, "storage": {"parts": ["s.x"]}}})
    outs = {s for s, *_ in R.outputs(reg)}
    assert "region.zz.gas.storage_implied" not in outs and "region.zz.gas.residual" not in outs
    got = {"i.x": {D(1): 10.0}, "e.x": {D(1): 4.0}, "d.x": {D(1): 5.0}, "s.x": {D(1): 1.0}}
    assert not [o for o in RB.region_obs(reg, got, NOW, NOW) if o.series_id.endswith(("storage_implied", "residual"))]


def test_a_line_covering_only_own_members_is_tagged_when_the_region_holds_a_clip():
    # Reviewer on #424: mbnwo also holds NW Ontario (sont's clip), so a Manitoba-only line says "(MB)"
    regs = R.load()
    assert regs["mbnwo"].received == ["CA-ON"] and regs["pnw"].received == ["CA-BC"]
    mb = regs["mbnwo"]
    assert R.line_tags(mb, mb.oil["production"])[0] == "MB"                              # Manitoba crude only
    assert "MB" not in R.line_tags(mb, mb.gas["demand"])                                 # covers MB + NW Ontario
    # an empty covers is the region's own members: pnw's US-only lines say so (Reviewer on #425); its lines that list
    # the Lower Mainland (CA-BC, Gas L-106 8c) count as the whole region
    pnw = regs["pnw"]
    assert R.line_tags(pnw, pnw.gas["storage"])[-1] == "WA, OR, ID"                    # Jackson Prairie: US only
    assert R.line_tags(pnw, pnw.gas["imports"]) == ["est."]                             # covers all four
    assert R.line_tags(pnw, pnw.gas["demand"]) == ["est."]


def test_covers_may_name_a_received_clip_but_nothing_else(tmp_path):
    # Gas on #426: pnw's Lower Mainland lines cover CA-BC, which pnw holds only through wcan's clip
    import shutil
    for f in R.REGIONS_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    ok = (tmp_path / "pnw.yaml").read_text(encoding="utf-8")
    assert R.load(tmp_path)["pnw"].gas["imports"].covers[-1] == "CA-BC"           # the real file names the clip
    bad = ok.replace("CA-BC]", "CA-AB]")
    assert bad != ok
    (tmp_path / "pnw.yaml").write_text(bad, encoding="utf-8")
    with pytest.raises(R.RegionError, match="neither members nor received clips"):
        R.load(tmp_path)


def test_a_section_lag_makes_its_region_series_stale_later_not_never():
    # Reviewer/Power on #428: BC Hydro posts weekdays, so wcan's composite power is days late by design
    reg = _reg({"power": {"lag": "5d", "load": {"parts": ["l.x"]}}})
    assert reg.lags == {"power": 5 * 86400}
    with pytest.raises(R.RegionError, match="duration"):
        _reg({"power": {"lag": "soon", "load": {"parts": ["l.x"]}}})
    R._REGIONS_CACHE = {"zz": reg}
    try:
        assert R.stale_after("region.zz.power.load") == 5 * 86400 + 3 * 3600
        assert R.stale_after("region.zz.gas.demand") == 0 and R.stale_after("aeso.csd.ail") == 0
        from montney import snapshot
        h = 3600.0
        s = {"ts": [1000 * h], "v": [9000.0]}
        meta = {"freq": "H", "source": "region_balance"}
        assert snapshot.summarize(s, meta, None, 1000 * h + 4 * 86400, sid="region.zz.power.load")["stale"] is False
        assert snapshot.summarize(s, meta, None, 1000 * h + 6 * 86400, sid="region.zz.power.load")["stale"] is True
        assert snapshot.summarize(s, meta, None, 1000 * h + 4 * h)["stale"] is True       # no sid: the normal rule
    finally:
        R._REGIONS_CACHE = None


def test_power_window_reaches_back_a_week():
    assert RB.WINDOW["power"] == timedelta(days=7)


def test_fill_seasonal_borrows_last_years_same_month():
    # Reviewer on #434/#435: a demand-type monthly flow keeps its season (Nov 2025 for Nov 2026), not August's level
    reg = _reg({"gas": {"demand": {"parts": [{"series": "d.m", "fill": "seasonal"}]}}})
    got = {"d.m": {datetime(2025, 11, 1): 4000.0, datetime(2026, 8, 1): 2400.0}}
    vals = _by(RB.region_obs(reg, got, NOW, NOW), "region.zz.gas.demand")
    assert vals[D(1)] == 4000.0 and all(v == 4000.0 for v in vals.values())
    assert R.line_tags(reg, reg.gas["demand"]) == ["seasonal", "est."]


def test_storage_flows_may_not_be_held_or_seasonal():
    for fill in ("latest", "seasonal"):
        with pytest.raises(R.RegionError, match="isn't allowed on a storage line"):
            _reg({"gas": {"storage": {"parts": [{"series": "s.m", "fill": fill}]}}})
    with pytest.raises(R.RegionError, match="isn't allowed on a inventory line"):
        _reg({"gas": {"inventory": {"parts": [{"series": "i.m", "fill": "seasonal"}]}}})
    _reg({"gas": {"storage": {"parts": [{"series": "s.m", "fill": "month"}]},
                  "inventory": {"parts": [{"series": "i.m", "fill": "latest"}]}}})          # allowed


def test_no_implied_storage_from_a_stand_in():
    reg = _reg({"gas": {"production": {"parts": ["p.x"]}, "imports": {"parts": ["i.x"]},
                        "demand": {"parts": ["d.x", {"series": "d.m", "fill": "seasonal"}]}}})
    assert "region.zz.gas.storage_implied" not in {s for s, *_ in R.outputs(reg)}
    got = {"p.x": {D(1): 10.0}, "i.x": {D(1): 5.0}, "d.x": {D(1): 8.0}, "d.m": {datetime(2025, 11, 1): 1.0}}
    obs = RB.region_obs(reg, got, NOW, NOW)
    assert _by(obs, "region.zz.gas.demand") == {D(1): 9.0}
    assert not [o for o in obs if o.series_id.endswith("storage_implied")]


def test_a_gas_lag_keeps_an_honest_monthly_storage_row_from_reading_stale():
    reg = _reg({"gas": {"lag": "100d", "storage": {"parts": [{"series": "s.m", "fill": "month"}]}}})
    R._REGIONS_CACHE = {"zz": reg}
    try:
        from montney import snapshot
        d = 86400.0
        s = {"ts": [1000 * d], "v": [-961.0]}
        meta = {"freq": "D"}
        assert snapshot.summarize(s, meta, None, 1000 * d + 60 * d, sid="region.zz.gas.storage")["stale"] is False
        assert snapshot.summarize(s, meta, None, 1000 * d + 60 * d)["stale"] is True       # without the lag: stale
    finally:
        R._REGIONS_CACHE = None


def test_a_month_only_storage_line_is_written_back_to_its_lag():
    # Reviewer on #436: in November, EIA's newest month is August, older than the 21-day gas window
    reg = _reg({"gas": {"lag": "100d", "storage": {"parts": [{"series": "s.m", "sign": -1, "fill": "month"}]}}})
    obs = RB.region_obs(reg, {"s.m": {datetime(2026, 8, 1): 961.0}}, NOW, NOW)
    vals = _by(obs, "region.zz.gas.storage")
    assert vals and max(vals) == datetime(2026, 8, 31) and set(vals.values()) == {-961.0}
    assert RB.span(reg, "gas") == timedelta(days=100) and RB.span(_reg({}), "gas") == RB.WINDOW["gas"]


def test_a_line_lag_stays_on_its_line():
    # Reviewer on #434: a monthly storage line's 100-day lag mustn't make the same section's daily imports look fresh
    reg = _reg({"gas": {"imports": {"parts": ["i.x"]}, "exports": {"parts": ["e.x"]},
                        "storage": {"lag": "100d", "parts": [{"series": "s.m", "fill": "month"}]}}})
    R._REGIONS_CACHE = {"zz": reg}
    try:
        assert R.stale_after("region.zz.gas.storage") == 100 * 86400 + 3 * 3600
        assert R.stale_after("region.zz.gas.imports") == 0 and R.stale_after("region.zz.gas.net_imports") == 0
        assert RB.span(reg, "gas") == timedelta(days=100)
    finally:
        R._REGIONS_CACHE = None
    sect = _reg({"power": {"lag": "5d", "load": {"parts": ["l.x"]}, "fuels": {"wind": {"parts": ["w.x"]}}}})
    R._REGIONS_CACHE = {"zz": sect}
    try:                                                  # a section lag is still every line's default
        assert R.stale_after("region.zz.power.load") == R.stale_after("region.zz.power.gen.wind") == 5 * 86400 + 3 * 3600
    finally:
        R._REGIONS_CACHE = None


def test_every_region_has_a_distinct_colour_on_its_outline_and_box():
    regs = R.load()
    cols = [r.color for r in regs.values()]
    assert all(cols) and len(set(cols)) == len(cols)
    outlines = json.loads(R.OUTLINES.read_text(encoding="utf-8"))
    assert {f["properties"]["region"]: f["properties"]["color"] for f in outlines["features"]} ==         {r.id: r.color for r in regs.values()}
    with pytest.raises(R.RegionError, match="color"):
        R.parse({"id": "zz", "name": "Z", "members": ["CA-AB"], "anchor": [-114, 51], "tz": "America/Edmonton",
                 "budget_region": "ab_nebc", "color": "gold"}, "t")
