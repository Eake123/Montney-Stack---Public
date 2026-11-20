"""US nuclear reactors: each unit's daily power level from the NRC Power Reactor Status Report (PLAN 9).

Source (keyless, public domain): NRC "Power Reactor Status Report for the last 365 days",
https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/power-reactor-status-reports/PowerReactorStatusForLast365Days.txt
(the old ``reading-rm`` URL 301s to an HTML page). Pipe-delimited text, header ``ReportDt|Unit|Power``; one row per
unit per day, newest day first, 95 units x 366 days (~1.3 MB, ~100 KB gzipped on the wire). ``Power`` is the integer
percent of the unit's licensed thermal power; ``ReportDt`` is the report date stamped ``12:00:00 AM``.
NRC collects the numbers between 4 and 8 a.m. Eastern; the day's rows are in the file by ~09:30 ET (~14:30 UTC).

Requests: the spec runs hourly, but a run downloads only when the file can hold a day it hasn't seen: once today's
(Eastern) report date is parsed, nothing more is requested until the next Eastern day after ``ready_after_et``
(09:00 ET). That is ~1-2 downloads a day, plus one after each restart. The request is ``ctx.get_if_changed`` (a 304
returns nothing new), but NRC's CDN sends no ETag / Last-Modified today, so the date gate is what saves the requests.
The first download after a start emits all 366 days (change-only writes keep what's new); later ones the last
``recent_days`` (14).

Series (freq D, ts = the report date at 00:00, naive; daily ``retention``):
* ``nrc.<unit>.power_pct``: % of licensed thermal power, as NRC reports it (0-100; a value over 105 fails closed).
* ``nrc.<unit>.mw_est``: power_pct / 100 x the unit's EIA-860M net summer capacity (MW). An estimate: thermal power %
  isn't exactly electrical output %, and summer capacity understates a cold-water winter by a few percent.
* ``nrc.plant.eia_<id>.mw`` / ``.capacity`` (PLAN 10b, L-108): per EIA plant, the sum over its units of power_pct / 100
  x net summer MW, and the sum of their net summer MW, written for a day only when every unit of the plant has a row.
  The map's nuclear plant feature ``power.plant.eia_<id>`` binds ``value``/``capacity`` to them, so its glow and motion
  follow its own reactors (Comanche Peak 1 at 0 % on 11-13 while ERCOT's fleet read ~75 %), not its BA's fleet estimate.

The unit table (``UNITS`` below) is static, keyed on the status file's unit names:
* plant, state, net summer capacity, coordinates: EIA-860M Preliminary Monthly Electric Generator Inventory,
  September 2026, sheet "Operating", Technology "Nuclear" (U.S. Energy Information Administration, public domain,
  https://www.eia.gov/electricity/data/eia860m/). 96 operating nuclear generators = the 95 status-file units + Crane
  (Three Mile Island 1, out of service, not in the status file).
* docket: NRC "List of Power Reactor Units", https://www.nrc.gov/facilities-safety/operating-reactors/list-power-reactor-units
  (2026-11-13). 89 names match it exactly; "Harris 1" = "Shearon Harris 1", "LaSalle 1/2" = "La Salle 1/2",
  "River Bend Station 1" = "River Bend 1"; Vogtle 3/4 are dockets 05200025/05200026. The state is EIA's, not the NRC
  list's location ("... of Toledo, OH" is Fermi 2 in Michigan; Salem / Hope Creek are in NJ, not DE; Catawba is in SC).
* region: ``montney.regions.region_of(lon, lat, country="US", member="US-<state>")``; a unit outside every region
  (most of the East and South) is stored without one.

Fail closed (FormatDrift): a missing or changed header, a row that isn't ``date|unit|integer``, a date not at
00:00, a unit not in ``UNITS`` (a new reactor must be mapped), a power level outside 0..105, two different values
for one unit and day, or no rows at all.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = ("https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/"
       "power-reactor-status-reports/PowerReactorStatusForLast365Days.txt")
HEADER = "ReportDt|Unit|Power"
ET = ZoneInfo("America/New_York")
MAX_PCT = 105                 # NRC reports 0-100; a short stretch-power reading could edge over, a typo can't pass
GROUP = "NRC / US nuclear reactor power (daily)"

# NRC status-file unit | slug | plant (EIA-860M) | state | NRC docket | EIA plant id | EIA generator id |
# net summer MW (EIA-860M, Sep 2026) | lon | lat
_TABLE = """
Arkansas Nuclear 1|arkansas_nuclear_1|Arkansas Nuclear One|AR|05000313|8055|1|825.5|-93.2351|35.311
Arkansas Nuclear 2|arkansas_nuclear_2|Arkansas Nuclear One|AR|05000368|8055|2|981.5|-93.2351|35.311
Beaver Valley 1|beaver_valley_1|Beaver Valley|PA|05000334|6040|1|907.0|-80.4336|40.6219
Beaver Valley 2|beaver_valley_2|Beaver Valley|PA|05000412|6040|2|901.0|-80.4336|40.6219
Braidwood 1|braidwood_1|Braidwood Generation Station|IL|05000456|6022|1|1183.0|-88.2286|41.2435
Braidwood 2|braidwood_2|Braidwood Generation Station|IL|05000457|6022|2|1149.3|-88.2286|41.2435
Browns Ferry 1|browns_ferry_1|Browns Ferry|AL|05000259|46|1|1227.4|-87.1189|34.7042
Browns Ferry 2|browns_ferry_2|Browns Ferry|AL|05000260|46|2|1207.7|-87.1189|34.7042
Browns Ferry 3|browns_ferry_3|Browns Ferry|AL|05000296|46|3|1226.6|-87.1189|34.7042
Brunswick 1|brunswick_1|Brunswick Nuclear|NC|05000325|6014|1|938.0|-78.0114|33.9597
Brunswick 2|brunswick_2|Brunswick Nuclear|NC|05000324|6014|2|932.0|-78.0114|33.9597
Byron 1|byron_1|Byron Generating Station|IL|05000454|6023|1|1164.0|-89.2819|42.0742
Byron 2|byron_2|Byron Generating Station|IL|05000455|6023|2|1136.0|-89.2819|42.0742
Callaway|callaway|Callaway|MO|05000483|6153|1|1190.0|-91.7788|38.7589
Calvert Cliffs 1|calvert_cliffs_1|Calvert Cliffs Nuclear Power Plant|MD|05000317|6011|1|884.2|-76.4417|38.4344
Calvert Cliffs 2|calvert_cliffs_2|Calvert Cliffs Nuclear Power Plant|MD|05000318|6011|2|861.0|-76.4417|38.4344
Catawba 1|catawba_1|Catawba|SC|05000413|6036|1|1160.0|-81.0694|35.0514
Catawba 2|catawba_2|Catawba|SC|05000414|6036|2|1150.1|-81.0694|35.0514
Clinton|clinton|Clinton Power Station|IL|05000461|204|1|1082.9|-88.8339|40.1719
Columbia Generating Station|columbia|Columbia Generating Station|WA|05000397|371|2|1151.0|-119.3339|46.4711
Comanche Peak 1|comanche_peak_1|Comanche Peak|TX|05000445|6145|1|1205.0|-97.7855|32.2984
Comanche Peak 2|comanche_peak_2|Comanche Peak|TX|05000446|6145|2|1195.0|-97.7855|32.2984
Cooper|cooper|Cooper Nuclear Station|NE|05000298|8036|1|768.5|-95.6408|40.3628
D.C. Cook 1|dc_cook_1|Donald C Cook|MI|05000315|6000|1|1009.0|-86.5652|41.9756
D.C. Cook 2|dc_cook_2|Donald C Cook|MI|05000316|6000|2|1168.0|-86.5652|41.9756
Davis-Besse|davis_besse|Davis Besse|OH|05000346|6149|1|894.0|-83.0861|41.5967
Diablo Canyon 1|diablo_canyon_1|Diablo Canyon|CA|05000275|6099|1|1122.0|-120.8555|35.2115
Diablo Canyon 2|diablo_canyon_2|Diablo Canyon|CA|05000323|6099|2|1118.0|-120.8555|35.2115
Dresden 2|dresden_2|Dresden Generating Station|IL|05000237|869|2|902.0|-88.27|41.39
Dresden 3|dresden_3|Dresden Generating Station|IL|05000249|869|3|895.0|-88.27|41.39
Farley 1|farley_1|Joseph M Farley|AL|05000348|6001|1|895.0|-85.1116|31.2231
Farley 2|farley_2|Joseph M Farley|AL|05000364|6001|2|896.0|-85.1116|31.2231
Fermi 2|fermi_2|Fermi|MI|05000341|1729|2|1141.0|-83.2581|41.9631
FitzPatrick|fitzpatrick|James A Fitzpatrick|NY|05000333|6110|1|844.0|-76.4084|43.5214
Ginna|ginna|R E Ginna Nuclear Power Plant|NY|05000244|6122|1|578.8|-77.3099|43.2777
Grand Gulf 1|grand_gulf_1|Grand Gulf|MS|05000416|6072|1|1396.7|-91.0478|32.0081
Harris 1|harris_1|Harris (NC)|NC|05000400|6015|1|964.0|-78.9556|35.6334
Hatch 1|hatch_1|Edwin I Hatch|GA|05000321|6051|1|876.0|-82.3447|31.9342
Hatch 2|hatch_2|Edwin I Hatch|GA|05000366|6051|2|883.0|-82.3447|31.9342
Hope Creek 1|hope_creek_1|PSEG Hope Creek Generating Station|NJ|05000354|6118|1|1174.2|-75.5377|39.4666
LaSalle 1|lasalle_1|LaSalle Generating Station|IL|05000373|6026|1|1130.5|-88.6691|41.2455
LaSalle 2|lasalle_2|LaSalle Generating Station|IL|05000374|6026|2|1133.9|-88.6691|41.2455
Limerick 1|limerick_1|Limerick|PA|05000352|6105|1|1119.7|-75.5874|40.2243
Limerick 2|limerick_2|Limerick|PA|05000353|6105|2|1122.1|-75.5874|40.2243
McGuire 1|mcguire_1|McGuire|NC|05000369|6038|1|1158.0|-80.9486|35.4331
McGuire 2|mcguire_2|McGuire|NC|05000370|6038|2|1157.6|-80.9486|35.4331
Millstone 2|millstone_2|Millstone|CT|05000336|566|2|863.4|-72.1677|41.3107
Millstone 3|millstone_3|Millstone|CT|05000423|566|3|1245.0|-72.1677|41.3107
Monticello|monticello|Monticello Nuclear Facility|MN|05000263|1922|1|617.0|-93.8493|45.3338
Nine Mile Point 1|nine_mile_point_1|Nine Mile Point Nuclear Station|NY|05000220|2589|1|619.7|-76.41|43.5211
Nine Mile Point 2|nine_mile_point_2|Nine Mile Point Nuclear Station|NY|05000410|2589|2|1283.4|-76.41|43.5211
North Anna 1|north_anna_1|North Anna|VA|05000338|6168|1|948.0|-77.7897|38.06
North Anna 2|north_anna_2|North Anna|VA|05000339|6168|2|944.0|-77.7897|38.06
Oconee 1|oconee_1|Oconee|SC|05000269|3265|1|861.9|-82.8986|34.7939
Oconee 2|oconee_2|Oconee|SC|05000270|3265|2|865.1|-82.8986|34.7939
Oconee 3|oconee_3|Oconee|SC|05000287|3265|3|872.9|-82.8986|34.7939
Palisades|palisades|Palisades|MI|05000255|1715|1|768.5|-86.3146|42.323
Palo Verde 1|palo_verde_1|Palo Verde|AZ|05000528|6008|1|1311.0|-112.8617|33.3881
Palo Verde 2|palo_verde_2|Palo Verde|AZ|05000529|6008|2|1314.0|-112.8617|33.3881
Palo Verde 3|palo_verde_3|Palo Verde|AZ|05000530|6008|3|1312.0|-112.8617|33.3881
Peach Bottom 2|peach_bottom_2|Peach Bottom|PA|05000277|3166|2|1264.7|-76.2687|39.7589
Peach Bottom 3|peach_bottom_3|Peach Bottom|PA|05000278|3166|3|1284.7|-76.2687|39.7589
Perry 1|perry_1|Perry|OH|05000440|6020|1|1240.0|-81.1439|41.8006
Point Beach 1|point_beach_1|Point Beach Nuclear Plant|WI|05000266|4046|1|591.3|-87.5369|44.2806
Point Beach 2|point_beach_2|Point Beach Nuclear Plant|WI|05000301|4046|2|598.2|-87.5369|44.2806
Prairie Island 1|prairie_island_1|Prairie Island|MN|05000282|1925|1|521.0|-92.6333|44.622
Prairie Island 2|prairie_island_2|Prairie Island|MN|05000306|1925|2|519.0|-92.6333|44.622
Quad Cities 1|quad_cities_1|Quad Cities Generating Station|IL|05000254|880|1|908.0|-90.3103|41.7261
Quad Cities 2|quad_cities_2|Quad Cities Generating Station|IL|05000265|880|2|911.0|-90.3103|41.7261
River Bend Station 1|river_bend_1|River Bend|LA|05000458|6462|1|968.3|-91.3327|30.757
Robinson 2|robinson_2|H B Robinson|SC|05000261|3251|2|759.0|-80.1589|34.4017
Saint Lucie 1|saint_lucie_1|St Lucie|FL|05000335|6045|1|981.0|-80.2464|27.3486
Saint Lucie 2|saint_lucie_2|St Lucie|FL|05000389|6045|2|987.0|-80.2464|27.3486
Salem 1|salem_1|PSEG Salem Generating Station|NJ|05000272|2410|1|1145.8|-75.5358|39.4625
Salem 2|salem_2|PSEG Salem Generating Station|NJ|05000311|2410|2|1138.9|-75.5358|39.4625
Seabrook 1|seabrook_1|Seabrook|NH|05000443|6115|1|1247.0|-70.8489|42.8992
Sequoyah 1|sequoyah_1|Sequoyah|TN|05000327|6152|1|1152.0|-85.0917|35.2267
Sequoyah 2|sequoyah_2|Sequoyah|TN|05000328|6152|2|1125.7|-85.0917|35.2267
South Texas 1|south_texas_1|South Texas Project|TX|05000498|6251|1|1300.0|-96.0481|28.795
South Texas 2|south_texas_2|South Texas Project|TX|05000499|6251|2|1280.0|-96.0481|28.795
Summer|summer|V C Summer|SC|05000395|6127|1|977.2|-81.3153|34.2983
Surry 1|surry_1|Surry|VA|05000280|3806|1|838.0|-76.6986|37.1661
Surry 2|surry_2|Surry|VA|05000281|3806|2|838.0|-76.6986|37.1661
Susquehanna 1|susquehanna_1|TalenEnergy Susquehanna|PA|05000387|6103|1|1247.0|-76.1462|41.0919
Susquehanna 2|susquehanna_2|TalenEnergy Susquehanna|PA|05000388|6103|2|1247.0|-76.1462|41.0919
Turkey Point 3|turkey_point_3|Turkey Point|FL|05000250|621|3|837.0|-80.3308|25.4356
Turkey Point 4|turkey_point_4|Turkey Point|FL|05000251|621|4|844.0|-80.3308|25.4356
Vogtle 1|vogtle_1|Vogtle|GA|05000424|649|1|1150.0|-81.7625|33.1427
Vogtle 2|vogtle_2|Vogtle|GA|05000425|649|2|1152.0|-81.7625|33.1427
Vogtle 3|vogtle_3|Vogtle|GA|05200025|649|3|1114.0|-81.7625|33.1427
Vogtle 4|vogtle_4|Vogtle|GA|05200026|649|4|1114.0|-81.7625|33.1427
Waterford 3|waterford_3|Waterford 3|LA|05000382|4270|3|1086.9|-90.4716|29.9953
Watts Bar 1|watts_bar_1|Watts Bar Nuclear Plant|TN|05000390|7722|1|1123.0|-84.7895|35.6021
Watts Bar 2|watts_bar_2|Watts Bar Nuclear Plant|TN|05000391|7722|2|1122.0|-84.7895|35.6021
Wolf Creek 1|wolf_creek_1|Wolf Creek Generating Station|KS|05000482|210|1|1176.7|-95.6898|38.2393
"""


@dataclass(frozen=True)
class Unit:
    nrc: str
    slug: str
    plant: str
    state: str
    docket: str
    eia_plant: int
    eia_gen: str
    mw: float
    lon: float
    lat: float


def _units() -> dict[str, Unit]:
    out = {}
    for line in _TABLE.strip().splitlines():
        n, slug, plant, st, dk, pid, gid, mw, lon, lat = line.split("|")
        out[n] = Unit(n, slug, plant, st, dk, int(pid), gid, float(mw), float(lon), float(lat))
    return out


UNITS = _units()
# PLAN 10b (Erik's plant animations, L-108): a map plant is the EIA plant its units share (feature power.plant.eia_<id>)
PLANTS: dict[int, list[Unit]] = {}
for _u in UNITS.values():
    PLANTS.setdefault(_u.eia_plant, []).append(_u)
PLANT_BASIS = ("NRC daily power level (% of licensed thermal power, collected 4-8 am ET) x each reactor's net summer "
               "capacity (EIA-860M), summed over the plant's units: a daily reading, not metered MW")


def plant_sid(eia_plant: int, what: str) -> str:
    """nrc.plant.eia_<id>.mw / .capacity: the map's nuclear plant feature (power.plant.eia_<id>) binds to these."""
    return f"nrc.plant.eia_{eia_plant}.{what}"


