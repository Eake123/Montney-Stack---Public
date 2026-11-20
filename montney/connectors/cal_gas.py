"""California gas (L-090, Erik: "add California. Again limiting the amount of requests. So have total Cali nat gas
demand, … storage, imports. Then break it out by PG&E and SoCal, Northern and southern imports").

One request per utility per run, both public (no login), probed 2026-11-03:

**1. PG&E (California Gas Transmission, Pipe Ranger): GasPge** (`gas_pge`). The Current Pipeline Status page's own
data call, `GET https://www.pge.com/bin/pipeline/supplydemand` (~17 KB JSON, MMcf/d): seven gas days, each a
`PlanData` record with `whichDay` "Yesterday-2 (F)" … "Yesterday (F)" (final), "Today (n)" (plan n) and "Today+1..3"
(forecasts, not stored). PG&E posts plans ~07:50, 13:00 and 16:30 PT (`Other_Info.Posted_Date/Posted_Time`, as_of).
- **Demand** `pge.demand.total` = System_Demand (PG&E's on-system customer demand) = Core + NonCore_Elec_Gen +
  NonCore_Industrial (656 + 825 + 627 = 2,107 on 2026-10-31); the three parts are stored.
- **Northern imports** (the Redwood path, at Malin): PgtNW (GTN) + Ruby. PG&E's own receipt, so it isn't GTN's
  delivery figure (gtn.malin.deliveries, which also feeds the PacNW box); nothing is counted twice in California.
- **Southern imports** (the Baja path): Epng (El Paso at Topock), Tw (Transwestern at Topock), Krgt (Kern River at
  Daggett), Southern_Trails. Receipts from SoCalGas (KRS_SoCal) are in-state transfers, not imports.
- California production: Calif. From SoCalGas: KRS_SoCal (Kern River Station), `pge.receipts.from_socal`: PG&E serves
  it inside System_Demand, and SoCalGas's System Sendout already counts it (ENVOY has no separate off-system line), so
  the California total takes it out once (Reviewer on #348: 111/93/93 MMcf/d on 10-31..11-02).
- **Storage** (+ withdrawal / − injection): PG&E's own = Withdrawal + Bal_Withdrawal − Injection − Bal_Injection (the
  balancing injections are most of it: 2026-10-31 57 − 60 + 205 = +202 injected, which is exactly the next day's
  working-gas change). Working gas PGE_Working_Gas_in_Storage (MMcf → Bcf) is the gas day's *opening* inventory, so it's
  stored on the day before, as that day's closing balance (like SoCalGas's ending balance), so it lags SoCalGas's by a
  day. The independents connected to PG&E (Wild Goose, Lodi, Gill Ranch, Central Valley) post daily net flows only:
  WG_Net_Wd − WG_Net_Inj + Lodi_Net_Wd − Lodi_Net_Inj + GRS_LLC_WD − GRS_LLC_Inj + CVGS_WD_Sched − CVGS_Inj_Sched.
  Their inventories show "n/a".

**2. SoCalGas (ENVOY): GasSoCal** (`gas_socalgas`). The public Daily Operations report,
`GET https://www.socalgasenvoy.com/Public/ViewExternalDailyOperations.getDailyOperation` (~57 KB HTML, gzipped; Dth):
columns Actual (yesterday), Estimate (today), three Forecasts (not stored); "Last updated: … PCT" is the as_of.
- **Demand** `socal.demand.sendout` = System Sendout. It includes SDG&E (SoCalGas delivers to SDG&E inside its
  system) and Mexican exports through SDG&E. Dth → MMcf/d at BTU_PER_CF.
- **Southern imports**: the interstate receipt points: El Paso at Ehrenberg and Topock, Transwestern at North Needles
  and Topock, Kern River/Mojave at Kramer Junction and Wheeler Ridge, North Baja at Blythe, TGN at Otay Mesa.
- In-state receipts: California producers ("CP - …", Elk Hills) and PG&E at Wheeler Ridge (a transfer, not an import).
- **Storage**: Net Injections (Withdrawals) (sign flipped to + withdrawal) and Ending Storage Balance (MCF → Bcf):
  Aliso Canyon, Honor Rancho, La Goleta and Playa del Rey together.

**3. California totals: GasCalBalance** (`gas_cal_balance`, no requests: it reads the store, like gas_balance).
- `cal.demand.total` = PG&E system demand + SoCalGas sendout (incl. SDG&E) − PG&E's receipts from SoCalGas (in both).
  The other transfer, PG&E → SoCalGas at Wheeler Ridge, is PG&E off-system (not in System_Demand), so it's counted once.
  Not in it: Southwest Gas's small
  California areas, the Kern River and Mojave direct-connect customers (oil-field steam in Kern County), and Wild
  Goose/Lodi/Gill Ranch's own fuel. PG&E's off-system deliveries to SoCalGas and SoCalGas's to PG&E are transfers
  (PG&E's System_Demand is end use; their size, 50-110 MMcf/d, is the overlap risk in SoCal's sendout).
- `cal.imports.north` = PG&E Malin (GTN + Ruby); `cal.imports.south` = PG&E Baja-path interstate receipts + SoCalGas
  interstate receipts; `cal.imports.total` = both.
- Southern imports by point, for the map (they sum to cal.imports.south): `cal.imports.topock` (El Paso,
  Transwestern, Southern Trails into PG&E and SoCalGas at Topock / North Needles), `.ehrenberg` (+ Blythe),
  `.kramer` (Kern/Mojave at Kramer Junction + Kern River into PG&E at Daggett), `.wheeler_ridge`, `.otay_mesa`.
- `cal.storage.net_withdrawal` = PG&E own + the PG&E-connected independents + SoCalGas; `cal.storage.inventory` =
  PG&E working gas + SoCalGas's ending balance (the independents post no inventory), on the latest day both have.

Units: MMcf/d (PG&E posts MMcf/d; SoCalGas's Dth at 1,037 Btu/cf, as for NWP/GTN), storage in Bcf. Gas day = the date.
Fails loudly (FormatDrift) if either report changes shape. Terms: public CPUC-required postings, personal use (L-006).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, local_to_utc, read_stored
from montney.store import Obs, SeriesMeta

PACIFIC = ZoneInfo("America/Los_Angeles")
BTU_PER_CF = 1037.0
PGE_URL = "https://www.pge.com/bin/pipeline/supplydemand"
SCG_URL = "https://www.socalgasenvoy.com/Public/ViewExternalDailyOperations.getDailyOperation"
PGE_GROUP, SCG_GROUP, CAL_GROUP = "PG&E gas system (CGT)", "SoCalGas system (ENVOY)", "California gas (derived)"

PGE_NORTH = {"PgtNW": "gtn", "Ruby": "ruby"}
PGE_SOUTH = {"Epng": "el_paso", "Tw": "transwestern", "Krgt": "kern_river", "Southern_Trails": "southern_trails"}
PGE_DEMAND = {"Core": "core", "NonCore_Elec_Gen": "electric_gen", "NonCore_Industrial": "industrial"}
PGE_INDEP = (("WG_Net_Wd", "WG_Net_Inj"), ("Lodi_Net_Wd", "Lodi_Net_Inj"), ("GRS_LLC_WD", "GRS_LLC_Inj"),
             ("CVGS_WD_Sched", "CVGS_Inj_Sched"))
# SoCalGas interstate receipt points -> series key (southern imports)
SCG_IMPORTS = {"El Paso - Ehrenberg": "ehrenberg", "El Paso - Topock": "topock_el_paso",
               "Transwestern - North Needles": "north_needles", "Transwestern - Topock": "topock_transwestern",
               "Kern River/Mojave - Kramer Junction": "kramer_junction",
               "Kern River/Mojave - Wheeler Ridge": "wheeler_ridge", "North Baja - Blythe": "blythe",
               "TGN - Otay Mesa": "otay_mesa"}
SCG_INSTATE = re.compile(r"^(CP - |Elk Hills)")
SCG_FROM_PGE = "PG & E - Wheeler Ridge"


def num(v) -> float:
    s = str(v).replace(",", "").replace("&nbsp;", "").strip()
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    return float(s) if s and s.lower() != "n/a" else 0.0


def pacific_to_utc(dt: datetime) -> datetime:
    return local_to_utc(dt, PACIFIC)


# ------------------------------------------------------------------ PG&E
def pge_days(text: str) -> tuple[datetime | None, list[tuple[datetime, dict]]]:
    """(posted UTC, [(gas day, PlanData)]) for the final days and today's plan; forecasts dropped."""
    try:
        recs = [x["PlanData"] for x in json.loads(text)]
    except (ValueError, KeyError, TypeError) as e:
        raise FormatDrift(f"PG&E supplydemand: not the expected JSON ({e})") from e
    need = {"Date", "whichDay", "System_Demand", "PgtNW", "Ruby", "Epng", "Tw", "PGE_Working_Gas_in_Storage"}
    if not recs or not need <= set(recs[0]):
        raise FormatDrift(f"PG&E supplydemand: missing {sorted(need - set(recs[0] if recs else {}))}")
    posted = None
    try:
        oi = recs[0]["Additional_Data"]["Other_Info"]
        posted = pacific_to_utc(datetime.strptime(f"{oi['Posted_Date']} {oi['Posted_Time']}", "%m/%d/%Y %I:%M %p"))
    except (KeyError, ValueError, TypeError):
        pass
    out = []
    for p in recs:
        which = str(p["whichDay"])
        if "(F)" in which or which.startswith("Today ("):
            out.append((datetime.strptime(p["Date"], "%m/%d/%Y"), p))
    return posted, out


