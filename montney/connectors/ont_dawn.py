"""Ontario gas in: the TC Mainline's Ontario side + the US pipelines that land at Dawn (L-093 part 2, Erik: "Ontario
demand should be an aggregate from mainline and dawn locations"; orchestrator: headline it as supply, not demand).

Dawn's own data (Enbridge Gas's Gas Day Summary) sits behind a reCAPTCHA, and Dawn/Tecumseh storage isn't public daily,
so this is the gas *arriving* in Ontario, storage injections included. Sources, each one public request, no login:

1. **Great Lakes (GLGT) at St. Clair** (`gas_glgt`): tcplus.com's Historical Operationally Available JSON for yesterday
   + today (every location and cycle, one request, like gas_gtn); each gas day's latest cycle. MMBtu.
   `glgt.st_clair.net_deliveries` = ST CLAIR DELIVERY − ST CLAIR RECEIPT (+ = into Ontario).
2. **Vector at St. Clair** (`gas_vector`): gasnom.com's public Vector Operationally Available page (vector-pipeline.com
   links it; ~580 KB HTML, every point and cycle for the last few gas days). `vector.st_clair.deliveries` = "St. Clair
   (US)" deliveries: Vector into Canada toward Dawn, NEXUS gas included (it reaches Dawn over Vector).
3. **Panhandle at Ojibway** (`gas_pepl_ojibway`): Energy Transfer's public Operationally Available by Location page
   (~1 MB HTML, all PEPL locations, the current gas day's latest cycle; its filters are ignored, so it's polled twice a
   day). `pepl.ojibway.deliveries` = "UNION OJIBWAY - WAYNE COUNTY" deliveries (Panhandle into Enbridge Gas at Ojibway).
4. **The aggregate** (`gas_ont_supply`, no requests): `supply.on.pipelines_est`, summed at Ontario's border:
     in   NOL into Ontario (tcm.area.mb.outflow_est, ±6 %) + the Sault receipt (tcm.area.on.receipts)
          + Niagara/Chippawa net (tcm.niagara.imports) + St. Clair (GLGT) + St. Clair (Vector) + Ojibway (Panhandle)
     out  TC to Québec (tcm.energir.deliveries + tcm.qc.exports) + Iroquois (tcm.iroquois.exports)
   TC's own Dawn / St. Clair points aren't used, so nothing is counted twice. Band: the NOL's ±6 % (#289) is the one
   estimated input, ~±130 MMcf/d: ±4.5 % of the total (~2.8 Bcf/d), ±7 % of the Mainline side (~1.8 Bcf/d).
   Not in it: MichCon / Bluewater's small St. Clair-area interconnects, and Ontario's own production (a few MMcf/d).
   **It isn't demand**: Ontario demand = this − net storage injection at Dawn/Tecumseh, which isn't public daily.
   StatCan's monthly Ontario storage swings −2.7 Bcf/d (Jan withdrawals) .. +1.7 Bcf/d (May injections), so the gap to
   demand is that large and seasonal: this reads high in summer and low in winter. No band and no storage correction.

Units: MMcf/d; Dth at 1,037 Btu/cf (as for NWP/GTN), TC's GJ at its 39.2 MJ/m³. Gas day = the date.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, local_to_utc, read_stored
from montney.connectors.pnw_gas import BTU_PER_CF, CENTRAL, gtn_latest, num
from montney.store import Obs, SeriesMeta

GROUP = "Ontario: gas in (TC + Dawn inflows)"
GLGT_URL = "https://www.tcplus.com/Great%20Lakes/HistoricalOperationalCapacity/Generate"
VECTOR_URL = "https://www.gasnom.com/ip/vector/cap_operationally_available.cfm"
PEPL_URL = "https://peplmessenger.energytransfer.com/ipost/capacity/operationally-available-by-location"
CYCLE_RANK = {"timely": 1, "evening": 2, "intraday 1": 3, "intraday 2": 4, "intraday 3": 5}


def mmcfd(dth: float) -> float:
    return dth / BTU_PER_CF


def _cells(row: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x)).replace("&amp;", "&").strip()
            for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]


# ------------------------------------------------------------------ Great Lakes (tcplus)
# L-095 (Midwest): the same request's Emerson receipt and its two ANR interconnects (both in Michigan): Farwell (Clare
# County, by ANR's storage fields) and Muttonville (St. Clair County). Net = DELIVERY − RECEIPT (+ = Great Lakes into ANR).
GLGT_MIDWEST = {"glgt.emerson.net_receipts": ("EMERSON RECEIPT", "EMERSON DELIVERY"),
                "glgt.farwell.net_deliveries": ("FARWELL DELIVERY", "FARWELL RECEIPT"),
                "glgt.muttonville.net_deliveries": ("MUTTONVILLE DELIVERY", "MUTTONVILLE RECEIPT")}
MW_GROUP = "Midwest: Chicago, Bakken, WI, MI"


class GasGreatLakes(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label: SeriesMeta(sid, self.id, label, "MMcf/d", "D", MW_GROUP, day="gas")   # noqa: E731
        return [SeriesMeta("glgt.st_clair.net_deliveries", self.id, "Great Lakes at St. Clair: net into Ontario "
                           "(deliveries − receipts, scheduled)", "MMcf/d", "D", GROUP, day="gas"),
                m("glgt.emerson.net_receipts", "Great Lakes net receipts at Emerson (from the TC Mainline, Emerson 2; "
                                               "scheduled)"),
                m("glgt.farwell.net_deliveries", "Great Lakes ↔ ANR at Farwell, MI: net into ANR (− = from ANR; "
                                                 "scheduled)"),
                m("glgt.muttonville.net_deliveries", "Great Lakes ↔ ANR at Muttonville, MI: net into ANR (− = from "
                                                     "ANR; scheduled)")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        today = ctx.now.replace(tzinfo=ZoneInfo("UTC")).astimezone(CENTRAL).date()
        start = today - timedelta(days=1)
        r = ctx.get(p.get("url", GLGT_URL), params={
            "LocationName": "", "CycleType": "", "GasDay": start.strftime("%m/%d/%Y"),
            "ToGasDay": today.strftime("%m/%d/%Y"), "PageRow": 5000, "page": 1}, timeout=120)
        try:
            content = r.json()["data"]["Content"] or []
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"GLGT historical OA: not the expected JSON ({e})") from e
        res = FetchResult(meta=self.declared_series())
        for day, (posted, rows) in sorted(gtn_latest(content).items()):
            if "ST CLAIR DELIVERY" not in rows:
                raise FormatDrift(f"GLGT historical OA: {day} has no ST CLAIR DELIVERY row")
            net = rows["ST CLAIR DELIVERY"][1] - rows.get("ST CLAIR RECEIPT", (0, 0, ""))[1]
            ts = datetime(day.year, day.month, day.day)
            res.obs.append(Obs("glgt.st_clair.net_deliveries", ts, mmcfd(net), posted))
            for sid, (plus, minus) in GLGT_MIDWEST.items():
                if plus in rows:      # optional: a missing Midwest point mustn't cost Ontario its St. Clair value
                    res.obs.append(Obs(sid, ts, mmcfd(rows[plus][1] - rows.get(minus, (0, 0, ""))[1]), posted))
                else:
                    res.warnings.append(f"GLGT {day}: no {plus} row")
        if not res.obs:
            res.warnings.append(f"GLGT: no cycle posted for {start}..{today} yet")
        return res


# ------------------------------------------------------------------ Vector (gasnom)
def vector_points(page: str, names: set[str]) -> dict[date, tuple[datetime, dict[str, float]]]:
    """{gas day: (posted UTC, {location name: TSQ Dth})} for each day's latest posted cycle, named locations only."""
    best: dict[date, tuple[tuple, datetime, dict]] = {}
    groups: dict[tuple[date, datetime], dict[str, float]] = {}
    header = False
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        c = _cells(row)
        if c[:2] == ["Location", "Location Name"]:
            header = c[7:12] == ["TSQ", "OAC", "Eff Gas Day/Time", "Cycle Desc", "Posting Date/Time"]
            continue
        if not header or len(c) < 12 or c[1] not in names:
            continue
        try:
            day = datetime.strptime(c[9].split()[0], "%m/%d/%Y").date()
            posted = local_to_utc(datetime.strptime(c[11], "%m/%d/%Y %I:%M:%S %p"), CENTRAL)
        except ValueError as e:
            raise FormatDrift(f"Vector OA: bad date in {c[:12]} ({e})") from e
        key = (day, posted)
        groups.setdefault(key, {})
        groups[key][c[1]] = groups[key].get(c[1], 0.0) + num(c[7])
        rank = (CYCLE_RANK.get(c[10].lower(), 0), posted)
        if day not in best or rank > best[day][0]:
            best[day] = (rank, posted, {})
    if not header:
        raise FormatDrift("Vector OA: no 'Location, Location Name, …, TSQ, OAC, Eff Gas Day/Time, Cycle Desc, Posting "
                          "Date/Time' header")
    return {day: (posted, groups[(day, posted)]) for day, (_, posted, _) in best.items()}


