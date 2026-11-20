"""EIA WPSR by PADD (oil_eia_wpsr): refinery runs and stocks. The CSVs copy the shape of ir.eia.gov/wpsr/table2, 4, 5a
and 6 (STUB_ columns, this week / last week / year-ago columns, nested PADD rows); the numbers are made up."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest

from montney.connectors import base, build, load_specs
from montney.connectors import oil_eia_wpsr as w
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

WEEK, PREV = "11/6/26", "10/30/26"
LM = "Thu, 12 Nov 2026 16:05:36 GMT"          # the Thursday (holiday-week) release, 11:05 ET
NOW = datetime(2026, 11, 12, 18, 0)           # Thursday 13:00 ET, after it


def row(stubs, cur, prev, extra=("1.0", "0.0", "1.0", "0.0")):
    return ",".join(f'"{c}"' for c in (*stubs, cur, prev, *extra))


def head(nstub, week=WEEK, prev=PREV):
    stubs = [f"STUB_{i + 1}" for i in range(nstub)]
    return ",".join(f'"{c}"' for c in (*stubs, week, prev, "Difference", "11/7/25", "Percent Change", "11/8/24"))


def t4(week=WEEK, prev=PREV, us_crude="420.100", drop=()):
    rows = [("Crude Oil", "700.000", "699.000"), ("Commercial (Excluding SPR)", us_crude, "418.000"),
            ("East Coast (PADD 1)", "8.100", "8.000"), ("Midwest (PADD 2)", "110.200", "109.000"),
            ("Cushing", "25.500", "25.000"), ("Gulf Coast (PADD 3)", "235.300", "234.000"),
            ("Rocky Mountain (PADD 4)", "23.400", "23.000"), ("West Coast (PADD 5)", "43.600", "44.000"),
            ("Alaska In-Transit", "3.000", "3.000"), ("SPR", "280.000", "281.000"),
            ("Total Motor Gasoline", "211.000", "209.000"), ("Distillate Fuel Oil", "104.000", "100.000")]
    return "\n".join([head(1, week, prev)] + [row((n,), a, b) for n, a, b in rows if n not in drop]) + "\n"


def t2(week=WEEK, prev=PREV):
    sec = "Refiner Inputs and Utilization "
    areas = ["East Coast (PADD 1)", "Midwest (PADD 2)", "Gulf Coast (PADD 3)", "Rocky Mountain (PADD 4)",
             "West Coast (PADD 5)"]
    blocks = {"Crude Oil Inputs": ("16,900", ["800", "4,000", "9,300", "600", "2,200"]),
              "Gross Inputs": ("17,100", ["810", "4,050", "9,400", "610", "2,230"]),
              "Operable Capacity": ("18,000", ["930", "4,280", "9,900", "650", "2,240"]),
              "Percent Utilization": ("94.9", ["87.1", "94.6", "NA", "93.8", "99.6"])}
    lines = [head(2, week, prev)]
    for parent, (us, vals) in blocks.items():
        lines.append(row((sec, parent), us, us))
        lines += [row((sec, a), v, v) for a, v in zip(areas, vals)]
    lines.append(row(("Refiner and Blender Net Production ", "Finished Motor Gasoline"), "9,600", "9,700"))
    lines.append(row(("Refiner and Blender Net Production ", "Gulf Coast (PADD 3)"), "2,000", "2,100"))
    return "\n".join(lines) + "\n"


SUBS = ["East Coast (PADD 1)", "New England (PADD 1A)", "Central Atlantic (PADD 1B)", "Lower Atlantic (PADD 1C)",
        "Midwest (PADD 2)", "Gulf Coast (PADD 3)", "Rocky Mountain (PADD 4)", "West Coast (PADD 5)"]


def t5a(week=WEEK, prev=PREV):
    vals = ["52.000", "3.500", "25.000", "23.500", "43.000", "82.000", "7.100", "27.400"]
    lines = [head(2, week, prev), row(("Motor Gasoline ", "Total Motor Gasoline"), "211.500", "209.000")]
    lines += [row(("Motor Gasoline ", a), v, v) for a, v in zip(SUBS, vals)]
    lines.append(row(("Motor Gasoline ", "Finished Motor Gasoline"), "14.000", "13.900"))
    lines += [row(("Motor Gasoline ", a), "1.000", "1.000") for a in SUBS if "PADD 1" not in a or a.endswith("1)")]
    return "\n".join(lines) + "\n"


def t6(week=WEEK, prev=PREV, drop=()):
    vals = ["24.000", "3.300", "11.400", "9.300", "23.000", "43.500", "3.200", "10.200"]
    lines = [head(1, week, prev), row(("Distillate Fuel Oil",), "104.000", "100.000")]
    lines += [row((a,), v, v) for a, v in zip(SUBS, vals) if a not in drop]
    lines.append(row(("15 ppm sulfur and Under",), "92.000", "88.000"))
    lines += [row((a,), "1.000", "1.000") for a in SUBS]
    return "\n".join(lines) + "\n"


def pages(**over):
    p = {"table4": t4(), "table2": t2(), "table5a": t5a(), "table6": t6()}
    p.update(over)
    return p


class Server:
    """The four CSVs, with Last-Modified / ETag, answering If-None-Match with 304. Counts every request."""

    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, r):
        name = str(r.url).rsplit("/", 1)[1].removesuffix(".csv")
        self.calls.append(name)
        if name not in self.files:
            return httpx.Response(404, text="")
        tag = f'W/"{abs(hash(self.files[name]))}"'
        if r.headers.get("if-none-match") == tag:
            return httpx.Response(304)
        return httpx.Response(200, text=self.files[name], headers={"Last-Modified": LM, "ETag": tag})


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    yield
    for k in [k for k in base._validators if k[0].startswith("test_wpsr")]:
        del base._validators[k]


def conn():
    return build(load_specs(include_disabled=True)["oil_eia_wpsr"])


def run(server, now=NOW, source="", c=None):
    ctx = Context(client=httpx.Client(transport=httpx.MockTransport(server)), now=now, source=source)
    res = (c or conn()).run(ctx)
    ctx.commit_validators()          # the runner does this after the write
    return res


def test_parse_units_and_padd_mapping():
    res = run(Server(pages()))
    v = {(o.series_id, o.ts): o for o in res.obs}
    fri, prev = datetime(2026, 11, 6), datetime(2026, 10, 30)
    assert v[("oil.padd3.crude_stocks", fri)].value == 235_300          # million bbl x 1000 -> kbbl
    assert v[("oil.padd3.crude_stocks", prev)].value == 234_000         # last week too (a revision vintage)
    assert v[("oil.us.crude_stocks", fri)].value == 420_100             # commercial, excl. SPR (not "Crude Oil")
    assert v[("oil.cushing.crude_stocks", fri)].value == 25_500
    assert v[("oil.us.spr_stocks", fri)].value == 280_000
    assert v[("oil.padd2.refinery_crude_input", fri)].value == 4_000    # kbbl/d, commas stripped
    assert v[("oil.us.refinery_crude_input", fri)].value == 16_900
    assert v[("oil.padd5.refinery_gross_input", fri)].value == 2_230
    assert v[("oil.padd4.refinery_capacity", fri)].value == 650
    assert v[("oil.padd1.utilization", fri)].value == 87.1
    assert ("oil.padd3.utilization", fri) not in v                      # "NA": skipped, never 0
    assert v[("oil.padd1a.gasoline_stocks", fri)].value == 3_500
    assert v[("oil.padd1c.distillate_stocks", fri)].value == 9_300
    assert v[("oil.padd3.gasoline_stocks", fri)].value == 82_000        # Total Motor Gasoline, not Finished
    assert v[("oil.padd2.distillate_stocks", fri)].value == 23_000      # Distillate total, not the 15 ppm block
    assert v[("oil.padd3.refinery_crude_input", fri)].as_of == datetime(2026, 11, 12)   # release date (ET)
    assert {o.series_id for o in res.obs} <= {m.series_id for m in res.meta}
    units = {m.series_id: m.unit for m in res.meta}
    assert units["oil.padd3.utilization"] == "%" and units["oil.padd3.crude_stocks"] == "kbbl"
    assert units["oil.padd3.refinery_crude_input"] == "kbbl/d"
    assert {m.freq for m in res.meta} == {"W"}
    assert "oil.padd1a.refinery_crude_input" not in units               # refinery data has no sub-PADDs


@pytest.mark.parametrize("over, msg", [
    ({"table4": t4(week="11/7/26", prev="10/31/26")}, "week-ending Fridays"),
    ({"table4": t4(week="11/6/26", prev="10/23/26")}, "consecutive"),
    ({"table6": t6(drop=("Gulf Coast (PADD 3)",))}, "no 'Distillate Fuel Oil' row for ['padd3']"),
    ({"table4": t4(us_crude="420,100")}, "outside"),                    # published in kbbl: a unit change
    ({"table2": "Series,Week\nfoo,1\n"}, "expected STUB_1"),
])
def test_format_drift(over, msg):
    with pytest.raises(FormatDrift, match=msg.replace("[", r"\[").replace("]", r"\]")):
        run(Server(pages(**over)))


def test_skip_when_current_and_probe_when_due():
    srv = Server(pages())
    c = conn()
    run(srv, NOW, c=c)
    assert sorted(srv.calls) == sorted(w.TABLES)                       # 4 requests: a full release
    srv.calls.clear()
    for now in (datetime(2026, 11, 13, 6), datetime(2026, 11, 17, 18), datetime(2026, 11, 18, 15, 0)):
        run(srv, now, c=c)                                             # up to Wed 10:00 ET: nothing due
    assert srv.calls == []
    run(srv, datetime(2026, 11, 18, 16, 0), c=c)                       # Wed 11:00 ET: week 11/13 due, not out
    assert srv.calls == ["table4"]                                     # the probe only
    run(srv, datetime(2026, 11, 18, 17, 0), c=c)                       # before next_try (14:30 ET): no request
    assert srv.calls == ["table4"]
    srv.files.update(table4=t4("11/13/26", "11/6/26"), table2=t2("11/13/26", "11/6/26"),
                     table5a=t5a("11/13/26", "11/6/26"), table6=t6("11/13/26", "11/6/26"))
    res = run(srv, datetime(2026, 11, 18, 20, 0), c=c)                 # 15:00 ET: out
    assert srv.calls == ["table4"] + w.TABLES
    assert max(o.ts for o in res.obs) == datetime(2026, 11, 13)
    srv.calls.clear()
    run(srv, datetime(2026, 11, 19, 2, 0), c=c)
    assert srv.calls == []


def test_late_probe_backs_off_to_next_day():
    srv = Server(pages())
    run(srv, NOW)
    srv.calls.clear()
    run(srv, datetime(2026, 11, 18, 23, 0))                            # Wed 18:00 ET, past the 4 h window
    assert srv.calls == ["table4"]
    run(srv, datetime(2026, 11, 19, 12, 0))                            # Thu 07:00 ET: waits for 10:30 ET
    assert srv.calls == ["table4"]
    run(srv, datetime(2026, 11, 19, 16, 0))                            # Thu 11:00 ET: probes again
    assert srv.calls == ["table4", "table4"]


def test_conditional_get_304_counts_one_request():
    srv = Server(pages())
    run(srv, NOW, source="test_wpsr_304")
    srv.calls.clear()
    res = run(srv, datetime(2026, 11, 18, 16, 0), source="test_wpsr_304")   # due, file unchanged -> 304
    assert srv.calls == ["table4"] and res.obs == []


def test_mid_publication_lagging_table_is_fetched_again():
    srv = Server(pages(table6=t6("10/30/26", "10/23/26")))             # table6 still last week's
    run(srv, NOW, source="test_wpsr_mid")
    srv.calls.clear()
    srv.files["table6"] = t6()
    res = run(srv, datetime(2026, 11, 12, 20, 0), source="test_wpsr_mid")   # after next_try: probe 304, rest refetched
    assert srv.calls == w.TABLES
    assert any(o.series_id == "oil.padd3.distillate_stocks" and o.ts == datetime(2026, 11, 6) for o in res.obs)


@pytest.mark.parametrize("now, want", [
    (datetime(2026, 11, 11, 15, 29), date(2026, 10, 30)),   # Wed 10:29 EST
    (datetime(2026, 11, 11, 15, 30), date(2026, 11, 6)),    # Wed 10:30 EST
    (datetime(2026, 10, 14, 14, 30), date(2026, 10, 9)),    # Wed 10:30 EDT (UTC-4)
    (datetime(2026, 10, 14, 14, 29), date(2026, 10, 2)),
    (datetime(2026, 11, 16, 3, 0), date(2026, 11, 6)),      # Sunday night
])
def test_due_week(now, want):
    assert w.due_week(now) == want


def test_next_try():
    assert w.next_try(datetime(2026, 11, 18, 16, 0)) == datetime(2026, 11, 18, 19, 30)   # within 4 h: 14:30 ET
    assert w.next_try(datetime(2026, 11, 18, 23, 0)) == datetime(2026, 11, 19, 15, 30)   # then next day 10:30 ET
    assert w.next_try(datetime(2026, 11, 19, 5, 0)) == datetime(2026, 11, 19, 15, 30)    # 00:00 ET: today 10:30


def test_snippets_validate():
    """docs/research/oil_groups_snippets.md: every block passes the region schema and names declared series only."""
    import pathlib
    import re

    import yaml

    from montney import regions

    text = (pathlib.Path(__file__).parents[1] / "docs/research/oil_groups_snippets.md").read_text(encoding="utf-8")
    specs = load_specs(include_disabled=True)
    declared = {m.series_id for s in ("oil_eia_wpsr", "gas_eia_states", "oil_statcan_provinces")
                for m in build(specs[s]).declared_series()}
    seen = {}
    for region, body in re.findall(r"^## (\w+)\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        for block in re.findall(r"```yaml\n(.*?)```", body, re.S):
            raw = yaml.safe_load(block)
            groups = raw["oil"] if isinstance(raw, dict) else raw     # cal's block is groups to append
            out = regions._detail({"oil": groups}, f"snippet {region}")
            for g in out["oil"]:
                for r in g.rows:
                    assert r.series in declared, f"{region}: {r.series} isn't declared by any source"
            seen[region] = [g.id for g in out["oil"]]
    assert set(seen) == {"tx", "sunbelt", "mtnw", "bakken", "umw", "nyc", "upne", "pnw", "cal"}
    assert all("refinery_runs" in ids and "stocks" in ids for ids in seen.values())
    assert "production_by_area" not in seen["cal"]                    # cal already has it