@functools.cache
def region(nrc_name: str) -> str | None:
    """The map region holding the unit (None: outside every region)."""
    from montney.regions import region_of

    u = UNITS[nrc_name]
    return region_of(u.lon, u.lat, country="US", member=f"US-{u.state}")


def parse(text: str) -> dict[tuple[str, date], int]:
    """The status file -> {(NRC unit name, report date): power %}. Fails closed (module docstring)."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != HEADER:
        raise FormatDrift(f"NRC status: header is {lines[0][:60] if lines else ''!r}, expected {HEADER!r}")
    out: dict[tuple[str, date], int] = {}
    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        f = line.split("|")
        if len(f) != 3:
            raise FormatDrift(f"NRC status line {i}: {line[:80]!r} isn't date|unit|power")
        ds, unit, ps = (x.strip() for x in f)
        try:
            dt = datetime.strptime(ds, "%m/%d/%Y %I:%M:%S %p")
        except ValueError:
            raise FormatDrift(f"NRC status line {i}: date {ds!r} doesn't parse") from None
        if dt.time() != time(0):
            raise FormatDrift(f"NRC status line {i}: date {ds!r} isn't at 12:00:00 AM")
        if unit not in UNITS:
            raise FormatDrift(f"NRC status: unknown unit {unit!r} (add it to power_nrc.UNITS)")
        if not ps.isdigit():
            raise FormatDrift(f"NRC status line {i}: {unit} power {ps!r} isn't an integer percent")
        pct = int(ps)
        if pct > MAX_PCT:
            raise FormatDrift(f"NRC status line {i}: {unit} power {pct} % outside 0..{MAX_PCT}")
        k = (unit, dt.date())
        if out.get(k, pct) != pct:
            raise FormatDrift(f"NRC status: {unit} {dt.date()} has two values ({out[k]} and {pct})")
        out[k] = pct
    if not out:
        raise FormatDrift("NRC status: no rows (NO-RESULT)")
    return out


# Newest report date parsed per source in this process (the runner's source id). A run skips the download while it is
# today's Eastern date (or yesterday's before `ready_after_et`). Only runs with a source keep it: `montney try` and
# tests always download. It is set when the file parses, before the runner writes: if that write fails, the day waits
# for the next day's download, whose `recent_days` window re-emits it.
_newest: dict[str, date] = {}


def expected_date(now_utc: datetime, ready_after: time) -> date:
    """The newest report date the file can hold at `now_utc`: today (Eastern) once past `ready_after`, else yesterday."""
    local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
    return local.date() if local.time() >= ready_after else local.date() - timedelta(days=1)


class NrcReactorStatus(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "daily", "tz": "report date (NRC: collected 4-8 am Eastern)",
              "lag": "same day: posted by ~09:30 ET (~14:30 UTC)"}
        m = []
        for u in UNITS.values():
            e = {**ex, "docket": u.docket, "eia_plant_id": u.eia_plant, "state": u.state, "net_summer_mw": u.mw}
            if (r := region(u.nrc)):
                e["region"] = r
            m.append(SeriesMeta(f"nrc.{u.slug}.power_pct", self.id,
                                f"{u.nrc} ({u.plant}, {u.state}) reactor power, % of licensed thermal power (NRC daily)",
                                "%", "D", GROUP, extra=e))
            m.append(SeriesMeta(f"nrc.{u.slug}.mw_est", self.id,
                                f"{u.nrc} ({u.plant}, {u.state}) output est. = NRC power % x {u.mw:,.0f} MW net "
                                "summer capacity (EIA-860M)", "MW", "D", GROUP, extra=e))
        for pid, units in sorted(PLANTS.items()):
            u0 = units[0]
            e = {**ex, "eia_plant_id": pid, "state": u0.state, "units": [u.nrc for u in units], "basis": PLANT_BASIS}
            if (r := region(u0.nrc)):
                e["region"] = r
            m.append(SeriesMeta(plant_sid(pid, "mw"), self.id, f"{u0.plant} ({u0.state}) output = NRC daily power level "
                                f"x net summer capacity, summed over {len(units)} unit(s); a daily reading", "MW", "D",
                                GROUP, extra=e))
            m.append(SeriesMeta(plant_sid(pid, "capacity"), self.id, f"{u0.plant} ({u0.state}) net summer capacity of "
                                f"its reactors reported to NRC (EIA-860M)", "MW", "D", GROUP, extra=e))
        return m

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params") or {}
        ready = time.fromisoformat(str(p.get("ready_after_et", "09:00")))
        recent = int(p.get("recent_days", 14))
        res = FetchResult(meta=self.declared_series())
        want = expected_date(ctx.now, ready)
        seen = _newest.get(ctx.source) if ctx.source else None
        if seen is not None and seen >= want:
            return res                                       # nothing newer can be posted yet: no request
        r = ctx.get_if_changed(p.get("url", URL), timeout=60)
        if r is None:
            return res                                       # 304: what the file says is already stored
        rows = parse(r.text)
        newest = max(d for _, d in rows)
        missing = sorted(set(UNITS) - {u for u, d in rows if d == newest})
        if missing:
            res.warnings.append(f"NRC {newest}: no row for {', '.join(missing)}")
        if newest < want - timedelta(days=1):
            res.warnings.append(f"NRC status: newest report date {newest} (expected {want})")
        since = newest - timedelta(days=recent - 1) if seen is not None else date.min
        for (unit, d), pct in sorted(rows.items()):
            if d < since:
                continue
            u, ts = UNITS[unit], datetime.combine(d, time(0))
            res.obs.append(Obs(f"nrc.{u.slug}.power_pct", ts, float(pct), ts))
            res.obs.append(Obs(f"nrc.{u.slug}.mw_est", ts, round(pct / 100 * u.mw, 1), ts))
        # per plant: written for a day only when EVERY unit of the plant has its row (a missing unit is never a 0)
        days = sorted({d for _, d in rows if d >= since})
        for pid, units in sorted(PLANTS.items()):
            cap = round(sum(u.mw for u in units), 1)
            for d in days:
                pcts = [rows.get((u.nrc, d)) for u in units]
                if any(x is None for x in pcts):
                    continue
                ts = datetime.combine(d, time(0))
                res.obs.append(Obs(plant_sid(pid, "mw"), ts, round(sum(x / 100 * u.mw for x, u in zip(pcts, units)), 1), ts))
                res.obs.append(Obs(plant_sid(pid, "capacity"), ts, cap, ts))
        if ctx.source:
            _newest[ctx.source] = newest
        return res
