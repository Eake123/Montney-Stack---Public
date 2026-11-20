"""Peace dams heads + estimated MW (montney/connectors/weather_peace_mw.py).

Hydromet fixtures are synthetic text in BC Hydro's real layout (fixed-width columns with blanks, newest row first,
"(PCT)" label). The values are made up, so no BC Hydro data is committed (L-041: never republished).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import weather_peace_mw as pm
from montney.connectors.base import Context

SPECS = load_specs(include_disabled=True)
NOW = datetime(2026, 11, 1, 18, 39)

HDR_DIR = ("          **   BC HYDRO - HYDROCLIMATE MONITORING AND DATA MANAGEMENT   **\n\n"
           "CLIMATE, SNOW AND/OR SURFACE WATER STATION: Test Res Forebay - operated by: BC Hydro (tst)\n\n"
           "      Date    Time      Water level      Water temperature      \n"
           "(yyyy/mm/dd) (PCT)         (m)               (°C)            \n")
HDR_LST = ("CLIMATE, SNOW AND/OR SURFACE WATER STATION: Test Lake - operated by: ECCC Water Survey of Canada (07XX000) (tst)\n\n"
           "      Date    Time      Precipitation      Air Temperature      Water level      Water level      \n"
           "(yyyy/mm/dd) (PCT)          (mm)                (°C)                (m)              (m)          \n")


def dir_text(rows):   # rows: (local stamp, level, temp or None), newest first
    return HDR_DIR + "".join(f"{s}         {lv:.3f}" + (f"                {t:.1f}" if t is not None else "") + "            \n"
                             for s, lv, t in rows)


def lst_text(rows):   # rows: (stamp, precip or None, air or None, level1, level2)
    out = HDR_LST
    for s, p, a, l1, l2 in rows:
        line = f"{s}"
        line += f"          {p:6.1f}" if p is not None else " " * 16
        line += f"              {a:6.1f}" if a is not None else " " * 20
        out += line.ljust(65) + f"{l1:.3f}             {l2:.3f}         \n"
    return out


# ---------------------------------------------------------------- hydromet parsing
def test_parse_dir_layout_pacific_time_utc7():
    text = dir_text([("2026/11/01 11:10:00", 501.973, None), ("2026/11/01 11:05:00", 501.976, 8.4)])
    pts, warns = pm.parse_hydromet(text, NOW)
    assert warns == [] and pts == {datetime(2026, 11, 1, 18, 10): 501.973, datetime(2026, 11, 1, 18, 5): 501.976}


def test_parse_takes_the_first_water_level_column_not_precip():
    """lst has precipitation, air temperature and TWO water levels with blanks: a whitespace split would grab the
    wrong number. The value must come from under the first 'Water level' header."""
    text = lst_text([("2026/11/01 11:15:00", None, None, 669.299, 669.275),
                     ("2026/11/01 11:10:00", 2.5, -3.1, 669.301, 669.276)])
    pts, warns = pm.parse_hydromet(text, NOW)
    assert warns == [] and pts == {datetime(2026, 11, 1, 18, 15): 669.299, datetime(2026, 11, 1, 18, 10): 669.301}


def test_seeded_red_a_fixed_utc8_clock_puts_stamps_in_the_future(monkeypatch):
    """The old "(PST)" reading as a fixed UTC-8 makes the newest 11:15 into 19:15Z, 36 min after the 18:39Z fetch. The
    guard must refuse the file; that's how the UTC-7 finding was made."""
    monkeypatch.setattr(pm, "BCH_CLOCK", pm.ZoneInfo("Etc/GMT+8"))
    pts, warns = pm.parse_hydromet(dir_text([("2026/11/01 11:15:00", 501.97, None)]), NOW)
    assert pts == {} and "future" in warns[0]


def test_parse_requires_a_pacific_time_label():
    utc = HDR_DIR.replace("(PCT)", "(UTC)") + "2026/11/01 11:10:00         501.973" + chr(10)
    pts, warns = pm.parse_hydromet(utc, NOW)
    assert pts == {} and "isn't Pacific time" in warns[0]
    old = HDR_DIR.replace("(PCT)", "(PST)") + "2026/10/31 17:00:00         501.854" + chr(10)
    assert pm.parse_hydromet(old, NOW)[0] == {datetime(2026, 11, 1, 0, 0): 501.854}      # PDT then: UTC-7


