"""The region overhaul (L-107, docs/REGIONS.md §5 / §5b): the six-row box, the `detail:` groups, the region page
(`GET /api/region/<id>`) and the per-host request pace."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from montney import regionpage
from montney import regions as R
from montney.connectors import base
from montney.connectors.base import utcnow
from montney.store import Obs, SeriesMeta, Store

BASE = {"id": "zz", "name": "Z", "members": ["CA-AB", "CA-BC"], "anchor": [-114, 51], "tz": "America/Edmonton",
        "budget_region": "ab_nebc"}
GAS = {"production": {"parts": ["p.x"]}, "imports": {"parts": ["i.x"], "scope": "PG&E"},
       "exports": {"none": True, "note": "no exit by pipe"}, "demand": {"parts": ["d.x"]}}
DETAIL = {"gas": [{"id": "demand_by_sector", "title": "Demand by sector", "of": "demand",
                   "rows": [{"label": "Power burn", "series": "d.power", "note": "pipeline  deliveries\n to plants"},
                            {"label": "Res + com", "series": "d.rescom"},
                            {"label": "Storage capacity", "value": 550, "unit": "Bcf", "as_of": "2026-06",
                             "source": "EIA-191"}]}],
          "power": [{"id": "data_centres", "title": "Data centres",
                     "rows": [{"label": "Estimated load", "value": 4200, "unit": "MW", "as_of": "2025",
                               "source": "LBNL 2024", "quality": "estimate"}]}]}


def _reg(extra: dict) -> R.Region:
    return R.parse({**BASE, **extra}, "t")


# ------------------------------------------------------------------ the box
def test_every_region_box_has_the_same_six_rows():
    for reg in R.load().values():
        rows = R.box_v2(reg)
        assert [r["line"] for r in rows] == ["gas.production", "gas.demand", "gas.imports", "gas.exports",
                                             "gas.storage_implied", "power.generation"], reg.id
        assert [r["sign"] for r in rows] == [1, -1, 1, -1, 1, 1]          # sources +, sinks -: four add up to the fifth
        for r in rows:                                                    # a row has data, is a known zero, or says why
            assert r["have"] or r.get("zero") or r["why"], (reg.id, r["line"])
            assert r["series"] == f"region.{reg.id}.{r['line']}"


def test_box_rows_say_why_a_line_is_missing():
    rows = {r["line"]: r for r in R.box_v2(_reg({"gas": GAS}))}
    assert rows["gas.exports"] == {"line": "gas.exports", "series": "region.zz.gas.exports", "label": "Exports",
                                   "unit": "MMcf/d", "section": "gas", "sign": -1, "tags": [], "have": False,
                                   "zero": True}
    assert rows["gas.storage_implied"]["have"] and rows["gas.storage_implied"]["tags"] == ["PG&E"]
    assert rows["power.generation"]["why"] == "no source yet"
    held = _reg({"gas": {**GAS, "demand": {"parts": [{"series": "d.m", "fill": "seasonal"}]}}})
    assert R.implied_why(held) == "demand is a monthly stand-in, not daily data"
    assert R.implied_why(_reg({"gas": {"imports": {"parts": ["i.x"]}}})) == "needs production, demand"
    # Reviewer on #471: a demand model with only a monthly error band is shown (est.), but closes no balance yet
    model = _reg({"gas": {**GAS, "demand": {"parts": ["d.model"], "quality": "estimate", "unproven": True}}})
    assert "region.zz.gas.storage_implied" not in {s for s, *_ in R.outputs(model)}
    assert "region.zz.gas.demand" in {s for s, *_ in R.outputs(model)}
    assert R.implied_why(model) == "demand is a model not yet checked against daily data"
    rows = {r["line"]: r for r in R.box_v2(model)}
    assert rows["gas.demand"]["have"] and rows["gas.demand"]["tags"] == ["est."] and not rows["gas.storage_implied"]["have"]
    with pytest.raises(R.RegionError, match="quality: estimate"):
        _reg({"gas": {"demand": {"parts": ["d.model"], "unproven": True}}})
    # the engine writes the demand line and no implied storage (seeded red: drop the unproven check in outputs())
    from datetime import datetime
    from montney.connectors import region_balance as RB
    now, day = datetime(2026, 11, 7, 12), datetime(2026, 11, 6)
    obs = RB.region_obs(model, {"p.x": {day: 100.0}, "i.x": {day: 10.0}, "d.model": {day: 80.0}}, now, now)
    got = {o.series_id.rsplit(".", 1)[1] for o in obs}
    assert "demand" in got and not got & {"storage_implied", "residual"}, got
    assert R.implied_why(_reg({})) == "needs production, demand, imports or exports"


def test_box_hint_and_rows_reach_the_map_feature():
    f = R.box_features({"zz": _reg({"gas": GAS, "box": {"dir": "SW"}})})["features"][0]["properties"]
    assert f["box"] == {"dir": "SW"} and len(f["box_rows"]) == 6 and f["coverage"] == "standard"
    assert "box" not in R.box_features({"zz": _reg({"gas": GAS})})["features"][0]["properties"]
    with pytest.raises(R.RegionError, match="box"):
        _reg({"box": {"dir": "up"}})


# ------------------------------------------------------------------ detail groups
def test_detail_groups_load_and_bad_ones_fail_loudly():
    reg = _reg({"gas": GAS, "detail": DETAIL})
    g = reg.detail["gas"][0]
    assert (g.id, g.of, [r.label for r in g.rows]) == ("demand_by_sector", "demand",
                                                       ["Power burn", "Res + com", "Storage capacity"])
    assert reg.detail_series() == ["d.power", "d.rescom"]
    row = {"label": "x", "series": "a.b"}
    for bad, msg in [
        ({"water": []}, "tabs"),
        ({"gas": [{"id": "Demand", "title": "t", "rows": [row]}]}, "snake_case"),
        ({"gas": [{"id": "a_b", "title": "t", "rows": []}]}, "non-empty"),
        ({"gas": [{"id": "a_b", "title": "t", "of": "load", "rows": [row]}]}, "`of`"),
        ({"gas": [{"id": "a_b", "title": "t", "rows": [row]}, {"id": "a_b", "title": "u", "rows": [row]}]}, "duplicate"),
        ({"gas": [{"id": "a_b", "title": "t", "rows": [{"label": "x"}]}]}, "one of the two"),
        ({"gas": [{"id": "a_b", "title": "t", "rows": [{"label": "x", "series": "a.b", "value": 1}]}]}, "one of the two"),
        ({"gas": [{"id": "a_b", "title": "t", "rows": [{"label": "x", "value": 1, "unit": "MW"}]}]}, "as_of"),   # no date
        ({"gas": [{"id": "a_b", "title": "t", "rows": [{"label": "x", "series": "nodots"}]}]}, "series id"),
        ({"gas": [{"id": "a_b", "title": "t", "rows": [{"label": "x", "series": "a.b", "colour": "red"}]}]}, "unknown"),
    ]:
        with pytest.raises(R.RegionError, match=msg):
            _reg({"detail": bad})


# ------------------------------------------------------------------ the page
@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    meta = [SeriesMeta(s, "t_src", unit="MMcf/d", freq="D") for s in
            ("region.zz.gas.production", "region.zz.gas.demand", "region.zz.gas.imports", "region.zz.gas.storage_implied",
             "d.power", "d.rescom")]
    st.upsert_meta(meta)
    vals = {"region.zz.gas.production": 1000.0, "region.zz.gas.demand": 700.0, "region.zz.gas.imports": 100.0,
            "region.zz.gas.storage_implied": 400.0, "d.power": 300.0, "d.rescom": 250.0}
    st.write([Obs(s, day, v, utcnow()) for s, v in vals.items()])
    yield st
    st.close()


def test_region_page_lines_groups_and_the_unexplained_rest(store):
    page = regionpage.build(store, _reg({"gas": GAS, "detail": DETAIL, "watch": ["linepack"]}))
    assert set(page["tabs"]) == {"gas", "power", "oil"} and page["watch"] == ["linepack"]
    gas = {x["line"]: x for x in page["tabs"]["gas"]["lines"]}
    assert [x["line"] for x in page["tabs"]["gas"]["lines"]][:4] == ["production", "demand", "imports", "exports"]
    assert gas["production"]["v"] == 1000.0 and gas["production"]["stale"] is False and gas["production"]["parts"] == ["p.x"]
    assert gas["imports"]["tags"] == ["PG&E"] and gas["exports"]["zero"] and gas["exports"]["note"] == "no exit by pipe"
    assert gas["storage"]["why"] == "no source yet" and gas["storage"]["v"] is None        # a line the region lacks
    g = page["tabs"]["gas"]["groups"][0]
    assert (g["id"], g["of"], g["unit"]) == ("demand_by_sector", "demand", "MMcf/d")
    assert g["rows"][0] == {"label": "Power burn", "series": "d.power", "unit": "MMcf/d", "v": 300.0,
                            "ts": g["rows"][0]["ts"], "prev": None, "prev_ts": None, "stale": False, "freq": "D",
                            "note": "pipeline deliveries to plants"}
    assert g["rows"][2] == {"label": "Storage capacity", "value": 550.0, "unit": "Bcf", "as_of": "2026-06",
                            "source": "EIA-191"}
    assert g["rest"] == 150.0                           # demand 700 - (300 + 250); the static fact isn't summed
    dc = page["tabs"]["power"]["groups"][0]["rows"][0]
    assert dc["quality"] == "estimate" and dc["value"] == 4200.0 and "v" not in dc
    box = {r["line"]: r for r in page["box"]}
    assert box["gas.storage_implied"]["v"] == 400.0 and box["power.generation"]["v"] is None


def test_rest_never_counts_a_missing_row_as_zero(store):
    detail = {"gas": [{"id": "demand_by_sector", "title": "Demand", "of": "demand",
                       "rows": [{"label": "Power burn", "series": "d.power"}, {"label": "LNG", "series": "d.lng"}]}]}
    g = regionpage.build(store, _reg({"gas": GAS, "detail": detail}))["tabs"]["gas"]["groups"][0]
    assert g["rows"][1]["v"] is None and g["rows"][1]["missing"] is True and g["rest"] is None


def test_rest_needs_every_row_from_the_same_reading_as_the_line(store):
    # Reviewer on #460: a row one bucket older than the line is late, not stale; subtracting it would be noise
    day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    store.upsert_meta([SeriesMeta("d.late", "t_src", unit="MMcf/d", freq="D")])
    store.write([Obs("d.late", day - timedelta(days=1), 250.0, utcnow())])
    detail = {"gas": [{"id": "demand_by_sector", "title": "Demand", "of": "demand",
                       "rows": [{"label": "Power burn", "series": "d.power"}, {"label": "Res + com", "series": "d.late"}]}]}
    g = regionpage.build(store, _reg({"gas": GAS, "detail": detail}))["tabs"]["gas"]["groups"][0]
    assert g["rows"][1]["v"] == 250.0 and g["rows"][1]["stale"] is False        # shown, and fresh by its own rule
    assert g["rows"][1]["ts"] < g["rows"][0]["ts"] and g["rest"] is None         # but not summed against a newer line


def test_power_tab_gets_the_fuel_mix_group_from_the_fuels(store):
    reg = _reg({"power": {"generation": {"parts": ["g.x"]}, "fuels": {"wind": {"parts": ["w.x"]}, "gas": {"parts": ["g.g"]}}}})
    g = regionpage.build(store, reg)["tabs"]["power"]["groups"][0]
    assert (g["id"], g["of"], g["gross"]) == ("generation_by_fuel", "generation", "fuels' total")   # the one gross opt-in
    assert [(r["label"], r["series"]) for r in g["rows"]] == [("Gas", "region.zz.power.gen.gas"),
                                                              ("Wind", "region.zz.power.gen.wind")]


def test_region_routes(tmp_path, monkeypatch, store):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    from montney.server import create_app
    with TestClient(create_app(store=store, schedule=False)) as c:
        idx = c.get("/api/regions").json()
        assert {r["id"] for r in idx} == set(R.load()) and all(r["anchor"] and r["name"] for r in idx)
        page = c.get("/api/region/cal").json()
        assert page["id"] == "cal" and len(page["box"]) == 6 and set(page["tabs"]) == {"gas", "power", "oil"}
        assert page["tabs"]["power"]["groups"][0]["id"] == "generation_by_fuel"
        assert c.get("/api/region/atlantis").status_code == 404
        assert c.post("/api/region/cal").status_code == 405                # read-only


# ------------------------------------------------------------------ the per-host pace
def test_requests_to_one_host_are_paced_and_other_hosts_are_not():
    base._host_next.clear()
    t = [100.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(round(s, 3))
        t[0] += s

    clock = lambda: t[0]  # noqa: E731
    assert base._pace("https://a.example/x", 0.5, clock, sleep) == 0.0            # the first goes at once
    assert round(base._pace("https://a.example/y", 0.5, clock, sleep), 3) == 0.5  # the second waits out the gap
    assert base._pace("https://b.example/x", 0.5, clock, sleep) == 0.0            # another host isn't held up
    t[0] += 10
    assert base._pace("https://a.example/z", 0.5, clock, sleep) == 0.0            # an idle host owes nothing
    assert slept == [0.5]
    assert base._pace("https://a.example/z", 0, clock, sleep) == 0.0              # 0 = no pacing (tests, `montney try`)
    base._host_next.clear()


# ------------------------------------------------------------------ engine asks from Power (#489 / #491)
def test_a_held_forward_line_is_tagged_held():
    # SaskPower's latest daily mean held to the current hour by its connector (saskpower.*_best)
    reg = _reg({"power": {"generation": {"parts": ["g.live", "g.sk_best"], "quality": "estimate", "held": True}}})
    assert R.line_tags(reg, reg.power["generation"]) == ["held", "est."]
    assert R.box_v2(reg)[-1]["tags"] == ["held", "est."]
    with pytest.raises(R.RegionError, match="quality: estimate"):
        _reg({"power": {"generation": {"parts": ["g.x"], "held": True}}})


def test_the_fuel_mix_group_carries_the_regions_note(store):
    reg = _reg({"power": {"generation": {"parts": ["g.x"]}, "fuels_note": "other = storage +  imports not broken out",
                          "fuels": {"wind": {"parts": ["w.x"]}}}})
    g = regionpage.build(store, reg)["tabs"]["power"]["groups"][0]
    assert g["id"] == "generation_by_fuel" and g["note"] == "other = storage + imports not broken out"
    plain = _reg({"power": {"generation": {"parts": ["g.x"]}, "fuels": {"wind": {"parts": ["w.x"]}}}})
    assert "note" not in regionpage.build(store, plain)["tabs"]["power"]["groups"][0]


def test_a_one_off_rebuild_reaches_back_past_the_window(monkeypatch):
    from datetime import datetime
    from montney.connectors import region_balance as RB
    reg = _reg({"power": {"generation": {"parts": ["g.x"]}}, "gas": {"production": {"parts": ["p.x"]}},
                "oil": {"production": {"parts": ["o.x"]}}})
    now, old = datetime(2026, 11, 13, 12), datetime(2026, 10, 20, 5)            # 24 days back: outside power's 7 days
    got = {"g.x": {old: 900.0, datetime(2026, 11, 13, 5): 1000.0}}
    assert RB.span(reg, "power") == RB.WINDOW["power"]
    assert old not in {o.ts for o in RB.region_obs(reg, got, now, now)}
    monkeypatch.setenv(RB.REBUILD_ENV, "30")
    assert RB.span(reg, "power") == RB.span(reg, "gas") == timedelta(days=30)
    assert RB.span(reg, "oil") == RB.WINDOW["oil"]                                # oil already reaches 400 days
    assert {o.ts: o.value for o in RB.region_obs(reg, got, now, now)}[old] == 900.0
    for bad in ("0", "401", "soon"):
        monkeypatch.setenv(RB.REBUILD_ENV, bad)
        with pytest.raises(ValueError):
            RB.span(reg, "power")


def test_a_missing_line_can_say_why_and_a_held_gas_line_closes_no_balance(store):
    # Reviewer on #492: umw's empty generation row should say why, not "no source yet"
    reg = _reg({"power": {"load": {"parts": ["l.x"]}, "why": {"generation": "MISO publishes no generation\n by region"}}})
    assert R.box_v2(reg)[-1]["why"] == "MISO publishes no generation by region"
    lines = {x["line"]: x for x in regionpage.build(store, reg)["tabs"]["power"]["lines"]}
    assert lines["generation"]["why"] == "MISO publishes no generation by region"
    assert lines["interchange"]["why"] == "no source yet"                    # no reason given: the default
    for bad, msg in [({"power": {"why": {"wind": "x"}}}, "unknown line"),
                     ({"power": {"load": {"parts": ["l.x"]}, "why": {"load": "x"}}}, "has a source"),
                     ({"power": {"why": {"generation": ""}}}, "short reason"), ({"gas": {"why": "none"}}, "must map")]:
        with pytest.raises(R.RegionError, match=msg):
            _reg(bad)
    # Reviewer on #493: a held-forward value is not the day's reading, so it never feeds storage implied (#434/#435)
    held = _reg({"gas": {**GAS, "demand": {"parts": ["d.best"], "quality": "estimate", "held": True}}})
    assert held.gas["demand"].stand_in and "region.zz.gas.storage_implied" not in {s for s, *_ in R.outputs(held)}
    assert R.implied_why(held) == "demand is held forward, not a reading of the day"


# ------------------------------------------------------------------ board-based regions (Gas on #503)
def test_through_yesterday_writes_only_finished_gas_days():
    from datetime import datetime
    from montney.connectors import region_balance as RB
    gas = {"through": "yesterday", "production": {"none": True}, "imports": {"parts": ["i.x"]},
           "exports": {"parts": ["e.x"]}, "demand": {"parts": ["d.x"]}}
    reg, live = _reg({"gas": gas}), _reg({"gas": {k: v for k, v in gas.items() if k != "through"}})
    assert reg.gas_through == "yesterday" and live.gas_through == ""
    d12, d13 = datetime(2026, 11, 12), datetime(2026, 11, 13)
    got = {"i.x": {d12: 500.0, d13: 100.0}, "e.x": {d12: 50.0, d13: 50.0}, "d.x": {d12: 434.0, d13: 600.0}}
    mid = datetime(2026, 11, 13, 20, 15)                 # 14:15 Central on the 13th: gas day 11-13 is in progress
    assert RB.current_gas_day(mid) == d13
    assert RB.current_gas_day(datetime(2026, 11, 13, 14, 59)) == d12        # 08:59 CST: still gas day 11-12
    assert RB.current_gas_day(datetime(2026, 7, 1, 14, 0)) == datetime(2026, 7, 1)   # 09:00 CDT (UTC-5): a new day
    by = lambda r: {(o.series_id.rsplit(".", 1)[1], o.ts): o.value for o in RB.region_obs(r, got, mid, mid)}  # noqa: E731
    fin, raw = by(reg), by(live)
    assert fin[("storage_implied", d12)] == 16.0 and not [k for k in fin if k[1] == d13]     # no mid-cycle day at all
    assert raw[("storage_implied", d13)] == -550.0                           # without the key: the false swing
    assert by(reg) == {k: v for k, v in raw.items() if k[1] < d13}           # every line stops at the same day
    nxt = datetime(2026, 11, 14, 15, 0)                                      # 09:00 CST on the 14th: 11-13 has ended
    assert {(o.series_id.rsplit(".", 1)[1], o.ts) for o in RB.region_obs(reg, got, nxt, nxt)} >= {("storage_implied", d13)}
    with pytest.raises(R.RegionError, match="through"):
        _reg({"gas": {"through": "today"}})


def test_a_region_without_storage_shows_its_closure_not_storage(store):
    reg = _reg({"gas": {**GAS, "storage": {"none": True, "note": "no underground storage"}}})
    assert reg.no_storage and not _reg({"gas": GAS}).no_storage
    rows = {r["line"]: r for r in R.box_v2(reg)}
    assert rows["gas.storage_implied"]["label"] == "Closure (no storage)" and rows["gas.storage_implied"]["have"]
    outs = {s for s, *_ in R.outputs(reg)}
    assert "region.zz.gas.storage_implied" in outs and "region.zz.gas.residual" not in outs
    assert "region.zz.gas.storage" not in outs
    lines = {x["line"]: x for x in regionpage.build(store, reg)["tabs"]["gas"]["lines"]}
    assert lines["storage_implied"]["label"].startswith("Closure (no underground storage)")
    assert lines["storage"]["zero"] is True
    from datetime import datetime
    from montney.connectors import region_balance as RB
    now, day = datetime(2026, 11, 7, 12), datetime(2026, 11, 6)
    obs = RB.region_obs(reg, {"p.x": {day: 100.0}, "i.x": {day: 10.0}, "d.x": {day: 80.0}}, now, now)
    assert {o.series_id.rsplit(".", 1)[1] for o in obs} == {"production", "imports", "net_imports", "demand",
                                                            "storage_implied"}
    with pytest.raises(R.RegionError, match="inventory"):
        R.outputs(_reg({"gas": {"storage": {"none": True}, "inventory": {"parts": ["inv.x"], "unit": "Bcf"}}}))
    assert "through" not in regionpage.build(store, reg)["tabs"]["gas"]
    assert regionpage.build(store, _reg({"gas": {**GAS, "through": "yesterday"}}))["tabs"]["gas"]["through"] == "yesterday"


# ------------------------------------------------------------------ what Frontend's live pass found (#530)
def test_a_known_zero_is_zero_and_never_stale_or_missing(store):
    page = regionpage.build(store, _reg({"gas": GAS}))            # GAS: exports {none: true}
    box = {r["line"]: r for r in page["box"]}
    assert (box["gas.exports"]["v"], box["gas.exports"]["stale"]) == (0.0, False) and "missing" not in box["gas.exports"]
    line = {x["line"]: x for x in page["tabs"]["gas"]["lines"]}["exports"]
    assert (line["v"], line["stale"], line["zero"]) == (0.0, False, True) and "missing" not in line
    assert box["power.generation"]["missing"] is True              # a line with no source is still missing


def test_a_line_older_than_the_snapshot_window_still_shows_its_value(store):
    # a storage flow built from a monthly value is written to the end of its month, months ago
    old = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=75)
    store.upsert_meta([SeriesMeta("region.zz.gas.storage", "t_src", unit="MMcf/d", freq="D")])
    store.write([Obs("region.zz.gas.storage", old, -961.0, utcnow())])
    reg = _reg({"gas": {**GAS, "storage": {"lag": "100d", "parts": [{"series": "s.m", "sign": -1, "fill": "month"}]}}})
    R._REGIONS_CACHE = {"zz": reg}
    try:
        line = {x["line"]: x for x in regionpage.build(store, reg)["tabs"]["gas"]["lines"]}["storage"]
    finally:
        R._REGIONS_CACHE = None
    assert line["v"] == -961.0 and line["stale"] is False        # inside its declared 100-day lag: late, not stale
    # ... and it reads as the month it is, never as a daily number (Reviewer on #531: August's injection on a
    # November page): monthly, stamped at the month's start, its period named, no day-on-day change
    first = datetime(old.year, old.month, 1)
    assert (line["freq"], line["period"]) == ("M", first.strftime("%Y-%m"))
    assert line["ts"] == (first - datetime(1970, 1, 1)).total_seconds() and line["prev"] is None
    plain = _reg({"gas": {**GAS, "storage": {"parts": ["s.d"]}}})  # a daily line of the same age is just stale
    daily = {x["line"]: x for x in regionpage.build(store, plain)["tabs"]["gas"]["lines"]}["storage"]
    assert daily["stale"] is True and daily["freq"] == "D" and "period" not in daily


def test_oil_production_is_a_monthly_series():
    reg = _reg({"oil": {"production": {"parts": [{"series": "o.m", "fill": "latest"}]}}})
    assert [f for s, _, _, f in R.outputs(reg) if s.endswith("oil.production")] == ["M"]
    from montney import snapshot
    now = 1_800_000_000.0
    s = {"ts": [now - 20 * 86400], "v": [245000.0]}               # this month's point, 20 days old
    assert snapshot.summarize(s, {"freq": "M"}, None, now, now, sid="region.zz.oil.production")["stale"] is False
    assert snapshot.summarize(s, {"freq": "D"}, None, now, now, sid="region.zz.oil.production")["stale"] is True