def pge_values(p: dict) -> dict[str, float]:
    g = lambda k: num(p.get(k, 0))                    # noqa: E731
    v = {"pge.demand.total": g("System_Demand"), "pge.receipts.california": g("Calif"),
         "pge.receipts.from_socal": g("KRS_SoCal")}
    v |= {f"pge.demand.{s}": g(k) for k, s in PGE_DEMAND.items()}
    v |= {f"pge.imports.{s}": g(k) for k, s in {**PGE_NORTH, **PGE_SOUTH}.items()}
    v["pge.imports.north"] = sum(g(k) for k in PGE_NORTH)
    v["pge.imports.south"] = sum(g(k) for k in PGE_SOUTH)
    v["pge.storage.net_withdrawal"] = g("Withdrawal") + g("Bal_Withdrawal") - g("Injection") - g("Bal_Injection")
    v["pge.storage.independent_net_withdrawal"] = sum(g(w) - g(i) for w, i in PGE_INDEP)
    return v


def pge_working_gas(p: dict) -> float | None:
    """The gas day's opening working gas (Bcf), or None (today's plan posts 0)."""
    wg = num(p.get("PGE_Working_Gas_in_Storage", 0))
    return wg / 1000.0 if wg > 0 else None


class GasPge(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label, unit="MMcf/d": SeriesMeta(sid, self.id, label, unit, "D", PGE_GROUP, day="gas")  # noqa
        return [m("pge.demand.total", "PG&E system demand (core + electric generation + industrial)"),
                m("pge.demand.core", "PG&E core demand (residential, commercial)"),
                m("pge.demand.electric_gen", "PG&E noncore demand: electric generation"),
                m("pge.demand.industrial", "PG&E noncore demand: industrial"),
                m("pge.imports.north", "PG&E northern imports at Malin (Redwood path): GTN + Ruby"),
                m("pge.imports.gtn", "PG&E receipts from GTN at Malin"),
                m("pge.imports.ruby", "PG&E receipts from Ruby at Malin (Onyx Hill)"),
                m("pge.imports.south", "PG&E southern imports (Baja path): El Paso + Transwestern at Topock, Kern "
                                       "River at Daggett, Southern Trails"),
                m("pge.imports.el_paso", "PG&E receipts from El Paso (Topock)"),
                m("pge.imports.transwestern", "PG&E receipts from Transwestern (Topock)"),
                m("pge.imports.kern_river", "PG&E receipts from Kern River (Daggett)"),
                m("pge.imports.southern_trails", "PG&E receipts from Southern Trails"),
                m("pge.receipts.california", "PG&E receipts from California production"),
                m("pge.receipts.from_socal", "PG&E receipts from SoCalGas at Kern River Station (in-state transfer)"),
                m("pge.storage.net_withdrawal", "PG&E storage net withdrawal (PG&E's own fields; + withdrawal / − "
                                                "injection)"),
                m("pge.storage.independent_net_withdrawal", "Independent storage on PG&E net withdrawal: Wild Goose, "
                                                            "Lodi, Gill Ranch, Central Valley (+ withdrawal)"),
                m("pge.storage.working_gas", "PG&E working gas in storage, end of gas day (PG&E's own fields)",
                  "Bcf")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        posted, days = pge_days(ctx.get(p.get("url", PGE_URL), timeout=60).text)
        if not days:
            raise FormatDrift("PG&E supplydemand: no final or current-plan gas day")
        res = FetchResult(meta=self.declared_series())
        as_of = posted or ctx.now.replace(minute=0, second=0, microsecond=0)
        for ts, rec in days:
            res.obs += [Obs(sid, ts, v, as_of) for sid, v in pge_values(rec).items()]
            wg = pge_working_gas(rec)
            if wg is not None:                        # opening inventory = the previous day's close
                res.obs.append(Obs("pge.storage.working_gas", ts - timedelta(days=1), wg, as_of))
        return res


# ------------------------------------------------------------------ SoCalGas
def _cell(x: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x).replace("&nbsp;", " ").replace("&amp;", "&")).strip()


def scg_report(page: str) -> tuple[datetime | None, list[datetime], dict[str, list[float]]]:
    """(updated UTC, [actual day, estimate day], {row label: [actual, estimate]} in Dth / MCF)."""
    head = None
    rows: dict[str, list[float]] = {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        c = [_cell(x) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if len(c) >= 3 and c[1].startswith("Actual (Dth)"):
            head = c
        elif head and len(c) >= 3 and c[0]:
            rows[c[0]] = [num(c[1]) if c[1] else None, num(c[2]) if c[2] else None]
    if not head or not head[2].startswith("Estimate"):
        raise FormatDrift("SoCalGas Daily Operations: no 'Actual (Dth) … Estimate (Dth)' header")
    days = [datetime.strptime(re.search(r"\d{2}/\d{2}/\d{4}", h).group(0), "%m/%d/%Y") for h in head[1:3]]
    for need in ("System Sendout", "Net Injections(Withdrawals)", "Ending Storage Balance (MCF)"):
        if need not in rows:
            raise FormatDrift(f"SoCalGas Daily Operations: no '{need}' row")
    if not any(k in rows for k in SCG_IMPORTS):
        raise FormatDrift("SoCalGas Daily Operations: none of the interstate receipt points")
    m = re.search(r"Last updated:\s*(\d{2}/\d{2}/\d{4} \d{1,2}:\d{2} [AP]M)", page)
    updated = pacific_to_utc(datetime.strptime(m.group(1), "%m/%d/%Y %I:%M %p")) if m else None
    return updated, days, rows


def scg_values(rows: dict[str, list], i: int) -> dict[str, float]:
    mm = lambda dth: dth / BTU_PER_CF                 # noqa: E731  Dth/d -> MMcf/d
    g = lambda k: rows.get(k, [None, None])[i]        # noqa: E731
    v = {"socal.demand.sendout": mm(g("System Sendout") or 0.0)}
    imp = {s: g(k) for k, s in SCG_IMPORTS.items() if g(k) is not None}
    v |= {f"socal.imports.{s}": mm(x) for s, x in imp.items()}
    v["socal.imports.south"] = mm(sum(imp.values()))
    v["socal.receipts.california"] = mm(sum(x[i] or 0.0 for k, x in rows.items() if SCG_INSTATE.match(k)))
    v["socal.receipts.from_pge"] = mm(g(SCG_FROM_PGE) or 0.0)
    v["socal.storage.net_withdrawal"] = -mm(g("Net Injections(Withdrawals)") or 0.0)
    bal = g("Ending Storage Balance (MCF)")
    if bal:
        v["socal.storage.inventory"] = bal / 1e6          # MCF -> Bcf
    return v


class GasSoCal(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label, unit="MMcf/d": SeriesMeta(sid, self.id, label, unit, "D", SCG_GROUP, day="gas")  # noqa
        names = {v: k for k, v in SCG_IMPORTS.items()}
        return [m("socal.demand.sendout", "SoCalGas system sendout (incl. SDG&E)"),
                m("socal.imports.south", "SoCalGas southern imports: interstate receipts (Ehrenberg, Topock, Needles, "
                                         "Kramer Junction, Wheeler Ridge, Blythe, Otay Mesa)"),
                *[m(f"socal.imports.{s}", f"SoCalGas receipts: {names[s]}") for s in SCG_IMPORTS.values()],
                m("socal.receipts.california", "SoCalGas receipts from California production (CP lines, Elk Hills)"),
                m("socal.receipts.from_pge", "SoCalGas receipts from PG&E at Wheeler Ridge (in-state transfer)"),
                m("socal.storage.net_withdrawal", "SoCalGas storage net withdrawal (+ withdrawal / − injection)"),
                m("socal.storage.inventory", "SoCalGas storage inventory (ending balance)", "Bcf")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        updated, days, rows = scg_report(ctx.get(p.get("url", SCG_URL), timeout=60).text)
        res = FetchResult(meta=self.declared_series())
        as_of = updated or ctx.now.replace(minute=0, second=0, microsecond=0)
        for i, ts in enumerate(days):
            if rows["System Sendout"][i] is None:
                continue
            res.obs += [Obs(sid, ts, v, as_of) for sid, v in scg_values(rows, i).items()]
        return res


# ------------------------------------------------------------------ California totals (from the store)
# a part with a leading "-" is subtracted
CAL_PARTS = {"cal.demand.total": ("pge.demand.total", "socal.demand.sendout", "-pge.receipts.from_socal"),
             "cal.imports.north": ("pge.imports.north",),
             "cal.imports.south": ("pge.imports.south", "socal.imports.south"),
             "cal.storage.net_withdrawal": ("pge.storage.net_withdrawal", "pge.storage.independent_net_withdrawal",
                                            "socal.storage.net_withdrawal"),
             "cal.storage.inventory": ("pge.storage.working_gas", "socal.storage.inventory"),
             # southern imports by point (they sum to cal.imports.south; the map's import nodes)
             "cal.imports.topock": ("pge.imports.el_paso", "pge.imports.transwestern", "pge.imports.southern_trails",
                                    "socal.imports.topock_el_paso", "socal.imports.topock_transwestern",
                                    "socal.imports.north_needles"),
             "cal.imports.ehrenberg": ("socal.imports.ehrenberg", "socal.imports.blythe"),
             "cal.imports.kramer": ("socal.imports.kramer_junction", "pge.imports.kern_river"),
             "cal.imports.wheeler_ridge": ("socal.imports.wheeler_ridge",),
             "cal.imports.otay_mesa": ("socal.imports.otay_mesa",),
             # L-094: Kern River (and Mojave, which shares its lines) into California = Kramer (+ PG&E's Kern River
             # receipt at Daggett) + Wheeler Ridge. The map's Kern River line value.
             "cal.imports.kern_mojave": ("socal.imports.kramer_junction", "pge.imports.kern_river",
                                         "socal.imports.wheeler_ridge")}


class GasCalBalance(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label, unit="MMcf/d": SeriesMeta(sid, self.id, label, unit, "D", CAL_GROUP, day="gas")  # noqa
        return [m("cal.demand.total", "California gas demand on PG&E + SoCalGas: PG&E system demand + SoCalGas sendout "
                                      "(incl. SDG&E) − PG&E's receipts from SoCalGas"),
                m("cal.imports.north", "California northern imports: PG&E at Malin (GTN + Ruby)"),
                m("cal.imports.south", "California southern imports: PG&E Baja path + SoCalGas interstate receipts"),
                m("cal.imports.total", "California imports: northern + southern"),
                m("cal.storage.net_withdrawal", "California storage net withdrawal: PG&E + independents on PG&E + "
                                                "SoCalGas (+ withdrawal / − injection)"),
                m("cal.storage.inventory", "California storage inventory: PG&E working gas + SoCalGas (independents "
                                           "post none)", "Bcf"),
                m("cal.imports.topock", "California imports at Topock / Needles: El Paso, Transwestern and Southern "
                                        "Trails into PG&E and SoCalGas"),
                m("cal.imports.ehrenberg", "California imports at Ehrenberg / Blythe: El Paso and North Baja into "
                                           "SoCalGas"),
                m("cal.imports.kramer", "California imports at Kramer Junction / Daggett: Kern River and Mojave into "
                                        "SoCalGas and PG&E"),
                m("cal.imports.wheeler_ridge", "California imports at Wheeler Ridge: Kern River and Mojave into "
                                               "SoCalGas"),
                m("cal.imports.otay_mesa", "California imports at Otay Mesa: TGN (Mexico) into SoCalGas"),
                m("cal.imports.kern_mojave", "Kern River / Mojave into California: Kramer Junction + Daggett (PG&E) + "
                                             "Wheeler Ridge")]

    def fetch(self, ctx: Context) -> FetchResult:
        days = int(self.spec.get("params", {}).get("days", 7))
        end = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ids = sorted({s.lstrip("-") for parts in CAL_PARTS.values() for s in parts})
        got = self.stored(ids, end - timedelta(days=days), end)
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        total: dict[datetime, list[float]] = {}
        for sid, parts in CAL_PARTS.items():
            series = [(-1.0 if p.startswith("-") else 1.0, got.get(p.lstrip("-")) or {}) for p in parts]
            for ts in sorted(set.intersection(*(set(s) for _, s in series))):
                val = sum(sign * s[ts] for sign, s in series)
                res.obs.append(Obs(sid, ts, val, as_of))
                if sid in ("cal.imports.north", "cal.imports.south"):
                    total.setdefault(ts, []).append(val)
        res.obs += [Obs("cal.imports.total", ts, sum(v), as_of) for ts, v in sorted(total.items()) if len(v) == 2]
        if not res.obs:
            res.warnings.append("no gas day with both PG&E and SoCalGas stored yet")
        return res
