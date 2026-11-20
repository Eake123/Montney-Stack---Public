"""NGTL production by area and segment: monthly actuals, plus a daily estimate with bands (PLAN 5a; DECISIONS P5a).

Inputs:
- TC **Station % Utilization** receipts: the same xlsx as ngtl_station_util, parsed with its functions. Gives monthly
  receipts per station and segment.
- TC **NGTL System Receipts & Deliveries** (`/assets/<yyyy>-NGTL-System-Receipts-and-Deliveries.xlsx`, one sheet per
  month, daily rows, 10³m³): daily **measured** receipts, storage, Gordondale and Groundbirch East. It's the oracle for
  GDSR's bias (L-032: its heating value is *not* used).
- **GDSR** daily field receipts (`ngtl.gdsr.field_receipts.flow`), read back from the store (gas_common.read_stored,
  read-only).

Series written (MMcf/d):
  ngtl.rd.<measured_receipts|storage_receipts|gordondale_receipts|groundbirch_east_receipts>
                                     D, TC measured daily (as_of = fetch day, a report vintage)
  ngtl.segment.<code>.receipt        M, sum of the segment's field stations (allocated; ~2.7 % below measured)
  ngtl.area.<usjr|rest>.receipt      M, USJR = segments UPRM PRLL NWML GRDL WAEX GPML CENT LPOL (TC's /areas note:
                                     segments 1-5, 7, 8 and part of 9, all of LPOL counted here); rest = the other
                                     field stations
  ngtl.area.<usjr|rest>.receipt_est      D (gas day), the daily estimate below
  ngtl.segment.<code>.receipt_est        D (gas day), the same with segment shares
  ngtl.province.<ab|bc>.receipt / _est   M / D, the same split by province (BC_STATIONS)
  production.<ab|bc>.daily_est           D (gas day), total provincial production today (PLAN 4b item 4), below
  production.ab.alliance_est             D, Alliance Zone 2 today (measured; part of the AB sum)
  production.bc.alliance_est             D, Alliance Zone 1 today, whole (BC + NW AB; measured; part of the BC sum)
  production.<ab|bc>.other_est           D, everything else, carried from the last regulator month (R below)
  production.bc.ngtl_est                 D, BC gas into NGTL today: BC field stations + Gordondale + Groundbirch East
  production.bc.cgl_direct_est           D, BC producer gas straight into CGL (lng.export.direct: daily regression)

**Provincial production today: a visible sum of public meters** (PLAN 4b item 4; Erik via the orchestrator, 2026-11-02):
    AB today = NGTL AB field receipts + Alliance Zone 2 + R_AB
    BC today = NGTL BC field receipts + Gordondale + Groundbirch East (T-North into NGTL) + Alliance Zone 1 (whole)
               + CGL producer-direct (the gas_lng_bcer regression) + T-North to T-South net of Aitken storage (6B) + R_BC
- **6B, T-North to T-South:** Westcoast's scheduled Station 2 South − Aitken Creek storage (gas_westcoast_oa), and the
  same in the regulator month (monthly mean − BCER's measured storage net), so R_BC no longer carries it. Back-test
  (Jan-Sep 2026, n 9, M-2): BC P90 5.6 % with it vs 4.7 % inside R. On BCER without Aitken withdrawals (L-079):
  3.8 % with it. The sum is visible and within the BC band (±5 %). Without Westcoast/BCER data for the month or the
  day it stays inside R (warned).
- **R = "everything else"**, carried from the latest regulator month Mref: the regulator total − the same parts that
  month (Petrinex AB / BCER BC production; Alliance as its Mref mean; NGTL as GDSR(Mref mean) × k × that month's share).
  - R_AB holds ATCO, Gas Alberta and plants direct.
  - R_BC holds Westcoast T-North's other gas, less the NW-AB part of Zone 1 (so it can be small, even negative).
- **Alliance Zone 1 is never split** (Alliance publishes no per-point volumes; Petrinex can't tell which pipe a plant
  near both used: Aug 2026, plants whose nearest meter ≤ 2 km is Alliance sum to 1,703 MMcf/d against Alliance's 1,396).
  The whole of Zone 1 is in BC's sum and R_BC absorbs the NW-AB part.
- **Until Alliance has ≥ 90 % of Mref's days stored** (gas_alliance re-reads its last 100 days on a rolling basis), the
  totals keep Alliance inside R, with a warning, and write no Alliance part.
- **Bands** (back-test Mar 2024-Sep 2026, n = 31: shares and remainder from M-2, as on a typical day; GDSR(M) × 1.03.
  The GDSR basis (L-088) moves a day's total only by (k − 1) × (GDSR(d) − GDSR(Mref)), so these totals' bands
  stand):
  - AB P90 3.3 % (max 5.5 %) → ±4 %. BC, re-run on BCER without Aitken storage withdrawals (L-079, #296):
    - P90 4.7 % (max 9.4 %; it was 5.8 % / 12.0 % when winter withdrawals counted as production);
    - on today's sum (Zone 1 + CGL direct + T-South net of Aitken in it, Jan-Sep 2026, n 9): P90 3.8 % (max 4.9 %).
    → ±5 % (was ±6 %).
  - The "everything else" rows: the totals' absolute-error P90 ÷ R at the latest regulator month: AB 401 ÷ 1,360 →
    ±30 % (R_AB is 1,090 + the 270 NGTL basis it holds since L-088; it was ±37 % of 1,090). BC: R_BC is small and
    negative since 6B (−266 in Sep 2026, −168 with its 97 basis since L-088: the NW-AB part of Zone 1), so a relative
    band would collapse as R nears 0 (Reviewer on #298: at −50, ±58 against a real ±300). BC's is **absolute**:
    ±300 MMcf/d (abs-error P90 303 on today's sum; 336 over n 31), REST_ABS_BAND.
    - R_BC is small and negative now: the NW-AB part of Zone 1, which is counted in BC's sum.
    - So its band is set to show ~±300 MMcf/d around it. Re-check it when R_BC's month changes much.
  - Monthly means, so a single day swings more.
- **BC_STATIONS:** the TC receipt stations whose number or name is a NOVA meter in Petrinex BC's facility list
  (PRAFacilityIds.csv, operator NOVA, BCMS…), 2024-2026 files. Gordondale Border (3886, an AB meter) and Groundbirch
  East (5120) carry BC gas but are counted from GDSR, not here.

**Daily estimate** = GDSR field receipts(d) × share(last station month): **on TC's published GDSR basis** (L-088,
Erik: the NGTL field receipts headline must match TC's official GDSR figure). Areas, segments, provinces and stations
are GDSR split by last month's station shares, so USJR + outside USJR = GDSR field receipts exactly. The station
monthly actuals (allocated, ~2.7 % below TC's meters) are on about the same basis, so the daily estimates now line up
with them too.
- **k** = the mean daily ratio of rd measured field receipts (measured − storage − Gordondale − Groundbirch East) to
  GDSR field receipts, over the latest month rd has published (2026: 1.026-1.033, sd 0.0021). It's no longer applied
  to the estimates. It gives `production.total.ngtl_field_measured_est` = GDSR field × k, the same receipts on TC's
  measured basis, for the panel, and it sizes the basis part of the remainders below. It needs ≥ 90 % of that month's
  days with both values, else NO-RESULT.
- **The regulator-anchored remainders absorb the basis** (until L-088 the NGTL part was GDSR × k):
  R = regulator − GDSR(Mref mean) × share − … is (k − 1) × that month's NGTL field part larger, so the provincial and
  total production are unchanged except for (k − 1) × (GDSR(d) − GDSR(Mref)) day to day (≲ 10 MMcf/d). That basis
  part of each R is written as `production.<ab|bc>.other_ngtl_basis_est`, so gas_balance's tie-out breakdown doesn't
  count it twice (its "NGTL basis" term already holds (k − 1) × GDSR field).
- **share** = that segment's or area's part of last month's field-station receipts.
- **Field stations** exclude the border and interconnect receipts that GDSR counts outside field receipts: 3886
  Gordondale, 5120 Groundbirch East, 1958 Empress, 1016 Hussar-Chancellor, 6404 McNeill (the P2 back-test set).
- **Bands** (P90 of the P2 back-test, M-1 shares → month M; gas-stations.md §4), carried in SeriesMeta.extra["band"]:
  - USJR ±0.7 %, rest ±6 %;
  - segments: GPML 3.8, LPOL 4.3, GRDL 5.0, CENT 5.4, ALEG 6.5, PRLL 8.3, WAEX 8.5, BLEG 14.8, WGAT 22.5 %;
  - any other segment ±25 % (small; shown only at deep zoom).

  On the GDSR basis k no longer adds to them.

Fails loudly (FormatDrift) if the rd sheet has no daily rows, or a column header changes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from montney.connectors import ngtl_station_util as su
from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import E3M3_TO_MMCF, FormatDrift, forget_on_error, get_at_most_every, read_stored
from montney.store import Obs, SeriesMeta

RD_URL = "https://www.tccustomerexpress.com/assets/{year}-NGTL-System-Receipts-and-Deliveries.xlsx"
FILE_MAX_AGE = timedelta(hours=12)     # PLAN 9: TC's page + workbooks, re-downloaded twice a day at most
GDSR = "ngtl.gdsr.field_receipts.flow"
RD_COLS = {"measured_receipts": "Measured Receipts", "storage_receipts": "Storage Receipts",
           "gordondale_receipts": "Gordondale Receipts", "groundbirch_east_receipts": "Groundbirch East Receipts"}
NON_FIELD = {3886, 5120, 1958, 1016, 6404}
USJR = {"UPRM", "PRLL", "NWML", "GRDL", "WAEX", "GPML", "CENT", "LPOL"}
SEGMENTS = ("UPRM", "PRLL", "NWML", "GRDL", "WAEX", "MRTN", "GPML", "CENT", "LPOL", "LIEG", "KIRB", "REDL", "COLD",
            "NLAT", "ELAT", "ALEG", "BLEG", "EGAT", "MLAT", "WGAT", "SLAT", "WAIN", "JUDY", "EDM")
AREA_BAND = {"usjr": 0.007, "rest": 0.06}
SEGMENT_BAND = {"GPML": 0.038, "LPOL": 0.043, "GRDL": 0.050, "CENT": 0.054, "ALEG": 0.065, "PRLL": 0.083,
                "WAEX": 0.085, "BLEG": 0.148, "WGAT": 0.225}
OTHER_BAND = 0.25
MIN_K_DAYS = 0.90
# Station-level "today" estimate (PLAN 4b / L-058: stations show today's estimate, not last month's value) = GDSR field
# receipts(d) × k × the station's share of the latest TC month. That month is M-2 for about days 1-20 (e.g. September
# shares in early November), so the band is the M-2 back-test: P90 24.4 % (2024-01..2026-09 TC files; M-1 18.0 %,
# M-3 27.3 %; Reviewer on #193). It's a monthly back-test, so a single day can swing more.
# Written for the latest STATION_EST_DAYS gas days only.
STATION_BAND = 0.25
STATION_EST_DAYS = 3
GROUP = "NGTL / production split"
BC_STATIONS = frozenset({3820, 5106, 5107, 5108, 5116, 5119, 5132, 5133, 5134, 5135, 5144, 5147, 5148, 5163, 5200, 5202,
                         5204, 5207, 5209, 5228, 5232, 5242, 5244, 5247, 5249})
ZONE1, LNG_DIRECT = "alliance.zone1.receipts", "lng.export.direct"
TOTALS_GROUP = "Gas production / totals"
TOTAL_BAND = {"ab": 0.04, "bc": 0.05}
# AB + BC (PLAN step 5 item 1): ±4 % of ~12.2 (490) ⊕ ±5 % of ~7.6 (380) = 620 on ~19.8 Bcf/d = 3.1 % in quadrature,
# 4.4 % if both moved together → ±4 %. Its NGTL part is GDSR × k (k's next-month error ≤ 0.25 %) + measured borders.
TOTAL, TOTAL_NGTL = "production.total.daily_est", "production.total.ngtl_est"
TOTAL_NGTL_FIELD = "production.total.ngtl_field_est"     # L-073 nested rows: NGTL field receipts, AB + BC (a sum)
TOTAL_NGTL_FIELD_MEASURED = "production.total.ngtl_field_measured_est"   # L-088: the same × k (TC's measured basis)
TOTAL_ALL_BAND = 0.04
REST_BAND = {"ab": 0.30}                  # the "everything else" rows, relative (AB: R ≈ 1.36 Bcf/d incl. basis)
REST_ABS_BAND = {"bc": 300.0}             # BC, absolute MMcf/d: R_BC sits near 0, where a relative band collapses
ZONE = {"ab": "alliance.zone2.receipts", "bc": "alliance.zone1.receipts"}
ALLIANCE_CARRY_DAYS = 3        # today's Alliance day is posted the next morning: use the latest within 3 days
GD, GBE = "ngtl.gdsr.gordondale.flow", "ngtl.gdsr.groundbirch_east.flow"
REG = {"ab": "production.ab.monthly", "bc": "production.bc.monthly"}
CGL = "cgl.producer_direct"
# PLAN 6B: Westcoast T-North's gas going south (T-South), net of Aitken Creek storage, as its own part of BC production.
# Today: Station 2 South scheduled − Aitken storage (est.); in the regulator month: the monthly mean − BCER's measured
# storage net. Its part to NGTL (Sunset Creek, Gordondale) is already in the GDSR borders, so it isn't added again.
WC_S2S, WC_AITKEN_EST, AITKEN_NET = ("westcoast.oa.station2_south.scheduled", "westcoast.oa.aitken_storage_est",
                                     "bcer.aitken_storage.net_withdrawal")
# Total export, the basis of the export boxes' share rows (PLAN 4b): gas leaving AB + BC on a daily-measured path.
# NGTL total export (Empress, McNeill, AB-BC, Willow Valley, other borders) + Alliance (all to the US) + producer gas
# straight into CGL, counted as an export (lng.export.direct, the 4c daily regression). Westcoast to Huntingdon has no
# daily data: left out.
EXPORT_TOTAL = "export.total.flow_est"
EXPORT_PARTS = ("ngtl.gdsr.total_export.flow", "alliance.receipts.total", "lng.export.direct")
EXPORT_TOTAL_DAYS = 10
EXPORT_TOTAL_BAND = 0.03   # the carried CGL-direct part: ±23 % of ~1.4 Bcf/d in ~11 Bcf/d; the rest is measured
# Gas leaving the AB + NE BC production area by pipeline (L-070/L-071, Erik's zoomed-out "Exports" box; LNG separate):
#   NGTL total export − Willow Valley (Empress, McNeill, West Gate / AB-BC, other borders; GDSR, measured)
#   + Alliance (scheduled receipts; all of it leaves for the US)
#   + Westcoast T-South = Station 2 South, scheduled (gas_westcoast_oa)
# Erik (L-071): "Huntington shouldn't be on the exports, it should be tsouth … just the idea of whats leaving the AB,
# western BC production area." So no estimated term and no band. No double count: T-North's deliveries into NGTL
# (Sunset Creek, 26" Alberta East) are NGTL receipts and leave, if at all, through the NGTL borders; Station 2 South is
# T-North's other outlet, downstream of its inlets; Alliance is its own system. The id keeps its "_est" for continuity
# (Frontend reads it).
PIPE_TOTAL, PIPE_NGTL, PIPE_TSOUTH = "export.pipeline.total_est", "export.pipeline.ngtl", "export.pipeline.tsouth"
PIPE_INPUTS = ("ngtl.gdsr.total_export.flow", "ngtl.gdsr.willow_valley.flow", "alliance.receipts.total",
               "westcoast.oa.station2_south.scheduled")


def band(seg: str) -> float:
    return SEGMENT_BAND.get(seg, OTHER_BAND)


def area_of(seg: str) -> str:
    return "usjr" if seg in USJR else "rest"


def _rd_rows(rows: list[list]) -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    cols = None
    for r in rows:
        if r and str(r[0]).strip() == "Gas Day":
            hdr = [str(c or "").strip() for c in r]
            cols = {}
            for key, label in RD_COLS.items():
                j = next((i for i, h in enumerate(hdr) if h.startswith(label)), None)
                if j is None:
                    raise FormatDrift(f"rd: no column starting {label!r} in {hdr}")
                cols[key] = j
            continue
        if cols and r and isinstance(r[0], (int, float)) and r[cols["measured_receipts"]] not in (None, ""):
            d = (datetime(1899, 12, 30) + timedelta(days=float(r[0]))).date()      # Excel serial date
            out[d] = {k: float(r[j] or 0.0) for k, j in cols.items()}
    return out


def read_rd_book(data: bytes) -> dict[date, dict[str, float]]:
    """Every month sheet of an rd workbook."""
    out: dict[date, dict[str, float]] = {}
    for rows in su.read_xlsx_sheets(data):
        out.update(_rd_rows(rows))
    if not out:
        raise FormatDrift("rd workbook: no daily rows found")
    return out


def rd_field(v: dict[str, float]) -> float:
    return v["measured_receipts"] - v["storage_receipts"] - v["gordondale_receipts"] - v["groundbirch_east_receipts"]


def month_shares(recs: list[dict]) -> dict[date, dict[str, float]]:
    """{month: {"seg:<code>" / "area:<usjr|rest>" / "province:<ab|bc>": MMcf/d}} over field stations."""
    out: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for x in recs:
        if x["station"] in NON_FIELD:
            continue
        v = su.to_mmcfd(x["flow_e3m3"], x["month"])
        seg = x["segment"] or "?"
        out[x["month"]][f"seg:{seg}"] += v
        out[x["month"]][f"area:{area_of(seg)}"] += v
        out[x["month"]][f"province:{'bc' if x['station'] in BC_STATIONS else 'ab'}"] += v
    return {m: dict(v) for m, v in out.items()}


def history(start: datetime, end: datetime) -> dict[datetime, float]:
    return read_stored([GDSR], start, end).get(GDSR, {})


def kind_sid(key: str, measure: str) -> str:
    kind, name = key.split(":")
    return f"ngtl.{'segment' if kind == 'seg' else kind}.{name.lower()}.{measure}"


class NgtlSplit(Connector):
    gdsr = staticmethod(history)           # tests inject a fake
    stored = staticmethod(read_stored)     # regulator months, Gordondale / Groundbirch East, CGL direct (tests inject)

    def declared_series(self) -> list[SeriesMeta]:
        out = [SeriesMeta(f"ngtl.rd.{k}", self.id, f"NGTL measured {lab.lower()} (TC R&D)", "MMcf/d", "D", GROUP,
                          day="gas") for k, lab in RD_COLS.items()]
        for a, b in AREA_BAND.items():
            nm = "USJR" if a == "usjr" else "outside USJR"
            out.append(SeriesMeta(f"ngtl.area.{a}.receipt", self.id, f"NGTL field receipts {nm} (monthly, allocated)",
                                  "MMcf/d", "M", GROUP))
            out.append(SeriesMeta(f"ngtl.area.{a}.receipt_est", self.id,
                                  f"NGTL field receipts {nm}, daily estimate (±{b:.1%})", "MMcf/d", "D", GROUP,
                                  extra={"band": b}, day="gas"))
        for s in SEGMENTS:
            out.append(SeriesMeta(f"ngtl.segment.{s.lower()}.receipt", self.id, f"NGTL segment {s} receipts (monthly)",
                                  "MMcf/d", "M", GROUP))
            out.append(SeriesMeta(f"ngtl.segment.{s.lower()}.receipt_est", self.id,
                                  f"NGTL segment {s} receipts, daily estimate (±{band(s):.0%})", "MMcf/d", "D", GROUP,
                                  extra={"band": band(s)}, day="gas"))
        for pv, nm in (("ab", "Alberta"), ("bc", "BC")):
            out.append(SeriesMeta(f"ngtl.province.{pv}.receipt", self.id, f"NGTL {nm} field receipts (monthly, allocated)",
                                  "MMcf/d", "M", GROUP))
            out.append(SeriesMeta(f"ngtl.province.{pv}.receipt_est", self.id, f"NGTL {nm} field receipts, daily estimate",
                                  "MMcf/d", "D", GROUP, day="gas"))
            out.append(SeriesMeta(f"production.{pv}.daily_est", self.id,
                                  f"{nm} gas production today (est., ±{TOTAL_BAND[pv]:.0%})", "MMcf/d", "D",
                                  TOTALS_GROUP, extra={"band": TOTAL_BAND[pv]}, day="gas"))
        out += [SeriesMeta(f"production.{pv}.other_est", self.id, f"{nm}: everything else, carried from the last "
                           "regulator month (" + (f"±{REST_BAND[pv]:.0%}" if pv in REST_BAND else
                                                  f"est., ±{REST_ABS_BAND[pv]:.0f} MMcf/d") + ")", "MMcf/d", "D",
                           TOTALS_GROUP, extra=({"band": REST_BAND[pv]} if pv in REST_BAND
                                                else {"band_mmcfd": REST_ABS_BAND[pv]}), day="gas")
                for pv, nm in (("ab", "Alberta"), ("bc", "BC"))]
        out += [SeriesMeta("production.ab.alliance_est", self.id, "Alliance Zone 2 today (part of AB production)",
                           "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta("production.bc.alliance_est", self.id, "Alliance Zone 1 today, whole: BC + NW AB (part of BC "
                           "production)", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta("production.bc.ngtl_est", self.id, "BC gas into NGTL today (BC stations + Gordondale + "
                           "Groundbirch East)", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta("production.bc.cgl_direct_est", self.id, "BC producer gas straight into CGL, daily regression (±20 %)",
                           "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta(TOTAL, self.id, f"AB + BC gas production today (est., ±{TOTAL_ALL_BAND:.0%})", "MMcf/d", "D",
                           TOTALS_GROUP, extra={"band": TOTAL_ALL_BAND}, day="gas"),
                SeriesMeta("production.bc.tnorth_south_est", self.id, "BC production: Westcoast T-North to T-South, net of "
                           "Aitken Creek storage (scheduled − storage est.)", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta(TOTAL_NGTL, self.id, "AB + BC production into NGTL today (field receipts + Gordondale + "
                           "Groundbirch East)", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta(TOTAL_NGTL_FIELD, self.id, "NGTL field receipts today, AB + BC (no border receipts): TC's "
                           "GDSR figure", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta(TOTAL_NGTL_FIELD_MEASURED, self.id, "NGTL field receipts today on TC's measured basis: "
                           "GDSR × k (k = TC metered ÷ GDSR, latest month with both, ~1.03)", "MMcf/d", "D",
                           TOTALS_GROUP, day="gas"),
                *[SeriesMeta(f"production.{pv}.other_ngtl_basis_est", self.id, f"{nm} everything else: the part that is "
                             "NGTL's measurement basis, (k − 1) × the regulator month's NGTL field receipts",
                             "MMcf/d", "D", TOTALS_GROUP, day="gas") for pv, nm in (("ab", "AB"), ("bc", "BC"))],
                SeriesMeta(PIPE_TOTAL, self.id, "Gas leaving the AB/NE BC production area (pipelines; LNG separate): "
                           "NGTL borders except Willow Valley + Alliance + Westcoast T-South", "MMcf/d", "D",
                           TOTALS_GROUP, day="gas"),
                SeriesMeta(PIPE_NGTL, self.id, "Pipeline exports: NGTL borders except Willow Valley (Empress, McNeill, "
                           "AB-BC, other borders; measured)", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta(PIPE_TSOUTH, self.id, "Gas leaving the production area: Westcoast T-South (Station 2 South, "
                           "scheduled)", "MMcf/d", "D", TOTALS_GROUP, day="gas"),
                SeriesMeta(EXPORT_TOTAL, self.id, "Total export: NGTL borders + Willow Valley, Alliance, CGL producer-"
                           "direct (Westcoast to Huntingdon not included)", "MMcf/d", "D", TOTALS_GROUP,
                           extra={"band": EXPORT_TOTAL_BAND}, day="gas")]
        out.append(SeriesMeta("montney.bc.via_tnorth_est", self.id, "BC gas into NGTL via Westcoast T-North (Gordondale + "
                              "Groundbirch East, measured)", "MMcf/d", "D", TOTALS_GROUP, day="gas"))
        return out

    @forget_on_error
    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        page = p.get("page", su.PAGE)
        today = local_today()
        as_of = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        res = FetchResult(meta=self.declared_series())
        declared = {m.series_id for m in res.meta}

        # 1. station months → monthly segment/area actuals, and the latest month's shares
        # PLAN 9: the source runs hourly so the daily estimates follow the GDSR (stored by its own source) within the
        # hour; TC's files change at most daily, so each is downloaded at most every FILE_MAX_AGE and reused between.
        def get(url: str, **kw):
            return get_at_most_every(ctx, url, FILE_MAX_AGE, **kw)

        links = su.receipt_links(get(page).text, page)
        recs = []
        for year in sorted(y for y in links if y >= today.year - 1):
            rows, cut = su.parse_receipts(su.read_sheet(links[year], get(links[year]).content), year)
            recs += [x for x in rows if su._month_end(x["month"]) <= cut]
        months = month_shares(recs)
        for m, v in months.items():
            ts = datetime(m.year, m.month, 1)
            for key, val in v.items():
                sid = kind_sid(key, "receipt")
                if sid in declared:
                    res.obs.append(Obs(sid, ts, val, as_of))
        share_month = max(months)
        last = months[share_month]
        field_total = last["area:usjr"] + last["area:rest"]

        # 2. rd measured days → rd series and k (latest complete rd month)
        rd: dict[date, dict[str, float]] = {}
        for year in sorted({today.year, (today.replace(day=1) - timedelta(days=1)).year}):
            r = get(p.get("rd_url", RD_URL).format(year=year), timeout=120, retries=0, allow=(404,))
            if r.status_code == 404:
                continue
            r.raise_for_status()
            rd.update(read_rd_book(r.content))
        for d, v in rd.items():
            for k, val in v.items():
                res.obs.append(Obs(f"ngtl.rd.{k}", datetime(d.year, d.month, d.day), val * E3M3_TO_MMCF, as_of))
        rd_month = max(d.replace(day=1) for d in rd)
        gd_from = min(rd_month, (share_month - timedelta(days=62)).replace(day=1))     # covers the regulator month too
        gd = {t.date(): v for t, v in self.gdsr(datetime(gd_from.year, gd_from.month, 1) - timedelta(days=1),
                                               ctx.now).items()}
        ratios = [rd_field(rd[d]) * E3M3_TO_MMCF / gd[d] for d in rd if d.replace(day=1) == rd_month and gd.get(d)]
        days_in = sum(1 for d in rd if d.replace(day=1) == rd_month)
        if not days_in or len(ratios) < MIN_K_DAYS * days_in:
            res.warnings.append(f"NO-RESULT estimates: GDSR covers {len(ratios)}/{days_in} days of {rd_month:%Y-%m} "
                                f"(need {MIN_K_DAYS:.0%}) for the k rescale")
            return res
        k = sum(ratios) / len(ratios)

        # 3. daily estimates for the gas days GDSR has after the rd month
        for d, g in sorted(gd.items()):
            if d.replace(day=1) <= rd_month:          # rd has measured values there; the estimate is for later days
                continue
            ts = datetime(d.year, d.month, d.day)
            for key, val in last.items():
                sid = kind_sid(key, "receipt_est")
                if sid in declared:
                    res.obs.append(Obs(sid, ts, g * val / field_total, as_of))       # GDSR basis (L-088)
        # 4. station estimates for the latest gas days: today's field receipts × k × last month's station share
        st_flow: dict[int, float] = defaultdict(float)
        st_name: dict[int, str] = {}
        for x in recs:
            if x["month"] == share_month and x["station"] not in NON_FIELD:
                st_flow[x["station"]] += su.to_mmcfd(x["flow_e3m3"], x["month"])
                st_name[x["station"]] = x["name"]
        est_days = sorted(d for d in gd if d.replace(day=1) > rd_month)[-STATION_EST_DAYS:]
        for st, v in st_flow.items():
            if v <= 0:
                continue
            sid = su.series_id(st, "receipt_est")
            for d in est_days:
                res.obs.append(Obs(sid, datetime(d.year, d.month, d.day), gd[d] * v / field_total, as_of))
            res.meta.append(SeriesMeta(sid, self.id, f"NGTL {st_name[st].title()} ({st}) receipts, today's estimate "
                                                     f"(±{STATION_BAND:.0%})", "MMcf/d", "D", GROUP,
                                       extra={"band": STATION_BAND, "station": st, "share_month": share_month.isoformat()},
                                       day="gas"))
        res.warnings.append(f"k = {k:.4f} from {rd_month:%Y-%m} ({len(ratios)} d); shares from {share_month:%Y-%m}; "
                            f"station estimates for {len(est_days)} gas day(s)")
        self._totals(ctx, res, gd, k, months, last, field_total, rd_month, as_of)
        return res

    def _export_total(self, ctx, res, as_of) -> None:
        """Total export (the share-row basis, PLAN 4b): NGTL total export + Alliance + CGL producer-direct, per gas day."""
        st = self.stored(list(EXPORT_PARTS), ctx.now - timedelta(days=EXPORT_TOTAL_DAYS), ctx.now)
        parts = [st.get(s) or {} for s in EXPORT_PARTS]
        for t in sorted(set.intersection(*(set(p) for p in parts))):
            res.obs.append(Obs(EXPORT_TOTAL, t, sum(p[t] for p in parts), as_of))
        self._pipeline_export(ctx, res, as_of)

    def _pipeline_export(self, ctx, res, as_of) -> None:
        """Gas leaving the AB + NE BC production area by pipeline (L-071), per gas day; see PIPE_* above."""
        st = self.stored(list(PIPE_INPUTS), ctx.now - timedelta(days=EXPORT_TOTAL_DAYS + 2), ctx.now)
        total, wv, alliance, tsouth = (st.get(s) or {} for s in PIPE_INPUTS)
        n = 0
        for t in sorted(set(total) & set(wv) & set(alliance) & set(tsouth)):
            ngtl = total[t] - wv[t]
            res.obs += [Obs(PIPE_NGTL, t, ngtl, as_of), Obs(PIPE_TSOUTH, t, tsouth[t], as_of),
                        Obs(PIPE_TOTAL, t, ngtl + alliance[t] + tsouth[t], as_of)]
            n += 1
        if not n:
            res.warnings.append("NO-RESULT export.pipeline.total_est: no gas day with NGTL exports, Willow Valley, "
                                "Alliance and Westcoast Station 2 South")

    def _province_totals(self, res, gd, k, last, field_total, days, border, st, months, rd_month, as_of) -> None:
        """AB / BC production today: the visible sum of public meters + everything else (see the module docstring)."""
        for pv in ("ab", "bc"):
            reg = st.get(REG[pv]) or {}
            if not reg:
                res.warnings.append(f"NO-RESULT production.{pv}: no {REG[pv]} stored yet")
                continue
            mref = max(reg)
            m = mref.date()
            n_days = ((m.replace(day=28) + timedelta(days=4)).replace(day=1) - m).days
            mdays = [d for d in gd if d.replace(day=1) == m]
            share = months.get(m, {})
            if len(mdays) < MIN_K_DAYS * n_days or not share.get(f"province:{pv}"):
                res.warnings.append(f"NO-RESULT production.{pv}: {m:%Y-%m} has {len(mdays)} GDSR days or no station "
                                    f"shares")
                continue
            field_m = (sum(gd[d] for d in mdays) / len(mdays) * share[f"province:{pv}"]
                       / (share["province:ab"] + share["province:bc"]))       # GDSR basis (L-088)
            ngtl_m, basis_m = field_m, (k - 1.0) * field_m       # R holds basis_m: TC's meters read k × GDSR
            if pv == "bc":
                bdays = [border[d] for d in mdays if d in border]
                if len(bdays) < MIN_K_DAYS * n_days:
                    res.warnings.append(f"NO-RESULT production.bc: {m:%Y-%m} lacks Gordondale/Groundbirch East days")
                    continue
                ngtl_m += sum(bdays) / len(bdays)
            cgl = (st.get(CGL) or {}).get(mref) if pv == "bc" else None       # BCER's actual, in Mref (for R)
            # today's CGL part: the daily regression (gas_lng_bcer, lng.export.direct); the Mref actual if a day lacks it
            cgl_day = {t.date(): v for t, v in (st.get(LNG_DIRECT) or {}).items()} if pv == "bc" else {}
            zone = {t.date(): v for t, v in (st.get(ZONE[pv]) or {}).items()}
            zm = [zone[d] for d in mdays if d in zone]
            with_alliance = len(zm) >= MIN_K_DAYS * n_days
            if not with_alliance:
                res.warnings.append(f"production.{pv}: Alliance has {len(zm)} of {n_days} days of {m:%Y-%m} stored; "
                                    f"it stays inside 'everything else' until the history fills (gas_alliance)")
            r = reg[mref] - ngtl_m - (sum(zm) / len(zm) if with_alliance else 0.0) - (cgl or 0.0)
            ts_m, ts_day = None, {}
            if pv == "bc":          # T-North south, net of Aitken (PLAN 6B): in the sum when both the month and the day have it
                s2 = [v for t, v in (st.get(WC_S2S) or {}).items() if t.date().replace(day=1) == m]
                anet = (st.get(AITKEN_NET) or {}).get(mref)
                if len(s2) >= MIN_K_DAYS * n_days and anet is not None:
                    ts_m = sum(s2) / len(s2) - anet
                    s2d = {t.date(): v for t, v in (st.get(WC_S2S) or {}).items()}
                    ts_day = {t.date(): s2d[t.date()] - v for t, v in (st.get(WC_AITKEN_EST) or {}).items()
                              if t.date() in s2d}
                else:
                    res.warnings.append(f"production.bc: T-North south not separated ({len(s2)} Station 2 South days of "
                                        f"{m:%Y-%m}, Aitken net {'in' if anet is not None else 'missing'}); it stays in "
                                        "'everything else'")
            for d in days:
                if pv == "bc" and d not in border:
                    continue
                za = None
                if with_alliance:
                    za = next((zone[d - timedelta(days=i)] for i in range(ALLIANCE_CARRY_DAYS + 1)
                               if d - timedelta(days=i) in zone), None)
                    if za is None:
                        continue
                ts = datetime(d.year, d.month, d.day)
                ngtl = gd[d] * last[f"province:{pv}"] / field_total + (border[d] if pv == "bc" else 0.0)
                cgl_d = cgl_day.get(d, cgl) if cgl is not None else None
                rd, tnorth = r, None
                if ts_m is not None and d in ts_day:          # the T-North part in, and out of the remainder
                    rd, tnorth = r - ts_m, ts_day[d]
                    res.obs.append(Obs("production.bc.tnorth_south_est", ts, tnorth, as_of))
                res.obs += [Obs(f"production.{pv}.daily_est", ts,
                                ngtl + (za or 0.0) + (cgl_d or 0.0) + (tnorth or 0.0) + rd, as_of),
                            Obs(f"production.{pv}.other_est", ts, rd, as_of),
                            Obs(f"production.{pv}.other_ngtl_basis_est", ts, basis_m, as_of)]
                if za is not None:
                    res.obs.append(Obs(f"production.{pv}.alliance_est", ts, za, as_of))
                if pv == "bc":
                    res.obs.append(Obs("production.bc.ngtl_est", ts, ngtl, as_of))
                    if cgl is not None:
                        res.obs.append(Obs("production.bc.cgl_direct_est", ts, cgl_d, as_of))
            res.warnings.append(f"production.{pv}: everything else {r:,.0f} MMcf/d from {m:%Y-%m} (regulator "
                                f"{reg[mref]:,.0f} − NGTL {ngtl_m:,.0f}"
                                + (f" − Alliance {sum(zm) / len(zm):,.0f}" if with_alliance else "")
                                + (f" − CGL direct {cgl:,.0f}" if cgl is not None else "")
                                + f"; incl. NGTL's measurement basis {basis_m:,.0f})")

    @staticmethod
    def _production_total(res, as_of) -> None:
        """AB + BC production today and its NGTL part, for the zoomed-out total box (PLAN step 5 item 1)."""
        by = {}
        for o in res.obs:
            if o.series_id in ("production.ab.daily_est", "production.bc.daily_est", "ngtl.province.ab.receipt_est",
                               "production.bc.ngtl_est", "ngtl.province.bc.receipt_est"):
                by.setdefault(o.series_id, {})[o.ts] = o.value
        ab, bc = by.get("production.ab.daily_est", {}), by.get("production.bc.daily_est", {})
        for ts in sorted(set(ab) & set(bc)):
            res.obs.append(Obs(TOTAL, ts, ab[ts] + bc[ts], as_of))
            ngtl_ab, ngtl_bc = by.get("ngtl.province.ab.receipt_est", {}).get(ts), by.get("production.bc.ngtl_est", {}).get(ts)
            if ngtl_ab is not None and ngtl_bc is not None:
                res.obs.append(Obs(TOTAL_NGTL, ts, ngtl_ab + ngtl_bc, as_of))
            field_bc = by.get("ngtl.province.bc.receipt_est", {}).get(ts)
            if ngtl_ab is not None and field_bc is not None:     # NGTL field receipts, AB + BC (no border receipts)
                res.obs.append(Obs(TOTAL_NGTL_FIELD, ts, ngtl_ab + field_bc, as_of))

    def _totals(self, ctx, res, gd, k, months, last, field_total, rd_month, as_of) -> None:
        """AB / BC production today = NGTL by province today + the remainder of the latest regulator month."""
        try:
            st = self.stored([REG["ab"], REG["bc"], CGL, GD, GBE, ZONE1, ZONE["ab"], LNG_DIRECT, WC_S2S, WC_AITKEN_EST,
                              AITKEN_NET], ctx.now - timedelta(days=200), ctx.now)
            self._export_total(ctx, res, as_of)
        except Exception as e:  # noqa: BLE001
            res.warnings.append(f"NO-RESULT production / export totals: store read failed ({e})")
            return
        gdv, gbe = st.get(GD) or {}, st.get(GBE) or {}
        border = {t.date(): gdv[t] + v for t, v in gbe.items() if t in gdv}
        days = sorted(d for d in gd if d.replace(day=1) > rd_month)
        for d in days:              # T-North into NGTL, for every day GDSR has both borders (the BC production rows)
            if d in border:
                res.obs.append(Obs("montney.bc.via_tnorth_est", datetime(d.year, d.month, d.day), border[d], as_of))
        self._province_totals(res, gd, k, last, field_total, days, border, st, months, rd_month, as_of)
        self._production_total(res, as_of)
        for o in [o for o in res.obs if o.series_id == TOTAL_NGTL_FIELD]:
            res.obs.append(Obs(TOTAL_NGTL_FIELD_MEASURED, o.ts, o.value * k, as_of))
