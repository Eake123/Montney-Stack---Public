"""Neighbouring provinces' generation, total + by type (L-078): IESO (Ontario) and StatCan 25-10-0015 (SK, MB).

Mocked payloads shaped like the live reports (checked 2026-11-02): IESO GenOutputCapability / GenOutputbyFuelHourly
XML and the StatCan WDS JSON. Ground truth: IESO's per-generator sum equals its fuel report (2026-11-01, every fuel,
0 MWh off), and StatCan's SK Aug 2026 types add up to its total, 2,246,082 MWh.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import power_ieso as ieso
from montney.connectors import power_statcan_gen as sc
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
NOW = datetime(2026, 11, 3, 2, 20)            # 21:20 EST on 2026-11-02


def gen(name: str, fuel: str, outs: dict[int, str]) -> str:
    o = "".join(f"<Output><Hour>{h}</Hour><EnergyMW>{v}</EnergyMW></Output>" for h, v in outs.items())
    return f"<Generator><GeneratorName>{name}</GeneratorName><FuelType>{fuel}</FuelType><Outputs>{o}</Outputs></Generator>"


def capability(d: str, gens: list[str]) -> str:
    return f"<Document><DocBody><Date>{d}</Date><Generators>{''.join(gens)}</Generators></DocBody></Document>"


def fuel_report(year: int, days: dict[str, dict[int, dict[str, str]]]) -> str:
    body = ""
    for d, hours in days.items():
        hs = "".join(f"<HourlyData><Hour>{h}</Hour>" + "".join(
            f"<FuelTotal><Fuel>{f}</Fuel><EnergyValue><OutputQuality>0</OutputQuality><Output>{v}</Output>"
            f"</EnergyValue></FuelTotal>" for f, v in row.items()) + "</HourlyData>" for h, row in hours.items())
        body += f"<DailyData><Day>{d}</Day>{hs}</DailyData>"
    return f"<Document><DocBody><DeliveryYear>{year}</DeliveryYear>{body}</DocBody></Document>"


def ctx(routes: dict[str, str | dict]) -> Context:
    def handler(req: httpx.Request) -> httpx.Response:
        for key, body in routes.items():
            if str(req.url).endswith(key):
                return httpx.Response(200, json=body) if isinstance(body, (list, dict)) else httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)


# ------------------------------------------------------------------ IESO hourly (live)
def test_ieso_hours_are_est_hour_ending():
    assert ieso.hour_start_utc(date(2026, 11, 2), 1) == datetime(2026, 11, 2, 5)      # HE1 EST = 00-01 EST
    assert ieso.hour_start_utc(date(2026, 7, 1), 24) == datetime(2026, 7, 2, 4)       # EST in summer too (no DST)
    assert ieso.ieso_date(NOW) == date(2026, 11, 2)


def test_ieso_hourly_sums_generators_by_fuel():
    gens = [gen("N1", "NUCLEAR", {1: "800", 2: "800", 3: "790"}), gen("N2", "NUCLEAR", {1: "700", 2: "", 3: "700"}),
            gen("G1", "GAS", {1: "300", 2: "310", 3: "320"}), gen("W1", "WIND", {1: "50", 2: "40", 3: "30"}),
            gen("S1", "SOLAR", {1: "0", 2: "0", 3: "0"}), gen("H1", "HYDRO", {1: "400", 2: "400", 3: "410"})]
    res = build(SPECS["power_ieso_hourly"]).run(ctx({
        "PUB_GenOutputCapability_20261101.xml": capability("2026-11-01", gens),
        "PUB_GenOutputCapability.xml": capability("2026-11-02", gens)}))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    t = datetime(2026, 11, 2, 6)                                              # 2026-11-02 HE2
    assert v[("ieso.gen.nuclear.hourly", t)] == 800                                  # an empty output counts as 0, like IESO
    assert v[("ieso.gen.total.hourly", t)] == 800 + 310 + 40 + 0 + 400
    assert v[("ieso.gen.biofuel.hourly", t)] == 0 and ("ieso.gen.total.hourly", datetime(2026, 11, 1, 5)) in v   # yesterday too
    assert {m.unit for m in res.meta} == {"MW"} and SPECS["power_ieso_hourly"]["mode"] == "live"


def test_ieso_unfinished_hour_left_out_and_unknown_fuel_fails():
    gens = [gen(f"G{i}", "GAS", {1: "10", 2: "10"} if i < 5 else {1: "10"}) for i in range(10)]
    _, hours, warns = ieso.parse_capability(capability("2026-11-02", gens))
    assert sorted(hours) == [1] and "left out" in warns[0]                    # hour 2: 5 of 10 generators so far
    with pytest.raises(FormatDrift, match="unknown fuel"):
        ieso.parse_capability(capability("2026-11-02", [gen("X", "COAL", {1: "1"})]))


# ------------------------------------------------------------------ IESO daily sums (stored)
CORE = {"NUCLEAR": "9000", "GAS": "3000", "HYDRO": "4000", "WIND": "1000", "SOLAR": "100", "BIOFUEL": "5"}


def test_ieso_daily_sums_with_optional_other():
    full = {h: {**CORE, "OTHER": "10", "CONTROL ACTIONS": "777"} for h in range(1, 25)}
    no_other = {h: dict(CORE) for h in range(1, 25)}
    gap = {h: dict(CORE) for h in range(1, 24)}                               # hour 24 missing
    spec = {**SPECS["power_ieso_daily"], "params": {"report": "daily", "from_year": 2026}}
    res = build(spec).run(ctx({"PUB_GenOutputbyFuelHourly.xml": fuel_report(2026, {
        "2026-10-30": full, "2026-10-31": no_other, "2026-11-01": gap})}))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    d1, d2 = datetime(2026, 10, 30), datetime(2026, 10, 31)
    assert v[("ieso.gen.nuclear.daily", d1)] == 9000 * 24
    assert v[("ieso.gen.total.daily", d1)] == (17105 + 10) * 24               # CONTROL ACTIONS isn't generation
    assert v[("ieso.gen.other.daily", d1)] == 240 and ("ieso.gen.other.daily", d2) not in v   # OTHER absent: no row
    assert v[("ieso.gen.total.daily", d2)] == 17105 * 24
    assert not [k for k in v if k[1] == datetime(2026, 11, 1)] and any("2026-11-01" in w for w in res.warnings)
    assert {m.unit for m in res.meta} == {"MWh/d"} and {m.freq for m in res.meta} == {"D"}


# ------------------------------------------------------------------ StatCan SK / MB monthly
AUG_SK = {"total": 2246082, "hydro": 387351, "fossil": 1704059, "biomass": 5455, "wind": 133203, "solar": 6365,
          "other": 9649}


def wds(values: dict[tuple[int, int], float | None], ref="2026-08-01") -> list:
    return [{"status": "SUCCESS", "object": {"coordinate": f"{g}.1.{t}.0.0.0.0.0.0.0", "vectorDataPoint": [
        {"refPer": ref, "value": v, "scalarFactorCode": 0}]}} for (g, t), v in values.items()]


def sk_payload(**over) -> list:
    vals = {(9, sc.TYPES[k]): over.get(k, v) for k, v in AUG_SK.items()}
    return wds(vals) + wds({(8, sc.TYPES[k]): 1.0 if k != "total" else 6.0 for k in AUG_SK})


def test_statcan_mean_mw_and_parts_add_up():
    res = build(SPECS["power_statcan_gen"]).run(ctx({"getDataFromCubePidCoordAndLatestNPeriods": sk_payload()}))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    aug = datetime(2026, 8, 1)
    assert v[("statcan.gen.sk.total", aug)] == pytest.approx(2246082 / 744)  # 31 days x 24 h: 3,018.9 MW
    assert res.warnings == []
    assert {m.unit for m in res.meta} == {"MW"} and {m.freq for m in res.meta} == {"M"}


def test_statcan_suppressed_is_not_zero_and_mismatch_warns():
    res = build(SPECS["power_statcan_gen"]).run(ctx({"getDataFromCubePidCoordAndLatestNPeriods":
                                                     sk_payload(wind=None)}))
    ids = {o.series_id for o in res.obs}
    assert "statcan.gen.sk.wind" not in ids and "statcan.gen.sk.total" in ids
    assert any("don't add up" in w and "sk 2026-08" in w for w in res.warnings)
    with pytest.raises(FormatDrift):
        sc.parse([{"status": "FAILED", "object": {"responseStatusCode": 2}}])


# ------------------------------------------------------------------ the map boxes
def test_province_boxes_use_declared_series_and_share_of_their_total():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    declared = {m.series_id for s in ("power_ieso_hourly", "power_ieso_daily", "power_statcan_gen",
                                      "power_ieso_interties", "power_ieso_interties_daily")
                for m in build(SPECS[s]).declared_series()}
    feats = json.loads((Path(network.__file__).resolve().parents[1] / "network/power/provinces.geojson")
                       .read_text(encoding="utf-8"))["features"]
    assert {f["properties"]["id"] for f in feats} == {"power.region.on", "power.region.sk", "power.region.mb"}
    for f in feats:
        p = f["properties"]
        assert p["value"] in declared and set(p["panel"]["series"]) <= declared
        for r in p["callouts_extra"]:
            assert r["series"] in declared, r
            if ".intertie." not in r["series"]:          # intertie rows (L-103) are flows, not shares of generation
                assert r["share"] == {"of": p["value"], "label": "% of gen"}, r
    on = next(f["properties"] for f in feats if f["properties"]["id"] == "power.region.on")
    gen_rows = {r["series"] for r in on["callouts_extra"] if ".intertie." not in r["series"]}
    assert gen_rows == {f"ieso.gen.{x}.hourly" for x in ieso.FUELS}


def test_seeded_red_counting_control_actions_is_caught(monkeypatch):
    monkeypatch.setattr(ieso, "NOT_GENERATION", set())
    with pytest.raises(FormatDrift, match="unknown fuel"):                  # it isn't a fuel: fails closed, not summed
        ieso.parse_fuel_hourly(fuel_report(2026, {"2026-10-30": {1: {**CORE, "CONTROL ACTIONS": "777"}}}))


def test_declared_tiers_match_the_specs_and_ids_are_distinct():
    """#290 bug: the catalog is seeded from declared_series() at startup, before any fetch. A meta without a tier was
    seeded from its freq (1h -> "hourly"), and the live write then failed ("tier is fixed at 'hourly'")."""
    seen = {}
    for sid in ("power_ieso_hourly", "power_ieso_daily", "power_statcan_gen"):
        for m in build(SPECS[sid]).declared_series():
            want = SPECS[sid]["retention"]
            assert (m.extra or {}).get("tier", want) == want, (m.series_id, m.extra)
            assert m.series_id not in seen, f"{m.series_id} declared by {seen.get(m.series_id)} and {sid}"
            seen[m.series_id] = sid
    hourly = {m.series_id: m for m in build(SPECS["power_ieso_hourly"]).declared_series()}
    assert all(k.endswith(".hourly") and m.extra["tier"] == "live" for k, m in hourly.items())


def test_yesterday_read_from_ieso_only_until_final(tmp_path, monkeypatch):
    """L-081: yesterday's GenOutputCapability is final once it has all 24 hours; from then on it's read from the cache
    (1 request a day, not 24), and the live window still has yesterday's hours. An unfinished day isn't cached."""
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    full = capability("2026-11-01", [gen("G1", "GAS", {h: "100" for h in range(1, 25)})])
    part = capability("2026-11-01", [gen("G1", "GAS", {h: "100" for h in range(1, 20)})])
    today = capability("2026-11-02", [gen("G1", "GAS", {1: "90", 2: "95"})])
    seen = []

    def run(yday: str) -> list:
        def handler(req):
            seen.append(str(req.url).rsplit("/", 1)[1])
            body = yday if "_20261101" in str(req.url) else today
            return httpx.Response(200, text=body)
        c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
        return build(SPECS["power_ieso_hourly"]).run(c).obs

    run(part)
    run(part)
    assert seen.count("PUB_GenOutputCapability_20261101.xml") == 2                # not final yet: read again
    seen.clear()
    run(full)
    obs = run(full)
    assert seen.count("PUB_GenOutputCapability_20261101.xml") == 1                # final: cached after one read
    assert seen.count("PUB_GenOutputCapability.xml") == 2                         # today's file every run
    assert any(o.series_id == "ieso.gen.total.hourly" and o.ts == datetime(2026, 11, 1, 5) for o in obs)
    assert [p.name for p in (tmp_path / "cache" / "ieso").glob("PUB_GenOutputCapability_*")] == [
        "PUB_GenOutputCapability_20261101.xml"]
