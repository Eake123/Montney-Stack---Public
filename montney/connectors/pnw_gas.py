"""Pacific Northwest gas (L-085, Erik: "fill in PacNW & BC demand … the derived Vancouver demand, Huntingdon exports to
NWPL, JP storage, Stanfield, Malin").

**1. Northwest Pipeline (Williams): GasNwp** (`gas_nwp`). The public EBB's Operationally Available Capacity report
(https://www.northwest.williams.com/NWP_Portal/CapacityResultsScrollable.action, no login): every NWP location's
operating capacity and total scheduled quantity (TSQ) for one gas day and its latest cycle, as one ~350 KB HTML table
(no CSV, no gzip, no validators: probed 2026-11-03).
- No parameter = the current gas day's latest cycle; `?StartGasFlowDate=MM-DD-YYYY` = that day's latest. The day after
  a gas day NWP posts its final "Storage Realignment" cycle (~10:00 Central).
- Each run reads the current gas day. Yesterday's final cycle is read once, after RECHECK_HOUR Central, until it's
  stored (state in <home>/cache, L-081).
- **Mainline nets** (`… COMPRESSOR NET (INCREASING)` = northbound, toward the PacNW; `(DECREASING)` = southbound):
  Baker, OR (`nwp.baker.net_north`, section 4) and Pegram, ID (`nwp.pegram.net_north`): Pegram is NWP's first
  compressor inside Idaho past Kemmerer, WY, so it is the Rockies' flow into Idaho, the entry for a pnw that holds all
  of Idaho (regions/pnw.yaml). Southern Idaho is fed from both ends: past Pegram (194-232 MMcf/d on 2026-11-11/12) and
  from the PacNW east past Baker.
- **Reno lateral** (`nwp.reno_lateral.deliveries`, the RENO LATERAL (TO PAIUTE) delivery at Owyhee, ID; ~56): NWP's
  only exit to Nevada. It is upstream of Baker, so it is *not* in `pnw.exports.total` (whose footprint ends at Baker);
  on the all-Idaho boundary (Pegram in) it is an export.
- **Jackson Prairie inventory**: the portal's "Daily Operations" panel is a GWT-RPC call
  (`DorPortalStartupService.loadDorPortalData`). Its page labels the four storage numbers "Total JP Facility Capacity",
  "JP Inventory", "Total NWP JP Capacity", "NWP JP Inventory" (Dth). We take the facility pair: the adjacent
  (inventory, capacity) integers, in that order, whose capacity is 15-40 million Dth (25,584,002 on 2026-11-03) and
  inventory 0.1 million Dth up to the capacity (exactly one such pair, else a warning), as of the newest past
  date in the payload. Read once a day. The GWT policy and permutation hashes change when Williams redeploys the portal;
  then the call fails, which is a warning (flows still come from the OA report), never a failed run.

**2. Gas Transmission Northwest (TC Energy): GasGtn** (`gas_gtn`). tcplus.com's Historical Operationally Available
JSON (`/GTN/HistoricalOperationalCapacity/Generate`): every location, every cycle (TIM, EVE, ID1-ID3) for a gas-day
range, one request (yesterday + today ≈ 200 KB). The latest cycle per gas day wins. The current-day report has the same
fields but needs one request per cycle.
- **Local deliveries** `gtn.local.deliveries` = every delivery but the named ones (Stanfield, Malin, Tuscarora) and
  GTN's deliveries into NWP at SPOKANE NPC and PALOUSE (`gtn.nwp_interconnect.deliveries`, ~142 MMcf/d: NWP's Starr
  Road and Palouse receipts, which NWP delivers on to Spokane, Pullman and Lewiston). Until 2026-11-13 they were in the
  local sum, so GTN local + NWP's city gates counted that gas twice; old values of the series are ~142 higher.
- **Receipts** besides Kingsgate and Stanfield: `gtn.receipts.ruby_turquoise` (TURQUOISE FLATS METER STATION, Rockies
  gas off Ruby, ~107) and `gtn.receipts.other` (Ridge Road, the RNG dairies, any new receipt; ~8).
- `gtn.fuel_linepack` = all receipts − all deliveries: GTN's compressor fuel and linepack change (~65-70 on 2026-11-11/12,
  ~2.3 % of throughput), its own closure as wcan's NGTL.
- A missing SPOKANE NPC, PALOUSE or TURQUOISE FLATS row is FormatDrift (local would read high, imports low).

**Units:** both post MMBtu (= Dth). MMcf/d = MMBtu ÷ BTU_PER_CF (1,037 Btu/cf, EIA's US average heat content). On 16
sampled days (Nov 2025-Jun 2026) NWP's Sumas receipt at 1,037 matched CER's measured Huntingdon export within
−0.3 % mean (sd ~2 %): the heat value is right for this gas. Jackson Prairie inventory: Bcf = Dth ÷ 1,037,000.

**3. Lower Mainland (Vancouver) demand, derived** (`fortisbc.lower_mainland.demand_est`):
    Westcoast's Huntingdon Delivery Area scheduled (westcoast.oa.huntingdon.scheduled, stored by gas_westcoast_oa)
    − NWP's Sumas receipts (the export at Huntingdon/Sumas) − the month's bias
- Westcoast's Huntingdon area = FortisBC Lower Mainland + the export, plus a bias. Back-test against CER's measured
  key points (2025-11-01..2026-06-30, n 242): Huntingdon scheduled − (CER Huntingdon export + CER FortisBC LM) has
  mean +81, median +78 MMcf/d, and it's seasonal: by month +46 (Dec) .. +132 (Jun). So the bias is that calendar
  month's mean (LM_BIAS_MONTH, Reviewer on #333); Jul-Oct have no back-test month yet and use the overall LM_BIAS = 81.
- Band LM_BAND = ±100 MMcf/d absolute: the month-corrected residual's P90 |deviation| (94; 99 with one bias for all
  months, so the monthly bias mostly fixes the level, not the spread) and the Sumas-vs-CER export noise (P90 ~30) in
  quadrature. CER's FortisBC LM ran 165-707 MMcf/d over the same days (mean 428).
- Orchestrator's form (T-South at Station 2 South − fuel/interior − export) is the same quantity: Station 2 South −
  Huntingdon is T-South's fuel + interior deliveries, so going from Huntingdon skips the one term nobody posts.
- No Huntingdon value for the gas day → no estimate (a warning).

**3b. BC implied demand (L-089, Erik: "for BC include the implied gas demand")** (`demand.bc.implied_est`):
    Westcoast Station 2 South scheduled (westcoast.oa.station2_south.scheduled) − NWP's Sumas receipts (the export)
- = the gas T-South delivers inside BC: FortisBC Lower Mainland (+ Vancouver Island), FortisBC Interior off
  Westcoast, Pacific Northern Gas (Summit Lake), Kingsvale, and T-South's compressor fuel. Aitken Creek storage is in
  it (Station 2 South carries T-North production and storage alike), and so are Huntingdon's own local deliveries.
- **Why not the full identity** (BC production − gas leaving BC − storage): with our production estimate its terms
  cancel down to the same Station 2 South − export, plus R_BC (the carried BC remainder, absolute ±300) and BC
  production's own ±5 % (±380-410 MMcf/d on 7.5-8.2 Bcf/d). That's a band of 50-150 % of a 260-800 MMcf/d demand, so
  it isn't shipped. (Willow Valley and CGL direct cancel: NGTL gas re-enters BC only to leave as LNG.)
- **Back-test** against StatCan table 25-10-0086 (BC, monthly), deliveries to consumers less industrial deliveries from
  transmission pipelines, i.e. what the distribution utilities deliver. Nov 2025-Aug 2026, n 10 (export: CER measured
  to Jun, NWP sampled Jul-Aug): mean −21 MMcf/d, sd 67, worst |111|.
  - Winter runs low (−50..−111): the Kootenays (Alberta gas via Southern Crossing at Yahk) and NE BC aren't on T-South.
  - Late spring and summer runs high (+53..+96): T-South fuel on a small base.
  - → BC_DEMAND_BAND = ±110 MMcf/d absolute (the P90).
- **Not in it:** industrial deliveries off transmission (LNG Canada's plant fuel, NE BC industry: 200-270 MMcf/d in
  StatCan's Jun-Aug 2026) and NE BC field and pipeline fuel. StatCan's all-in BC deliveries + pipeline fuel were
  570-890 MMcf/d over the same months.
- No Station 2 South value for the gas day → no estimate (a warning).

**4. The PacNW summary (L-086, Erik: "PacNW Imports, which is King + Huntington + some NWPL number … from the Rockies
… total gas demand for PacNW … total storage … visible when fully zoomed out"): GasPnwBalance** (`gas_pnw_balance`, no
requests: it reads the store). PacNW = Washington + Oregon + north Idaho.
- **Imports** `pnw.imports.total` = GTN at Kingsgate + NWP at Sumas (Huntingdon) + NWP at Baker + GTN's other receipts
  (Turquoise Flats off Ruby, Ridge Road, RNG; added 2026-11-13: they had been missing, ~115 MMcf/d, so the derived
  demand read that much low).
  - **The Rockies number is NWP's Baker compressor (Baker City, OR), net flow** (`nwp.baker.net_north`, BAKER COMPRESSOR
    NET (INCREASING) = northbound, (DECREASING) = southbound). Baker is the first NWP mainline point inside Oregon:
    upstream are the Rockies and southern Idaho (Muddy Creek … Pocatello, Boise, Caldwell), downstream Stanfield and
    the I-5 market. So it counts Rockies gas once, before Stanfield (whose GTN gas is already in at Kingsgate), and
    leaves out southern Idaho's own demand.
  - It's signed and often negative: in winter PacNW gas flows east past Baker to southern Idaho (Nov 2025-Aug 2026
    sampled monthly means −446 .. +1 MMcf/d). A negative import is that outflow.
- **Exits** `pnw.exports.total` = GTN at Malin (California) + GTN to Tuscarora (Nevada). NWP has no other exit west of
  Baker (its Reno lateral to Nevada is upstream, in southern Idaho: `nwp.reno_lateral.deliveries`, section 1).
- **Storage** (+ withdrawal / − injection): `pnw.storage.net_withdrawal` = Jackson Prairie + Plymouth LNG (NWP's
  daily schedules). NW Natural's Mist (Oregon) posts nothing daily, so it isn't in the daily net. Inventory
  `pnw.storage.inventory_est` = Jackson Prairie (NWP, daily) + Mist (EIA's Oregon working gas, the last reported
  month, `eia.or.storage.working_gas`, ~2-3 months behind). Plymouth LNG's inventory isn't public.
- **Demand** `pnw.demand_est` = imports − exits + storage net withdrawal. Its parts on the map: GTN's local deliveries
  (measured schedules, Idaho panhandle, eastern WA, central OR) and `pnw.demand.nwp_est`, the rest, by difference.
  All inputs are scheduled, so pipeline fuel is in it, as it should be. Since 2026-11-13 `pnw.demand.nwp_est` rises by
  ~257 MMcf/d: +~115 from the GTN receipts now in imports, +~142 because GTN's local no longer holds the gas it hands
  NWP (before, that gas was subtracted from the total although NWP delivers it). It is now NWP's deliveries west of
  Baker + both pipes' fuel (~75-90 more than NWP's posted WA / OR / north Idaho deliveries, gas_ebb_nwp).
  Band ±10 % (DEMAND_BAND). Back-test against EIA's monthly natural gas delivered to consumers in WA + OR (Nov
  2025-Aug 2026, 9 months with EIA data, 5 sampled gas days each): the derived demand runs +50 MMcf/d (+2.5 %) high on
  average, sd ~60, worst month +157 (+8.7 %). The excess is north Idaho and pipeline fuel, which belong in the total.
  That back-test predates the GTN receipts in imports (2026-11-13): with them the derived demand reads ~+115 more
  (if Turquoise Flats flowed then as now), i.e. ~+165 over EIA's WA + OR, which is about GTN + NWP fuel (~80) plus
  north Idaho's GTN-fed load (Rathdrum, Lancaster, Coeur d'Alene). Not re-run yet. Mist isn't seen: on a cold day its
  withdrawals (up to a few hundred MMcf/d) make the estimate low. So there's no bias correction, and the band is
  relative.

Sign rules (docs/WIDGETS.md, PLAN 4c): flows are positive in their named direction; storage `net_withdrawal` is
+ withdrawal / − injection (the gas_balance convention); demand is positive here and drawn as demand by the box.
Fails loudly (FormatDrift) if either report changes shape or has none of the locations below.
Terms: public FERC-required postings (18 CFR 284.13), personal use (L-006). Nothing per location is stored except the
locations named here.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save, local_to_utc, read_stored
from montney.store import Obs, SeriesMeta

CENTRAL = ZoneInfo("America/Chicago")          # NAESB clock (CCT) both pipelines post in
BTU_PER_CF = 1037.0
NWP_URL = "https://www.northwest.williams.com/NWP_Portal/CapacityResultsScrollable.action"
NWP_BASE = "https://www.northwest.williams.com/NWP_Portal/"
GWT_POLICY = "C5645EAC81810B2CA5668DD8E63348A2"          # DorPortalStartupService serialization policy (2026-11-03)
GWT_PERMUTATION = "A342CB552F9AE4F2102F59C9A5936009"
GTN_URL = "https://www.tcplus.com/GTN/HistoricalOperationalCapacity/Generate"
STATE = "nwp_state"
RECHECK_HOUR = 11                               # Central: NWP's Storage Realignment cycle posts ~10:00
NWP_GROUP, GTN_GROUP = "Northwest Pipeline (Williams)", "Gas Transmission Northwest (TC)"
HUNTINGDON = "westcoast.oa.huntingdon.scheduled"
LM = "fortisbc.lower_mainland.demand_est"
LM_BIAS, LM_BAND = 81.0, 100.0
S2S = "westcoast.oa.station2_south.scheduled"
BC_DEMAND, BC_DEMAND_BAND = "demand.bc.implied_est", 110.0      # L-089; back-test in the docstring (3b)
LM_BIAS_MONTH = {1: 48.6, 2: 58.6, 3: 51.4, 4: 98.2, 5: 124.9, 6: 131.6, 11: 89.0, 12: 46.5}   # n 242, see docstring
RENO_LATERAL = "RENO LATERAL (TO PAIUTE)"
NWP_POINTS = ("SUMAS RECEIPT", "SUMAS DELIVERY", "JACKSON PRAIRIE RECEIPT", "JACKSON PRAIRIE DELIVERY",
              "STANFIELD RECEIPT", "STANFIELD DELIVERY", "PLYMOUTH LNG RECEIPT", "PLYMOUTH LNG DELIVERY", RENO_LATERAL)
# mainline compressor nets; increasing milepost = northbound (from the Rockies toward the PacNW)
MAINLINE_NET = re.compile(r"(BAKER|PEGRAM) COMPRESSOR NET \((INCREASING|DECREASING)\)")
DEMAND_BAND = 0.10
GTN_NWP = ("SPOKANE NPC", "PALOUSE")             # GTN deliveries into NWP (NWP's Starr Road and Palouse receipts)
GTN_TURQUOISE = "TURQUOISE FLATS METER STATION"  # receipt from Ruby (Rockies gas), Klamath County, OR
GTN_NAMED = {"KINGSGATE", "Flow Past Kingsgate", "STANFIELD DELIVERY", "STANFIELD RECEIPT", "MALIN",
             "TUSCARORA/GTN INTERCONNECT", *GTN_NWP, GTN_TURQUOISE}
CYCLE_ORDER = {"TIM": 1, "EVE": 2, "ID1": 3, "ID2": 4, "ID3": 5}


def mmcfd(mmbtu: float) -> float:
    return mmbtu / BTU_PER_CF


def num(s: str | None) -> float:
    s = (s or "").replace(",", "").strip()
    return float(s) if s else 0.0


def central_to_utc(dt: datetime) -> datetime:
    return local_to_utc(dt, CENTRAL)


# ------------------------------------------------------------------ NWP
def _text(cell: str) -> str:
    t = re.sub(r"<[^>]+>", " ", cell).replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t).strip()


def nwp_report(page: str) -> tuple[date, str, datetime, dict[str, tuple[float, float]]]:
    """(gas day, cycle, posted (naive UTC), {location name: (operating capacity, TSQ)}) in MMBtu."""
    flat = _text(page)
    m_cyc = re.search(r"Cycle:\s*(.+?)\s+Post Date/Time:", flat)
    m_post = re.search(r"Post Date/Time:\s*(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)", flat)
    m_day = re.search(r"Eff Gas Day/Time:\s*(\d{1,2}/\d{1,2}/\d{4})", flat)
    if not (m_cyc and m_post and m_day):
        raise FormatDrift("NWP OA report: no 'Cycle / Post Date/Time / Eff Gas Day/Time' header")
    if "Meas Basis Desc: MMBtu" not in flat:
        raise FormatDrift("NWP OA report: measurement basis is no longer MMBtu")
    rows: dict[str, tuple[float, float]] = {}
    header_ok = False
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        c = [_text(x) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if c[:2] == ["Loc Prop", "Loc Name"]:
            header_ok = c[7:9] == ["Operating Capacity", "Total Scheduled Qty"]
            continue
        if header_ok and len(c) >= 10 and c[1] in NWP_POINTS:
            opc, tsq = rows.get(c[1], (0.0, 0.0))
            rows[c[1]] = (opc + num(c[7]), tsq + num(c[8]))
        elif header_ok and len(c) >= 10 and (m := MAINLINE_NET.fullmatch(c[1])):
            rows[f"{m.group(1)} NET"] = (num(c[7]), num(c[8]) * (1 if m.group(2) == "INCREASING" else -1))
    if not header_ok:
        raise FormatDrift("NWP OA report: expected columns 'Operating Capacity, Total Scheduled Qty' (8th, 9th)")
    if "SUMAS RECEIPT" not in rows:
        raise FormatDrift("NWP OA report: no SUMAS RECEIPT row")
    day = datetime.strptime(m_day.group(1), "%m/%d/%Y").date()
    posted = central_to_utc(datetime.strptime(m_post.group(1), "%m/%d/%Y %I:%M %p"))
    return day, m_cyc.group(1).strip(), posted, rows


def nwp_values(rows: dict[str, tuple[float, float]]) -> dict[str, float]:
    g = lambda k, i=1: rows.get(k, (0.0, 0.0))[i]          # noqa: E731
    return {"nwp.sumas.receipts": mmcfd(g("SUMAS RECEIPT") - g("SUMAS DELIVERY")),
            "nwp.sumas.capacity": mmcfd(g("SUMAS RECEIPT", 0)),
            "nwp.jackson_prairie.net_withdrawal": mmcfd(g("JACKSON PRAIRIE RECEIPT") - g("JACKSON PRAIRIE DELIVERY")),
            "nwp.stanfield.from_gtn": mmcfd(g("STANFIELD RECEIPT") - g("STANFIELD DELIVERY")),
            "nwp.plymouth_lng.net_withdrawal": mmcfd(g("PLYMOUTH LNG RECEIPT") - g("PLYMOUTH LNG DELIVERY")),
            **({"nwp.baker.net_north": mmcfd(g("BAKER NET"))} if "BAKER NET" in rows else {}),
            **({"nwp.pegram.net_north": mmcfd(g("PEGRAM NET"))} if "PEGRAM NET" in rows else {}),
            **({"nwp.reno_lateral.deliveries": mmcfd(g(RENO_LATERAL))} if RENO_LATERAL in rows else {})}


def jp_inventory(body: str, today: date) -> tuple[date, float, float]:
    """(as-of day, facility inventory Dth, facility capacity Dth) from the DOR panel's GWT-RPC response."""
    if not body.startswith("//OK["):
        raise ValueError(f"not a GWT OK response: {body[:60]!r}")
    i = body.find('["')
    ints = [int(x) for x in re.findall(r"-?\d+", body[5:i]) if "." not in x]
    pairs = [(a, b) for a, b in zip(ints, ints[1:]) if 15e6 <= b <= 40e6 and 1e5 <= a <= b]   # (inventory, capacity)
    if len(pairs) != 1:
        raise ValueError(f"expected one JP facility (inventory, capacity) pair, found {len(pairs)}")
    days = [datetime.strptime(s, "%m/%d/%Y").date() for s in re.findall(r'"(\d{2}/\d{2}/\d{4})"', body[i:])]
    past = [d for d in days if d < today]
    if not past:
        raise ValueError("no as-of date before today in the payload")
    return max(past), float(pairs[0][0]), float(pairs[0][1])


