"""SPP West (SWPW) (PLAN 9 item 1, mtnw): mocked gen-mix-swpw + interchange-trend-swpw CSVs in the live shape
(real rows, fetched 2026-11-13 ~16:40 UTC)."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_spp_west as spp
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
GEN_HEAD = ("GMT MKT Interval,BAA,Coal,Diesel Fuel Oil,Hydro,Natural Gas,Nuclear,Solar,Waste Disposal Services,Wind,"
            "Waste Heat,Energy Storage,Other,Load\n")
# Real gen-mix-swpw (the whole file: the last 2 h, stamps are interval ENDS): 15:05 .. 16:00 is the 15:00 hour.
GEN = GEN_HEAD + """\
2026-11-13T14:45:00Z,SWPW,1151.40,0.00,498.80,515.40,0.00,162.40,0.00,319.10,0.00,-12.40,0.00,2924.330
2026-11-13T14:50:00Z,SWPW,1157.70,0.00,496.70,508.80,0.00,202.80,0.00,310.30,0.00,3.10,0.00,2912.970
2026-11-13T14:55:00Z,SWPW,1139.10,0.00,495.70,493.60,0.00,251.20,0.00,308.70,0.00,-16.20,0.00,2900.060
2026-11-13T15:00:00Z,SWPW,1139.90,0.00,495.40,489.00,0.00,283.90,0.00,309.60,0.00,1.10,0.00,2896.180
2026-11-13T15:05:00Z,SWPW,1193.30,0.00,461.30,584.90,0.00,321.40,0.00,311.60,0.00,19.70,0.00,2896.720
2026-11-13T15:10:00Z,SWPW,1180.40,0.00,464.70,532.60,0.00,374.80,0.00,313.50,0.00,-2.10,0.00,2894.410
2026-11-13T15:15:00Z,SWPW,1160.20,0.00,467.70,516.20,0.00,417.00,0.00,314.40,0.00,-5.10,0.00,2879.660
2026-11-13T15:20:00Z,SWPW,1152.80,0.00,466.80,517.50,0.00,454.00,0.00,314.50,0.00,-5.00,0.00,2876.760
2026-11-13T15:25:00Z,SWPW,1137.20,0.00,462.90,508.30,0.00,492.60,0.00,311.40,0.00,-8.50,0.00,2862.050
2026-11-13T15:30:00Z,SWPW,1132.90,0.00,461.50,514.60,0.00,509.10,0.00,305.60,0.00,-11.70,0.00,2856.820
2026-11-13T15:35:00Z,SWPW,1120.20,0.00,464.10,497.20,0.00,533.40,0.00,300.80,0.00,-12.90,0.00,2848.220
2026-11-13T15:40:00Z,SWPW,1113.50,0.00,460.80,492.90,0.00,542.70,0.00,298.40,0.00,-16.50,0.00,2833.150
2026-11-13T15:45:00Z,SWPW,1103.10,0.00,462.50,454.20,0.00,561.10,0.00,295.10,0.00,-19.90,0.00,2831.150
2026-11-13T15:50:00Z,SWPW,1094.70,0.00,463.00,449.50,0.00,581.20,0.00,296.20,0.00,-23.10,0.00,2822.270
2026-11-13T15:55:00Z,SWPW,1089.60,0.00,466.70,454.70,0.00,610.80,0.00,292.70,0.00,-31.00,0.00,2822.970
2026-11-13T16:00:00Z,SWPW,1084.40,0.00,462.40,454.90,0.00,638.70,0.00,300.00,0.00,-34.90,0.00,2819.030
2026-11-13T16:05:00Z,SWPW,1086.20,0.00,412.70,501.10,0.00,642.10,0.00,298.20,0.00,-36.60,0.00,2817.080
2026-11-13T16:10:00Z,SWPW,1077.90,0.00,410.20,500.30,0.00,620.80,0.00,297.00,0.00,-37.20,0.00,2814.600
2026-11-13T16:15:00Z,SWPW,1093.10,0.00,412.40,466.10,0.00,598.00,0.00,290.50,0.00,-36.10,0.00,2808.800
2026-11-13T16:20:00Z,SWPW,1105.60,0.00,414.60,436.00,0.00,607.90,0.00,283.90,0.00,-34.10,0.00,2799.350
2026-11-13T16:25:00Z,SWPW,1090.70,0.00,414.40,427.20,0.00,631.90,0.00,279.60,0.00,-37.20,0.00,2793.780
2026-11-13T16:30:00Z,SWPW,1097.10,0.00,412.10,436.00,0.00,625.30,0.00,285.30,0.00,-34.70,0.00,2782.040
2026-11-13T16:35:00Z,SWPW,1099.70,0.00,411.60,436.00,0.00,594.70,0.00,285.50,0.00,-34.80,0.00,2783.020
2026-11-13T16:40:00Z,SWPW,1081.90,0.00,411.10,409.00,0.00,641.70,0.00,289.80,0.00,-49.70,0.00,2778.340
"""
TREND_HEAD = "GMTTime,SWPW NSI,SWPW NAI,APS,BHBA,NWMT,PACE,PNM,PSCO,WALC,SWPW NSI Future\n"
# Real interchange-trend-swpw rows 15:01 .. 16:00 (the 15:00 hour): minute, NAI, APS, BHBA, NWMT, PACE, PNM, PSCO, WALC
H15 = """\
15:01,49.47,80.4,180.5,21.75,33.08,99.87,-618.48,136.85
15:02,73.14,91.39,180.2,13.2,50.97,142.2,-664.83,144.59
15:03,75.43,83.59,180.6,8.19,42.96,176.61,-676.17,146.04
15:04,84.17,86.2,182.5,9.73,50.35,181.07,-683.24,147.16
15:05,82.18,79.29,173.9,11.99,44.74,182.52,-666.35,145.98
15:06,12.23,81.3,161.5,14.46,24.58,180.12,-705.28,147.44
15:07,0.08,77.39,149.6,16.01,-6.31,151.11,-630.8,139.97
15:08,-31.81,78.99,154.6,25.59,5.95,153.71,-687.49,140.84
15:09,-11,78.61,163.1,29.07,17.78,159.58,-692.99,142.76
15:10,-19.49,80.81,154.1,28.65,-4.97,155.63,-665.58,145.86
15:11,-39.6,86.1,158.6,35.09,-6.54,141.43,-684.58,145.2
15:12,-61.71,89.11,154.5,38.46,-7.71,133.29,-701.44,146.98
15:13,-66.78,87.1,148.3,39.91,-40.22,134.65,-673.08,150.47
15:14,-77.06,84.71,146,38.03,-44.08,116.36,-649.13,142.95
15:15,-53.28,78,149,41.93,-39.85,136.01,-649.37,143.99
15:16,-47.69,75.3,151.1,38,-33.73,140.21,-650.11,141.55
15:17,-44.33,73.71,148.5,32.81,-36.29,145.53,-646.89,142.2
15:18,-44.31,78.5,153.4,28.6,-19.16,125.03,-654.75,141.98
15:19,-48.28,90.3,162.1,26.72,-3.62,118.34,-695.57,146.34
15:20,-60.05,96,161.9,23.66,-14.71,103.12,-691.36,149.34
15:21,-63.75,93.2,162.2,23.13,-7.58,109.63,-708.1,150.68
15:22,-52.07,91.99,154.7,22.96,-34.44,110.14,-663.11,152.6
15:23,-52.34,89.41,144.6,26.94,-45.38,112.83,-646.82,153.08
15:24,-58.37,86.7,148.2,19.97,-43.8,110.25,-650.67,150.88
15:25,-47.31,93.29,154,17.62,-14.6,96.42,-669.31,150.18
15:26,-39,97.4,152.1,16.2,-10.61,84.06,-656.79,148.53
15:27,-37.75,99.51,156,8.98,-9.61,77.31,-655.26,150.21
15:28,-37.89,102.39,164.1,5.91,6.97,76.9,-683.11,147.86
15:29,-30.92,101.3,165.5,-0.21,-1.58,77.9,-670.97,151.04
15:30,-32.71,102.71,166.5,-3.81,19.12,58.94,-673.38,144.12
15:31,-47.75,100.2,175.7,-1.81,37.77,41.68,-702.45,144.16
15:32,-30.07,108.09,180.7,-3.64,67.85,46.79,-732.29,142.34
15:33,-18.66,119.19,187.2,-2.55,98.08,42.38,-767.15,144.09
15:34,6.36,120.69,189.7,-3.99,111.43,48.55,-769.6,143.48
15:35,15.96,109.4,181.3,-4.02,80.05,59.72,-724.34,147.76
15:36,11.39,103.4,176.7,-4.31,54.37,62.95,-697.64,150.82
15:37,18.12,97.89,177.1,-5.12,24.61,66.68,-659.45,151.33
15:38,37.11,96.59,187.7,-3.47,19.58,56.33,-634.26,149.54
15:39,45.31,93.89,193.7,-2.89,8.38,71.18,-638.59,154.54
15:40,56.04,94.1,188.9,6.01,-12.45,80.21,-625.42,159.59
15:41,62.46,91.09,179.9,4.19,-27.55,96.56,-613.26,166.43
15:42,61.13,86.99,175.2,12.26,-56.78,110.59,-601.07,168.86
15:43,46.6,87.99,168.3,13.15,-54.75,118.48,-616.76,167.09
15:44,57.37,89.5,170.1,15.46,-55.23,119.68,-609.83,166.19
15:45,75.19,83.01,167.4,20.19,-63.38,134.15,-588.06,165.38
15:46,99.7,94.29,159.8,27.39,-69.15,134.99,-571.07,169.46
15:47,106.56,99.8,165.1,33.17,-58.24,157,-620.46,177.49
15:48,106.37,95.41,168.3,32.98,-74.27,170.48,-619.27,180.46
15:49,93.42,86.9,161,32.82,-97.79,191.05,-618.08,186.51
15:50,96.07,90.39,171.3,39.61,-85.85,196.4,-657.57,187.3
15:51,107.89,80.3,173.6,42.85,-101.23,234.78,-670.28,190.27
15:52,99.25,85.1,172.9,46.35,-91.29,233.51,-699.52,194.51
15:53,85.12,86.41,169,47.71,-104.22,220.19,-689.28,195.8
15:54,62.19,80.39,164.5,45.1,-116.01,207.54,-678.26,192.63
15:55,53.41,78.7,163.1,42.84,-111.75,185.72,-663.26,186.27
15:56,52.58,79.4,168.1,43.83,-110.98,177.19,-668.27,184.01
15:57,54.77,72.69,163.6,38.14,-122.23,176.88,-644.21,180
15:58,72.86,71.5,168.5,36.26,-110.36,169.16,-639.28,178.98
15:59,70.65,72.59,164.5,31.92,-115.83,182.25,-650.86,185.37
16:00,62.76,58.11,171.2,26.5,-137.52,198.67,-650.5,181.29"""
DECLARED = {m.series_id for m in build(SPECS["power_spp_west"]).declared_series()}
# The lead's proposed mtnw parts (regions/mtnw.yaml isn't wired in this PR): all must be declared.
PROPOSED = {"sppw.load", "sppw.gen.total", "sppw.interchange.net", "sppw.gen.gas", "sppw.gen.coal", "sppw.gen.nuclear",
            "sppw.gen.hydro", "sppw.gen.wind", "sppw.gen.solar", "sppw.gen.battery", "sppw.gen.oil", "sppw.gen.other",
            "sppw.gen.waste", "sppw.gen.waste_heat"}


def trend() -> str:
    """The real hour, then a partial 16:xx (two rows) and two future rows (NSI Future only), as the live file has."""
    rows = [f"2026-11-13T{r[:5]}:00Z,5.6,{r[6:]},-164.00" for r in H15.splitlines()]
    rows += ["2026-11-13T16:01:00Z,5.6,60.0,58,171,26,-137,198,-650,181,-164.00",
             "2026-11-13T16:02:00Z,5.6,61.0,58,171,26,-137,198,-650,181,-164.00",
             "2026-11-13T16:41:00Z,,,,,,,,,,-164.00", "2026-11-13T16:42:00Z,,,,,,,,,,-164.00"]
    return TREND_HEAD + "\n".join(rows) + "\n"


def run(gen=GEN, tr=None, *, trend_status=200):
    tr = trend() if tr is None else tr

    def handler(req):
        if "gen-mix-swpw" in str(req.url):
            return httpx.Response(200, text=gen)
        if "interchange-trend-swpw" in str(req.url):
            return httpx.Response(trend_status, text=tr)
        return httpx.Response(404)
    c = Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=datetime(2026, 11, 13, 16, 41))
    return build(SPECS["power_spp_west"]).run(c)


def test_interval_end_hours_and_identity():
    res = run()
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    h = datetime(2026, 11, 13, 15)
    assert {o.ts for o in res.obs} == {h}                    # 14:xx and 16:xx are partial in the 2 h / 1-min windows
    assert {o.series_id for o in res.obs} == DECLARED
    rows = [r for r in GEN.splitlines()[1:] if "2026-11-13T15:05" <= r[:16] <= "2026-11-13T16:00"]
    assert len(rows) == 12
    load = round(sum(float(r.split(",")[-1]) for r in rows) / 12, 1)
    assert v[("sppw.load", h)] == load
    assert v[("sppw.gen.coal", h)] == round(sum(float(r.split(",")[2]) for r in rows) / 12, 1)
    nai = sum(float(r.split(",")[1]) for r in H15.splitlines()) / 60
    assert v[("sppw.interchange.net", h)] == round(-nai, 1)          # SPP's NAI is + export -> our + import
    assert v[("sppw.tie.psco.flow", h)] < -500                           # SPP's sign, unchanged
    gen = v[("sppw.gen.total", h)]
    assert gen == pytest.approx(sum(v[(f"sppw.gen.{k}", h)] for k in spp.FUELS.values()), abs=0.1)
    # generation + net imports ~ load (real hour: 2,887.0 - 15.7 vs 2,853.6, i.e. +18 MW)
    assert abs(gen + v[("sppw.interchange.net", h)] - v[("sppw.load", h)]) < 60


def test_interchange_network_error_is_a_warning_and_gen_still_written():
    res = run(trend_status=503)
    assert any(o.series_id == "sppw.gen.total" for o in res.obs)
    assert not any(o.series_id.startswith(("sppw.interchange", "sppw.tie")) for o in res.obs)
    assert any("interchange-trend-swpw not read" in w for w in res.warnings)


def test_partial_hour_and_blank_cell_not_written():
    lines = GEN.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("2026-11-13T15:30"))
    lines[i] = "2026-11-13T15:30:00Z,SWPW,,0.00,461.50,514.60,0.00,509.10,0.00,305.60,0.00,-11.70,0.00,2856.820"
    assert spp.parse_gen("\n".join(lines)) == {}                         # 11 of 12 rows: not complete, never a 0
    short = [r for r in H15.splitlines() if not r.startswith("15:30")]
    tr = TREND_HEAD + "\n".join(f"2026-11-13T{r[:5]}:00Z,5.6,{r[6:]}," for r in short)
    assert spp.parse_trend(tr) == {}


@pytest.mark.parametrize("text, needle", [
    (GEN.replace(",Other,Load", ",Other,Geothermal,Load"), "unknown \\['Geothermal'\\]"),
    (GEN.replace(",Wind,", ",Wnd,"), "missing columns \\['Wind'\\]"),
    (GEN.replace("2026-11-13T15:20:00Z", "13/11/2026 15:20"), "doesn't parse"),
    (GEN.replace("2026-11-13T15:20:00Z", "2026-11-13T15:20:00"), "no UTC offset"),
    (GEN.replace("1152.80", "n/a"), "isn't a number"),
    (GEN.replace("1152.80", "91152.80"), "outside"),
    (GEN.replace("2026-11-13T15:20:00Z,SWPW", "2026-11-13T15:20:00Z,SPP"), "not SWPW"),
])
def test_gen_fails_closed(text, needle):
    with pytest.raises(FormatDrift, match=needle):
        spp.parse_gen(text)


@pytest.mark.parametrize("text, needle", [
    (trend().replace(",WALC,", ",WALC,LDWP,"), "unknown \\['LDWP'\\]"),
    (trend().replace(",SWPW NAI,", ",SWPW NAX,"), "missing columns"),
    (trend().replace("62.76", "x"), "isn't a number"),
    (trend().replace("-650.5", "-65000.5"), "outside"),
])
def test_trend_fails_closed(text, needle):
    with pytest.raises(FormatDrift, match=needle):
        spp.parse_trend(text)


def test_no_complete_generation_hour_fails_closed():
    two_rows = GEN_HEAD + "\n".join(GEN.splitlines()[1:3])
    with pytest.raises(FormatDrift, match="no complete hour"):
        run(gen=two_rows)


def test_series_declared_hourly_and_proposed_parts_exist():
    metas = build(SPECS["power_spp_west"]).declared_series()
    assert all(m.freq == "1h" and m.unit == "MW" and m.extra["tier"] == "hourly" for m in metas)
    assert all(m.extra["region"] == "mtnw" for m in metas)
    assert PROPOSED <= DECLARED
    spec = SPECS["power_spp_west"]
    assert spec["interval"] == "15m" and spec["retention"] == "hourly" and spec["region"] == "rockies"
