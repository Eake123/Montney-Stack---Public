"""ERCOT generation by fuel, demand and DC ties (PLAN 9 item 1): mocked dashboard JSONs, live shape (2026-11-13)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_ercot as pe
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
CT = ZoneInfo("America/Chicago")
# The real 09:30 CST row of 2026-11-13 (research note): fuels sum to 50,842.3 MW; North tie +222 (ERCOT: export);
# demand 50,621. Charging = the net Power Storage, so adding all three would triple-count storage.
FUEL_ROW = {"Coal and Lignite": {"gen": 5489.2}, "Hydro": {"gen": 88.0}, "Nuclear": {"gen": 3861.3},
            "Other": {"gen": 0}, "Power Storage": {"gen": -8420.9}, "Solar": {"gen": 29972.8}, "Wind": {"gen": 2820.6},
            "Natural Gas": {"gen": 17031.3}, "Power Storage Charging": {"gen": -8420.9},
            "Power Storage Discharging": {"gen": 0}}
GEN_TOTAL = 50842.3
DEMAND = 50621
TIE_ROW = {"dcE": 0, "dcN": 222, "dcL": 0, "dcR": 0}


def stamp(u: datetime) -> str:
    return u.replace(tzinfo=timezone.utc).astimezone(CT).strftime("%Y-%m-%d %H:%M:%S%z")


def utc(local: str) -> datetime:
    """'2026-11-13 00:00' Central -> naive UTC (first occurrence)."""
    return datetime.fromisoformat(local).replace(tzinfo=CT).astimezone(timezone.utc).replace(tzinfo=None)


def five_min(a: datetime, b: datetime) -> list[datetime]:
    out = []
    while a < b:
        out.append(a)
        a += timedelta(minutes=5)
    return out


def fuel_mix(stamps, row=FUEL_ROW):
    data: dict = {}
    for u in stamps:
        s = stamp(u)
        data.setdefault(s[:10], {})[s] = json.loads(json.dumps(row))
    return {"lastUpdated": stamp(stamps[-1]), "monthlyCapacity": {"Solar": 40226}, "data": data}


def supply_demand(stamps, demand=DEMAND):
    rows = [{"capacity": 87175, "demand": demand, "forecast": 0, "dstFlag": 0, "interval": u.minute,
             "hourEnding": 9, "timestamp": stamp(u), "epoch": int(u.replace(tzinfo=timezone.utc).timestamp() * 1000)}
            for u in stamps]
    nxt = stamps[-1] + timedelta(minutes=5)                       # a forecast row: never stored
    rows.append({"capacity": 87142, "demand": 1, "forecast": 1, "dstFlag": 0, "interval": 50, "hourEnding": 9,
                 "available": 88259, "timestamp": stamp(nxt),
                 "epoch": int(nxt.replace(tzinfo=timezone.utc).timestamp() * 1000)})
    return {"lastUpdated": stamp(stamps[-1]), "data": rows, "forecast": []}


def ties(stamps, row=TIE_ROW):
    return {"lastUpdated": stamp(stamps[-1]),
            "data": [{"repeatedHourFlag": "N", **row, "updateTs": int(u.replace(tzinfo=timezone.utc).timestamp())}
                     for u in stamps]}


def system_wide(day_start_utc: datetime, load=44471.59):
    def day(d0):
        return {"dayDate": stamp(d0), "data": [{"hourEnding": i + 1, "dstFlag": "N", "systemLoad": load,
                                                 "timestamp": stamp(d0 + timedelta(hours=i + 1)),
                                                 "epoch": int((d0 + timedelta(hours=i + 1)).replace(
                                                     tzinfo=timezone.utc).timestamp() * 1000)} for i in range(24)]}
    return {"lastUpdated": "", "previousDay": day(day_start_utc - timedelta(days=1)), "currentDay": day(day_start_utc),
            "nextDay": {"dayDate": "", "data": []}}


NOW = utc("2026-11-13 09:46")
TODAY = five_min(utc("2026-11-13 00:00"), utc("2026-11-13 09:45"))          # the 09:00 hour lacks 09:45 and 09:50
BOTH = five_min(utc("2026-11-12 00:00"), utc("2026-11-13 09:45"))           # fuel-mix: yesterday + today


def files(fuel=None, demand=None, tie=None, swd=None):
    return {"fuel-mix": fuel_mix(BOTH) if fuel is None else fuel,
            "supply-demand": supply_demand(TODAY) if demand is None else demand,
            "dc-tie-flows": ties(TODAY) if tie is None else tie,
            "system-wide-demand": system_wide(utc("2026-11-13 00:00")) if swd is None else swd}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(pe, "PAUSE_S", 0)


def run(payloads, now=NOW, log=None):
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if log is not None:
            log.append((name, req.headers.get("if-none-match")))
        body = payloads[name]
        if isinstance(body, str):
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        tag = f'"{name}-{hash(json.dumps(body, sort_keys=True)) & 0xffff:x}"'
        if req.headers.get("if-none-match") == tag:
            return httpx.Response(304)
        return httpx.Response(200, json=body, headers={"etag": tag})

    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)
    return build(SPECS["power_ercot"]).run(c)


def values(res):
    return {(o.series_id, o.ts): o.value for o in res.obs}


def test_hourly_means_storage_net_tie_sign_and_balance(env):
    log = []
    v = values(run(files(), log=log))
    h = utc("2026-11-13 08:00")
    assert v[("ercot.gen.battery", h)] == -8420.9                     # net storage, never + charging + discharging
    assert v[("ercot.gen.gas", h)] == 17031.3 and v[("ercot.gen.coal", h)] == 5489.2
    assert v[("ercot.gen.total", h)] == GEN_TOTAL
    assert v[("ercot.tie.north.flow", h)] == -222                     # ERCOT's +222 export -> -222 net import
    assert v[("ercot.interchange.net", h)] == -222
    assert v[("ercot.load", h)] == DEMAND
    gen, net, load = (v[(s, h)] for s in ("ercot.gen.total", "ercot.interchange.net", "ercot.load"))
    assert abs(gen + net - load) <= 1                                 # 50,620 against 50,621
    assert ("ercot.gen.total", utc("2026-11-13 09:00")) not in v       # incomplete hour: never written
    assert ("ercot.load", utc("2026-11-13 09:00")) not in v
    assert not any(k[1] >= utc("2026-11-13 09:00") for k in v)        # a forecast row is never a value
    # fuel-mix holds yesterday too: a restart back-fills it; first run: yesterday's load from system-wide-demand
    assert v[("ercot.gen.total", utc("2026-11-12 00:00"))] == GEN_TOTAL
    assert v[("ercot.load", utc("2026-11-12 00:00"))] == 44471.6       # HE1 -> the 00:00 hour (1 dp)
    assert [n for n, _ in log] == ["fuel-mix", "supply-demand", "dc-tie-flows", "system-wide-demand"]
    declared = {m.series_id for m in build(SPECS["power_ercot"]).declared_series()}
    assert {o.series_id for o in run(files()).obs} <= declared


def test_second_run_is_conditional_and_skips_the_backfill(env):
    run(files())
    log = []
    v = values(run(files(), log=log))
    assert [n for n, _ in log] == ["fuel-mix", "supply-demand", "dc-tie-flows"]     # no gap: 3 requests
    assert all(tag for _, tag in log)                                 # each with If-None-Match -> 304, cached body
    assert v[("ercot.gen.total", utc("2026-11-13 08:00"))] == GEN_TOTAL


def test_after_midnight_only_the_missing_hour_is_filled(env, tmp_path):
    # 00:20 CST: supply-demand rolled to the new day (no complete hour yet) and yesterday's 23:00 hour was never
    # complete (its 23:55 row landed with the roll-over): system-wide-demand fills just that hour.
    (tmp_path / "cache" / "ercot").mkdir(parents=True)
    state = tmp_path / "cache" / "ercot" / "state.json"
    state.write_text(json.dumps({"load_last": utc("2026-11-12 22:00").isoformat()}))
    now = utc("2026-11-13 00:20")
    early = five_min(utc("2026-11-13 00:00"), utc("2026-11-13 00:20"))
    log = []
    v = values(run(files(demand=supply_demand(early), tie=ties(early)), now=now, log=log))
    assert "system-wide-demand" in [n for n, _ in log]
    assert sorted(t for s, t in v if s == "ercot.load") == [utc("2026-11-12 23:00")]
    assert json.loads(state.read_text())["load_last"] == "2026-11-13T05:00:00"


def test_fall_back_day_has_25_hours():
    day = five_min(utc("2026-11-01 00:00"), utc("2026-11-02 00:00"))
    assert len(day) == 300                                            # 25 h of 5-min rows on 2026-11-01 (CDT -> CST)
    fm = fuel_mix(day)
    assert {"2026-11-01 01:00:00-0500", "2026-11-01 01:00:00-0600"} <= set(fm["data"]["2026-11-01"])
    gen = pe.hourly(pe.parse_fuel_mix(fm))
    assert len(gen) == 25 and min(gen) == datetime(2026, 11, 1, 5) and max(gen) == datetime(2026, 11, 2, 5)
    assert datetime(2026, 11, 1, 6) in gen and datetime(2026, 11, 1, 7) in gen        # 01:00 CDT and 01:00 CST
    td = ties(day)
    lo, hi = (datetime(2026, 11, 1, h, tzinfo=timezone.utc).timestamp() for h in (7, 8))
    for r in td["data"]:
        if lo <= r["updateTs"] < hi:
            r["repeatedHourFlag"] = "Y"                                # ERCOT flags the repeated (CST) 01:xx hour
    assert len(pe.hourly(pe.parse_ties(td))) == 25
    assert len(pe.hourly({t: {"v": x} for t, x in pe.parse_demand(supply_demand(day)).items()})) == 25


def test_blank_cell_skips_the_row_never_zero(env):
    fm = fuel_mix(BOTH)
    fm["data"]["2026-11-13"][stamp(utc("2026-11-13 08:05"))]["Wind"]["gen"] = None
    v = values(run(files(fuel=fm)))
    h = utc("2026-11-13 08:00")
    assert ("ercot.gen.wind", h) not in v and ("ercot.gen.total", h) not in v
    assert v[("ercot.gen.wind", utc("2026-11-13 07:00"))] == 2820.6


def _with(row_patch=None, drop=None):
    row = {**json.loads(json.dumps(FUEL_ROW)), **(row_patch or {})}
    for k in drop or ():
        row.pop(k)
    return fuel_mix(BOTH, row)


@pytest.mark.parametrize("over, match", [
    ({"fuel": _with({"Geothermal": {"gen": 5}})}, "unknown fuel"),
    ({"fuel": _with(drop=["Nuclear"])}, "missing from every row"),
    ({"fuel": _with({"Natural Gas": {"gen": 500000}})}, "outside"),
    ({"fuel": _with({"Solar": {"gen": "n/a"}})}, "isn't a number"),
    ({"tie": ties(TODAY, {**TIE_ROW, "dcS": 5})}, "unknown key"),
    ({"tie": ties(TODAY, {"dcE": 0, "dcN": 0, "dcL": 0})}, "missing from every row"),
    ({"tie": ties(TODAY, {**TIE_ROW, "dcE": 9000})}, "outside"),
    ({"demand": supply_demand(TODAY, demand=-5)}, "outside"),
    ({"fuel": "<html>Request unsuccessful. Incapsula incident ID</html>"}, "not JSON"),
    ({"fuel": {"lastUpdated": "x"}}, "no `data`"),
    ({"fuel": fuel_mix(five_min(utc("2026-11-13 00:00"), utc("2026-11-13 00:30")))}, "no complete hour"),
])
def test_fail_closed(env, over, match):
    with pytest.raises(FormatDrift, match=match):
        run(files(**over))


def test_bad_stamps_fail_closed():
    with pytest.raises(FormatDrift, match="doesn't parse"):
        pe.parse_fuel_mix({"data": {"2026-11-13": {"2026-11-13 09:30": FUEL_ROW}}})     # no offset: never guessed
    sd = supply_demand(TODAY)
    sd["data"][0]["epoch"] += 3_600_000
    with pytest.raises(FormatDrift, match="disagrees with its epoch"):
        pe.parse_demand(sd)
    with pytest.raises(FormatDrift, match="updateTs"):
        pe.parse_ties({"data": [{"repeatedHourFlag": "N", **TIE_ROW, "updateTs": "soon"}]})


# The power: block proposed for regions/tx.yaml's ERCOT parts (not wired yet: El Paso Electric stays on EIA-930).
PROPOSED = ["ercot.load", "ercot.gen.total", "ercot.interchange.net", "ercot.gen.gas", "ercot.gen.coal",
            "ercot.gen.nuclear", "ercot.gen.hydro", "ercot.gen.wind", "ercot.gen.solar", "ercot.gen.battery",
            "ercot.gen.other", "ercot.tie.east.flow", "ercot.tie.north.flow", "ercot.tie.laredo.flow",
            "ercot.tie.railroad.flow"]


def test_proposed_region_series_are_declared_hourly():
    meta = {m.series_id: m for m in build(SPECS["power_ercot"]).declared_series()}
    assert set(PROPOSED) <= set(meta)
    assert all(m.unit == "MW" and m.freq == "1h" and m.extra["tier"] == "hourly" for m in meta.values())
    from montney.regions import FUELS
    assert {s.split(".")[2] for s in PROPOSED if s.startswith("ercot.gen.") and s != "ercot.gen.total"} <= set(FUELS)