NWP_HEAL_DAYS = 7                       # = gas_pnw_balance's window
NWP_HEAL_SERIES = "nwp.pegram.net_north"  # added in #524: a window day without it is re-read once, one request per day


class GasNwp(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def _heal_days(self, today: date, res: FetchResult) -> list[date]:
        """Gas days of the balance window (today − NWP_HEAL_DAYS .. today − 2) with no stored NWP_HEAL_SERIES: a series
        added by a source change (#524: Pegram, the Reno lateral) heals itself on the first run after deploy, one NWP
        report per missing day (at most NWP_HEAL_DAYS − 1 requests, once); then the usual one or two a run. Like
        GasGtn._heal_from (#525), so no one has to run a back-fill by hand."""
        lo, hi = today - timedelta(days=NWP_HEAL_DAYS), today - timedelta(days=2)
        try:
            got = self.stored([NWP_HEAL_SERIES], datetime(lo.year, lo.month, lo.day),
                              datetime(hi.year, hi.month, hi.day)).get(NWP_HEAL_SERIES, {})
        except Exception as e:  # noqa: BLE001 - no store to read: the usual run, never a crash
            res.warnings.append(f"NWP: stored history not readable ({type(e).__name__}); no back-fill check")
            return []
        have = {t.date() for t in got}
        return [lo + timedelta(days=i) for i in range((hi - lo).days + 1) if lo + timedelta(days=i) not in have]

    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label, unit="MMcf/d", g=NWP_GROUP, **kw: SeriesMeta(sid, self.id, label, unit, "D", g,  # noqa
                                                                           day="gas", **kw)
        return [
            m("nwp.sumas.receipts", "NWP Sumas: receipts from Westcoast, i.e. the Huntingdon export (scheduled, net)"),
            m("nwp.sumas.capacity", "NWP Sumas: receipt operating capacity"),
            m("nwp.jackson_prairie.net_withdrawal", "Jackson Prairie storage: net withdrawal into NWP (scheduled; "
                                                    "+ withdrawal / − injection)"),
            m("nwp.stanfield.from_gtn", "NWP Stanfield: net receipts from GTN (scheduled)"),
            m("nwp.baker.net_north", "NWP at Baker, OR: net flow north into the PacNW from the Rockies and southern "
                                     "Idaho (scheduled; − = PacNW gas flowing east)"),
            m("nwp.pegram.net_north", "NWP at Pegram, ID: net flow from the Rockies (Wyoming) into Idaho (scheduled; "
                                      "− = flowing back toward Wyoming)"),
            m("nwp.reno_lateral.deliveries", "NWP Reno lateral (Owyhee, ID): deliveries to Paiute / Great Basin for "
                                             "northern Nevada (scheduled)"),
            m("nwp.plymouth_lng.net_withdrawal", "Plymouth LNG storage: net withdrawal into NWP (scheduled; "
                                                 "+ withdrawal / − injection)"),
            m("nwp.jackson_prairie.inventory", "Jackson Prairie working gas inventory, whole facility (NWP's "
                                               "preliminary storage levels)", "Bcf"),
            m("nwp.jackson_prairie.capacity", "Jackson Prairie working gas capacity, whole facility", "Bcf"),
            m(LM, f"Lower Mainland (Vancouver) demand: Westcoast Huntingdon − NWP Sumas export − a monthly bias "
                  f"(est., ±{LM_BAND:.0f})", g="BC demand (derived)", extra={"band_abs": LM_BAND}),
            m(BC_DEMAND, f"BC gas demand off Westcoast T-South: Station 2 South − NWP Sumas export (FortisBC, PNG, "
                         f"T-South fuel; est., ±{BC_DEMAND_BAND:.0f}; excludes LNG plant fuel and NE BC industry)",
              g="BC demand (derived)", extra={"band_abs": BC_DEMAND_BAND}),
        ]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        res = FetchResult(meta=self.declared_series())
        now_c = ctx.now.replace(tzinfo=ZoneInfo("UTC")).astimezone(CENTRAL).replace(tzinfo=None)
        state = cache_load(STATE)
        reports = []
        if p.get("from_date"):                                         # back-fill: every day, then nothing else
            d = date.fromisoformat(p["from_date"])
            while d <= now_c.date():
                reports.append(nwp_report(ctx.get(p.get("url", NWP_URL), params={
                    "StartGasFlowDate": d.strftime("%m-%d-%Y")}, timeout=120).text))
                d += timedelta(days=1)
        else:
            reports.append(nwp_report(ctx.get(p.get("url", NWP_URL), timeout=120).text))
            current = reports[0][0]
            prev = current - timedelta(days=1)
            if state.get("final") != prev.isoformat() and now_c.hour >= RECHECK_HOUR:
                reports.append(nwp_report(ctx.get(p.get("url", NWP_URL), params={
                    "StartGasFlowDate": prev.strftime("%m-%d-%Y")}, timeout=120).text))
            heal = [d for d in self._heal_days(now_c.date(), res) if d not in (current, prev)]
            if heal:
                res.warnings.append(f"NWP: back-filling {len(heal)} day(s) from {heal[0]} ({NWP_HEAL_SERIES} missing)")
            for d in heal:
                reports.append(nwp_report(ctx.get(p.get("url", NWP_URL), params={
                    "StartGasFlowDate": d.strftime("%m-%d-%Y")}, timeout=120).text))
        days = {}
        for day, cycle, posted, rows in reports:
            ts = datetime(day.year, day.month, day.day)
            vals = nwp_values(rows)
            res.obs += [Obs(sid, ts, v, posted) for sid, v in vals.items()]
            days[day] = (ts, vals["nwp.sumas.receipts"], posted)
            if cycle.lower().startswith("storage realignment") and day.isoformat() > state.get("final", ""):
                state["final"] = day.isoformat()
        self._lower_mainland(days, res)
        if not p.get("from_date") and state.get("inv") != now_c.date().isoformat():
            self._inventory(ctx, p, now_c.date(), res, state)
        try:
            cache_save(STATE, state)
        except OSError as e:
            res.warnings.append(f"NWP state not written: {e}")
        return res

    def _lower_mainland(self, days: dict, res: FetchResult) -> None:
        if not days:
            return
        lo, hi = min(days), max(days)
        got = self.stored([HUNTINGDON, S2S], datetime(lo.year, lo.month, lo.day), datetime(hi.year, hi.month, hi.day))
        h, s2s = got.get(HUNTINGDON) or {}, got.get(S2S) or {}
        for day, (ts, sumas, posted) in sorted(days.items()):
            if ts in s2s:                       # L-089: BC demand off T-South
                res.obs.append(Obs(BC_DEMAND, ts, s2s[ts] - sumas, posted))
            else:
                res.warnings.append(f"{day}: no Westcoast Station 2 South schedule stored; no BC demand estimate")
            if ts not in h:
                res.warnings.append(f"{day}: no Westcoast Huntingdon schedule stored; no Lower Mainland estimate")
                continue
            res.obs.append(Obs(LM, ts, h[ts] - sumas - LM_BIAS_MONTH.get(day.month, LM_BIAS), posted))

    def _inventory(self, ctx: Context, p: dict, today: date, res: FetchResult, state: dict) -> None:
        base = p.get("portal", NWP_BASE)
        policy, perm = p.get("gwt_policy", GWT_POLICY), p.get("gwt_permutation", GWT_PERMUTATION)
        body = f"7|0|4|{base}|{policy}|com.williams.gwt.client.DorPortalStartupService|loadDorPortalData|1|2|3|4|0|"
        try:
            r = ctx.post(f"{base}com.williams.gwt.PortalApp/DorPortalStartupService", data=body, retries=1, headers={
                "Content-Type": "text/x-gwt-rpc; charset=utf-8", "X-GWT-Permutation": perm, "X-GWT-Module-Base": base})
            day, inv, cap = jp_inventory(r.text, today)
        except Exception as e:     # noqa: BLE001 - flows are still good; the portal may have been redeployed
            res.warnings.append(f"Jackson Prairie inventory not read (GWT policy changed? see pnw_gas.py): {e}")
            return
        ts = datetime(day.year, day.month, day.day)
        res.obs += [Obs("nwp.jackson_prairie.inventory", ts, inv / BTU_PER_CF / 1000.0, ts),
                    Obs("nwp.jackson_prairie.capacity", ts, cap / BTU_PER_CF / 1000.0, ts)]
        state["inv"] = today.isoformat()


# ------------------------------------------------------------------ GTN
def gtn_latest(content: list[dict]) -> dict[date, tuple[datetime, dict[str, tuple[float, float, str]]]]:
    """{gas day: (posted UTC, {location: (operating capacity, TSQ, Loc/QTI)})} from each day's latest cycle (MMBtu)."""
    groups: dict[tuple[date, int, datetime], dict] = {}
    for r in content:
        try:
            day = datetime.strptime(r["GasDay"].split()[0], "%m/%d/%Y").date()
            posted = central_to_utc(datetime.strptime(r["PostedDateTime"], "%m/%d/%Y %I:%M:%S %p"))
            key = (day, CYCLE_ORDER.get(r["CycleName"], 0), posted)
            groups.setdefault(key, {})[r["LocationName"]] = (num(r["OperatingCapacity"]),
                                                            num(r["TotalScheduledQuantity"]), r["LocQti"])
        except (KeyError, ValueError, AttributeError) as e:
            raise FormatDrift(f"GTN historical OA: unexpected row {str(r)[:120]} ({e})") from e
    out: dict = {}
    for (day, _, posted), rows in sorted(groups.items()):        # later cycle (then later posting) wins
        out[day] = (posted, rows)
    return out


def gtn_values(rows: dict) -> dict[str, float]:
    g = lambda k, i=1: rows.get(k, (0.0, 0.0, ""))[i]          # noqa: E731
    local = sum(v[1] for k, v in rows.items() if v[2] == "DPQ" and k not in GTN_NAMED)
    other_in = sum(v[1] for k, v in rows.items() if v[2] == "RPQ" and k not in GTN_NAMED)
    received = sum(v[1] for v in rows.values() if v[2] == "RPQ")
    delivered = sum(v[1] for v in rows.values() if v[2] == "DPQ")
    return {"gtn.kingsgate.receipts": mmcfd(g("KINGSGATE")),
            "gtn.kingsgate.capacity": mmcfd(g("Flow Past Kingsgate", 0)),
            "gtn.stanfield.to_nwp": mmcfd(g("STANFIELD DELIVERY") - g("STANFIELD RECEIPT")),
            "gtn.malin.deliveries": mmcfd(g("MALIN")),
            "gtn.malin.capacity": mmcfd(g("MALIN", 0)),
            "gtn.tuscarora.deliveries": mmcfd(g("TUSCARORA/GTN INTERCONNECT")),
            "gtn.local.deliveries": mmcfd(local),
            "gtn.nwp_interconnect.deliveries": mmcfd(sum(g(k) for k in GTN_NWP)),
            "gtn.receipts.ruby_turquoise": mmcfd(g(GTN_TURQUOISE)),
            "gtn.receipts.other": mmcfd(other_in),
            "gtn.fuel_linepack": mmcfd(received - delivered)}


GTN_HEAL_DAYS = 7                     # = gas_pnw_balance's window: the days it recomputes from GTN's stored series
GTN_HEAL_SERIES = "gtn.fuel_linepack"   # written by every successful run since #524; a missing day = a day to re-read


class GasGtn(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def _heal_from(self, today: date, res: FetchResult) -> date | None:
        """The earliest gas day of the balance's window (today − GTN_HEAL_DAYS .. today − 2) with no stored
        GTN_HEAL_SERIES, or None. A source change that adds series (#524: the GTN receipts and fuel + linepack) heals
        itself on the first run after deploy: the endpoint takes a range, so the gap costs no extra request, and once the
        days are stored the next run is back to yesterday + today. Nothing for the deployer to run."""
        lo, hi = today - timedelta(days=GTN_HEAL_DAYS), today - timedelta(days=2)
        try:
            got = self.stored([GTN_HEAL_SERIES], datetime(lo.year, lo.month, lo.day),
                              datetime(hi.year, hi.month, hi.day)).get(GTN_HEAL_SERIES, {})
        except Exception as e:  # noqa: BLE001 - no store to read: the usual two days, never a crash
            res.warnings.append(f"GTN: stored history not readable ({type(e).__name__}); no back-fill check")
            return None
        have = {t.date() for t in got}
        missing = [lo + timedelta(days=i) for i in range((hi - lo).days + 1) if lo + timedelta(days=i) not in have]
        return missing[0] if missing else None

    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label: SeriesMeta(sid, self.id, label, "MMcf/d", "D", GTN_GROUP, day="gas")   # noqa: E731
        return [m("gtn.kingsgate.receipts", "GTN Kingsgate: receipts from Foothills BC (scheduled)"),
                m("gtn.kingsgate.capacity", "GTN Flow Past Kingsgate: operating capacity"),
                m("gtn.stanfield.to_nwp", "GTN Stanfield: net deliveries to NWP (scheduled)"),
                m("gtn.malin.deliveries", "GTN Malin: deliveries into California (PG&E / Ruby, scheduled)"),
                m("gtn.malin.capacity", "GTN Malin: delivery operating capacity"),
                m("gtn.tuscarora.deliveries", "GTN → Tuscarora (Nevada): deliveries (scheduled)"),
                m("gtn.local.deliveries", "GTN local deliveries: Idaho, Washington, Oregon towns and power plants "
                                          "(scheduled; excludes the deliveries into NWP at Spokane and Palouse)"),
                m("gtn.nwp_interconnect.deliveries", "GTN → NWP at Spokane (NWP's Starr Road) and Palouse: deliveries "
                                                     "NWP passes on to its own city gates (scheduled)"),
                m("gtn.receipts.ruby_turquoise", "GTN Turquoise Flats, OR: receipts from Ruby (Rockies gas, "
                                                 "scheduled)"),
                m("gtn.receipts.other", "GTN other receipts: Ridge Road, RNG dairies (scheduled; not Kingsgate, "
                                        "Stanfield or Turquoise Flats)"),
                m("gtn.fuel_linepack", "GTN fuel and linepack change: all receipts − all deliveries (scheduled)")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        today = ctx.now.replace(tzinfo=ZoneInfo("UTC")).astimezone(CENTRAL).date()
        res = FetchResult(meta=self.declared_series())
        start = date.fromisoformat(p["from_date"]) if p.get("from_date") else today - timedelta(days=1)
        heal = None if p.get("from_date") else self._heal_from(today, res)
        if heal is not None:
            start = heal
            res.warnings.append(f"GTN: back-filling from {heal} (days in the balance window lack {GTN_HEAL_SERIES})")
        r = ctx.get(p.get("url", GTN_URL), params={
            "LocationName": "", "CycleType": "", "GasDay": start.strftime("%m/%d/%Y"),
            "ToGasDay": today.strftime("%m/%d/%Y"), "PageRow": int(p.get("page_rows", 5000)), "page": 1}, timeout=120)
        try:
            j = r.json()
            content = j["data"]["Content"] or []
            basis = j["data"].get("MeasurementBasis") or ""
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"GTN historical OA: not the expected JSON ({e})") from e
        if content and "BTU" not in basis.upper():
            raise FormatDrift(f"GTN historical OA: measurement basis {basis!r}, expected MMBtu")
        if j.get("total") and len(content) < int(j["total"]):
            raise FormatDrift(f"GTN historical OA: {len(content)} of {j['total']} rows (page_rows too small)")
        for day, (posted, rows) in sorted(gtn_latest(content).items()):
            if "KINGSGATE" not in rows or "MALIN" not in rows:
                raise FormatDrift(f"GTN historical OA: {day} has no KINGSGATE or MALIN row")
            gone = [k for k in (*GTN_NWP, GTN_TURQUOISE) if k not in rows]
            if gone:        # renamed: local deliveries would read high, the imports low
                raise FormatDrift(f"GTN historical OA: {day} has no {gone} row")
            ts = datetime(day.year, day.month, day.day)
            res.obs += [Obs(sid, ts, v, posted) for sid, v in gtn_values(rows).items()]
        if not any(o.series_id.startswith("gtn.") for o in res.obs):
            res.warnings.append(f"GTN: no cycle posted for {start}..{today} yet")
        return res


# ------------------------------------------------------------------ EIA: Mist (Oregon) working gas, monthly
# Written by gas_eia_states since L-106 (Oregon's only underground storage is NW Natural's Mist).
MIST = "eia.or.storage.working_gas"


def eia_monthly(page: str) -> tuple[dict[date, float], datetime | None]:
    """EIA dnav monthly history table -> ({month: value}, release date)."""
    out = {}
    for y, row in re.findall(r"<td class='B4'>[^<]*?(\d{4})\s*</td>(.*?)</tr>", page, re.S):
        vals = [re.sub(r"<[^>]+>|&nbsp;|,", "", c).strip() for c in re.findall(r"<td class='B3'>(.*?)</td>", row, re.S)]
        for mo, v in enumerate(vals, 1):
            if re.fullmatch(r"\d+(\.\d+)?", v):
                out[date(int(y), mo, 1)] = float(v)
    rel = re.search(r"Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})", page)
    return out, (datetime.strptime(rel.group(1), "%m/%d/%Y") if rel else None)


# ------------------------------------------------------------------ the PacNW summary (derived from the store)
PNW_IN = {"gtn.kingsgate.receipts": "Kingsgate", "nwp.sumas.receipts": "Sumas", "nwp.baker.net_north": "Baker",
          "gtn.receipts.ruby_turquoise": "Turquoise Flats", "gtn.receipts.other": "GTN other receipts"}
PNW_OUT = ("gtn.malin.deliveries", "gtn.tuscarora.deliveries")
PNW_STORE = ("nwp.jackson_prairie.net_withdrawal", "nwp.plymouth_lng.net_withdrawal")
GTN_LOCAL = "gtn.local.deliveries"
JP_INV = "nwp.jackson_prairie.inventory"


class GasPnwBalance(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        m = lambda sid, label, unit="MMcf/d", **kw: SeriesMeta(sid, self.id, label, unit, "D", "PacNW (derived)",  # noqa
                                                               day="gas", **kw)
        return [m("pnw.imports.total", "PacNW imports: GTN at Kingsgate + NWP at Sumas + NWP at Baker (Rockies, net) "
                                       "+ GTN's other receipts (Turquoise Flats off Ruby, Ridge Road, RNG)"),
                m("pnw.exports.total", "PacNW exits: GTN at Malin (California) + to Tuscarora (Nevada)"),
                m("pnw.storage.net_withdrawal", "PacNW storage net withdrawal: Jackson Prairie + Plymouth LNG "
                                                "(+ withdrawal / − injection; Mist not posted daily)"),
                m("pnw.demand_est", f"PacNW gas demand (WA + OR + north Idaho): imports − exits + storage "
                                    f"(est., ±{DEMAND_BAND:.0%}; excludes Mist's withdrawals)",
                  extra={"band": DEMAND_BAND}),
                m("pnw.demand.nwp_est", "PacNW demand served off NWP: total demand − GTN's local deliveries (by "
                                        "difference, incl. GTN and NWP fuel; est.)", extra={"band": DEMAND_BAND}),
                m("pnw.storage.inventory_est", "PacNW storage inventory: Jackson Prairie (today) + Mist (EIA's latest "
                                               "reported month, ~2-3 months behind)", "Bcf")]

    def fetch(self, ctx: Context) -> FetchResult:
        days = int(self.spec.get("params", {}).get("days", 7))
        end = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ids = [*PNW_IN, *PNW_OUT, *PNW_STORE, JP_INV, GTN_LOCAL]
        got = self.stored(ids, end - timedelta(days=days), end)
        mist = self.stored([MIST], end - timedelta(days=400), end).get(MIST) or {}
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)      # as gas_balance: one vintage per hour
        flows = [*PNW_IN, *PNW_OUT, *PNW_STORE]
        for ts in sorted(set().union(*(set(got.get(i) or {}) for i in flows))):
            missing = [i for i in flows if ts not in (got.get(i) or {})]
            if missing:
                res.warnings.append(f"{ts:%Y-%m-%d}: no {', '.join(missing)}; no PacNW balance that day")
                continue
            v = {i: got[i][ts] for i in flows}
            imp = sum(v[i] for i in PNW_IN)
            out = sum(v[i] for i in PNW_OUT)
            stor = sum(v[i] for i in PNW_STORE)
            res.obs += [Obs("pnw.imports.total", ts, imp, as_of), Obs("pnw.exports.total", ts, out, as_of),
                        Obs("pnw.storage.net_withdrawal", ts, stor, as_of),
                        Obs("pnw.demand_est", ts, imp - out + stor, as_of)]
            if ts in (got.get(GTN_LOCAL) or {}):
                res.obs.append(Obs("pnw.demand.nwp_est", ts, imp - out + stor - got[GTN_LOCAL][ts], as_of))
        jp = got.get(JP_INV) or {}
        if jp and mist:
            last = max(jp)
            m_ts = max(t for t in mist if t <= last) if any(t <= last for t in mist) else None
            if m_ts is not None:
                res.obs.append(Obs("pnw.storage.inventory_est", last, jp[last] + mist[m_ts], as_of))
        elif jp:
            res.warnings.append("no EIA Mist month stored; no PacNW inventory total")
        return res
