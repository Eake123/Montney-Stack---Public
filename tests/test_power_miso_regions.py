"""MISO by region (L-100): Midwest (LRZ 1-7) and South (LRZ 8-10) load from MISO's daily df_al.xls.

The fixture tests/fixtures/power_miso_df_al.xls is SYNTHETIC (made with xlwt, no real data) in the live report's
shape (checked on the 2026-11-02 and 2026-11-03 files): headers per market day, zone MTLF/ActualLoad column pairs plus
the MISO total, yesterday with actuals, and a flat placeholder last day.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import power_miso_regions as mr
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
FIX = (Path(__file__).parent / "fixtures" / "power_miso_df_al.xls").read_bytes()
NOW = datetime(2026, 11, 3, 18, 20)            # 13:20 EST on 2026-11-03


def run(tmp_path, monkeypatch, routes: dict[str, bytes | int]):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    seen = []

    def handler(req):
        name = str(req.url).rsplit("/", 1)[1]
        seen.append(name)
        body = routes.get(name, 404)
        return httpx.Response(body, text="missing") if isinstance(body, int) else httpx.Response(200, content=body)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    return build(SPECS["power_miso_regions"]).run(c), seen, c


def test_regions_sum_their_zones_and_match_miso():
    d = mr.parse_df_al(FIX)
    cell = d[date(2026, 11, 2)][1]
    assert cell["fc"]["midwest"] == 10010 + 15010 + 9010 + 4010 + 9010 and cell["fc"]["south"] == 20010
    assert cell["act"]["midwest"] == cell["fc"]["midwest"] + 25 and "act" not in d[date(2026, 11, 3)][1]
    assert cell["fc"]["midwest"] + cell["fc"]["south"] == cell["fc"]["miso"]
    assert cell["fc"]["umw"] == 10010 + 15010 and cell["act"]["umw"] == cell["fc"]["umw"] + 10   # LRZ1 + LRZ2_7 (PLAN 9)


def test_umw_hourly_actuals_for_its_load_line(tmp_path, monkeypatch):
    """PLAN 9: umw's load line = MISO's actual hourly load for LRZ1 + LRZ2_7, yesterday's 24 hours (day-late)."""
    res, _, _ = run(tmp_path, monkeypatch, {"20261103_df_al.xls": FIX})
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    act = sorted(t for s, t in v if s == "miso.umw.load.actual")
    assert len(act) == 24 and act[0] == datetime(2026, 11, 2, 5) and act[-1] == datetime(2026, 11, 3, 4)   # EST day
    assert v[("miso.umw.load.actual", act[0])] == 10010 + 15010 + 10
    assert all(("miso.midwest.load.actual", t) in v for t in act)
    from montney import regions
    umw = regions.load()["umw"]
    declared = {m.series_id for m in build(SPECS["power_miso_regions"]).declared_series()}
    assert {p.series for p in umw.power["load"].parts} <= declared and "generation" not in umw.power


def test_series_forecast_this_hour_yesterday_actual_and_placeholder_skipped(tmp_path, monkeypatch):
    res, seen, ctx = run(tmp_path, monkeypatch, {"20261103_df_al.xls": FIX})
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    he14 = datetime(2026, 11, 3, 18)                                   # HE14 EST = 13:00-14:00 EST = 18:00 UTC
    assert v[("miso.midwest.load.forecast", he14)] == 47000 + 5 * 140 and v[("miso.south.load.forecast", he14)] == 20140
    y = datetime(2026, 11, 2)
    assert v[("miso.midwest.load.actual_mean", y)] == pytest.approx(47000 + 5 * 125 + 25)   # mean of HE1..24
    assert v[("miso.midwest.load.actual_peak", y)] == 47000 + 5 * 240 + 25
    assert v[("miso.south.load.forecast_peak", datetime(2026, 11, 3))] == 20240
    assert ("miso.midwest.load.forecast_peak", y) not in v                 # a past day's forecast peak isn't shown
    assert not [k for k in v if k[1] >= datetime(2026, 11, 4, 5) and k[0].endswith(".forecast")]   # flat day (EST) skipped
    assert ("miso.midwest.load.forecast", datetime(2026, 11, 4, 4)) in v                 # HE24 EST of the 3rd is kept
    assert any("placeholder" in w for w in res.warnings)
    assert seen == ["20261103_df_al.xls"] and ctx.n_requests == 1         # counted in fetch_run (L-081)
    assert {m.extra["tier"] for m in res.meta} == {"live"} and SPECS["power_miso_regions"]["region"] == "midwest"


def test_cached_and_not_yet_published(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, {"20261103_df_al.xls": FIX})
    _, seen, _ = run(tmp_path, monkeypatch, {})
    assert seen == []                                                   # today's file is cached: 0 requests
    (tmp_path / "cache" / "miso" / "20261103_df_al.xls").rename(tmp_path / "cache" / "miso" / "20261102_df_al.xls")
    res, seen, _ = run(tmp_path, monkeypatch, {})                       # today's not published: yesterday's cached file
    assert seen == ["20261103_df_al.xls"] and any("not published yet" in w for w in res.warnings) and res.obs


def test_unknown_zone_or_bad_file_fails_closed():
    with pytest.raises(FormatDrift, match="readable"):
        mr.parse_df_al(b"not an xls")
    real = mr.REGIONS
    try:
        mr.REGIONS = {"midwest": ("LRZ1", "LRZ2_7", "LRZ3_5", "LRZ4"), "south": ("LRZ8_9_10",)}   # LRZ6 unmapped
        mr.ZONES = {z for zs in mr.REGIONS.values() for z in zs}
        with pytest.raises(FormatDrift, match="unknown zone column"):
            mr.parse_df_al(FIX)
    finally:
        mr.REGIONS = real
        mr.ZONES = {z for zs in real.values() for z in zs}


def test_boxes_overview_midwest_forecast_labelled_and_mix_miso_wide():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    feats = {f["properties"]["id"]: f["properties"] for f in json.loads(
        (Path(network.__file__).resolve().parents[1] / "network/power/miso.geojson").read_text(encoding="utf-8"))["features"]}
    declared = {m.series_id for s in ("power_miso", "power_miso_regions") for m in build(SPECS[s]).declared_series()}
    mid, south, allr = feats["power.region.miso_midwest"], feats["power.region.miso_south"], feats["power.region.miso"]
    assert mid["callout"]["priority"] == 1 and south["callout"]["priority"] == 2 and allr["callout"]["priority"] == 2
    for p in (mid, south):
        assert "forecast" in p["callout"]["label"] and p["value"].endswith(".load.forecast")
        assert {r["series"] for r in p["callouts_extra"]} | set(p["panel"]["series"]) <= declared
    mix = [r for r in allr["callouts_extra"] if "share" in r]
    assert mix and all("MISO-wide" in r["label"] and "MISO-wide" in r["share"]["label"] for r in mix)
