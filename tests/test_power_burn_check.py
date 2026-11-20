"""5b last item: NGTL delivery stations for the 6 GHGRP-validated plants, and the monthly burn check (DECISIONS P5b).

* Connector (montney/connectors/power_ngtl_delivery.py): synthetic delivery workbooks shaped like TC's. One row is
  hand-copied from TC's real "12-2025 Station Percent Utilization – Delivery" file: Shepard 31127, Sep 2025, 86.3 % of
  4,295,820 GJ, IT 0 = 3,707,293 GJ, i.e. 111.9 MMcf/d at the L-031 fallback 39.0 MJ/m3.
* Burn check (montney/power_report.burn_check): a seeded DuckDB store, read through store.series like the CLI.
* Map: the link properties on network/power/plants.geojson.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from montney import network
from montney import power_report as pr
from montney.connectors import build, load_specs
from montney.connectors import power_ngtl_delivery as nd
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta, Store

SPECS = load_specs(include_disabled=True)
SPEC = SPECS["power_ngtl_deliveries"]
PAGE = "https://www.tccustomerexpress.com/2883.html"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
NOW = datetime(2026, 11, 1, 18, 0)
AB = ZoneInfo("America/Edmonton")


# ------------------------------------------------------------------ connector
def delivery_rows(year=2025, months=12, as_of="As of: 1/20/2026 2:54:29 PM", title="NGTL Station Percent Utilization – Delivery",
                  drop=()):
    hdr = [None] * 5 + sum(([f"{MONTHS[m]} {year}", None, None] for m in range(months)), [])
    sub = ["Station Number", "Name", None, "Segment", None] + ["%", "Demand", "IT"] * months
    rows = [[title], [], ["The following report provides ..."], [], hdr, sub]
    shepard = [[50.0, 4_000_000.0, 0.0]] * months
    if months >= 9:
        shepard[8] = [86.3, 4_295_820.0, 0.0]                      # hand-copied: TC 12-2025 file, Sep 2025
    stations = {31127: ("SHEPARD ENERGY CENTRE SALES - APS", sum(shepard, [])),
                31016: ("CALGARY ENERGY CENTRE SALES APS", [50, 1_700_000, 0] * months),
                3686: ("SHEERNESS NO 2 SALES", [10, 4_600_000, 0] * months),
                31021: ("CLOVERBAR POWER PLANT SALES APN", [30, 1_085_000, 0] * months),
                31017: ("CARSELAND COGEN SALES APS", [90, 713_000, 0] * months),
                6142: ("KEEPHILLS 4 SALES", [80, 2_500_000, 0] * months),
                32759: ("KEEPHILLS EXPANSION DELIVERY APN", [40, 2_700_000, 0] * months),
                31046: ("KEEPHILLS 3 SALES APN", [15, 31_000, 0] * months),
                31064: ("REDWATER COGEN SALES APN", [99, 999_999, 0] * months)}   # not configured: must be ignored
    for st, (nm, vals) in stations.items():
        if st not in drop:
            rows.append([st, nm, None, "WGAT", None] + list(vals))
    rows += [[], [as_of]]
    return rows


def run(monkeypatch, files: dict[int, list], params=None, now=NOW):
    hrefs = {y: f"/docs/x/{y}-Delivery.xlsx" for y in files}
    page = "".join(f'<a href="{h}">{y}&nbsp;NGTL Station Percent Utilization - Delivery</a>'
                   f'<a href="/docs/x/{y}-R.xlsx">{y} NGTL Station Percent Utilization - Receipt</a>' for y, h in hrefs.items())

    def handler(req):
        return httpx.Response(200, text=page) if str(req.url).endswith("2883.html") else httpx.Response(200, content=b"x")

    by_url = {f"https://www.tccustomerexpress.com{h}": files[y] for y, h in hrefs.items()}
    monkeypatch.setattr(nd, "read_sheet", lambda url, data: by_url[url])
    monkeypatch.setattr(nd, "local_today", lambda: now.date())
    spec = {**SPEC, "params": {**SPEC["params"], **(params or {})}}
    return build(spec).run(Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now))


def test_hand_checked_shepard_sep_2025(monkeypatch):
    res = run(monkeypatch, {2025: delivery_rows(), 2026: delivery_rows(2026, 9, "As of: 10/20/2026 1:27:03 PM")})
    s = {(o.series_id, o.ts): o.value for o in res.obs}
    gj = 0.863 * 4_295_820
    assert s[("ngtl.station.31127.delivery", datetime(2025, 9, 1))] == pytest.approx(gj / (39.0 * 0.0283168466) / 30 / 1000)
    assert s[("ngtl.station.31127.delivery", datetime(2025, 9, 1))] == pytest.approx(111.9, abs=0.05)
    assert s[("ngtl.heat_value.delivery", datetime(2025, 9, 1))] == pytest.approx(1.1044, abs=1e-4)
    keep = sum(s[(f"ngtl.station.{x}.delivery", datetime(2025, 9, 1))] for x in (6142, 32759, 31046))
    assert s[("power.plant.keephills.ngtl_delivery", datetime(2025, 9, 1))] == pytest.approx(keep)
    assert not any("31064" in o.series_id for o in res.obs)                      # an unconfigured station is ignored
    assert not any(o.ts == datetime(2026, 10, 1) for o in res.obs)               # Oct 2026 not over at "As of": no data
    meta = {m.series_id: m for m in res.meta}
    assert meta["ngtl.heat_value.delivery"].extra["fallback"] is True
    assert any("fallback" in w for w in res.warnings)


def test_seeded_red_receipt_heat_value_is_refused(monkeypatch):
    with pytest.raises(FormatDrift, match="outside"):
        run(monkeypatch, {2026: delivery_rows(2026, 9, "As of: 10/20/2026 1:27:03 PM")}, params={"hv_mj_m3": 40.9})


def test_seeded_red_receipt_file_is_refused(monkeypatch):
    rows = delivery_rows(2026, 9, "As of: 10/20/2026 1:27:03 PM", title="NGTL Station Percent Utilization – Receipt")
    with pytest.raises(FormatDrift, match="not a delivery"):
        run(monkeypatch, {2026: rows})


def test_missing_station_warns_and_its_plant_gets_nothing(monkeypatch):
    res = run(monkeypatch, {2026: delivery_rows(2026, 9, "As of: 10/20/2026 1:27:03 PM", drop=(32759,))})
    assert any("32759" in w for w in res.warnings)
    assert not any(o.series_id == "power.plant.keephills.ngtl_delivery" for o in res.obs)   # fail closed (derive)
    assert any(o.series_id == "power.plant.shepard.ngtl_delivery" for o in res.obs)


# ------------------------------------------------------------------ burn check
def month_utc(y, m):
    a = datetime(y, m, 1, tzinfo=AB).astimezone(timezone.utc).replace(tzinfo=None)
    b = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=AB).astimezone(timezone.utc).replace(tzinfo=None)
    return a, b


def seed(tmp_path, est: dict, act: dict, hv=1.1044):
    """est: {plant: (fraction of the month covered, mean, running fraction)}; act: {plant: MMcf/d}. Month: Sep 2026."""
    st = Store(tmp_path / "b.duckdb")
    a, b = month_utc(2026, 9)
    metas, obs = [], []
    for p, (cov, mean, run_frac) in est.items():
        sid = f"power.plant.{p}.gas_burn.est"
        metas.append(SeriesMeta(sid, "aeso_ets_units", sid, "MMcf/d", "5min", "x"))
        n = int((b - a).total_seconds() / 300 * cov)
        off = int(n * (1 - run_frac))
        for i in range(n):
            t = a + timedelta(minutes=5 * i)
            v = 0.0 if i < off else mean + (1 if i % 2 else -1)                 # changes every point (change-only safe)
            obs.append(Obs(sid, t, v + (1e-6 * i if i < off else 0), t))
    for p, v in act.items():
        sid = f"power.plant.{p}.ngtl_delivery"
        metas.append(SeriesMeta(sid, "power_ngtl_deliveries", sid, "MMcf/d", "M", "x"))
        obs.append(Obs(sid, datetime(2026, 9, 1), v, datetime(2026, 10, 20)))
    metas.append(SeriesMeta("ngtl.heat_value.delivery", "power_ngtl_deliveries", "hv", "GJ/Mcf", "M", "x"))
    obs.append(Obs("ngtl.heat_value.delivery", datetime(2026, 9, 1), hv, datetime(2026, 10, 20)))
    st.upsert_meta(metas)
    st.write(obs)
    return st


def check(tmp_path, est, act):
    st = seed(tmp_path, est, act)
    rep = pr.burn_check(st, datetime(2026, 10, 25, 12, 0), months=1)
    st.close()
    assert rep["months"] == ["2026-09"]
    return {p: rows["2026-09"] for p, rows in rep["plants"].items()}


def test_burn_check_statuses(tmp_path):
    r = check(tmp_path,
              est={"shepard": (1.0, 100.0, 1.0), "calgary_energy_centre": (1.0, 40.0, 1.0),
                   "sheerness": (1.0, 10.0, 0.6), "clover_bar": (0.5, 10.0, 1.0), "carseland": (1.0, 19.0, 1.0),
                   "keephills": (1.0, 70.0, 1.0)},
              act={"shepard": 100.0, "calgary_energy_centre": 30.0, "sheerness": 12.0, "clover_bar": 10.0,
                   "carseland": 19.0})
    assert r["shepard"]["status"] == "ok" and abs(r["shepard"]["gap"]) < 0.01                  # HV-aware, GJ vs GJ
    assert r["calgary_energy_centre"]["status"] == "FLAG" and r["calgary_energy_centre"]["gap"] > 0.10
    assert r["sheerness"]["status"] == "not steady"                                              # starts: never flagged
    assert r["clover_bar"]["status"] == "NO-RESULT" and "coverage" in r["clover_bar"]["reason"]
    assert r["carseland"]["status"] == "ok" and abs(r["carseland"]["gap"]) < 0.01               # cogen: site fuel vs site fuel
    assert r["keephills"]["status"] == "NO-RESULT" and "no NGTL delivery" in r["keephills"]["reason"]


def test_seeded_red_unbounded_hold_hides_a_gap(tmp_path, monkeypatch):
    """With no hold cap, half a month of data would pass as a full month: the coverage test must catch that."""
    monkeypatch.setattr(pr, "MAX_HOLD_H", 24 * 40)              # a 40-day hold: half a month "covers" all of it
    r = check(tmp_path, est={"clover_bar": (0.5, 10.0, 1.0)}, act={"clover_bar": 10.0})
    assert r["clover_bar"]["status"] != "NO-RESULT"          # i.e. the cap is what makes the check fail closed


def test_held_mean_exact():
    h = pr.held_mean([0, 100, 200], [1.0, 3.0, 5.0], 0, 400, 150)
    assert h["mean"] == pytest.approx((1 * 100 + 3 * 100 + 5 * 150) / 350) and h["coverage"] == pytest.approx(350 / 400)
    assert pr.held_mean([], [], 0, 10, 5) == {"mean": None, "coverage": 0.0, "running": None}


def test_format_is_ascii(tmp_path):
    st = seed(tmp_path, {"shepard": (1.0, 100.0, 1.0)}, {"shepard": 100.0})
    pr.format_burn_check(pr.burn_check(st, datetime(2026, 10, 25), months=2)).encode("cp1252")
    st.close()


# ------------------------------------------------------------------ map links
def plants() -> dict[str, dict]:
    fc = json.loads((Path(__file__).resolve().parents[1] / "network" / "power" / "plants.geojson").read_text("utf-8"))
    return {f["properties"]["id"].removeprefix("power.plant."): f["properties"] for f in fc["features"]}


def test_map_links_match_the_spec():
    assert not network.load()["errors"]
    ps = plants()
    spec_stations: dict[str, set[int]] = {}
    for st, slug in SPEC["params"]["stations"].items():
        spec_stations.setdefault(slug, set()).add(int(st))
    assert set(spec_stations) == set(pr.VALIDATED)
    derives = {d["id"] for d in SPEC["derive"]}
    for slug, sts in spec_stations.items():
        nd_ = ps[slug]["ngtl_delivery"]
        assert {int(l["to"].split(".")[2]) for l in nd_["links"]} == sts, slug
        assert all(l["to"] == f"ngtl.station.{l['alias'].split(':')[1]}" and l["kind"] == "supplies_gas_to"
                   and l["confidence"] == "validated:ghgrp" for l in nd_["links"])
        assert nd_["series"] in derives and nd_["series"] in ps[slug]["panel"]["series"]
        if "callout" in ps[slug]:
            assert any(r["series"] == nd_["series"] for r in ps[slug]["callouts_extra"])
    for slug in ("milner", "battle_river", "genesee"):                           # context only: no series, no check
        c = ps[slug]["ngtl_delivery_context"]
        assert all(l["confidence"] == "context" for l in c["links"]) and "ngtl_delivery" not in ps[slug]
    assert all("ngtl_delivery" not in p for s, p in ps.items() if s not in spec_stations)
