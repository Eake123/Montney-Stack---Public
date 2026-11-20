"""BPA balancing authority (PLAN 9 item 1, pnw): mocked baltwg3.txt in the live shape (rows copied 2026-11-13)."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_bpa as bpa
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
PRE = ("BPA Balancing Authority Load & Total VER\r\nat 5-minute intervals, last 7 days\r\n"
       "Dates: 07Nov2026 - 14Nov2026 (last updated 13Nov2026 08:36:07) Pacific Time\r\n"
       "Based on 5-min MW readings from the BPA SCADA system for points 45583, 177167, 79682, 164377, 45581, 70681\r\n"
       "\r\nThis represents loads and resources in BPA's Balancing Authority (BA) "
       "including some that are not BPA's.\r\n"
       "It does not include BPA loads served by transfer, scheduled out of region,\r\n"
       "or scheduled to customers with their own BAs such as Seattle and Tacoma\r\n\r\n"
       "BPA/Technical Operations (TOT-OpInfo@bpa.gov)\r\n\r\n")
HEAD = "Date/Time       \tLoad\tVER\tHydro\tFossil/Biomass\tInterchange\tNuclear\r\n"
LM = "Fri, 13 Nov 2026 16:36:13 GMT"                 # = 08:36 PST: the preamble's "last updated"
# 2026-11-13 07:00-07:55 PST, real rows: Load VER Hydro Fossil/Biomass Interchange Nuclear
REAL = """07:00	7467	517	6881	891	2015	1152
07:05	7515	492	7397	890	2455	1150
07:10	7535	491	7417	889	2458	1155
07:15	7476	507	7298	890	2410	1150
07:20	7512	507	7179	887	2257	1155
07:25	7578	504	7254	888	2257	1150
07:30	7593	491	7164	888	2141	1151
07:35	7608	502	6793	888	1766	1151
07:40	7606	504	6836	887	1829	1155
07:45	7621	500	6935	887	1897	1155
07:50	7635	475	7012	886	1924	1157
07:55	7639	469	6853	887	1767	1155
08:00	7650	470	6900	887	1800	1155"""
DECLARED = {m.series_id for m in build(SPECS["power_bpa"]).declared_series()}
# The lead's proposed pnw parts (regions/pnw.yaml isn't wired in this PR): all must be declared.
PROPOSED = {"bpa.load", "bpa.gen.total", "bpa.interchange.net", "bpa.gen.hydro", "bpa.gen.wind_solar",
            "bpa.gen.thermal", "bpa.gen.nuclear"}


def body(rows: str, day: str = "11/13/2026", future: int = 3) -> str:
    lines = [f"{day} {r}" for r in rows.strip().splitlines()]
    last = datetime.strptime(lines[-1][:16], "%m/%d/%Y %H:%M")
    lines += [f"{last + timedelta(minutes=5 * (i + 1)):%m/%d/%Y %H:%M}\t\t\t\t\t\t" for i in range(future)]
    return PRE + HEAD + "\r\n".join(lines) + "\r\n"


def flat(hh_mm: list[str], load: int, ver=100, hydro=5000, thermal=800, ic=0, nuc=1150) -> str:
    return "\n".join(f"{t}\t{load}\t{ver}\t{hydro}\t{thermal}\t{ic}\t{nuc}" for t in hh_mm)


def hour(h: int) -> list[str]:
    return [f"{h:02d}:{m:02d}" for m in range(0, 60, 5)]


def run(text, tmp_path, monkeypatch, *, lm=LM, status=200, seen=None):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))

    def handler(req):
        if seen is not None:
            seen.append(dict(req.headers))
        if status == 304:
            return httpx.Response(304)
        return httpx.Response(200, text=text, headers={"Last-Modified": lm} if lm else {})
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 13, 16, 40))
    return build(SPECS["power_bpa"]).run(c)


def test_live_hour_means_pst_to_utc_and_signs(tmp_path, monkeypatch):
    res = run(body(REAL), tmp_path, monkeypatch)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    h = datetime(2026, 11, 13, 15)                              # 07:00 PST = 15:00 UTC
    assert {o.ts for o in res.obs} == {h}                      # 08:00 alone (one snapshot) isn't a complete hour
    assert {o.series_id for o in res.obs} == DECLARED
    rows = [list(map(float, r.split("\t")[1:])) for r in REAL.splitlines()[:12]]
    mean = [round(sum(c) / 12, 1) for c in zip(*rows)]
    assert v[("bpa.load", h)] == mean[0] and v[("bpa.gen.hydro", h)] == mean[2]
    assert v[("bpa.interchange.net", h)] == -mean[4]          # BPA's + export -> our + import
    gen = v[("bpa.gen.total", h)]
    assert gen == pytest.approx(mean[1] + mean[2] + mean[3] + mean[5], abs=0.2)
    # BPA: generation = load + interchange (+ export); measured -40 MW on average (min -86) over 1,832 rows
    assert abs(gen + v[("bpa.interchange.net", h)] - v[("bpa.load", h)]) < 90


def test_fall_back_day_repeated_hour_is_two_utc_hours():
    # 2026-11-01: BPA lists 01:00-01:55 twice (PDT then PST), as its YTD workbook's UTC column shows (08:00Z, 09:00Z)
    rows = "\n".join([flat(hour(0), 6500), flat(hour(1), 6491), flat(hour(1), 6455), flat(hour(2), 6451)])
    hours = bpa.hourly_means(bpa.parse(body(rows, "11/01/2026")))
    assert sorted(hours) == [datetime(2026, 11, 1, h) for h in (7, 8, 9, 10)]
    assert hours[datetime(2026, 11, 1, 8)]["load"] == 6491 and hours[datetime(2026, 11, 1, 9)]["load"] == 6455
    assert hours[datetime(2026, 11, 1, 10)]["load"] == 6451        # 02:00 PST


def test_spring_forward_skips_two_am():
    rows = "\n".join([flat(hour(1), 5900), flat(hour(3), 5960)])  # 2026-03-08: 01:55 PST -> 03:00 PDT
    hours = bpa.hourly_means(bpa.parse(body(rows, "03/08/2026")))
    assert sorted(hours) == [datetime(2026, 3, 8, 9), datetime(2026, 3, 8, 10)]


def test_partial_hour_and_blank_cells_are_not_written():
    rows = flat(hour(5), 7000).splitlines()
    rows[4] = rows[4].split("\t")[0] + "\t\t\t\t\t\t"              # a blank (missing) snapshot mid-hour
    assert bpa.hourly_means(bpa.parse(body("\n".join(rows)))) == {}


@pytest.mark.parametrize("text, needle", [
    (body(REAL).replace("\tNuclear", "\tNuclear\tSolar"), "map a new one"),
    (body(REAL).replace("\tVER", ""), "expected"),
    (body(REAL).replace("Date/Time", "When"), "no Date/Time header"),
    (body(REAL).replace("11/13/2026 07:05", "13-11-2026 07:05"), "doesn't parse"),
    (body(REAL).replace("7397", "n/a"), "isn't a number"),
    (body(REAL).replace("7397", "73970"), "outside"),
    (body(REAL).replace("\t2455\t", "\t-22455\t"), "outside"),
    (body(REAL).replace("11/13/2026 07:05", "11/13/2026 06:05"), "step forward"),
])
def test_fails_closed(text, needle):
    with pytest.raises(FormatDrift, match=needle):
        bpa.parse(text)


def test_no_complete_hour_fails_closed(tmp_path, monkeypatch):
    with pytest.raises(FormatDrift, match="no complete hour"):
        run(body("07:00\t7467\t517\t6881\t891\t2015\t1152"), tmp_path, monkeypatch)


def test_clock_check_fails_closed_if_bpa_leaves_pacific_prevailing(tmp_path, monkeypatch):
    assert bpa.clock_problem(body(REAL), LM) is None
    with pytest.raises(FormatDrift, match="clock changed"):              # the same stamp read as UTC-7 in November
        run(body(REAL), tmp_path, monkeypatch, lm="Fri, 13 Nov 2026 15:36:13 GMT")
    summer = body(REAL).replace("13Nov2026 08:36:07", "15Jul2026 08:36:07")
    assert bpa.clock_problem(summer, "Wed, 15 Jul 2026 15:36:10 GMT") is None          # PDT: UTC-7
    assert "clock changed" in bpa.clock_problem(summer, "Wed, 15 Jul 2026 16:36:10 GMT")  # fixed PST would be -8
    res = run(body(REAL), tmp_path / "nolm", monkeypatch, lm=None)
    assert res.obs and any("Last-Modified" in w for w in res.warnings)


def test_conditional_get_reuses_the_kept_body(tmp_path, monkeypatch):
    seen: list[dict] = []
    first = run(body(REAL), tmp_path, monkeypatch, seen=seen)
    assert "if-modified-since" not in seen[0]
    again = run("", tmp_path, monkeypatch, status=304, seen=seen)
    assert seen[1]["if-modified-since"] == LM
    assert [(o.series_id, o.ts, o.value) for o in again.obs] == [(o.series_id, o.ts, o.value) for o in first.obs]


def test_series_declared_hourly_and_proposed_parts_exist():
    metas = build(SPECS["power_bpa"]).declared_series()
    assert all(m.freq == "1h" and m.unit == "MW" and m.extra["tier"] == "hourly" for m in metas)
    assert PROPOSED <= DECLARED
    spec = SPECS["power_bpa"]
    assert spec["interval"] == "15m" and spec["retention"] == "hourly" and spec["region"] == "pnw"
