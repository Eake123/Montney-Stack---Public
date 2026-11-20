"""NYISO real-time generation, load and tie flows (PLAN 9 item 1): mocked MIS day files in the live shape.

Rows copy the live layout (rtfuelmix, pal and ExternalLimitsFlows, 2026-11-13); values are synthetic but sized like
the 10:10 EST stamp that day (gen 16,402 + net imports 1,250 = load 17,653 within 1 MW)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_nyiso as ny
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
ET, UTC = ZoneInfo("America/New_York"), timezone.utc
STEP = timedelta(minutes=5)
FUEL = {"Dual Fuel": 5588.0, "Natural Gas": 3813.0, "Nuclear": 3338.0, "Other Fossil Fuels": 0.0,
        "Other Renewables": 371.0, "Wind": 432.0, "Hydro": 2860.0}                         # sum 16,402
ZONE = {"CAPITL": (61757, 1310.6), "CENTRL": (61754, 1700.1), "DUNWOD": (61760, 590.3), "GENESE": (61753, 1020.4),
        "HUD VL": (61758, 1050.2), "LONGIL": (61762, 2240.8623), "MHK VL": (61756, 820.5), "MILLWD": (61759, 330.1),
        "N.Y.C.": (61761, 5811.782), "NORTH": (61755, 640.3), "WEST": (61752, 2137.855)}   # sum 17,653
TIE = {"CENTRAL EAST - VC": (23330, 1330.97), "DYSINGER EAST": (23326, 295.3), "MOSES SOUTH": (23319, 122.27),
       "SCH - HQ - NY": (23324, -599), "SCH - HQ_CEDARS": (325274, 0), "SCH - HQ_CHPE": (625211, 859),
       "SCH - HQ_IMPORT_EXPORT": (325376, -599), "SCH - NE - NY": (23318, -491.52), "SCH - NPX_1385": (325277, 0),
       "SCH - NPX_CSC": (325154, 30), "SCH - OH - NY": (23317, -350.93), "SCH - PJ - NY": (23316, 1092.9),
       "SCH - PJM_HTP": (325905, 49.55), "SCH - PJM_NEPTUNE": (325305, 660), "SCH - PJM_VFT": (325658, 0),
       "SPR/DUN-SOUTH": (23320, 730.32), "TOTAL EAST": (23314, 3421.88), "UPNY CONED": (23315, 1685.86),
       "WEST CENTRAL": (23312, 21.49)}                                                    # external net 1,250


def day_utc(d: date) -> tuple[datetime, datetime]:
    a = datetime(d.year, d.month, d.day, tzinfo=ET).astimezone(UTC).replace(tzinfo=None)
    e = d + timedelta(days=1)
    return a, datetime(e.year, e.month, e.day, tzinfo=ET).astimezone(UTC).replace(tzinfo=None)


def instants(d: date, *, ending: bool, until: datetime | None = None) -> list[datetime]:
    a, b = day_utc(d)
    out, t = [], a + STEP if ending else a
    while t < b + (STEP if ending else timedelta(0)) and (until is None or t <= until):
        out.append(t)
        t += STEP
    return out


def local(t: datetime) -> tuple[str, str]:
    lt = t.replace(tzinfo=UTC).astimezone(ET)
    return lt.strftime("%m/%d/%Y %H:%M:%S"), lt.tzname()


def fuel_csv(ts, val=lambda t, f, v: v) -> str:
    rows = ["Time Stamp,Time Zone,Fuel Category,Gen MW"]
    for t in ts:
        s, z = local(t)
        rows += [f"{s},{z},{f},{val(t, f, v)}" for f, v in FUEL.items()]
    return "\n".join(rows) + "\n"


def pal_csv(ts) -> str:
    rows = ['"Time Stamp","Time Zone","Name","PTID","Load"']
    for t in ts:
        s, z = local(t)
        rows += [f'"{s}","{z}","{n}",{p},{v}' for n, (p, v) in ZONE.items()]
    return "\n".join(rows) + "\n"


def flows_csv(ts, val=lambda t, n, v: v) -> str:
    rows = ["Timestamp,Interface Name,Point ID,Flow (MWH),Positive Limit (MWH),Negative Limit (MWH)"]
    for t in ts:
        s = local(t)[0][:-3]                                         # no seconds, no zone column
        rows += [f"{s},{n},{p},{val(t, n, v)},1400,-1800" for n, (p, v) in TIE.items()]
    return "\n".join(rows) + "\n"


def edt_or_est(t: datetime, n: str, v):
    """The HQ - NY tie reads -100 through 01:00-02:00 EST (06:00 UTC: the repeated block + 02:00) and -599 else."""
    return -100 if n == "SCH - HQ - NY" and ny.hour_of(t) == datetime(2026, 11, 1, 6) else v


def files(now: datetime, *, flows_val=edt_or_est) -> dict[str, str]:
    """Nov 1 (fall-back, 25 h) complete + Nov 2 up to ``now``."""
    y, t = date(2026, 11, 1), date(2026, 11, 2)
    return {f"rtfuelmix/{y:%Y%m%d}rtfuelmix.csv": fuel_csv(instants(y, ending=True)),
            f"pal/{y:%Y%m%d}pal.csv": pal_csv(instants(y, ending=False)),
            f"ExternalLimitsFlows/{y:%Y%m%d}ExternalLimitsFlows.csv": flows_csv(instants(y, ending=False), flows_val),
            f"rtfuelmix/{t:%Y%m%d}rtfuelmix.csv": fuel_csv(instants(t, ending=True, until=now)),
            f"pal/{t:%Y%m%d}pal.csv": pal_csv(instants(t, ending=False, until=now)),
            f"ExternalLimitsFlows/{t:%Y%m%d}ExternalLimitsFlows.csv":
                flows_csv(instants(t, ending=False, until=now), flows_val)}


class Server:
    """The MIS host: serves ``files``, honours If-Modified-Since, logs requests."""

    LM = "Mon, 02 Nov 2026 06:10:00 GMT"

    def __init__(self, f: dict[str, str]):
        self.files, self.log = f, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/public/csv/")
        self.log.append((path, req.headers.get("if-modified-since")))
        assert req.url.host == "mis.nyiso.com"
        if path not in self.files:
            return httpx.Response(404)
        if req.headers.get("if-modified-since") == self.LM:
            return httpx.Response(304)
        return httpx.Response(200, text=self.files[path], headers={"Last-Modified": self.LM})


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(ny, "PAUSE_S", 0)


def run(server: Server, now: datetime):
    c = Context(client=httpx.Client(transport=httpx.MockTransport(server)), now=now)
    return build(SPECS["power_nyiso"]).run(c)


NOW = datetime(2026, 11, 2, 6, 10)                       # 01:10 EST on Nov 2


def values(res) -> dict[tuple[str, datetime], float]:
    return {(o.series_id, o.ts): o.value for o in res.obs}


def test_fall_back_day_has_25_utc_hours_and_the_repeated_block_is_est():
    v = values(run(Server(files(NOW)), NOW))
    hours = [datetime(2026, 11, 1, 4) + timedelta(hours=i) for i in range(26)]   # Nov 1 (25 h) + Nov 2 00:00 EST
    for sid in ("nyiso.gen.total", "nyiso.load", "nyiso.interchange.net", "nyiso.load.nyc", "nyiso.gen.wind"):
        assert [h for h in hours if (sid, h) not in v] == [], sid
    assert ("nyiso.gen.total", datetime(2026, 11, 2, 6)) not in v                 # 01:00-02:00 EST: in progress
    # 01:00-02:00 EDT = 05:00 UTC reads the first block; 01:00-02:00 EST = 06:00 UTC the second (file order)
    assert v[("nyiso.tie.hq_ny.flow", datetime(2026, 11, 1, 5))] == -599
    assert v[("nyiso.tie.hq_ny.flow", datetime(2026, 11, 1, 6))] == -100
    assert v[("nyiso.tie.hq_ny.flow", datetime(2026, 11, 1, 4))] == -599          # 00:00-01:00 EDT
    assert v[("nyiso.tie.hq_ny.flow", datetime(2026, 11, 1, 7))] == -599          # 02:00 EST on: back to normal


def test_fuel_and_load_zone_columns_place_the_hours():
    ts = instants(date(2026, 11, 1), ending=True)
    tag = {t: 100 + ny.hour_of(t).hour for t in ts}                               # wind = 100 + the UTC hour
    pts = ny.parse_fuel(fuel_csv(ts, lambda t, f, v: tag[t] if f == "Wind" else v))
    h = ny.hourly_means(pts)
    assert h[datetime(2026, 11, 1, 5)]["wind"] == 105 and h[datetime(2026, 11, 1, 6)]["wind"] == 106
    assert datetime(2026, 11, 1, 4) in h and datetime(2026, 11, 2, 4) in h and len(h) == 25
    assert min(pts) == datetime(2026, 11, 1, 4, 5) and max(pts) == datetime(2026, 11, 2, 5)


def test_identity_gen_plus_net_imports_is_load_and_hq_duplicate_not_added():
    v = values(run(Server(files(NOW)), NOW))
    h = datetime(2026, 11, 1, 15)
    gen, net, load = v[("nyiso.gen.total", h)], v[("nyiso.interchange.net", h)], v[("nyiso.load", h)]
    assert gen == 16402 and net == 1250                                          # HQ_IMPORT_EXPORT (-599) left out
    # Live hourly check (Nov 1-13, 299 h): gen + net - load had a median of +50 MW and 250 of 299 hours within
    # 150 MW; off-peak hours reach ~+650 MW (likely pumped-storage pumping, which PAL load leaves out).
    assert abs(gen + net - load) <= 150
    nb = sum(v[(f"nyiso.interchange.{n}", h)] for n in ny.NEIGHBOURS)
    assert nb == pytest.approx(net, abs=0.2)                                     # each mean rounds to 0.1
    assert v[("nyiso.interchange.hq", h)] == -599 + 0 + 859
    assert sum(v[(f"nyiso.load.{z}", h)] for z in ny.ZONES.values()) == pytest.approx(load, abs=0.6)


def test_complete_hours_only_and_blank_cells_are_not_zero():
    ts = instants(date(2026, 11, 2), ending=True, until=datetime(2026, 11, 2, 8, 20))
    gap = [t for t in ts if ny.hour_of(t) == datetime(2026, 11, 2, 6)][:3]          # 3 of 12 missing: < MIN_N
    blank = ts[2]                                                                  # one blank wind cell
    text = fuel_csv([t for t in ts if t not in gap], lambda t, f, v: "" if (t, f) == (blank, "Wind") else v)
    h = ny.hourly_means(ny.parse_fuel(text))
    assert sorted(h) == [datetime(2026, 11, 2, 5), datetime(2026, 11, 2, 7)]       # 06 thin, 08 in progress
    assert h[datetime(2026, 11, 2, 5)]["wind"] == 432 and h[datetime(2026, 11, 2, 5)]["total"] == 16402


def test_rtd_reruns_off_grid_and_repeated_minutes():
    ts = instants(date(2026, 11, 2), ending=False, until=datetime(2026, 11, 2, 7, 5))
    text = flows_csv(ts)
    lines = text.splitlines()
    extra = [ln.replace("11/02/2026 01:35", "11/02/2026 01:37") for ln in lines if ln.startswith("11/02/2026 01:35")]
    dup = [ln.replace(",-599,", ",-601,") for ln in lines if ln.startswith("11/02/2026 01:40")]
    i = next(k for k, ln in enumerate(lines) if ln.startswith("11/02/2026 01:40"))
    out = lines[:i] + extra + lines[i:i + len(TIE)] + dup + lines[i + len(TIE):]   # a re-run after 01:35; 01:40 twice
    pts = ny.parse_flows("\n".join(out) + "\n")
    assert pts[datetime(2026, 11, 2, 6, 37)]["net"] == 1250
    assert pts[datetime(2026, 11, 2, 6, 40)]["hq_ny"] == -601                        # the later row wins
    assert ny.hourly_means(pts)[datetime(2026, 11, 2, 6)]["net"] == pytest.approx((13 * 1250 - 2) / 13, abs=0.05)


def test_conditional_requests_and_final_yesterday():
    srv = Server(files(NOW))
    first = run(srv, NOW)
    assert len(srv.log) == 6 and all(h is None for _, h in srv.log)                 # nothing cached yet
    srv.log.clear()
    later = NOW + timedelta(minutes=15)
    second = run(srv, later)
    assert sorted(p for p, _ in srv.log) == sorted(p for p in srv.files if "20261102" in p)   # yesterday: cached
    assert all(h == Server.LM for _, h in srv.log)                                 # If-Modified-Since -> 304
    assert values(first) == values(second)                                         # a 304 re-emits the cached copy


def test_yesterday_rechecked_until_an_hour_after_midnight():
    srv = Server(files(datetime(2026, 11, 2, 5, 20)))
    run(srv, datetime(2026, 11, 2, 5, 20))                                          # 00:20 EST: not final yet
    srv.log.clear()
    run(srv, datetime(2026, 11, 2, 5, 35))
    assert sum("20261101" in p for p, _ in srv.log) == 3                            # re-checked (conditional)
    run(srv, datetime(2026, 11, 2, 6, 5))                                           # 01:05 EST: this check is final
    srv.log.clear()
    run(srv, datetime(2026, 11, 2, 6, 20))
    assert sum("20261101" in p for p, _ in srv.log) == 0


def test_no_complete_hour_fails_closed():
    now = datetime(2026, 11, 2, 5, 20)
    f = {k: v for k, v in files(now).items() if "20261102" in k}                     # yesterday 404, today 20 min
    with pytest.raises(FormatDrift, match="no complete hour"):
        run(Server(f), now)


@pytest.mark.parametrize("make, match", [
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace("Wind", "Solar"), "unknown fuel"),
    (lambda: "\n".join(ln for ln in fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).splitlines()
                       if "Nuclear" not in ln), "fuels missing"),
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace("EST", "CST"), "time zone"),
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace(",EST,", ",EDT,"), "valid Eastern"),
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace("11/02/2026 00:10:00", "11/02/2026 0:1x"),
     "doesn't parse"),
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace("3338.0", "93338.0"), "outside"),
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace("3338.0", "n/a"), "isn't a number"),
    (lambda: fuel_csv(instants(date(2026, 11, 2), ending=True)[:3]).replace("Gen MW", "Gen MWh"), "header changed"),
])
def test_fuel_file_fails_closed(make, match):
    with pytest.raises(FormatDrift, match=match):
        ny.parse_fuel(make())


@pytest.mark.parametrize("make, match", [
    (lambda: pal_csv(instants(date(2026, 11, 2), ending=False)[:3]).replace("MILLWD", "MILLWOOD"), "unknown zone"),
    (lambda: "\n".join(ln for ln in pal_csv(instants(date(2026, 11, 2), ending=False)[:3]).splitlines()
                       if "NORTH" not in ln), "zones missing"),
    (lambda: pal_csv(instants(date(2026, 11, 2), ending=False)[:3]).replace("5811.782", "-5811.782"), "outside"),
])
def test_load_file_fails_closed(make, match):
    with pytest.raises(FormatDrift, match=match):
        ny.parse_load(make())


@pytest.mark.parametrize("make, match", [
    (lambda: flows_csv(instants(date(2026, 11, 2), ending=False)[:3]).replace("SCH - PJM_VFT", "SCH - PJM_NEW"),
     "unknown external tie"),
    (lambda: "\n".join(ln for ln in flows_csv(instants(date(2026, 11, 2), ending=False)[:3]).splitlines()
                       if "HQ_CEDARS" not in ln), "ties missing"),
    (lambda: flows_csv(instants(date(2026, 11, 2), ending=False)[:3]).replace(",1092.9,", ",9092.9,"), "outside"),
    (lambda: flows_csv(instants(date(2026, 11, 2), ending=False)[:3][::-1]), "steps back"),
    (lambda: flows_csv(instants(date(2026, 11, 2), ending=False)[:3]).replace("Flow (MWH)", "Flow"), "header"),
])
def test_flows_file_fails_closed(make, match):
    with pytest.raises(FormatDrift, match=match):
        ny.parse_flows(make())


def test_internal_interface_changes_are_ignored():
    text = flows_csv(instants(date(2026, 11, 2), ending=False)[:3]).replace("MOSES SOUTH", "MOSES NORTH")
    assert ny.parse_flows(text)[datetime(2026, 11, 2, 5)]["net"] == 1250


def test_nyc_region_reads_declared_nyiso_series():
    from montney import regions
    nyc = regions.load()["nyc"]
    declared = {m.series_id for m in build(SPECS["power_nyiso"]).declared_series()}
    power = {p.series for ln in [*nyc.power.values(), *nyc.fuels.values()] for p in ln.parts}
    assert power and power <= declared
    assert nyc.power["interchange"].parts[0].series == "nyiso.interchange.net"
    assert nyc.power["interchange"].parts[0].sign == 1                             # already + import
    assert SPECS["power_nyiso"]["region"] == "nyc" and SPECS["power_nyiso"]["interval"] == "15m"
    assert {m.extra["tier"] for m in build(SPECS["power_nyiso"]).declared_series()} == {"hourly"}
    assert all(m.unit == "MW" and m.freq == "1h" for m in build(SPECS["power_nyiso"]).declared_series())