def test_parse_rejects_bad_units_and_warns_stale():
    bad = HDR_DIR.replace("(m)", "(ft)") + "2026/11/01 11:10:00         1646.900\n"
    assert pm.parse_hydromet(bad, NOW)[0] == {}
    old, warns = pm.parse_hydromet(dir_text([("2026/11/01 05:00:00", 501.9, None)]), NOW)
    assert old and "over 3 h old" in warns[0]
    assert pm.parse_hydromet("<html>WAF</html>", NOW)[1] == ["no 'Water level' column in the header"]


# ---------------------------------------------------------------- the estimate
END = datetime(2026, 11, 1, 18)


def series(value, rise=0.0, step=5):
    """Five-minute points over the 24 h window ending at END; `rise` = total linear change over the window (m)."""
    n = 24 * 60 // step
    return {END - timedelta(minutes=step * i): value + rise * (1 - i / n) for i in range(n)}


def inputs(**over):
    base = {"07FA004.q": series(1000.0), "07FA004.elev": series(407.0), "07FA006.q": series(20.0),
            "07FB008.q": series(5.0), "07EF003.elev": series(669.3), "lst": series(669.32), "dir": series(502.0),
            "cta": series(461.5)}
    base.update(over)
    return base


def test_estimate_physics_and_bands():
    est, warns = pm.estimate(inputs(), END)
    assert warns == [] and set(est) == {"site_c", "peace_canyon", "wac_bennett"}
    sc, pc, gm = est["site_c"], est["peace_canyon"], est["wac_bennett"]
    assert sc["q"] == pytest.approx(1000) and sc["h"] == pytest.approx(461.5 - 407.0 - 1.5)            # 53.0 m
    assert sc["mw_est"] == pytest.approx(9.81 * 0.90 * 1000 * 53.0 / 1000)                             # 467.9 MW
    assert pc["q"] == pytest.approx(975) and pc["h"] == pytest.approx(502.0 - 461.5 - 0.5)              # no storage change
    assert gm["h"] == pytest.approx(669.3 - 502.0) and gm["head_source"] == "measured"
    for e in est.values():                                     # always a band, never a bare number
        assert e["mw_lo"] < e["mw_est"] < e["mw_hi"]
        assert e["mw_hi"] - e["mw_est"] == pytest.approx(2 * e["eps"] * e["mw_est"])
        assert e["eps"] == pytest.approx(math.sqrt(e["eps_q"] ** 2 + e["eps_h"] ** 2 + pm.EPS_ETA ** 2))
    assert sc["eps_q"] == pytest.approx(0.014 + 0.026)         # DECISIONS discrepancy #2: |b| + sigma_g
    assert 0.07 < sc["eps"] < 0.09                             # ~ +/-15-17 % at 2 sigma: Power's +/-15-20 % daily


def test_site_c_storage_term_feeds_peace_canyon():
    est, _ = pm.estimate(inputs(cta=series(461.5, rise=0.10)), END)
    storage = pm.A_SITEC_M2 * 0.10 / 86400                      # ~108 m3/s
    first, last = [461.5 + 0.10 * (1 - i / 288) for i in range(288)][-12:], [461.5 + 0.10 * (1 - i / 288) for i in range(12)]
    dh = sum(last) / 12 - sum(first) / 12                       # end_change over the window's first/last hour
    assert est["peace_canyon"]["q"] == pytest.approx(1000 - 25 + pm.A_SITEC_M2 * dh / 86400)
    assert est["peace_canyon"]["q"] > 975 + 0.9 * storage
    assert est["peace_canyon"]["eps_q"] > est["site_c"]["eps_q"]


