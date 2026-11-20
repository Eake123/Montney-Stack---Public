"""P5b implied gas burn per plant (MMcf/d = MW x HR x 24 / HHV / 1000), estimate with an HR x HHV band.

* The weighting is written down in docs/research/v2/power-heat-rates.md. Every derive weight in
  sources/aeso_ets_units.yaml is recomputed from that table, so the doc and the spec can't drift.
* Out-of-sample check (Reviewer, #150): Shepard (EGC1) HR 7.40 comes from 2024 data (GHGRP + NGTL station 31127).
  We test it on **2025** months it never saw: NGTL station 31127 monthly delivered energy (TC "12-2025 Station Percent
  Utilization – Delivery", hand-copied) against AESO metered MWh for EGC1 (keyless hourly CSV, which ends July 2025).
  Delivered GJ = %/100 x Demand + IT: TC's "%" is the firm allocation divided by the eligible monthly quantity ("Demand").
  This checks the HEAT RATE only. HHV is the same on both sides and cancels, so it is not tested here (it's L-031's
  fallback; L-032 tests the source).
* Map: plant boxes show the estimate only together with its low and high rows (same priority), never a bare number.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from montney import snapshot
from montney.connectors import build, load_specs
from montney.connectors.base import Context
from montney.store import Obs

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "research" / "v2" / "power-heat-rates.md"
PLANTS = ROOT / "network" / "power" / "plants.geojson"
FIX = Path(__file__).parent / "fixtures"
SPECS = load_specs(include_disabled=True)
UNITS = SPECS["aeso_ets_units"]
HHV, HHV_LO, HHV_HI = 1.104, 1.093, 1.116
TYPE_RANGE = {"combined_cycle": (6.0, 9.5), "simple_cycle": (9.0, 14.0), "gas_fired_steam": (9.5, 14.0),
              "cogeneration": (8.0, 16.0)}   # cogen SITE FUEL (ruling after #177)
# month: (TC "%", Demand GJ, IT GJ, AESO EGC1 metered MWh), 2025; hand-copied from the two public files
SHEPARD_2025 = {"2025-01": (87.1, 4_439_014, 0, 526_612), "2025-02": (87.3, 4_121_432, 0, 495_041),
                "2025-03": (82.4, 4_563_014, 0, 510_146), "2025-04": (70.4, 4_415_820, 0, 419_025),
                "2025-05": (87.0, 4_191_014, 0, 493_726), "2025-06": (79.5, 4_472_820, 0, 477_103),
                "2025-07": (88.9, 4_439_014, 0, 532_485)}
FLAG, STEADY = 0.10, 0.04   # DECISIONS P5b: flag a month above ±10 %; a steady month runs ±2-4 %


def table() -> dict[str, dict]:
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) == 9 and re.fullmatch(r"[A-Z0-9]+", c[1]) and c[4][:1].isdigit():
            rows[c[1]] = {"plant": c[0], "type": c[3], "hr": float(c[4]), "lo": float(c[5]), "hi": float(c[6]),
                          "basis": c[7], "source": c[8]}
    return rows


def derives(spec=UNITS) -> dict[str, dict]:
    return {d["id"]: d for d in spec.get("derive") or []}


def plants() -> list[dict]:
    return [f["properties"] for f in json.loads(PLANTS.read_text(encoding="utf-8"))["features"]]


def weight(h, hhv):
    return h * 24 / hhv / 1000


def weight_mismatches(tab: dict, ders: dict) -> list[str]:
    """Every plant's est/lo/hi derive must use exactly its units, weighted from the doc table. Fails closed."""
    if not tab:
        return ["NO-RESULT: no rows parsed from the heat-rate table"]
    bad = []
    for p in plants():
        slug = p["id"].removeprefix("power.plant.")
        for k, (col, hhv) in {"est": ("hr", HHV), "lo": ("lo", HHV_HI), "hi": ("hi", HHV_LO)}.items():
            d = ders.get(f"power.plant.{slug}.gas_burn.{k}")
            if d is None:
                bad.append(f"{slug}: no gas_burn.{k} derive")
                continue
            want = {f"aeso.unit.{u.lower()}.tng": weight(tab[u][col], hhv) for u in p["aeso_assets"] if u in tab}
            if len(want) != len(p["aeso_assets"]):
                bad.append(f"{slug}: units missing from the table: {set(p['aeso_assets']) - set(tab)}")
            if set(d["weights"]) != set(want):
                bad.append(f"{slug}.{k}: derive units {sorted(d['weights'])} != {sorted(want)}")
            bad += [f"{slug}.{k}: {s} weight {d['weights'][s]} != {w:.6f}" for s, w in want.items()
                    if s in d["weights"] and abs(float(d["weights"][s]) - w) > 2e-6]
    return bad


