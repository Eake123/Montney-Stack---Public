"""ISO-NE generation by fuel, load and interchange (PLAN 9 item 1, upne): mocked ISO Express CSVs in the live shape.

The identity hour is real (2026-11-13 08:00-09:00 EST, fetched 15:47 UTC): every fuel-mix snapshot from the one before
08:00 to the first after 09:00, the 12 five-minute load rows, 12 x 8 zone rows and 12 x 7 tie rows.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest

from montney.connectors import build, load_specs
from montney.connectors import power_isone as ne
from montney.connectors.base import Context
from montney.connectors.gas_common import FormatDrift

SPECS = load_specs(include_disabled=True)
ROLLUP = {"Landfill Gas": "Renewables", "Refuse": "Renewables", "Solar": "Renewables", "Wind": "Renewables",
          "Wood": "Renewables"}

# ------------------------------------------------------------------ real rows, 2026-11-13 (EST)
FUEL = {
    "07:59:23": {"Batteries": 21, "Hydro": 380, "Natural Gas": 7876, "Nuclear": 2104, "Other": 0, "Landfill Gas": 23, "Refuse": 338, "Solar": 58, "Wind": 1644, "Wood": 180},
    "08:09:05": {"Batteries": 0, "Hydro": 397, "Natural Gas": 8082, "Nuclear": 2101, "Other": 0, "Landfill Gas": 23, "Refuse": 337, "Solar": 67, "Wind": 1635, "Wood": 182},
    "08:15:38": {"Hydro": 394, "Natural Gas": 8065, "Nuclear": 2097, "Other": 0, "Landfill Gas": 23, "Refuse": 343, "Solar": 73, "Wind": 1639, "Wood": 180},
    "08:17:29": {"Batteries": 0, "Hydro": 395, "Natural Gas": 8086, "Nuclear": 2091, "Other": 0, "Landfill Gas": 23, "Refuse": 341, "Solar": 79, "Wind": 1623, "Wood": 182},
    "08:26:07": {"Batteries": 3, "Hydro": 397, "Natural Gas": 8006, "Nuclear": 2088, "Other": 0, "Landfill Gas": 23, "Refuse": 336, "Solar": 92, "Wind": 1607, "Wood": 184},
    "08:35:04": {"Batteries": 3, "Hydro": 394, "Natural Gas": 7950, "Nuclear": 2080, "Other": 0, "Landfill Gas": 23, "Refuse": 343, "Solar": 99, "Wind": 1615, "Wood": 184},
    "08:37:51": {"Batteries": 0, "Hydro": 391, "Natural Gas": 7927, "Nuclear": 2077, "Other": 0, "Landfill Gas": 23, "Refuse": 342, "Solar": 99, "Wind": 1608, "Wood": 184},
    "08:40:41": {"Batteries": 0, "Hydro": 391, "Natural Gas": 7927, "Nuclear": 2077, "Other": 0, "Landfill Gas": 23, "Refuse": 342, "Solar": 99, "Wind": 1608, "Wood": 184},
    "08:41:38": {"Batteries": 5, "Hydro": 396, "Natural Gas": 7826, "Nuclear": 2073, "Other": 0, "Landfill Gas": 22, "Refuse": 344, "Solar": 106, "Wind": 1597, "Wood": 183},
    "08:50:10": {"Batteries": 5, "Hydro": 393, "Natural Gas": 7599, "Nuclear": 2063, "Other": 1, "Landfill Gas": 22, "Refuse": 344, "Solar": 117, "Wind": 1579, "Wood": 186},
    "08:52:21": {"Batteries": 13, "Hydro": 394, "Natural Gas": 7606, "Nuclear": 2065, "Other": 1, "Landfill Gas": 22, "Refuse": 345, "Solar": 120, "Wind": 1568, "Wood": 186},
    "09:03:11": {"Batteries": 5, "Hydro": 256, "Natural Gas": 7796, "Nuclear": 2051, "Other": 1, "Landfill Gas": 22, "Refuse": 331, "Solar": 149, "Wind": 1546, "Wood": 186},
}
LOAD = [  # Date/Time, Total Load, Native Load, Asset Related Load, Total + est. solar, Native + est. solar
    ("08:00", 14214.939, 13779.239, 435.7, 14553.461, 14117.761), ("08:05", 14259.699, 13813.499, 446.2, 14616.149, 14169.949),
    ("08:10", 14292.843, 13851.743, 441.1, 14686.136, 14245.036), ("08:15", 14287.901, 13852.101, 435.8, 14726.847, 14291.047),
    ("08:20", 14197.119, 13837.019, 360.1, 14679.947, 14319.847), ("08:25", 14190.539, 13831.339, 359.2, 14716.055, 14356.855),
    ("08:30", 14153.063, 13816.463, 336.6, 14710.049, 14373.449), ("08:35", 14164.19, 13840.99, 323.2, 14759.764, 14436.564),
    ("08:40", 13870.358, 13821.058, 49.3, 14509.497, 14460.197), ("08:45", 13763.466, 13755.966, 7.5, 14427.876, 14420.376),
    ("08:50", 13777.243, 13775.043, 2.2, 14472.154, 14469.954), ("08:55", 13749.602, 13747.202, 2.4, 14484.034, 14481.634),
]
ZN = (".Z.MAINE", ".Z.NEWHAMPSHIRE", ".Z.VERMONT", ".Z.NEMASSBOST", ".Z.RHODEISLAND", ".Z.SEMASS", ".Z.WCMASS",
      ".Z.CONNECTICUT")
ZONAL = {
    "08:00": (1370.383, 1341.115, 664.715, 2878.723, 843.082, 1601.648, 1879.085, 3189.866),
    "08:05": (1368.352, 1351.774, 670.594, 2901.568, 841.861, 1612.843, 1891.251, 3201.089),
    "08:10": (1362.552, 1345.174, 666.581, 2893.262, 840.673, 1616.973, 1902.416, 3230.828),
    "08:15": (1349.627, 1343.6, 654.011, 2904.213, 847.288, 1621.106, 1902.626, 3232.234),
    "08:20": (1320.623, 1339.481, 653.826, 2918.269, 841.17, 1624.205, 1897.683, 3229.491),
    "08:25": (1297.846, 1338.64, 654.67, 2928.094, 847.688, 1626.222, 1894.267, 3229.651),
    "08:30": (1287.572, 1335.825, 651.264, 2925.037, 848.883, 1625.758, 1891.322, 3243.144),
    "08:35": (1276.876, 1337.004, 660.564, 2932.754, 853.673, 1621.845, 1894.407, 3240.835),
    "08:40": (1261.91, 1344.167, 658.031, 2947.817, 855.929, 1632.234, 1900.102, 3228.734),
    "08:45": (1221.446, 1330.931, 668.939, 2948.807, 852.309, 1626.39, 1885.075, 3244.928),
    "08:50": (1206.6, 1334.906, 665.652, 2942.95, 850.524, 1629.978, 1881.852, 3249.514),
    "08:55": (1168.114, 1328.81, 667.917, 2953.034, 858.938, 1618.831, 1883.597, 3239.365),
}
TN = (".I.SALBRYNB345 1", ".I.ROSETON 345 1", ".I.HQ_P1_P2345 5", ".I.HQHIGATE120 2", ".I.SHOREHAM138 99",
      ".I.NRTHPORT138 5", ".I.HQMRL_RD345 1")
TIES = {
    "08:00": (114.176, -1689.314, 0, -0.305, 102.074, -13.189, 0.006),
    "08:05": (114.471, -1679.267, 0, -0.311, 102.152, -9.763, 0),
    "08:10": (120.472, -1685.73, 0, -0.3, 102.185, -5.961, 0.01),
    "08:15": (111.615, -1650.09, 0, -0.31, 102.028, 1.214, 0.009),
    "08:20": (116.761, -1680.589, 0, -0.306, 102.199, -0.891, 0.01),
    "08:25": (115.924, -1657.721, 0, -0.302, 102.192, 3.469, -0.003),
    "08:30": (116.863, -1671.19, 0, -0.304, 101.981, 5.194, -0.014),
    "08:35": (130.916, -1712.103, 0, -0.301, 101.99, 2.043, -0.004),
    "08:40": (140.288, -1644.306, 0, -0.299, 101.962, -6.131, -0.014),
    "08:45": (131.319, -1701.896, 0, -0.306, 101.898, -22.92, 0.008),
    "08:50": (115.157, -1666.627, 0, -0.31, 101.907, -15.37, 0.002),
    "08:55": (122.199, -1642.745, 0, -0.312, 101.993, -10.28, 0.004),
}


# ------------------------------------------------------------------ live-shaped files
def _file(title: str, d: str, head: list[str], units: list[str], rows: list[list], n: int | None = None) -> str:
    q = lambda v: f'"{v}"' if isinstance(v, str) else f"{v}"                      # noqa: E731
    out = [f'"C","{title}"', f'"C","Report for {d}"', f'"C","Report generated {d} 10:46:59 EST"',
           ",".join(q(c) for c in ["H", *head]), ",".join(q(c) for c in ["H", *units])]
    out += [",".join(q(c) for c in ["D", *r]) for r in rows]
    out.append(f'"T","{len(rows) if n is None else n} lines"')
    return "\n".join(out) + "\n"


def fuel_csv(d: str, snaps: list[tuple[str, dict]], **kw) -> str:
    rows = [[d, t, cat, ROLLUP.get(cat, cat), mw, "No"] for t, s in snaps for cat, mw in s.items()]
    return _file("Dispatch Fuel Mix Report", d, ne.HEADERS[ne.FUEL],
                 ["Local Date", "Local Time", "String", "String", "MWh", "String"], rows, **kw)


def load_csv(d: str, rows: list[tuple], **kw) -> str:
    return _file("Real-Time Five-Minute System Load Report", d, ne.HEADERS[ne.LOAD],
                 ["Date/Time", "MW", "MW", "MW"], [[f"{d} {r[0]}:00", *r[1:]] for r in rows], **kw)


def zonal_csv(d: str, stamps: dict[str, tuple], zones=ZN, **kw) -> str:
    rows = [[f"{d} {t}:00", 4000 + i, z, v, 0] for t, vals in stamps.items() for i, (z, v) in enumerate(zip(zones, vals))]
    return _file("Five-Minute Estimated Zonal Load", d, ne.HEADERS[ne.ZONAL],
                 ["Date/Time", "Number", "String", "MW", "MW"], rows, **kw)


def ties_csv(d: str, stamps: dict[str, tuple], names=TN, he=None, **kw) -> str:
    rows = [[n, d, f"{t}:00", (he or {}).get(t, f"{int(t[:2]) + 1:02d}"), v, 0, 0]
            for t, vals in stamps.items() for n, v in zip(names, vals)]
    return _file("Real-Time Market Actual Five Minute Scheduled Interchange", d, ne.HEADERS[ne.TIES_R],
                 ["Label", "YYYY-MM-DD", "Time", "HE", "MWh", "MWh", "MWh"], rows, **kw)


def empty(report: str, d: str) -> str:
    return _file(report, d, ne.HEADERS[report], [], [])


def live_day() -> dict[str, str]:
    d = "11/13/2026"
    return {ne.FUEL: fuel_csv(d, list(FUEL.items())), ne.LOAD: load_csv(d, LOAD),
            ne.ZONAL: zonal_csv(d, ZONAL), ne.TIES_R: ties_csv(d, TIES)}


class Server:
    """ISO Express: the warm-up page sets isox_token; /transform/csv answers 403 without it."""

    def __init__(self, files: dict[tuple[str, str], str], *, token: bool = True, warm_status: int = 200,
                 always_403: bool = False):
        self.files, self.token, self.warm_status, self.always_403 = files, token, warm_status, always_403
        self.urls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.urls.append(str(req.url))
        if req.url.path.startswith("/isoexpress/"):
            hdr = {"set-cookie": "isox_token=abc123; Path=/; Secure"} if self.token else {}
            return httpx.Response(self.warm_status, text="<html>ISO Express</html>", headers=hdr)
        if "isox_token" not in req.headers.get("cookie", "") or self.always_403:
            return httpx.Response(403, text="<html><body>fof</body></html>")
        report, start = req.url.path.rsplit("/", 1)[1], req.url.params["start"]
        assert req.url.params["end"] == start                     # most reports answer 500 without end=
        return httpx.Response(200, text=self.files.get((report, start), empty(report, start)),
                              headers={"content-type": "text/csv"})


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(ne, "PAUSE_S", 0)


def run(server: Server, now: datetime, client: httpx.Client | None = None):
    client = client or httpx.Client(transport=httpx.MockTransport(server))
    ctx = Context(client=client, now=now)
    return build(SPECS["power_isone"]).run(ctx), ctx, client


# ------------------------------------------------------------------ tests
def test_live_hour_values_and_identities():
    srv = Server({(r, "20261113"): t for r, t in live_day().items()})
    res, ctx, client = run(srv, datetime(2026, 11, 13, 14, 30))          # 09:30 EST
    v = {(o.series_id, o.ts): o.value for o in res.obs}
    h = datetime(2026, 11, 13, 13)                                        # 08:00 EST = 13:00 UTC
    assert {o.ts for o in res.obs} == {h}                                 # 07:00 EST: no snapshot near its start
    gen, net, load = v[("isone.gen.total", h)], v[("isone.interchange.net", h)], v[("isone.load", h)]
    assert gen == pytest.approx(12629.4, abs=0.2) and net == pytest.approx(1456.9, abs=0.2)
    assert load == pytest.approx(14076.7, abs=0.1)
    # generation + net imports = Total Load: +0.07 % here; within -0.27 / +0.44 % over 57 live hours (2026-11-01 with
    # its fall-back hour, 2026-11-12, 2026-11-13); 1 % is the tolerance.
    assert abs(gen + net - load) / load < 0.01
    fuels = sum(v[(f"isone.gen.{k}", h)] for k in ne.FUELS.values())
    assert fuels == pytest.approx(gen, abs=1.0)                           # total = the fuel categories, no imports
    assert v[("isone.gen.coal", h)] == 0 and v[("isone.gen.oil", h)] == 0   # listed by no snapshot: 0, not missing
    zones = sum(v[(f"isone.load.{z}", h)] for z in ne.ZONES.values())
    assert abs(zones - v[("isone.load.native", h)]) / zones < 0.01        # the 8 zones = Native Load (+0.02 %)
    ties = sum(v[(f"isone.tie.{t}.flow", h)] for t in ne.TIES.values())
    assert v[("isone.tie.ny_ac.flow", h)] < 0 < net and net == pytest.approx(-ties, abs=0.2)   # published: - = import
    by_nb = sum(v[(f"isone.interchange.{n}", h)] for n in ne.NEIGHBOURS)
    assert by_nb == pytest.approx(net, abs=0.2)                           # the neighbours add up to the net (imports +)
    assert v[("isone.interchange.ny", h)] == pytest.approx(-sum(v[(f"isone.tie.{t}.flow", h)] for t in ne.NEIGHBOURS["ny"]), abs=0.2)
    assert len(srv.urls) == 9 and ctx.n_requests == 9                     # warm-up + 4 today + 4 yesterday
    again, ctx2, _ = run(srv, datetime(2026, 11, 13, 14, 45), client)
    assert ctx2.n_requests == 0                                           # nothing new can exist yet: cached
    assert sorted((o.series_id, o.ts, o.value) for o in again.obs) == sorted((o.series_id, o.ts, o.value) for o in res.obs)
    _, ctx3, _ = run(srv, datetime(2026, 11, 13, 15, 5), client)          # 10:05 EST: the 09:00 hour has ended
    assert ctx3.n_requests == 4 and len(srv.urls) == 13                   # today's four again; the cookie is reused


def test_every_series_is_declared_with_tier_and_tz():
    metas = build(SPECS["power_isone"]).declared_series()
    assert len({m.series_id for m in metas}) == len(metas) == 12 + 1 + 3 + 8 + 7 + 3 + 1
    assert all(m.unit == "MW" and m.freq == "1h" and m.extra["tier"] == "hourly" and m.extra["lag"]
               and "America/New_York" in m.extra["tz"] for m in metas)
    net = next(m for m in metas if m.series_id == "isone.interchange.net")
    assert "net imports" in net.label
    assert all("+ export, - import" in m.label for m in metas if m.series_id.startswith("isone.tie."))


def test_region_uses_declared_series():
    from montney import regions
    upne = regions.load()["upne"]
    used = {p.series for ln in [*upne.power.values(), *upne.fuels.values()] for p in ln.parts}
    declared = {m.series_id for m in build(SPECS["power_isone"]).declared_series()}
    assert used <= declared and {"isone.gen.total", "isone.load", "isone.interchange.net"} <= used
    assert all(p.sign == 1 for p in upne.power["interchange"].parts)      # already net imports: not negated
    assert SPECS["power_isone"]["region"] == "upne" and SPECS["power_isone"]["interval"] == "15m"


def test_fall_back_day_file_order_decides_edt_then_est():
    d = "11/01/2026"
    blocks = [("00", 100.0), ("01", 200.0), ("01", 300.0), ("02", 400.0)]      # 01:xx twice: EDT, then EST
    rows = [(f"{hh}:{m:02d}", v, v, 0, v, v) for hh, v in blocks for m in range(0, 60, 5)]
    hours = ne.five_min_hours(ne.parse_load(load_csv(d, rows)))
    assert {h: r["load"] for h, r in hours.items()} == {datetime(2026, 11, 1, 4): 100, datetime(2026, 11, 1, 5): 200,
                                                        datetime(2026, 11, 1, 6): 300, datetime(2026, 11, 1, 7): 400}
    # the interchange file marks only the first repeated row "02X": file order is what counts
    stamps = {f"{hh}:{m:02d}": (1,) * 7 for hh in ("01",) for m in range(0, 60, 5)}
    first, second = ties_csv(d, stamps), ties_csv(d, stamps, he={"01:00": "02X"})
    text = first.rsplit('"T"', 1)[0] + second.split('"H","Label"', 1)[1].split("\n", 1)[1]
    text = text.rsplit('"T"', 1)[0] + '"T","168 lines"\n'
    pts = ne.parse_ties(text)
    assert min(pts) == datetime(2026, 11, 1, 5) and max(pts) == datetime(2026, 11, 1, 6, 55) and len(pts) == 24
    # fuel-mix snapshots: the clock steps back from 01:58 to 01:01 -> EST
    snaps = [("00:58:00", {"Nuclear": 1}), ("01:30:00", {"Nuclear": 1}), ("01:58:00", {"Nuclear": 1}),
             ("01:01:00", {"Nuclear": 1}), ("01:01:00", {"Wind": 1}), ("02:00:30", {"Nuclear": 1})]
    got = sorted(ne.parse_fuel(fuel_csv(d, snaps, n=6)))
    assert got == [datetime(2026, 11, 1, 4, 58), datetime(2026, 11, 1, 5, 30), datetime(2026, 11, 1, 5, 58),
                   datetime(2026, 11, 1, 6, 1), datetime(2026, 11, 1, 7, 0, 30)]


def test_fuel_mix_hours_are_time_weighted_and_complete_only():
    d = "11/13/2026"
    snaps = [("09:50:00", {"Natural Gas": 0}), ("10:10:00", {"Natural Gas": 200}),   # 10:00 interpolates to 100
             ("10:30:00", {"Natural Gas": 200}), ("10:31:00", {"Natural Gas": 200}), ("10:32:00", {"Natural Gas": 200}),
             ("10:45:00", {"Natural Gas": 200}), ("11:00:00", {"Natural Gas": 200}),
             ("11:30:00", {"Natural Gas": 200}),                                     # a 30-min gap: 11:00 not written
             ("12:00:00", {"Natural Gas": 50}), ("12:15:00", {"Natural Gas": 50})]  # 12:00 has no snapshot >= 13:00
    hours = ne.snapshot_hours(ne.parse_fuel(fuel_csv(d, snaps)))
    assert list(hours) == [datetime(2026, 11, 13, 15)]                    # 10:00 EST only
    # 10:00-10:10 ramps 100 -> 200 (mean 150), then 200 for 50 min; a plain mean of the readings would give 200
    assert hours[datetime(2026, 11, 13, 15)]["gas"] == pytest.approx((150 * 10 + 200 * 50) / 60)
    assert hours[datetime(2026, 11, 13, 15)]["solar"] == 0                 # not listed: 0 in that snapshot
    # the first snapshot read is held back to the hour start only within EDGE (10 min)
    rest = [("10:25:00", {"Wind": 5}), ("10:40:00", {"Wind": 5}), ("10:55:00", {"Wind": 5}), ("11:00:00", {"Wind": 5})]
    late = ne.snapshot_hours(ne.parse_fuel(fuel_csv(d, [("10:11:00", {"Wind": 5}), *rest])))
    near = ne.snapshot_hours(ne.parse_fuel(fuel_csv(d, [("10:09:00", {"Wind": 5}), *rest])))
    assert late == {} and near[datetime(2026, 11, 13, 15)]["wind"] == 5


def test_five_minute_hours_need_all_twelve_rows():
    d = "11/13/2026"
    full = [(f"08:{m:02d}", 10, 10, 0, 10, 10) for m in range(0, 60, 5)]
    short = [(f"09:{m:02d}", 10, 10, 0, 10, 10) for m in range(0, 55, 5)]               # 11 rows
    blank = [(f"10:{m:02d}", 10 if m else "", 10, 0, 10, 10) for m in range(0, 60, 5)]  # a blank cell: not 0
    q = load_csv(d, full + short + blank).replace('""', "")
    hours = ne.five_min_hours(ne.parse_load(q))
    assert list(hours) == [datetime(2026, 11, 13, 13)]


@pytest.mark.parametrize("make, needle", [
    (lambda: fuel_csv("11/13/2026", [("08:00:00", {"Hydrogen": 5})]), "unknown fuel category"),
    (lambda: fuel_csv("11/13/2026", [("08:00:00", {"Wind": 5})]).replace(',"Marginal Flag"', ""), "header"),
    (lambda: load_csv("11/13/2026", LOAD).replace('"Native Load With Estimated Solar"',
                                                  '"Native Load With Estimated Solar","Storage"'), "header"),
    (lambda: ties_csv("11/13/2026", {"08:00": (1,) * 8}, names=TN + (".I.NEWTIE 345 1",)), "unknown interface"),
    (lambda: ties_csv("11/13/2026", {"08:00": (1,) * 6}, names=TN[:6]), "interfaces missing"),
    (lambda: zonal_csv("11/13/2026", {"08:00": (1,) * 7}, zones=ZN[:7]), "zones missing"),
    (lambda: zonal_csv("11/13/2026", {"08:00": (1,) * 8}, zones=ZN[:7] + (".Z.NEWZONE",)), "unknown zone"),
    (lambda: load_csv("11/13/2026", [("08:00", 1, 1, 0, 1, 1)]).replace("11/13/2026 08:00", "13/45/2026 08:00"),
     "doesn't parse"),
    (lambda: load_csv("11/13/2026", [("08:00", 99999, 1, 0, 1, 1)]), "outside"),
    (lambda: ties_csv("11/13/2026", {"08:00": (9000, 0, 0, 0, 0, 0, 0)}), "outside"),
    (lambda: fuel_csv("11/13/2026", [("08:00:00", {"Wind": "n/a"})]), "isn't a number"),
    (lambda: fuel_csv("11/13/2026", [("08:00:00", {"Wind": ""})]).replace('""', ""), "blank Gen Mw"),
    (lambda: fuel_csv("11/13/2026", [("08:00:00", {"Wind": 5})], n=99), "truncated"),
    (lambda: "<!DOCTYPE html><html>Sign in</html>", "not the CSV"),
])
def test_parsers_fail_closed(make, needle):
    text = make()
    report = next((r for r in ne.REPORTS if f'"H","{ne.HEADERS[r][0]}","{ne.HEADERS[r][1]}"' in text), ne.FUEL)
    with pytest.raises(FormatDrift, match=needle):
        ne.PARSERS[report](text)


def test_handshake_fails_closed():
    now = datetime(2026, 11, 13, 14, 30)
    with pytest.raises(FormatDrift, match="set no isox_token"):
        run(Server({}, token=False), now)
    with pytest.raises(FormatDrift, match="warm-up page failed"):
        run(Server({}, warm_status=404), now)
    srv = Server({}, always_403=True)
    with pytest.raises(FormatDrift, match="fresh isox_token"):
        run(srv, now)
    assert sum("/isoexpress/" in u for u in srv.urls) == 1                 # one warm-up per run, then fail


def test_expired_token_is_refreshed_once():
    srv = Server({(r, "20261113"): t for r, t in live_day().items()})

    def gate(req):                                                        # the stale token is refused
        if "isox_token=stale" in req.headers.get("cookie", ""):
            return httpx.Response(403, text="fof")
        return srv(req)

    client = httpx.Client(transport=httpx.MockTransport(gate))
    client.cookies.set("isox_token", "stale", domain="www.iso-ne.com")
    res, ctx, _ = run(srv, datetime(2026, 11, 13, 14, 30), client)
    assert any(o.series_id == "isone.gen.total" for o in res.obs)
    assert ctx.n_requests == 1 + 1 + 8                                    # refused, warm-up, then the eight files


def test_no_complete_hour_fails_closed():
    d = "11/13/2026"
    files = {**{(r, "20261113"): t for r, t in live_day().items()},
             (ne.ZONAL, "20261113"): zonal_csv(d, dict(list(ZONAL.items())[:11]))}   # zones: 11 of 12 stamps
    with pytest.raises(FormatDrift, match="fiveminuteestimatedzonalload: no complete hour"):
        run(Server(files), datetime(2026, 11, 13, 14, 30))


def test_after_midnight_reads_yesterday_and_only_todays_fuel_mix():
    """00:30 EST: no hour of today has ended, so today's 5-min files aren't read; today's fuel mix is (its first
    snapshot completes yesterday's last hour). Yesterday's files are final once read >= 1 h after the day ended."""
    srv = Server({})
    with pytest.raises(FormatDrift, match="no complete hour"):           # empty files here; the reads are the point
        run(srv, datetime(2026, 11, 13, 5, 30))
    csvs = sorted(u.split("/csv/")[1] for u in srv.urls if "/csv/" in u)
    assert csvs == sorted([f"{r}?start=20261112&end=20261112" for r in ne.REPORTS]
                          + ["genfuelmix?start=20261113&end=20261113"])


def test_eastern_day_bounds():
    assert ne.day_bounds(date(2026, 11, 1)) == (datetime(2026, 11, 1, 4), datetime(2026, 11, 2, 5))   # 25 h
    assert ne.eastern_date(datetime(2026, 11, 13, 4, 59)) == date(2026, 11, 12)