def test_missing_head_falls_back_to_typical_with_a_wider_band():
    est, warns = pm.estimate(inputs(dir={}), END)
    assert est["peace_canyon"]["head_source"] == "typical" and est["peace_canyon"]["h"] == 39.7
    assert est["wac_bennett"]["head_source"] == "typical" and est["site_c"]["head_source"] == "measured"
    assert any("Q4 fallback (c)" in w for w in warns)
    measured, _ = pm.estimate(inputs(), END)
    assert est["wac_bennett"]["eps"] > measured["wac_bennett"]["eps"]
    williston_from_lst, _ = pm.estimate(inputs(**{"07EF003.elev": {}}), END)
    assert williston_from_lst["wac_bennett"]["h"] == pytest.approx(669.32 - 502.0)                  # BC Hydro lst


def test_thin_flow_is_no_result_never_a_guess():
    thin = {END - timedelta(minutes=5 * i): 1000.0 for i in range(100)}                             # 100 < 144 points
    est, warns = pm.estimate(inputs(**{"07FA004.q": thin}), END)
    assert est == {} and "NO-RESULT" in warns[0]
    est, warns = pm.estimate(inputs(**{"07FA006.q": {}}), END)
    assert set(est) == {"site_c"} and any("no Peace Canyon / GM Shrum estimate" in w for w in warns)


def test_seeded_red_a_bare_number_is_caught(monkeypatch):
    """With every error term zeroed the band collapses: lo == est == hi. The band check must fail."""
    for k in ("B_Q", "SIGMA_Q", "EPS_TRIB", "EPS_LOCAL", "EPS_AREA", "DATUM_M", "SITEC_SLOPE_SD", "PCN_BACKWATER_SD",
              "EPS_ETA", "A_DINO_MAX_M2"):
        monkeypatch.setattr(pm, k, 0.0)
    est, _ = pm.estimate(inputs(), END)
    assert not all(e["mw_lo"] < e["mw_est"] < e["mw_hi"] for e in est.values())


