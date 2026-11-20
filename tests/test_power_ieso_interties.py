"""Ontario intertie flows (L-103): mocked IESO IntertieScheduleFlow XML + IntertieScheduleFlowYear CSV (live shapes,
2026-11-05). Sign: + export from Ontario, - import. Quebec = the sum of every PQ.* sub-intertie."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from montney import network
from montney.connectors import build, load_specs
from montney.connectors import power_ieso_interties as it
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
ZONES = ["MANITOBA", "MANITOBA SK", "MICHIGAN", "MINNESOTA", "NEW-YORK", "PQ.AT", "PQ.B5D.B31L", "PQ.X2Y"]
FLOW = {"MANITOBA": 0, "MANITOBA SK": 16, "MICHIGAN": 669, "MINNESOTA": -30, "NEW-YORK": 623, "PQ.AT": 600,
        "PQ.B5D.B31L": 408, "PQ.X2Y": -10}                    # quebec 998, net 2,276


def xml(hours_full: int, zones=ZONES, flow=FLOW) -> str:
    body = ""
    for z in zones:
        acts = "".join(f"<Actual><Hour>{h}</Hour><Interval>{i}</Interval><Flow>{flow[z]}</Flow></Actual>"
                       for h in range(1, hours_full + 1) for i in range(1, 13))
        acts += f"<Actual><Hour>{hours_full + 1}</Hour><Interval>1</Interval><Flow>{flow[z]}</Flow></Actual>"
        body += (f"<IntertieZone><IntertieZoneName>{z}</IntertieZoneName><Schedules></Schedules>"
                 f"<Actuals>{acts}</Actuals></IntertieZone>")
    return f"<IMODocument><IMODocBody><Date>2026-11-05</Date>{body}</IMODocBody></IMODocument>"


def year_csv(days: list[date], flow_of=lambda d, z: FLOW[z]) -> str:
    head = ["\\\\Yearly Intertie Schedule and Flow Report", "\\\\Created at 2026-11-05 08:02:07", "\\\\For 2026"]
    zrow = ["", ""] + [z for z in ZONES + ["Total"] for _ in range(3)]
    krow = ["Date", "Hour"] + ["Imp", "Exp", "Flow"] * (len(ZONES) + 1)
    lines = [",".join([h] + [""] * (len(zrow) - 1)) for h in head] + [",".join(zrow), ",".join(krow)]
    for d in days:
        for he in range(1, 25):
            vals = []
            for z in ZONES:
                vals += ["0", "0", str(flow_of(d, z))]
            vals += ["0", "0", str(sum(flow_of(d, z) for z in ZONES))]
            lines.append(",".join([d.isoformat(), str(he)] + vals))
    return "\n".join(lines) + "\n"


def ctx(routes: dict[str, str], now=datetime(2026, 11, 5, 20, 0)) -> Context:
    def handler(req):
        for k, body in routes.items():
            if str(req.url).endswith(k):
                return httpx.Response(200, text=body)
        return httpx.Response(404)
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)


def check_quebec_and_net(row: dict[str, float]) -> None:
    """What a correct rollup must give for FLOW (also the target of the seeded red)."""
    assert row["quebec"] == 998 and row["net"] == 2276 and row["minnesota"] == -30 and row["michigan"] == 669


def test_hourly_complete_hours_sign_and_quebec_sum():
    res = build(SPECS["power_ieso_interties"]).run(ctx({"PUB_IntertieScheduleFlow.xml": xml(10)}))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    he10 = datetime(2026, 11, 5, 14)                                   # HE10 EST = 09:00-10:00 EST = 14:00 UTC
    check_quebec_and_net({k: v[(f"ieso.intertie.{k}.flow", he10)] for k in it.KEYS})
    assert ("ieso.intertie.net.flow", datetime(2026, 11, 5, 15)) not in v          # HE11 has 1 of 12 actuals
    assert {m.unit for m in res.meta} == {"MW"} and SPECS["power_ieso_interties"]["region"] == "ont"


def test_daily_history_and_trend_means(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    days = [date(2026, 10, d) for d in range(1, 32)] + [date(2026, 11, d) for d in range(1, 5)]
    falling = lambda d, z: FLOW[z] - (days.index(d) * 10 if z == "PQ.AT" else 0)   # Quebec falls 10 MW a day
    spec = {**SPECS["power_ieso_interties_daily"], "params": {"report": "daily", "from_year": 2026}}
    res = build(spec).run(ctx({"PUB_IntertieScheduleFlowYear_2026.csv": year_csv(days, falling)}))
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    n4 = datetime(2026, 11, 4)
    assert v[("ieso.intertie.quebec.flow_daily", n4)] == 998 - 340
    assert v[("ieso.intertie.quebec.flow_7d", n4)] == pytest.approx(998 - 310)      # mean of days 28..34
    assert v[("ieso.intertie.quebec.flow_30d", n4)] == pytest.approx(998 - 195)     # mean of days 5..34
    assert v[("ieso.intertie.quebec.flow_daily", n4)] < v[("ieso.intertie.quebec.flow_7d", n4)] < \
        v[("ieso.intertie.quebec.flow_30d", n4)]                                     # declining reads at a glance
    assert ("ieso.intertie.quebec.flow_30d", datetime(2026, 10, 29)) not in v       # needs 30 days
    assert v[("ieso.intertie.net.flow", datetime(2026, 10, 1, 5))] == 2276          # hourly history, HE1 EST


def test_unknown_zone_fails_closed():
    with pytest.raises(FormatDrift, match="unknown zone"):
        it.parse_daily_xml(xml(1, ZONES + ["NEW-ENGLAND"], {**FLOW, "NEW-ENGLAND": 5}))


def test_seeded_red_dropping_pq_subinterties_is_caught(monkeypatch):
    real = it.zone_key
    monkeypatch.setattr(it, "zone_key", lambda n: "other" if n.startswith("PQ.") and n != "PQ.AT" else real(n))
    monkeypatch.setattr(it, "KEYS", it.KEYS + ("other",))
    _, hours = it.parse_daily_xml(xml(1))
    with pytest.raises(AssertionError):
        check_quebec_and_net(hours[1])                                 # Quebec must be every PQ.* path


def test_ontario_box_rows_and_labels():
    fc = network.load()
    assert not fc["errors"], fc["errors"]
    root = Path(network.__file__).resolve().parents[1]
    on = next(f["properties"] for f in json.loads((root / "network/power/provinces.geojson").read_text("utf-8"))[
        "features"] if f["properties"]["id"] == "power.region.on")
    declared = {m.series_id for s in ("power_ieso_interties", "power_ieso_interties_daily")
                for m in build(SPECS[s]).declared_series()}
    rows = {r["series"]: r for r in on["callouts_extra"] if "intertie" in r["series"]}
    assert set(rows) <= declared
    top = {s for s, r in rows.items() if r["priority"] == 1}
    assert top == {"ieso.intertie.quebec.flow", "ieso.intertie.quebec.flow_7d", "ieso.intertie.quebec.flow_30d",
                   "ieso.intertie.net.flow", "ieso.intertie.net.flow_7d"}
    assert "(+)" in rows["ieso.intertie.quebec.flow"]["label"] and "(-)" in rows["ieso.intertie.net.flow"]["label"]
    assert all(rows[f"ieso.intertie.{k}.flow"]["priority"] == 2 for k in ("michigan", "new_york", "minnesota",
                                                                           "manitoba", "manitoba_sk"))
