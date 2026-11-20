"""California (CAISO) demand + generation by fuel (L-090), mocked Today's Outlook CSVs (shape checked 2026-11-03)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import power_caiso as ca
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
HEAD = "Time,Solar,Wind,Geothermal,Biomass,Biogas,Small hydro,Coal,Nuclear,Natural Gas,Large Hydro,Batteries,Imports,Other"
ROW = "100,200,700,150,140,100,5,1100,8000,1000,-500,3000,0"          # supply = 13,995; gen = 10,995
NOW = datetime(2026, 11, 3, 22, 20)                                    # 14:20 PST on 2026-11-03


def fuel(n: int, row: str = ROW, head: str = HEAD) -> str:
    return head + "\n" + "\n".join(f"{i // 12:02d}:{i % 12 * 5:02d},{row}" for i in range(n)) + "\n"


def demand(n: int, mw: int = 15000) -> str:
    rows = [f"{i // 12:02d}:{i % 12 * 5:02d},1,1,{mw},," for i in range(n)]
    return "Time,Day ahead forecast,Hour ahead forecast,Current demand,Demand response\n" + "\n".join(
        rows + ["00:00,1,1,,"]) + "\n"                                 # the next day's 00:00 forecast row: dropped


def ctx(routes: dict[str, str], seen: list | None = None) -> Context:
    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if seen is not None:
            seen.append(u.split("/outlook/")[1])
        for k, body in routes.items():
            if u.endswith(k):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)


def test_parse_groups_totals_and_pacific_time():
    pts = ca.parse_day(date(2026, 11, 3), fuel(3), demand(3))
    t0 = datetime(2026, 11, 3, 8)                                      # 00:00 PST = 08:00 UTC
    assert sorted(pts)[0] == t0 and len(pts) == 3
    r = pts[t0]
    assert r["hydro"] == 1100 and r["biomass"] == 290 and r["other"] == 5 and r["batteries"] == -500
    assert r["gen"] == 10995 and r["supply"] == 13995 and r["demand"] == 15000
    assert sorted(ca.parse_day(date(2026, 7, 1), fuel(1), demand(1)))[0] == datetime(2026, 7, 1, 7)   # PDT


def test_blank_rows_skipped_and_unknown_column_fails():
    text = fuel(2) + "00:10," + ",".join([""] * 13) + "\n"             # the rest of today: blank
    assert len(ca.parse_day(date(2026, 11, 3), text, demand(3))) == 2
    with pytest.raises(FormatDrift, match="unknown column"):
        ca.parse_day(date(2026, 11, 3), fuel(1, ROW + ",9", HEAD + ",Hydrogen"), demand(1))


def test_hourly_complete_hours_and_yesterday_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    routes = {"history/20261102/fuelsource.csv": fuel(288), "history/20261102/demand.csv": demand(288),
              "current/fuelsource.csv": fuel(30), "current/demand.csv": demand(30)}
    seen: list = []
    for _ in range(2):
        res = build(SPECS["power_caiso_hourly"]).run(ctx(routes, seen))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v[("caiso.demand.hourly", datetime(2026, 11, 3, 9))] == 15000
    assert ("caiso.demand.hourly", datetime(2026, 11, 3, 10)) not in v          # 30 rows: hour 3 has 6 of 12
    assert len({ts for (_, ts) in v}) == 24 + 2 and v[("caiso.supply.total.hourly", datetime(2026, 11, 2, 8))] == 13995
    assert seen.count("history/20261102/fuelsource.csv") == 1                  # final: read once, then cached
    assert seen.count("current/fuelsource.csv") == 2
    assert {m.extra["tier"] for m in res.meta} == {"live"} and SPECS["power_caiso_hourly"]["mode"] == "live"


def test_daily_sums_and_bounded_backfill(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    routes = {f"history/202611{d:02d}/{f}": (fuel(288) if f == "fuelsource.csv" else demand(288))
              for d in (1, 2) for f in ("fuelsource.csv", "demand.csv")}
    routes |= {f"history/202610{d:02d}/{f}": (fuel(288) if f == "fuelsource.csv" else demand(288))
               for d in range(1, 32) for f in ("fuelsource.csv", "demand.csv")}
    spec = {**SPECS["power_caiso_daily"], "params": {"report": "daily", "from_date": "2026-10-28", "backfill_per_run": 2}}
    seen: list = []
    res = build(spec).run(ctx(routes, seen))
    days = sorted({o.ts for o in res.obs})
    assert days == [datetime(2026, 10, 31), datetime(2026, 11, 1), datetime(2026, 11, 2)]   # yesterday + 2 back
    assert len(seen) == 6 and any("still to read" in w for w in res.warnings)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v[("caiso.demand.daily", datetime(2026, 11, 2))] == pytest.approx(15000 * 24)
    assert v[("caiso.gen.gas.daily", datetime(2026, 11, 2))] == pytest.approx(8000 * 24)
    seen.clear()
    res = build(spec).run(ctx(routes, seen))                               # next run: only the 2 older days are read
    assert len(seen) == 4 and len({o.ts for o in res.obs}) == 5            # every cached day is re-emitted
    assert {m.unit for m in res.meta} == {"MWh/d"}


def test_california_box_uses_declared_series():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    declared = {m.series_id for s in ("power_caiso_hourly", "power_caiso_daily") for m in build(SPECS[s]).declared_series()}
    feats = json.loads((Path(network.__file__).resolve().parents[1] / "network/power/california.geojson")
                       .read_text(encoding="utf-8"))["features"]
    p = feats[0]["properties"]
    assert p["id"] == "power.region.ca" and p["value"] == "caiso.demand.hourly"
    assert set(p["panel"]["series"]) <= declared
    assert {r["series"] for r in p["callouts_extra"]} == {f"caiso.gen.{g}.hourly" for g in ca.GROUPS}
    assert all(r["share"]["of"] == "caiso.supply.total.hourly" for r in p["callouts_extra"])
    assert all(SPECS[s].get("region") == "cal" for s in ("power_caiso_hourly", "power_caiso_daily"))


def test_half_blank_row_is_skipped_not_zeroed():
    """A row with one blank cell is skipped: summed as 0 it would read as a real 5-min point."""
    text = fuel(1) + "00:05,100,,700,150,140,100,5,1100,8000,1000,-500,3000,0\n"
    assert len(ca.parse_day(date(2026, 11, 3), text, demand(2))) == 1


def test_fall_back_day_is_flagged(tmp_path, monkeypatch):
    """Reviewer on #350: 2026-11-01 has 25 h but CAISO's file 24 h of rows, so its sums are ~4 % low: say so."""
    assert (ca.day_hours(date(2026, 11, 1)), ca.day_hours(date(2026, 3, 8)), ca.day_hours(date(2026, 11, 2))) == (25, 23, 24)
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    routes = {f"history/202611{d:02d}/{f}": (fuel(288) if f == "fuelsource.csv" else demand(288))
              for d in (1, 2) for f in ("fuelsource.csv", "demand.csv")}
    spec = {**SPECS["power_caiso_daily"], "params": {"report": "daily", "from_date": "2026-11-01", "backfill_per_run": 1}}
    res = build(spec).run(ctx(routes))
    assert [w for w in res.warnings if "25-hour" in w] and all("2026-11-01" in w for w in res.warnings if "25-hour" in w)