# ---------------------------------------------------------------- connector (mocked HTTP)
def wsc_rows(stn, q=None, lv=None):
    return [{"properties": {"STATION_NUMBER": stn, "DATETIME": (END - timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "DISCHARGE": q, "LEVEL": lv}} for i in range(300)]


def seiche(t):          # a 3 h, 6 mm lake oscillation: enough signal to align the clocks
    return 0.006 * math.sin(2 * math.pi * (t - END).total_seconds() / 10800)


def handler(fail=(), shift_min=0):
    hydro = {"dir": dir_text([((END - timedelta(hours=7, minutes=5 * i)).strftime("%Y/%m/%d %H:%M:%S"), 502.0, None)
                              for i in range(300)]),
             "cta": dir_text([((END - timedelta(hours=7, minutes=5 * i)).strftime("%Y/%m/%d %H:%M:%S"), 461.5, None)
                              for i in range(300)]),
             "lst": lst_text([((END - timedelta(hours=7, minutes=5 * i)).strftime("%Y/%m/%d %H:%M:%S"), None, None,
                               669.32 + seiche(END - timedelta(minutes=5 * i - shift_min)), 669.30) for i in range(300)])}
    def h(req):
        if "bchydro" in req.url.host:
            code = req.url.path.rsplit("/", 1)[1][:-4]
            return httpx.Response(404 if code in fail else 200, text=hydro.get(code, ""))
        raise AssertionError(f"no WSC request expected (L-081: read from the store): {req.url}")
    return h


def stored_wsc():
    """What weather_wsc has stored (L-081): the same fixture values, elevations with the HYDAT factors applied."""
    ts = [END - timedelta(minutes=5 * i) for i in range(300)]
    return {"wsc.realtime.07fa004.discharge": {t: 1000.0 for t in ts},
            "wsc.realtime.07fa004.elevation": {t: 1.109 + 405.891 for t in ts},
            "wsc.realtime.07fa006.discharge": {t: 20.0 for t in ts},
            "wsc.realtime.07fb008.discharge": {t: 5.0 for t in ts},
            "wsc.realtime.07ef003.elevation": {t: 39.24 + 630.060 for t in ts},
            "wsc.realtime.07ef002.elevation": {t: 39.28 + 630.018 + seiche(t) for t in ts}}


def run(spec=None, fail=(), shift_min=0):
    from unittest import mock

    conn = build(spec or SPECS["weather_peace_mw"])
    with mock.patch.object(pm, "read_stored", lambda ids, start, end: {i: v for i, v in stored_wsc().items() if i in ids}):
        return conn.run(Context(client=httpx.Client(transport=httpx.MockTransport(handler(fail, shift_min))), now=NOW))


def test_connector_end_to_end():
    res = run()
    ids = {o.series_id for o in res.obs}
    assert {"bchydro.hydromet.dir.level", "power.hydro.site_c.mw_est", "power.hydro.wac_bennett.mw_hi",
            "power.hydro.peace_canyon.head"} <= ids
    v = {o.series_id: o.value for o in res.obs if o.ts == END}
    assert v["power.hydro.site_c.head"] == pytest.approx(461.5 - (1.109 + 405.891) - 1.5, abs=1e-3)   # CGVD2013 tail
    assert v["power.hydro.wac_bennett.head"] == pytest.approx(39.24 + 630.060 - 502.0, abs=1e-3)       # 07EF003
    assert all(o.as_of == o.ts for o in res.obs) and res.warnings == []


def test_connector_bc_hydro_down_still_estimates_with_typical_heads():
    res = run(fail={"dir", "cta", "lst"})
    v = {o.series_id: o.value for o in res.obs}
    assert v["power.hydro.site_c.head"] == 51.0 and v["power.hydro.peace_canyon.head"] == 39.7
    assert any("Q4 fallback (c)" in w for w in res.warnings)


def test_shipped_spec():
    spec = SPECS["weather_peace_mw"]
    assert spec["retention"] == {"default": "hourly", "bchydro.hydromet.*": "fast"}
    ids = [m.series_id for m in build(spec).declared_series()]
    assert len(ids) == 18 and all(m.freq in ("1h", "5min") for m in build(spec).declared_series())


# ---------------------------------------------------------------- the clock check (lst vs WSC 07EF002)
def lake(shift=0, amp=0.006):
    return {END - timedelta(minutes=5 * i): 669.3 + amp * math.sin(2 * math.pi * ((5 * i + shift) * 60) / 10800)
            for i in range(288)}


def test_clock_check_ok_shifted_inconclusive():
    wsc = {t: v - 630.04 for t, v in lake().items()}
    assert pm.clock_check(lake(), wsc)[0] == "ok"
    state, detail = pm.clock_check(lake(shift=60), wsc)        # BC Hydro stamps an hour off
    assert state == "shifted" and "clock changed" in detail
    flat = {t: 669.3 for t in lake()}
    state, detail = pm.clock_check(flat, {t: 39.26 for t in flat})
    assert state == "inconclusive" and "lake signal" in detail       # was float-noise "shifted" on Windows (#146)
    assert pm.clock_check({}, wsc)[0] == "inconclusive"


def test_clock_check_mm_quantised_calm_lake_is_never_shifted():
    """BC Hydro and WSC both report to the mm. A calm lake drifting < 1 mm, or a tiny aligned seiche, must not refuse
    the hydromet data on rounding (Reviewer, #146)."""
    times = list(lake())
    drift = {t: round(669.3 + 0.0015 * i / len(times), 3) for i, t in enumerate(times)}          # 0-1.5 mm drift
    assert pm.clock_check(drift, {t: v - 630.04 for t, v in drift.items()})[0] == "inconclusive"
    small = {t: round(v, 3) for t, v in lake(amp=0.0018).items()}                                   # 1.8 mm seiche, mm steps
    state, _ = pm.clock_check(small, {t: round(v - 630.04, 3) for t, v in small.items()})
    assert state != "shifted"


def test_seeded_red_bc_hydro_clock_shift_refuses_all_hydromet():
    """If BC Hydro's stamps move by an hour (e.g. back to UTC-8, invisible to the future guard), the WSC cross-check
    must refuse every hydromet file, and the plants fall back to typical heads. Never shift the data silently."""
    res = run(shift_min=60)
    assert any("clock changed" in w and "refused" in w for w in res.warnings)
    assert not any(o.series_id.startswith("bchydro.hydromet.") for o in res.obs)
    v = {o.series_id: o.value for o in res.obs}
    assert v["power.hydro.peace_canyon.head"] == 39.7 and v["power.hydro.site_c.head"] == 51.0


def test_raw_bc_hydro_levels_never_on_the_map():
    """Q4 (L-041): only derived estimates go on the map. No network feature may reference a bchydro.hydromet series."""
    from pathlib import Path

    net = Path(__file__).parents[1] / "network"
    hits = [p.name for p in net.rglob("*.geojson") if "bchydro.hydromet." in p.read_text(encoding="utf-8")]
    assert list(net.rglob("*.geojson")) and hits == []


# ---------------------------------------------------------------- pre-2026 PST/PDT parsing must fail (orchestrator, #146)
OLD_RULES = pm.ZoneInfo("America/Los_Angeles")   # US Pacific: still PDT -> PST on 2026-11-01, i.e. BC's pre-2026 rules


def test_zone_is_the_named_vancouver_zone():
    assert str(pm.BCH_CLOCK) == "America/Vancouver"
    assert datetime(2026, 11, 10, 12).replace(tzinfo=pm.BCH_CLOCK).utcoffset() == timedelta(hours=-7)
    assert datetime(2026, 11, 10, 12).replace(tzinfo=OLD_RULES).utcoffset() == timedelta(hours=-8)


def test_seeded_red_old_rules_on_todays_file_hit_the_future_guard(monkeypatch):
    """Today's file (newest 11:15 local at the 18:39Z fetch) read on pre-2026 rules: after the fall-back that's PST,
    so 19:15Z, in the future. Refused."""
    monkeypatch.setattr(pm, "BCH_CLOCK", OLD_RULES)
    pts, warns = pm.parse_hydromet(dir_text([("2026/11/01 11:15:00", 501.97, None)]), NOW)
    assert pts == {} and "future" in warns[0]


def test_seeded_red_old_rules_on_a_post_switch_week_fail_the_wsc_cross_check(monkeypatch):
    """A full week after the switch, stamped by BC Hydro in UTC-7 and read on pre-2026 rules (PST, UTC-8): every stamp is
    1 h late. With the usual ~25 min data lag the future guard catches that. When the data lag more than an hour,
    nothing lies in the future, and only the 07EF002 cross-check catches it, as "shifted". Vancouver's reading of the
    same file stays "ok"."""
    end = datetime(2026, 11, 10, 18, 0)                  # UTC
    utc_times = [end - timedelta(minutes=5 * i) for i in range(7 * 288)]
    wave = lambda t: 0.006 * math.sin(2 * math.pi * (t - end).total_seconds() / 10800)
    local = [(t - timedelta(hours=7)).strftime("%Y/%m/%d %H:%M:%S") for t in utc_times]       # true clock: UTC-7
    text = lst_text([(s, None, None, 669.30 + wave(t), 669.28) for s, t in zip(local, utc_times)])
    wsc = {t: 39.28 + wave(t) for t in utc_times}
    now = end + timedelta(minutes=90)                     # data 90 min old: an old-rules hour shift isn't "future"
    good, _ = pm.parse_hydromet(text, now)
    assert pm.clock_check(good, wsc)[0] == "ok"
    monkeypatch.setattr(pm, "BCH_CLOCK", OLD_RULES)
    bad, warns = pm.parse_hydromet(text, now)
    assert bad and warns == []                           # the future guard can't see it...
    assert pm.clock_check(bad, wsc)[0] == "shifted"      # ...the WSC cross-check does


def test_mw_labels_say_24h_mean_ending_at_ts():
    metas = {m.series_id: m.label for m in build(SPECS["weather_peace_mw"]).declared_series()}
    for k in ("wac_bennett", "peace_canyon", "site_c"):
        for m in ("mw_est", "mw_lo", "mw_hi", "head", "flow"):
            assert "24 h mean ending at ts" in metas[f"power.hydro.{k}.{m}"], (k, m)