class GasVector(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta("vector.st_clair.deliveries", self.id, "Vector at St. Clair: into Canada toward Dawn "
                           "(scheduled; NEXUS gas included)", "MMcf/d", "D", GROUP, day="gas")]

    def fetch(self, ctx: Context) -> FetchResult:
        page = ctx.get(self.spec.get("params", {}).get("url", VECTOR_URL), timeout=120).text
        days = vector_points(page, {"St. Clair (US)"})
        if not days:
            raise FormatDrift("Vector OA: no 'St. Clair (US)' rows")
        res = FetchResult(meta=self.declared_series())
        for day, (posted, v) in sorted(days.items()):
            res.obs.append(Obs("vector.st_clair.deliveries", datetime(day.year, day.month, day.day),
                               mmcfd(v.get("St. Clair (US)", 0.0)), posted))
        return res


# ------------------------------------------------------------------ Panhandle at Ojibway (Energy Transfer)
OJIBWAY = "UNION OJIBWAY - WAYNE COUNTY"


def pepl_ojibway(page: str) -> tuple[date, str, float]:
    """(gas day, cycle, Ojibway delivery TSQ Dth) from the Operationally Available by Location page."""
    m_day = re.search(r'name="gasDay"[^>]*value="(\d{2}/\d{2}/\d{4})"', page) or \
        re.search(r'value="(\d{2}/\d{2}/\d{4})"[^>]*name="gasDay"', page)
    m_cyc = re.search(r'name="cycleDesc"[^>]*value="([^"]*)"', page)
    if not m_day:
        raise FormatDrift("Panhandle OA by location: no gas day")
    head = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        c = _cells(row)
        if c[:2] == ["Loc", "Loc Name"]:
            head = c
            continue
        if head and len(c) >= 7 and c[1] == OJIBWAY and c[3] == "DPQ":
            if not head[6].startswith("TSQ"):
                raise FormatDrift(f"Panhandle OA by location: 7th column is {head[6][:20]!r}, expected TSQ")
            return datetime.strptime(m_day.group(1), "%m/%d/%Y").date(), (m_cyc.group(1) if m_cyc else ""), num(c[6])
    raise FormatDrift(f"Panhandle OA by location: no {OJIBWAY} delivery row")