def test_weights_follow_the_written_down_table():
    assert weight_mismatches(table(), derives()) == []


def test_heat_rates_are_in_range_and_cited():
    tab = table()
    assert len(tab) == sum(len(p["aeso_assets"]) for p in plants())
    for u, r in tab.items():
        lo, hi = TYPE_RANGE[r["type"]]
        assert lo <= r["lo"] <= r["hr"] <= r["hi"] <= hi, (u, r)
        assert r["basis"] in ("measured", "default") and len(r["source"]) > 15, u


def shepard_gaps(w: dict[str, float]) -> dict[str, tuple[float, bool]]:
    """Per 2025 month: (estimate / delivered - 1, delivered inside [lo, hi]), with w = the EGC1 est/lo/hi weights."""
    out = {}
    for m, (pct, dem, it, mwh) in SHEPARD_2025.items():
        delivered = (pct / 100 * dem + it) / HHV / 1000            # MMcf over the month
        est, lo, hi = (mwh * w[k] / 24 for k in ("est", "lo", "hi"))  # MMcf: MWh x (MMcf/d per MW) / 24
        out[m] = (est / delivered - 1, lo <= delivered <= hi)
    return out


def shepard_problems(w: dict[str, float]) -> list[str]:
    gaps = shepard_gaps(w)
    if len(gaps) < 6:
        return ["NO-RESULT: fewer than 6 out-of-sample months"]
    bad = [f"{m}: gap {g:+.1%} beyond ±{FLAG:.0%}" for m, (g, _) in gaps.items() if abs(g) > FLAG]
    bad += [f"{m}: delivered outside [lo, hi]" for m, (_, inside) in gaps.items() if not inside]
    mean = sum(g for g, _ in gaps.values()) / len(gaps)
    if abs(mean) > STEADY:
        bad.append(f"mean gap {mean:+.1%} beyond ±{STEADY:.0%}")
    return bad


def test_shepard_out_of_sample_2025():
    """Out-of-sample: the 2024 HR against 7 months of 2025 metered NGTL delivery and AESO MWh. Observed gaps run
    -0.7 % to +1.8 %, mean +0.3 %."""
    d = derives()
    w = {k: float(d[f"power.plant.shepard.gas_burn.{k}"]["weights"]["aeso.unit.egc1.tng"]) for k in ("est", "lo", "hi")}
    assert shepard_problems(w) == [], shepard_gaps(w)


def test_fleet_weights_use_the_type_defaults():
    d = derives(SPECS["aeso_csd"])
    hr = {"combined_cycle": (7.5, 7.0, 8.6), "simple_cycle": (10.8, 9.9, 12.5), "gas_fired_steam": (11.3, 10.5, 12.6),
          "cogeneration": (11.3, 8.5, 15.0)}
    for sid, (i, hhv) in {"implied": (0, HHV), "implied_low": (1, HHV_HI), "implied_high": (2, HHV_LO)}.items():
        ws = d[f"power.ab.gas_burn.{sid}"]["weights"]
        for f, h in hr.items():
            assert abs(float(ws[f"aeso.gen.{f}.net"]) - weight(h[i], hhv)) < 2e-6, (sid, f)


