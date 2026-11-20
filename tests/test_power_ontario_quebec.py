"""L-091: Ontario major plants (IESO per-generator, same request as the fuel sums) and Quebec (Hydro-Quebec open data).

Mocked payloads in the live shapes (checked 2026-11-03): GenOutputCapability <Generator> blocks, and the Opendatasoft
JSON exports of HQ's rolling and history datasets.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import power_hq as hq
from montney.connectors import power_ieso as ieso
from montney.connectors.base import Context

SPECS = load_specs(include_disabled=True)
ROOT = Path(network.__file__).resolve().parents[1]


def gen(name: str, fuel: str, out: dict[int, str], cap: dict[int, str]) -> str:
    o = "".join(f"<Output><Hour>{h}</Hour><EnergyMW>{v}</EnergyMW></Output>" for h, v in out.items())
    c = "".join(f"<Capability><Hour>{h}</Hour><EnergyMW>{v}</EnergyMW></Capability>" for h, v in cap.items())
    return (f"<Generator><GeneratorName>{name}</GeneratorName><FuelType>{fuel}</FuelType><Outputs>{o}</Outputs>"
            f"<Capabilities>{c}</Capabilities></Generator>")


def goc(d: str, gens: list[str]) -> str:
    return f"<Document><DocBody><Date>{d}</Date><Generators>{''.join(gens)}</Generators></DocBody></Document>"


# ------------------------------------------------------------------ Ontario plants
def test_plants_sum_their_units_and_unknown_units_are_ignored():
    hours = range(1, 25)
    text = goc("2026-11-02", [gen("BRUCEA-G2", "NUCLEAR", {h: "800" for h in hours}, {h: "828" for h in hours}),
                              gen("BRUCEB-G6", "NUCLEAR", {h: "810" for h in hours}, {h: "817" for h in hours}),
                              gen("PICKERINGB-G5", "NUCLEAR", {h: "" for h in hours}, {h: "0" for h in hours}),
                              gen("SITHE GOREWAY-G11", "GAS", {h: "200" for h in hours}, {h: "212" for h in hours}),
                              gen("SOMEWIND-LT.AG", "WIND", {h: "50" for h in hours}, {h: "99" for h in hours})])
    d, plants, warns = ieso.parse_plants(text)
    assert d == date(2026, 11, 2) and plants["bruce"][1] == (1610.0, 1645.0)        # A + B units, output + capability
    assert plants["pickering"][3] == (0.0, 0.0) and plants["goreway"][24] == (200.0, 212.0)
    assert "darlington" not in plants and any("darlington" in w for w in warns)       # missing: a warning, not a 0


def test_hourly_emits_plants_for_complete_hours_and_daily_sums(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    hours = {h: "500" for h in range(1, 25)}
    yday = goc("2026-11-01", [gen("DARLINGTON-G1", "NUCLEAR", hours, {h: "850" for h in range(1, 25)})])
    today = goc("2026-11-02", [gen("DARLINGTON-G1", "NUCLEAR", {1: "600", 2: "610"}, {1: "850", 2: "850"})])

    def handler(req):
        u = str(req.url)
        if u.endswith("PUB_GenOutputCapability_20261101.xml"):
            return httpx.Response(200, text=yday)
        if u.endswith("PUB_GenOutputCapability.xml"):
            return httpx.Response(200, text=today)
        return httpx.Response(404)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 3, 2, 20))
    v = {(o.series_id, o.ts): o.value for o in build(SPECS["power_ieso_hourly"]).run(c).obs}
    assert v[("ieso.plant.darlington.hourly", datetime(2026, 11, 2, 6))] == 610      # HE2 EST
    assert v[("ieso.plant.darlington.capability.hourly", datetime(2026, 11, 2, 5))] == 850
    res = build(SPECS["power_ieso_daily"]).__class__(SPECS["power_ieso_daily"])
    out = type("R", (), {"obs": [], "warnings": []})()
    res._plant_days(c, out)                                                          # yesterday read from the cache
    assert [(o.series_id, o.value) for o in out.obs] == [("ieso.plant.darlington.daily", 500 * 24)]


def test_plant_features_are_typed_and_use_declared_series():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    feats = json.loads((ROOT / "network/power/ontario_plants.geojson").read_text(encoding="utf-8"))["features"]
    declared = {m.series_id for s in ("power_ieso_hourly", "power_ieso_daily") for m in build(SPECS[s]).declared_series()}
    assert {f["properties"]["id"] for f in feats} == {f"power.plant.ont_{k}" for k in ieso.PLANTS}
    for f in feats:
        p = f["properties"]
        assert p["type"] == "plant" and p["fuel_group"] in ("nuclear", "gas")
        assert p["fuel"] == "nuclear" if p["fuel_group"] == "nuclear" else p["fuel"] in (
            "combined_cycle", "simple_cycle", "cogeneration", "gas_fired_steam")
        assert {p["value"], p["capacity"], *p["panel"]["series"]} <= declared
        lon, lat = f["geometry"]["coordinates"]
        assert 41.6 < lat < 45.5 and -83.2 < lon < -76.5, p["id"]                      # southern Ontario
    nukes = {f["properties"]["id"] for f in feats if f["properties"]["fuel_group"] == "nuclear"}
    assert nukes == {"power.plant.ont_bruce", "power.plant.ont_darlington", "power.plant.ont_pickering"}


# ------------------------------------------------------------------ Quebec
def demand_rows(start: datetime, n: int, mw: float) -> list[dict]:
    from datetime import timedelta
    return [{"date": (start + timedelta(minutes=15 * (i + 1))).isoformat() + "+00:00", "valeurs_demandetotal": mw}
            for i in range(n)]


def prod_rows(start: datetime, n: int) -> list[dict]:
    from datetime import timedelta
    return [{"date": (start + timedelta(hours=i, minutes=30)).isoformat() + "+00:00", "valeurs_hydraulique": 18000.0,
             "valeurs_eolien": 1500.0, "valeurs_solaire": 1.0, "valeurs_thermique": 0.0, "valeurs_autres": 600.0,
             "valeurs_total": 20101.0} for i in range(n)]


def test_hq_hourly_demand_and_generation_separately():
    t0 = datetime(2026, 11, 3, 5)                                                    # 00:00 EST
    routes = {"demande-electricite-quebec": demand_rows(t0, 10, 20000.0)               # 2.5 hours of 15-min rows
              + [{"date": "2026-11-03T08:00:00+00:00", "valeurs_demandetotal": None}],
              "production-electricite-quebec": prod_rows(t0, 1)
              + [{"date": "2026-11-03T06:30:00+00:00", "valeurs_total": 0.0, "valeurs_hydraulique": None,
                  "valeurs_eolien": None, "valeurs_solaire": None, "valeurs_thermique": None, "valeurs_autres": None}]}

    def handler(req):
        ds = str(req.url).split("/datasets/")[1].split("/")[0]
        return httpx.Response(200, json=routes[ds])
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 3, 9))
    v = {(o.series_id, o.ts): o.value for o in build(SPECS["power_hq_hourly"]).run(c).obs}
    assert v[("hq.demand.hourly", t0)] == 20000 and v[("hq.demand.hourly", datetime(2026, 11, 3, 6))] == 20000
    assert ("hq.demand.hourly", datetime(2026, 11, 3, 7)) not in v                   # 2 of 4 quarter-hours
    assert v[("hq.gen.total.hourly", t0)] == 20101 and ("hq.gen.total.hourly", datetime(2026, 11, 3, 6)) not in v
    assert ("hq.demand.hourly", datetime(2026, 11, 3, 6)) in v                       # demand isn't held to production


def test_hq_hourly_total_does_not_wait_for_thermal():
    """HQ sends thermal 6 h late, the rest 90 min: the total and hydro are written without it (PLAN 9)."""
    t0 = datetime(2026, 11, 3, 5)
    late = prod_rows(t0, 1)[0] | {"date": "2026-11-03T06:30:00+00:00", "valeurs_thermique": None}
    routes = {"demande-electricite-quebec": demand_rows(t0, 8, 20000.0),
              "production-electricite-quebec": prod_rows(t0, 1) + [late]}

    def handler(req):
        return httpx.Response(200, json=routes[str(req.url).split("/datasets/")[1].split("/")[0]])
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 3, 9))
    v = {(o.series_id, o.ts): o.value for o in build(SPECS["power_hq_hourly"]).run(c).obs}
    h = datetime(2026, 11, 3, 6)
    assert v[("hq.gen.total.hourly", h)] == 20101 and v[("hq.gen.hydro.hourly", h)] == 18000
    assert ("hq.gen.thermal.hourly", h) not in v and ("hq.gen.thermal.hourly", t0) in v     # never a 0 for a null


def test_hq_daily_sums_dst_and_missing_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    assert len(hq.local_day_hours(date(2025, 11, 2))) == 25 and len(hq.local_day_hours(date(2025, 3, 9))) == 23
    day = date(2025, 6, 1)
    hrs = hq.local_day_hours(day)
    hist_d = [{"date": (h.replace(minute=0) + __import__("datetime").timedelta(hours=1)).isoformat() + "+00:00",
               "moyenne_mw": 20000.0} for h in hrs]
    hist_p = [{"date": None, "hydraulique": 1.0, "eolien": 1.0, "autres": 1.0, "solaire": 0.0, "thermique": 0.0,
               "total": 3.0}]                                                         # HQ's dateless 2021 rows
    routes = {"historique-demande-electricite-quebec": hist_d, "historique-production-electricite-quebec": hist_p,
              "demande-electricite-quebec": [], "production-electricite-quebec": []}

    def handler(req):
        return httpx.Response(200, json=routes[str(req.url).split("/datasets/")[1].split("/")[0]])
    spec = {**SPECS["power_hq_daily"], "params": {"report": "daily", "from_date": "2020-01-01"}}
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 3, 12))
    res = build(spec).run(c)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v[("hq.demand.daily", datetime(2025, 6, 1))] == pytest.approx(20000 * 24)
    assert ("hq.gen.total.daily", datetime(2025, 6, 1)) not in v                     # no dated generation: no value
    assert any("demand but no generation" in w for w in res.warnings)


def ie_row(ts: str, on=None, ny=None, ne=None, nb=None) -> dict:
    """A row of HQ's importations-exportations-avec-transits export (live shape, 2026-11-07): net export per market,
    its mirror in importations_sources_<m>_total."""
    r = {"date": ts, "exportations_total": None}
    for f, v in (("ontario", on), ("newyork", ny), ("newengland", ne), ("newbrunswick", nb)):
        r[f"exportations_{f}"] = v
        r[f"importations_sources_{f}_total"] = 0.0 if v is None else max(-v, 0.0)
    return r


def ie_context(rows: list[dict]) -> Context:
    def handler(req):
        assert "/datasets/importations-exportations-avec-transits/exports/json" in str(req.url)
        return httpx.Response(200, json=rows)
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 7, 20))


def test_hq_interchange_net_needs_on_ny_ne_and_skips_placeholders():
    rows = [ie_row("2026-11-07T03:00:00+00:00", on=-665.1, ny=0.0, ne=-389.9),             # Quebec importing
            ie_row("2026-11-07T07:00:00+00:00", on=-420.5, ny=0.0, ne=99.1),
            ie_row("2026-11-06T20:00:00+00:00", on=-568.9, ny=None, ne=143.4),            # NY missing: no net
            ie_row("2026-11-08T01:00:00+00:00")]                                          # placeholder ahead
    v = {(o.series_id, o.ts): o.value for o in build(SPECS["power_hq_interchange"]).run(ie_context(rows)).obs}
    assert v[("hq.intertie.net.flow", datetime(2026, 11, 7, 3))] == pytest.approx(-1055.0)
    assert v[("hq.intertie.net.flow", datetime(2026, 11, 7, 7))] == pytest.approx(-321.4)
    assert ("hq.intertie.net.flow", datetime(2026, 11, 6, 20)) not in v                   # never a 0 for a null
    assert v[("hq.intertie.ontario.flow", datetime(2026, 11, 6, 20))] == -568.9
    assert not any(s == "hq.intertie.new_brunswick.flow" for s, _ in v)                   # null: not written
    assert not any(t == datetime(2026, 11, 8, 1) for _, t in v)


def test_hq_interchange_fails_closed():
    from montney.connectors.gas_common import FormatDrift
    bad = ie_row("2026-11-07T03:00:00+00:00", on=-665.1, ny=0.0, ne=-389.9)
    del bad["exportations_newengland"]
    for rows in ([bad], [ie_row("2026-11-07T03:00:00+00:00", on=-66510.0, ny=0.0, ne=0.0)],
                 [ie_row("2026-11-08T01:00:00+00:00")]):
        with pytest.raises(FormatDrift):
            build(SPECS["power_hq_interchange"]).fetch(ie_context(rows))


def test_qc_interchange_line_uses_the_declared_net():
    from montney import regions
    qc = regions.load()["qc"]
    declared = {m.series_id for m in build(SPECS["power_hq_interchange"]).declared_series()}
    part = qc.power["interchange"].parts[0]
    assert part.series in declared and part.sign == -1                                   # + export -> + import
    assert qc.power["interchange"].lag and qc.power["generation"].lag


def test_quebec_box_and_regions():
    feats = json.loads((ROOT / "network/power/quebec.geojson").read_text(encoding="utf-8"))["features"]
    p = feats[0]["properties"]
    declared = {m.series_id for s in ("power_hq_hourly", "power_hq_daily", "power_hq_interchange")
                for m in build(SPECS[s]).declared_series()}
    assert p["id"] == "power.region.qc" and p["value"] == "hq.demand.hourly"
    assert {r["series"] for r in p["callouts_extra"]} | set(p["panel"]["series"]) <= declared
    assert {SPECS[s]["region"] for s in ("power_hq_hourly", "power_hq_daily", "power_hq_interchange")} == {"qc"}
    assert {SPECS[s]["region"] for s in ("power_ieso_hourly", "power_ieso_daily")} == {"ont"}
    for s in ("power_hq_hourly", "power_hq_interchange", "power_ieso_hourly"):
        assert {m.extra["tier"] for m in build(SPECS[s]).declared_series()} == {"live"}