class GasPanhandleOjibway(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta("pepl.ojibway.deliveries", self.id, "Panhandle at Ojibway: into Enbridge Gas (Ontario; "
                           "scheduled)", "MMcf/d", "D", GROUP, day="gas")]

    def fetch(self, ctx: Context) -> FetchResult:
        page = ctx.get(self.spec.get("params", {}).get("url", PEPL_URL), params={"asset": "PEPL"}, timeout=120).text
        day, cycle, tsq = pepl_ojibway(page)
        res = FetchResult(meta=self.declared_series())
        res.obs.append(Obs("pepl.ojibway.deliveries", datetime(day.year, day.month, day.day), mmcfd(tsq),
                           ctx.now.replace(minute=0, second=0, microsecond=0)))
        res.warnings.append(f"Panhandle Ojibway {day}: {cycle or 'cycle n/a'}")
        return res


# ------------------------------------------------------------------ the aggregate (from the store)
SUPPLY = "supply.on.pipelines_est"
IN = ("tcm.area.mb.outflow_est", "tcm.area.on.receipts", "tcm.niagara.imports", "glgt.st_clair.net_deliveries",
      "vector.st_clair.deliveries", "pepl.ojibway.deliveries")
OUT = ("tcm.energir.deliveries", "tcm.qc.exports", "tcm.iroquois.exports")


class GasOntSupply(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(SUPPLY, self.id, "Ontario gas in: TC Mainline + Dawn inflows (daily; includes storage "
                           "injection, not demand; est. ±4.5 %: the NOL's ±6 %)", "MMcf/d", "D", GROUP,
                           extra={"band": 0.045}, day="gas"),
                SeriesMeta("supply.on.dawn_inflows_est", self.id, "Dawn-side inflows: St. Clair (Great Lakes, Vector) "
                           "+ Ojibway (Panhandle)", "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta("supply.on.mainline_est", self.id, "TC Mainline into Ontario, net of Québec and Iroquois "
                           "(NOL + Sault + Niagara − Énergir − QC exports − Iroquois; est. ±7 %)", "MMcf/d", "D",
                           GROUP, extra={"band": 0.07}, day="gas")]

    def fetch(self, ctx: Context) -> FetchResult:
        days = int(self.spec.get("params", {}).get("days", 7))
        end = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        got = self.stored([*IN, *OUT], end - timedelta(days=days), end)
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        dawn = IN[3:]
        for ts in sorted(set().union(*(set(got.get(s) or {}) for s in (*IN, *OUT)))):
            missing = [s for s in (*IN, *OUT) if ts not in (got.get(s) or {})]
            if missing:
                res.warnings.append(f"{ts:%Y-%m-%d}: no {', '.join(missing)}; no Ontario total that day")
                continue
            v = {s: got[s][ts] for s in (*IN, *OUT)}
            mainline = sum(v[s] for s in IN[:3]) - sum(v[s] for s in OUT)
            inflow = sum(v[s] for s in dawn)
            res.obs += [Obs("supply.on.mainline_est", ts, mainline, as_of),
                        Obs("supply.on.dawn_inflows_est", ts, inflow, as_of), Obs(SUPPLY, ts, mainline + inflow, as_of)]
        return res
