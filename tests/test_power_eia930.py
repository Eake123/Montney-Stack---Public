"""EIA-930 per-BA hourly grid (L-106, PLAN 8b): mocked Grid Monitor 930-api payloads in the live shape (2026-11-07)."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_eia930 as e9
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
DATES = ["11/07/2026 15:00:00", "11/07/2026 16:00:00", "11/07/2026 17:00:00"]   # UTC hour ending


def region(rows):
    return [{"data": [{"RESPONDENT_ID": ba, "TYPE_ID": t, "VALUES": {"DATES": DATES, "DATA": vals}}
                      for ba, t, vals in rows]}]


def fuel(rows):
    return [{"data": [{"RESPONDENT_ID": ba, "TYPE_ID": "NG", "FUEL_TYPE_ID": code, "FUEL_TYPE_NAME": code,
                       "VALUES": {"DATES": DATES, "DATA": vals}} for ba, code, vals in rows]}]


def ctx(routes):
    seen = []

    def handler(req):
        seen.append(req.url.path.rsplit("/930-api/", 1)[1])
        q = str(req.url.query)
        for key, body in routes.items():
            if req.url.path.endswith(key[0]) and key[1] in q:
                return httpx.Response(200, json=body)
        return httpx.Response(404)
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 7, 18, 20)), seen


def test_demand_hour_start_aggregates_dropped_nulls_skipped():
    c, seen = ctx({("region_data/series_data", "D"): region([("CISO", "D", [25000, None, 25857]),
                                                             ("US48", "D", [1, 2, 3]), ("CAL", "D", [1, 2, 3])])})
    res = build(SPECS["power_eia930_demand"]).run(c)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    assert v == {("eia930.ciso.demand", datetime(2026, 11, 7, 14)): 25000,          # HE 15:00 -> hour start 14:00
                 ("eia930.ciso.demand", datetime(2026, 11, 7, 16)): 25857}
    assert len(seen) == 3 and {m.unit for m in res.meta} == {"MW"} and SPECS["power_eia930_demand"]["interval"] == "1h"


def test_gen_interchange_fuels_summed_and_util():
    c, seen = ctx({("region_data/series_data", "NG"): region([("ERCO", "NG", [40000, 41000, 42000]),
                                                              ("ERCO", "TI", [-500, 200, 300])]),
                   ("region_data_by_fuel_type/series_data", ""): fuel([
                       ("ERCO", "NG", [20000, 21000, 22000]), ("ERCO", "SUN", [0, 100, 200]),
                       ("ERCO", "SNB", [10, None, 20]), ("ERCO", "NUC", [3837, 3837, 3837])])})
    res = build(SPECS["power_eia930_gen"]).run(c)
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    h14, h15 = datetime(2026, 11, 7, 14), datetime(2026, 11, 7, 15)
    assert v[("eia930.erco.interchange", h14)] == -500                               # EIA sign: + net export
    assert v[("eia930.erco.gen.solar", h14)] == 10 and ("eia930.erco.gen.solar", h15) not in v   # SNB missing at 16
    cap = e9.json.loads(e9.CAPACITY.read_text(encoding="utf-8"))["ba"]["erco"]["nuclear"]
    assert v[("eia930.erco.util.nuclear", h14)] == pytest.approx(3837 / cap, abs=1e-4)
    assert len(seen) == 6 and SPECS["power_eia930_gen"]["interval"] == "1h"          # 2 endpoints x 3 BA batches
    assert all(m.extra.get("estimate") for m in res.meta if ".util." in m.series_id)


def test_requests_go_in_batches_of_at_most_15_respondents():
    """An unfiltered (all-BA) or 16+-respondent request hangs (2026-11-13): every request names <= 15 respondents, and
    the batches together cover every BA once."""
    asked = []

    def handler(req):
        resp = [v for k, v in req.url.params.multi_items() if k.startswith("respondent[")]
        asked.append(resp)
        return httpx.Response(200, json=region([(resp[0], "D", [1, 2, 3])]))
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 7, 18, 20))
    build(SPECS["power_eia930_demand"]).run(c)
    assert len(asked) == 3 and all(0 < len(r) <= 15 for r in asked)
    flat = [b for r in asked for b in r]
    assert len(flat) == len(set(flat)) == 38 and {"CISO", "AVRN", "ERCO", "PJM"} <= set(flat)


def test_a_failed_batch_is_a_warning_all_failed_raises(monkeypatch):
    monkeypatch.setattr("montney.connectors.base.time.sleep", lambda s: None)       # the retries' backoff
    def handler(req):
        if req.url.params.get("respondent[0]") == "IPCO":
            raise httpx.ReadTimeout("hung", request=req)
        return httpx.Response(200, json=region([(req.url.params["respondent[0]"], "D", [1, 2, 3])]))
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 7, 18, 20))
    res = build(SPECS["power_eia930_demand"]).run(c)
    assert {o.series_id for o in res.obs} == {"eia930.ciso.demand", "eia930.erco.demand"}
    assert any("IPCO..DEAA" in w and "ReadTimeout" in w for w in res.warnings)

    def html(req):                                    # an HTML error page in one batch (Reviewer on #482)
        if req.url.params.get("respondent[0]") == "IPCO":
            return httpx.Response(200, text="<html>busy</html>")
        return httpx.Response(200, json=region([(req.url.params["respondent[0]"], "D", [1, 2, 3])]))
    c = Context(client=httpx.Client(transport=httpx.MockTransport(html)), now=datetime(2026, 11, 7, 18, 20))
    res = build(SPECS["power_eia930_demand"]).run(c)
    assert {o.series_id for o in res.obs} == {"eia930.ciso.demand", "eia930.erco.demand"}
    assert any("IPCO..DEAA" in w and "JSONDecodeError" in w for w in res.warnings)

    def dead(req):
        raise httpx.ReadTimeout("hung", request=req)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(dead)), now=datetime(2026, 11, 7, 18, 20))
    with pytest.raises(httpx.ReadTimeout):
        build(SPECS["power_eia930_demand"]).fetch(c)


def test_unknown_fuel_or_changed_service_fails_closed():
    with pytest.raises(FormatDrift, match="unknown fuel code"):
        e9.parse_fuel(fuel([("ERCO", "HYDROGEN", [1, 2, 3])]))
    with pytest.raises(FormatDrift, match="v2 fallback"):
        e9.parse_region({"response": {"data": []}})                                   # a v2-shaped answer


def test_seeded_red_summing_a_partial_hour_is_caught(monkeypatch):
    """The check can fail: a fuel sum that doesn't require every EIA code would write SUN alone (100) at 16:00 UTC."""
    real = e9.parse_fuel

    def partial(payload):
        out = {}
        for s in e9._series(payload):
            for ts, x in e9._points(s):
                k = (f"eia930.{s['RESPONDENT_ID'].lower()}.gen.{e9.FUEL[s['FUEL_TYPE_ID']]}", ts)
                out[k] = out.get(k, 0) + x
        return [e9.Obs(k[0], k[1], val, k[1]) for k, val in out.items()]
    monkeypatch.setattr(e9, "parse_fuel", partial)
    obs = e9.parse_fuel(fuel([("ERCO", "SUN", [0, 100, 200]), ("ERCO", "SNB", [10, None, 20])]))
    assert ("eia930.erco.gen.solar", datetime(2026, 11, 7, 15)) in {(o.series_id, o.ts) for o in obs}
    monkeypatch.setattr(e9, "parse_fuel", real)
    good = e9.parse_fuel(fuel([("ERCO", "SUN", [0, 100, 200]), ("ERCO", "SNB", [10, None, 20])]))
    assert ("eia930.erco.gen.solar", datetime(2026, 11, 7, 15)) not in {(o.series_id, o.ts) for o in good}