def test_shipped_path_csd_to_plant_burn():
    """Real parser + spec derives: a CSD with Shepard (EGC1) in the CC block yields est/lo/hi = TNG x weights."""
    body = (FIX / "power_csd_sample.csv").read_text(encoding="utf-8").replace("Test Combined (TCC1)", "Shepard (EGC1)")
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))),
                  now=datetime(2026, 10, 31, 20, 10))
    res = build(UNITS).run(ctx)
    got = {o.series_id: o.value for o in res.obs if o.series_id.startswith("power.plant.shepard.gas_burn.")}
    tng = next(o.value for o in res.obs if o.series_id == "aeso.unit.egc1.tng")
    d = derives()
    assert set(got) == {f"power.plant.shepard.gas_burn.{k}" for k in ("est", "lo", "hi")}
    for k, v in got.items():
        assert v == pytest.approx(tng * float(d[k]["weights"]["aeso.unit.egc1.tng"]))
    assert got["power.plant.shepard.gas_burn.lo"] < got["power.plant.shepard.gas_burn.est"] < got["power.plant.shepard.gas_burn.hi"]
    # a plant with a unit missing from the fetch gets no value at all (fail closed), not a partial sum
    assert not any(o.series_id.startswith("power.plant.genesee.gas_burn") for o in res.obs)


def base(token: str) -> str:
    return token.split("@", 1)[0]


def box_problems(p: dict) -> list[str]:
    """The burn estimate may only show with both band rows, at the same priority and the same window."""
    rows = {base(r["series"]): r for r in p.get("callouts_extra", []) if ".gas_burn." in r["series"]}
    if not rows:
        return []
    keys = {k: rows.get(p["gas_burn"][k]) for k in ("est", "lo", "hi")}
    if any(v is None for v in keys.values()):
        return [f"{p['id']}: burn rows {sorted(k for k, v in keys.items() if v is None)} missing (a bare number would show)"]
    if len({v["priority"] for v in keys.values()}) != 1:
        return [f"{p['id']}: burn rows at different priorities (a bare number at some zoom)"]
    wins = {v["series"].partition("@")[2] for v in keys.values()}
    if wins != {p["gas_burn"]["window"]}:
        return [f"{p['id']}: burn rows windows {sorted(wins)} != {p['gas_burn']['window']!r} (est and band must agree)"]
    return []


def test_plant_boxes_never_show_a_bare_burn_number():
    ps = plants()
    ders = derives()
    assert sum(1 for p in ps if "callout" in p) >= 5
    for p in ps:
        assert all(p["gas_burn"][k] in ders for k in ("est", "lo", "hi")), p["id"]
        assert set(p["gas_burn"][k] for k in ("est", "lo", "hi")) <= set(p["panel"]["series"])   # panel: plain ids
        assert p["gas_burn"]["window"] == "1h"
        # The map renders agg (#158): a burn row shows agg.mean tagged "1 h mean · n 12" from the payload, or NO-RESULT
        # when the hour is thin, never the latest 5-min point (tests/test_web.py::
        # test_burn_rows_show_the_window_mean_never_the_latest_point). The tag is the single place the window appears:
        # a label never names a mean or a window (it can't contradict the data), and every burn row asks for @1h.
        for r in p.get("callouts_extra", []):
            if ".gas_burn." in r["series"]:
                assert not re.search(r"\bmean\b|\b\d+\s*(h|min|d)\b|\bhour", r["label"], re.I), (p["id"], r["label"])
                assert r["series"].endswith("@1h"), (p["id"], r["series"])
        if "callout" in p:
            assert {base(r["series"]) for r in p["callouts_extra"]} >= {p["gas_burn"][k] for k in ("est", "lo", "hi")}
        assert box_problems(p) == [], p["id"]


def test_burn_derives_declare_freq():
    """A window mean needs the series' freq (missing freq falls back to 3600 s: no 1 h mean). Shipped path."""
    body = (FIX / "power_csd_sample.csv").read_text(encoding="utf-8").replace("Test Combined (TCC1)", "Shepard (EGC1)")
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))),
                  now=datetime(2026, 10, 31, 20, 10))
    meta = {m.series_id: m for m in build(UNITS).run(ctx).meta}
    for k in ("est", "lo", "hi"):
        assert meta[f"power.plant.shepard.gas_burn.{k}"].freq == "5min", k
    assert all(d.get("freq") == "5min" for i, d in derives().items() if ".gas_burn." in i)


