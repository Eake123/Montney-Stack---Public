from datetime import datetime

import httpx
import pytest

from montney import units
from montney.connectors import build
from montney.connectors.base import Context
from montney.store import Obs, Store


def ctx_for(handler) -> Context:
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)),
                   now=datetime(2026, 10, 31, 19, 30))


def by_id(res):
    out = {}
    for o in res.obs:
        out.setdefault(o.series_id, []).append(o)
    return out


# ------------------------------------------------------------------ units
def test_units():
    assert units.convert(557599.6, "e3m3", "MMcf") == pytest.approx(19691.4, abs=0.1)
    assert units.convert(1000, "MMcf/d", "Bcf/d") == pytest.approx(1.0)
    with pytest.raises(ValueError):
        units.convert(1, "MMcf/d", "MMcf")
    assert units.convert(5, "TJ", "TJ") == 5


# ------------------------------------------------------------- http specs
# NGTL CSR/GDSR spec tests live in tests/test_gas_ngtl.py (gas lane).


def test_aeso_csd_spec(monkeypatch):
    monkeypatch.setenv("AESO_API_KEY", "test-key")
    from montney.connectors import load_specs
    spec = load_specs()["aeso_csd"]
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("API-KEY")
        return httpx.Response(200, json={"responseCode": "200", "return": {
            "effective_datetime_utc": "2026-10-31 19:25",
            "alberta_internal_load": 10123, "total_net_generation": 10500,
            "total_max_generation_capability": 19000, "net_to_grid_generation": 9800,
            "net_actual_interchange": -300,
            "generation_data_list": [
                {"fuel_type": "COGENERATION", "aggregated_net_generation": 3000, "aggregated_maximum_capability": 5000},
                {"fuel_type": "COMBINED CYCLE", "aggregated_net_generation": 2000, "aggregated_maximum_capability": 2500},
                {"fuel_type": "SIMPLE CYCLE", "aggregated_net_generation": 300, "aggregated_maximum_capability": 1200},
                {"fuel_type": "GAS FIRED STEAM", "aggregated_net_generation": 1000, "aggregated_maximum_capability": 1600},
                {"fuel_type": "WIND", "aggregated_net_generation": 2500, "aggregated_maximum_capability": 5000},
            ],
            "interchange_list": [{"path": "British Columbia", "actual_flow": -250}],
        }})

    res = build(spec).run(ctx_for(handler))
    s = by_id(res)
    assert seen["key"] == "test-key"
    assert s["aeso.csd.ail"][0].value == 10123
    assert s["aeso.gen.combined_cycle.net"][0].value == 2000
    assert s["aeso.gen.gas_total.net"][0].value == 6300
    assert s["aeso.intertie.british_columbia"][0].value == -250
    labels = {m.series_id: m.label for m in res.meta}
    assert labels["aeso.gen.combined_cycle.net"] == "AESO Combined Cycle net gen"


def test_aeso_load_skips_blank(monkeypatch):
    monkeypatch.setenv("AESO_API_KEY", "k")
    from montney.connectors import load_specs
    spec = load_specs()["aeso_load"]
    payload = {"return": {"Actual Forecast Report": [
        {"begin_datetime_utc": "2026-10-31 18:00", "alberta_internal_load": "10200", "forecast_alberta_internal_load": "10150"},
        {"begin_datetime_utc": "2026-11-01 18:00", "alberta_internal_load": "", "forecast_alberta_internal_load": "10400"},
    ]}}
    s = by_id(build(spec).run(ctx_for(lambda r: httpx.Response(200, json=payload))))
    assert len(s["aeso.load.ail"]) == 1
    assert len(s["aeso.load.ail_forecast"]) == 2
    assert s["aeso.load.ail_forecast"][0].as_of == datetime(2026, 10, 31, 19, 30)  # fetch vintage


def test_missing_secret_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("AESO_API_KEY", raising=False)
    import montney.secrets as sec
    monkeypatch.setattr(sec.keyring, "get_password", lambda *a: None)
    from montney.connectors import load_specs
    from montney.runner import run_source
    st = Store(tmp_path / "t.duckdb")
    r = run_source(st, load_specs()["aeso_csd"])
    assert not r["ok"] and "AESO_API_KEY" in r["error"]


# ------------------------------------------------------------------ store
def test_store_vintages(tmp_path):
    st = Store(tmp_path / "t.duckdb")
    d = datetime(2026, 11, 5)
    st.write([Obs("wx.x.hdd", d, 10.0, datetime(2026, 10, 30, 12)),
              Obs("wx.x.hdd", d, 14.0, datetime(2026, 10, 31, 12))])
    assert st.series(["wx.x.hdd"])["wx.x.hdd"]["v"] == [14.0]
    old = st.series(["wx.x.hdd"], as_of_max=datetime(2026, 10, 31))["wx.x.hdd"]
    assert old["v"] == [10.0]
    st.write([Obs("live.a", d, 1.0, d)], live=True)
    st.write([Obs("live.a", d, 2.0, d)], live=True)
    assert st.series(["live.a"])["live.a"]["v"] == [2.0]
    assert st.count() == 2  # live rows are not persisted


# ---------------------------------------------------------------- secrets
def test_secrets_roundtrip(monkeypatch):
    import montney.secrets as sec
    vault = {}
    monkeypatch.setattr(sec.keyring, "set_password", lambda s, n, v: vault.__setitem__((s, n), v))
    monkeypatch.setattr(sec.keyring, "get_password", lambda s, n: vault.get((s, n)))
    monkeypatch.delenv("AESO_API_KEY", raising=False)
    sec.set("AESO_API_KEY", sec.clean("\x16 abcd1234efgh \r\n"))
    sec.set("OTHER", "x" * 10)
    sec.set("AESO_API_KEY", "abcd1234efgh")  # re-set doesn't duplicate the index
    assert sec.get("AESO_API_KEY") == "abcd1234efgh"
    assert sec.list_names() == ["AESO_API_KEY", "OTHER"]
    assert sec.masked("abcd1234efgh") == "12 chars, ending ...efgh"