def _seeded_snapshot(tmp_path, freq: str | None):
    """A DuckDB store with an hour of 5-min burn points for Shepard, read back through snapshot.build with the
    geojson's own row tokens (the request map.js makes)."""
    from montney.store import SeriesMeta, Store
    p = next(x for x in plants() if x["id"] == "power.plant.shepard")
    tokens = [r["series"] for r in p["callouts_extra"] if ".gas_burn." in r["series"]]
    st = Store(tmp_path / "s.duckdb")
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    vals = {"est": 120.0, "lo": 115.0, "hi": 134.0}
    obs, metas = [], []
    for k, v in vals.items():
        sid = f"power.plant.shepard.gas_burn.{k}"
        metas.append(SeriesMeta(sid, "aeso_ets_units", sid, "MMcf/d", freq, "AESO / Gas burn (plants)"))
        for i in range(13):                                    # every 5 min over the last hour, plus one
            t = now - timedelta(minutes=5 * i)
            obs.append(Obs(sid, t, v + i, t))
    st.upsert_meta(metas)
    st.write(obs)
    snap = snapshot.build(st, {}, {}, tokens)
    st.close()
    return tokens, snap


def test_seeded_snapshot_gives_last_hour_means(tmp_path):
    tokens, snap = _seeded_snapshot(tmp_path, "5min")
    assert sorted(t.partition("@")[2] for t in tokens) == ["1h"] * 3
    for t in tokens:
        a = snap["series"][t]["agg"]
        k = base(t).rsplit(".", 1)[1]
        assert a["window"] == "1h" and a["reason"] is None and a["n"] >= a["n_min"] > 0, a
        want = {"est": 120.0, "lo": 115.0, "hi": 134.0}[k]
        assert a["mean"] == pytest.approx(want + 5.5, abs=0.6), a   # points want+0..want+11 inside the hour
    m = {base(t).rsplit(".", 1)[1]: snap["series"][t]["agg"]["mean"] for t in tokens}
    assert m["lo"] < m["est"] < m["hi"]


def test_seeded_red_missing_freq_gives_no_mean(tmp_path):
    tokens, snap = _seeded_snapshot(tmp_path, None)
    for t in tokens:
        a = snap["series"][t]["agg"]
        assert a["mean"] is None and a["reason"], a


def test_cogen_boxes_say_site_fuel():
    """Ruling after #177: the map shows gas demand, so cogen burn is total site fuel and says so."""
    for p in plants():
        rows = [r for r in p.get("callouts_extra", []) if ".gas_burn." in r["series"]]
        cogen = (p.get("fuel_primary") or p["fuel"]) == "cogeneration"
        assert (p["gas_burn"].get("kind") == "site fuel (cogen)") == cogen, p["id"]
        assert all(("site fuel" in r["label"]) == cogen for r in rows), p["id"]
    tab = table()
    assert tab["TC01"]["hr"] == 11.3 and tab["TC01"]["basis"] == "measured"            # Carseland, GHGRP 11.31 / stn 11.23
    assert all(r["hr"] >= 8.0 for r in tab.values() if r["type"] == "cogeneration")    # never the power-only 7.0 again


def test_seeded_reds():
    tab = table()
    typo = {**tab, "EGC1": {**tab["EGC1"], "hr": 74.0}}                  # a typo in the doc
    assert any("shepard.est" in b for b in weight_mismatches(typo, derives()))
    assert weight_mismatches({}, derives()) == ["NO-RESULT: no rows parsed from the heat-rate table"]
    w = {k: weight(h, hhv) for k, h, hhv in (("est", 8.3, HHV), ("lo", 8.0, HHV_HI), ("hi", 9.3, HHV_LO))}
    assert shepard_problems(w)                                            # a wrong Shepard HR (8.3) is caught
    p = next(x for x in plants() if x["id"] == "power.plant.shepard")
    q = json.loads(json.dumps(p))
    p["callouts_extra"] = [r for r in p["callouts_extra"] if base(r["series"]) != p["gas_burn"]["lo"]]
    assert box_problems(p)
    for r in q["callouts_extra"]:
        if base(r["series"]) == q["gas_burn"]["hi"]:
            r["series"] = base(r["series"])            # the high band without the window: est and band disagree
    assert any("windows" in b for b in box_problems(q))
