"""AB + BC tie-out check, daily (L-079, Erik): the NGTL net storage box's "does it add up?" panel.

Erik: "AB + BC prod − exports − demand = NGTL net storage + Aitken Creek net storage … This is essentially just saying
AB + BC should tie out. And that formula is the check." and "LNG should count as an export". So exports = pipelines +
LNG. Findings: docs/research/gas-balance.md. In short, it ties in timing once storage is counted with the right sign
(a production surplus is an **injection**, and the storage series are + = withdrawal). The monthly LHS and −(storage)
correlate 0.99. But the tie-out error is a steady ~+1.25 Bcf/d. That gas is Alberta's: Petrinex sales gas that never
enters NGTL or Alliance (AB use off NGTL), the daily estimate's "everything else" (production.ab.other_est). The
export parts don't overlap: Willow Valley is in LNG and out of the pipeline export, and production's CGL direct = LNG's
Sunset + D/C.

Inputs (read back from the store, read-only; MMcf/d per gas day, + = withdrawal for storage):
  P   production.total.daily_est          AB + BC production today (ngtl_split; ±4 %)
  R   production.ab.other_est + production.bc.other_est   its parts carried from the last regulator month
  E   export.pipeline.total_est           gas leaving by pipeline: NGTL borders except Willow Valley + Alliance + T-South
  L   lng.export.total                    LNG: Willow Valley + Sunset Creek + derived D/C (±166 MMcf/d)
  D   ngtl.gdsr.intraprovincial.flow      AB demand on NGTL (measured)
  NS  ngtl.gdsr.net_storage.flow          NGTL net storage (measured)
  A   westcoast.oa.aitken_storage_est     Aitken Creek net storage (est.; band westcoast.oa.aitken_storage_band)

Series (MMcf/d, D, gas day; storage-signed ones + = withdrawal, so they plot against the NGTL net storage box):
  balance.ab_bc.production_est         P as used: production.total.daily_est with LNG's current CGL direct (+); the
                                       tie-out panel's production bar, so the waterfall sums to the residual
  balance.ab_bc.exports_est            E + L: exports, pipelines + LNG (+)
  balance.ab_bc.implied_storage_est    E + L + D − P: what storage would have to supply for Erik's identity to hold
  balance.ab_bc.storage_actual         NS + A: what storage did
  balance.ab_bc.residual_est           the tie-out error: actual − implied = P − E − L − D + NS + A, production (or
                                       withdrawal) that no measured use accounts for. ~+1.25 Bcf/d is normal (AB use
                                       off NGTL).
  balance.ab_bc.residual_metered_est   residual − R: metered production only. Every part then appears on both sides,
                                       so this reduces to NGTL's own books: ≈ 0 ± NGTL fuel/linepack (daily P10/P90
                                       −262/+324 over 5 years). Since L-088 the NGTL part and R are on the GDSR
                                       basis (R holds the (k − 1) × GDSR basis), so this is GDSR's closure; before,
                                       it carried +(k − 1) × GDSR field receipts (~+400) on top
  balance.ab_bc.band_est               ± on implied and residual: √((4 % P)² + 166² + Aitken band²)
  balance.ab_bc.metered_band_est       ± on the metered residual: √(300² + Aitken band²) (300 = NGTL's daily closure)

A gas day is written only when every input is stored. No guesses: Aitken's estimate started on 2026-11-01.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.connectors.petrinex_destination import GROUP_LABEL, OFF_GROUPS
from montney.store import Obs, SeriesMeta

GROUP = "Gas balance (AB + BC)"
P, RAB, RBC = "production.total.daily_est", "production.ab.other_est", "production.bc.other_est"
RBC_BASIS = "production.bc.other_ngtl_basis_est"      # L-088: the basis part of R_BC (ngtl_split)
E, L, D = "export.pipeline.total_est", "lng.export.total", "ngtl.gdsr.intraprovincial.flow"
NS, A, AB = "ngtl.gdsr.net_storage.flow", "westcoast.oa.aitken_storage_est", "westcoast.oa.aitken_storage_band"
INPUTS = (P, RAB, RBC, E, L, D, NS, A)
# The same gas on both sides: production's "CGL direct" (ngtl_split, its last run) and LNG's Sunset + D/C
# (lng.export.direct, gas_lng_bcer, revised hourly). They cancel only if they're the same vintage. A fresh gas day can
# differ by hundreds (2026-11-02: 1,445 vs 1,201 after Sunset Creek was revised), so the check uses LNG's current one.
CGL_P, CGL_L = "production.bc.cgl_direct_est", "lng.export.direct"
P_BAND, LNG_BAND_MMCFD, AITKEN_BAND_MMCFD, NGTL_CLOSURE_MMCFD = 0.04, 166.0, 164.0, 300.0
DAYS = 14
PFX = "balance.ab_bc."


def balance(v: dict[str, float], aitken_band: float | None = None) -> dict[str, float]:
    """One gas day's inputs (the INPUTS ids) → the balance series."""
    implied = v[E] + v[L] + v[D] - v[P]
    actual = v[NS] + v[A]
    resid = actual - implied
    ab = AITKEN_BAND_MMCFD if aitken_band is None else aitken_band
    return {PFX + "production_est": v[P], PFX + "exports_est": v[E] + v[L], PFX + "implied_storage_est": implied, PFX + "storage_actual": actual, PFX + "residual_est": resid,
            PFX + "residual_metered_est": resid - v[RAB] - v[RBC],
            PFX + "band_est": math.sqrt((P_BAND * v[P]) ** 2 + LNG_BAND_MMCFD ** 2 + ab ** 2),
            PFX + "metered_band_est": math.sqrt(NGTL_CLOSURE_MMCFD ** 2 + ab ** 2)}


# ------------------------------------------------------------------ what the tie-out error is (L-080 item 4)
# docs/research/gas-residual.md. The daily tie-out error splits exactly into:
#   NGTL measurement basis   (k − 1) × GDSR field receipts: production's NGTL part is TC-measured (GDSR × k, k ≈ 1.03,
#                            ngtl_split), but NGTL's deliveries and storage in the check are GDSR; TC's meters read 3 %
#                            above GDSR (fuel / unaccounted-for / allocation basis). k from the latest month with both.
#   NGTL closure (GDSR)      field + Gordondale + Groundbirch East + net storage − intra − borders − Willow Valley, that
#                            day: linepack change and GDSR noise (5-year daily P10/P90 −262/+324, mean +29)
#   AB gas off NGTL          Petrinex sales gas by each plant's nearest meter station's system (petrinex_destination),
#                            the latest Petrinex month, carried: Gas Alberta, ATCO Pipelines outside NGTL's receipts,
#                            AEC Suffield → TC Mainline, Pioneer Brazeau, Suncor Albersun, AltaGas Cold Lake, other
#   BC remainder             production.bc.other_est less its NGTL-basis part (production.bc.other_ngtl_basis_est,
#                            L-088: the remainders absorb (k − 1) × GDSR, already in the basis term above): BC
#                            meters above BCER plant outlets (incl. NW-Alberta gas in
#                            Alliance Zone 1, which BC's sum carries)
#   unexplained              the rest, kept: AB's carried remainder vs the attribution, carry/vintage mismatches
UA = "balance.ab.unaccounted."
RD = ("ngtl.rd.measured_receipts", "ngtl.rd.storage_receipts", "ngtl.rd.gordondale_receipts",
      "ngtl.rd.groundbirch_east_receipts")
GF, GGD, GGB = "ngtl.gdsr.field_receipts.flow", "ngtl.gdsr.gordondale.flow", "ngtl.gdsr.groundbirch_east.flow"
G_OUT = tuple(f"ngtl.gdsr.{k}.flow" for k in ("empress", "mcneill", "alberta_bc", "other_borders", "willow_valley"))
SALES_TO = {g: f"petrinex.ab.sales_to.{g}" for g in OFF_GROUPS}
BASIS_DAYS = 75
BASIS_BAND_MMCFD = 50.0          # k ranged 1.026-1.033 over Jan-Sep 2026: ±0.0035 × ~14.4 Bcf/d
ATTRIB_BAND = 0.4                # nearest-station attribution: the unambiguous share is ~60 % of the off-system total
BC_REMAINDER_BAND_MMCFD = 300.0  # ngtl_split REST_ABS_BAND["bc"]


def basis_k(rd: dict, gf: dict) -> tuple[float, datetime] | None:
    """TC measured field receipts ÷ GDSR field receipts for the latest month with ≥ 90 % of days of both."""
    field = {t: rd[RD[0]][t] - rd[RD[1]].get(t, 0.0) - rd[RD[2]].get(t, 0.0) - rd[RD[3]].get(t, 0.0)
             for t in rd.get(RD[0], {})}
    days = [t for t in field if gf.get(t)]
    for m in sorted({t.replace(day=1) for t in days}, reverse=True):
        md = [t for t in days if t.replace(day=1) == m]
        n = ((m.replace(day=28) + timedelta(days=4)).replace(day=1) - m).days
        if len(md) >= 0.9 * n:
            return sum(field[t] for t in md) / sum(gf[t] for t in md), m
    return None


def unaccounted(resid: float, k: float, day: dict[str, float], off: dict[str, float], bc: float) -> dict[str, float]:
    """One gas day's tie-out error → its parts (they sum to resid exactly)."""
    basis = (k - 1.0) * day[GF]
    closure = day[GF] + day[GGD] + day[GGB] + day[NS] - day[D] - sum(day[g] for g in G_OUT)
    ab_off = sum(off.values())
    out = {UA + "ngtl_basis_est": basis, UA + "ngtl_closure": closure, UA + "ab_offsystem_est": ab_off,
           UA + "bc_remainder_est": bc, UA + "unexplained_est": resid - basis - closure - ab_off - bc,
           # the parts' bands in quadrature (Reviewer on #313): the unexplained is no more precise than they are
           UA + "unexplained_band_est": math.sqrt(BASIS_BAND_MMCFD ** 2 + (ATTRIB_BAND * ab_off) ** 2
                                                  + BC_REMAINDER_BAND_MMCFD ** 2)}
    out.update({UA + f"{g}_est": v for g, v in off.items()})
    return out


class GasBalance(Connector):
    stored = staticmethod(read_stored)          # tests inject

    def declared_series(self) -> list[SeriesMeta]:
        m = lambda k, label: SeriesMeta(PFX + k, self.id, label, "MMcf/d", "D", GROUP, day="gas")   # noqa: E731
        return [m("production_est", "AB + BC production as the tie-out uses it (est.): production.total.daily_est with "
                  "LNG's current CGL direct"),
                m("exports_est", "AB + BC exports: pipelines + LNG (est.)"),
                m("implied_storage_est", "AB + BC tie-out: storage implied by production − exports (pipelines + LNG) − "
                  "AB demand (+ = withdrawal; est.)"),
                m("storage_actual", "AB + BC storage, actual: NGTL net + Aitken Creek net (+ = withdrawal)"),
                m("residual_est", "AB + BC tie-out error: production − exports − AB demand + storage (~+1.25 Bcf/d "
                  "is AB use off NGTL; est.)"),
                m("residual_metered_est", "AB + BC tie-out error with metered production only (= NGTL's own books; "
                  "est.)"),
                m("band_est", "AB + BC tie-out: ± on the implied storage and the tie-out error"),
                m("metered_band_est", "AB + BC tie-out: ± on the metered tie-out error")] + self.unaccounted_meta()

    def unaccounted_meta(self) -> list[SeriesMeta]:
        u = lambda k, label: SeriesMeta(UA + k, self.id, label, "MMcf/d", "D", GROUP, day="gas")   # noqa: E731
        return [u("ngtl_basis_est", f"Tie-out error part: NGTL measurement basis, TC meters vs GDSR (k − 1) × field "
                  f"receipts (est., ±{BASIS_BAND_MMCFD:.0f} MMcf/d)"),
                u("ngtl_closure", "Tie-out error part: NGTL's own GDSR closure that day (linepack, fuel; measured)"),
                u("ab_offsystem_est", f"Tie-out error part: AB sales gas off NGTL and Alliance, by nearest meter station "
                  f"(latest Petrinex month; est., ±{ATTRIB_BAND:.0%})"),
                u("bc_remainder_est", "Tie-out error part: BC remainder, BC meters vs BCER plant outlets (est., ±300 "
                  "MMcf/d)"),
                u("unexplained_est", "Tie-out error part: unexplained (what the parts above don't account for; ± "
                  "balance.ab.unaccounted.unexplained_band_est)"),
                u("unexplained_band_est", "Tie-out error part: ± on the unexplained, the parts' bands in quadrature "
                  "(basis, attribution, BC remainder)")] + [
                u(f"{g}_est", f"AB gas off NGTL: {GROUP_LABEL[g]} (latest Petrinex month; est., ±{ATTRIB_BAND:.0%}"
                  + (", destination unknown" if g == "pioneer_brazeau" else "") + ")") for g in OFF_GROUPS]

    def fetch(self, ctx: Context) -> FetchResult:
        days = int(self.spec.get("params", {}).get("days", DAYS))
        st = self.stored(list(INPUTS) + [AB, CGL_P, CGL_L, RBC_BASIS], ctx.now - timedelta(days=days), ctx.now)
        series = {s: st.get(s) or {} for s in INPUTS}
        cgl_p, cgl_l = st.get(CGL_P) or {}, st.get(CGL_L) or {}
        band = st.get(AB) or {}
        rbc_basis = st.get(RBC_BASIS) or {}
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        common = set.intersection(*(set(v) for v in series.values()))
        if common:      # the breakdown's own inputs (L-080 item 4)
            ex = self.stored(list(RD) + [GF, GGD, GGB, *G_OUT], ctx.now - timedelta(days=max(days, BASIS_DAYS)), ctx.now)
            sales = self.stored(list(SALES_TO.values()), ctx.now - timedelta(days=150), ctx.now)
            kk = basis_k({s: ex.get(s) or {} for s in RD}, ex.get(GF) or {})
            if kk is None:
                res.warnings.append("unaccounted: no month with TC measured and GDSR field receipts; no breakdown")
        for t in sorted(common):
            v = {s: series[s][t] for s in INPUTS}
            if t in cgl_p and t in cgl_l:          # one vintage of CGL direct on both sides (see CGL_P above)
                v[P] += cgl_l[t] - cgl_p[t]
                if abs(cgl_l[t] - cgl_p[t]) > 50:
                    res.warnings.append(f"{t:%Y-%m-%d}: production's CGL direct {cgl_p[t]:,.0f} vs LNG's {cgl_l[t]:,.0f}; "
                                        "the check uses LNG's (ngtl_split hasn't re-run since)")
            vals = balance(v, band.get(t))
            res.obs += [Obs(sid, t, v, as_of) for sid, v in vals.items()]
            if kk is None:
                continue
            day = {s: (ex.get(s) or {}).get(t) for s in (GF, GGD, GGB, *G_OUT)}
            day[NS], day[D] = series[NS][t], series[D][t]
            months = [m for m in set.intersection(*(set(sales.get(v) or {}) for v in SALES_TO.values())) if m <= t]
            if any(v is None for v in day.values()) or not months:
                res.warnings.append(f"unaccounted {t:%Y-%m-%d}: GDSR borders or a Petrinex destination month missing")
                continue
            off = {g: sales[sid][max(months)] for g, sid in SALES_TO.items()}
            parts = unaccounted(vals[PFX + "residual_est"], kk[0], day, off, series[RBC][t] - rbc_basis.get(t, 0.0))
            res.obs += [Obs(sid, t, v, as_of) for sid, v in parts.items()]
        if not common:
            missing = [s for s, v in series.items() if not v]
            res.warnings.append("NO-RESULT balance: no gas day with every input" + (f" (none stored: {missing})"
                                                                                  if missing else ""))
        return res


# ------------------------------------------------------------------ monthly history (regulator months)
# The daily check starts 2026-11-01 (its inputs start then). For the chart's history, the same tie-out per regulator
# month, from regulator and measured data (docs/research/gas-balance.md §2). Every term + except storage (+ = withdrawal):
#   production = Petrinex AB (storage out) + BCER BC (Aitken storage out)         production.<ab|bc>.monthly
#   exports    = NGTL Empress + McNeill + AB-BC + other borders (GDSR monthly means)
#                + Alliance: its receipts when ≥ 90 % of the month is stored, else the CER border key point
#                + T-South: Westcoast Station 2 South scheduled, else the CER key points Huntingdon export + FortisBC
#                  Lower Mainland + Kingsvale (~70-160 MMcf/d less: PNG, FortisBC Interior, fuel)
#                + LNG: LNG Canada feed-gas receipts (BCER; 0 before its first reported month, 2024-08)
#   demand     = NGTL intraprovincial (GDSR)
#   storage    = NGTL net (GDSR) + Aitken net withdrawal (BCER)
M_PROD = ("production.ab.monthly", "production.bc.monthly")
M_LNG, M_AIT = "lng.lng_canada.receipts", "bcer.aitken_storage.net_withdrawal"
G = "ngtl.gdsr.{}.flow"
M_BORDERS = tuple(G.format(k) for k in ("empress", "mcneill", "alberta_bc", "other_borders"))
M_DEMAND, M_NS = G.format("intraprovincial"), G.format("net_storage")
M_ALLIANCE = ("alliance.receipts.total", "cer.alliance.border.throughput")
M_TSOUTH = ("westcoast.oa.station2_south.scheduled",
            ("cer.westcoast.huntingdon_export.throughput", "cer.westcoast.fortisbc_lm.throughput",
             "cer.westcoast.kingsvale.throughput"))
MIN_DAYS = 0.9
FROM_MONTH = "2024-01"


def month_means(series: dict, min_share: float = MIN_DAYS) -> dict:
    """{month start: mean} over the months with ≥ min_share of their days stored."""
    acc: dict = {}
    for t, v in series.items():
        acc.setdefault(t.replace(day=1, hour=0, minute=0, second=0, microsecond=0), []).append(v)
    out = {}
    for m, vs in acc.items():
        n = ((m.replace(day=28) + timedelta(days=4)).replace(day=1) - m).days
        if len(vs) >= min_share * n:
            out[m] = sum(vs) / len(vs)
    return out


class GasBalanceMonthly(Connector):
    stored = staticmethod(read_stored)          # tests inject

    def declared_series(self) -> list[SeriesMeta]:
        m = lambda k, label: SeriesMeta(PFX + k, self.id, label, "MMcf/d", "M", GROUP)   # noqa: E731
        return [m("production_monthly", "AB + BC production, monthly (Petrinex + BCER, storage out)"),
                m("exports_monthly", "AB + BC exports, monthly: NGTL borders + Alliance + T-South + LNG"),
                m("demand_monthly", "AB demand on NGTL, monthly (GDSR intraprovincial)"),
                m("implied_storage_monthly", "AB + BC tie-out, monthly: storage implied by production − exports − "
                  "demand (+ = withdrawal)"),
                m("storage_actual_monthly", "AB + BC storage, monthly: NGTL net + Aitken Creek net (+ = withdrawal)"),
                m("residual_monthly", "AB + BC tie-out error, monthly (~+1.25 Bcf/d is AB use off NGTL)")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        start = datetime.fromisoformat(p.get("from_month", FROM_MONTH) + "-01")
        cer_ts = M_TSOUTH[1]
        ids = list(M_PROD) + [M_LNG, M_AIT, M_DEMAND, M_NS, *M_BORDERS, *M_ALLIANCE, M_TSOUTH[0], *cer_ts]
        st = self.stored(ids, start, ctx.now)
        mon = {s: (st.get(s) or {}) for s in list(M_PROD) + [M_LNG, M_AIT]}
        mon = {s: {t.replace(day=1): v for t, v in by.items()} for s, by in mon.items()}
        mm = {s: month_means(st.get(s) or {}) for s in [M_DEMAND, M_NS, *M_BORDERS, *M_ALLIANCE, M_TSOUTH[0], *cer_ts]}
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        fallback: list[str] = []
        lng_first = min(mon[M_LNG]) if mon[M_LNG] else None     # LNG Canada reports from 2024-08: before that, 0
        months = sorted(set(mon[M_PROD[0]]) & set(mon[M_PROD[1]]) & set(mon[M_AIT])
                        & set(mm[M_DEMAND]) & set(mm[M_NS]) & set.intersection(*(set(mm[b]) for b in M_BORDERS)))
        n = 0
        for m in months:
            alli = mm[M_ALLIANCE[0]].get(m, mm[M_ALLIANCE[1]].get(m))
            ts = mm[M_TSOUTH[0]].get(m)
            if ts is None and all(m in mm[c] for c in cer_ts):
                ts = sum(mm[c][m] for c in cer_ts)
                fallback.append(f"{m:%Y-%m}")
            lng = mon[M_LNG].get(m, 0.0 if lng_first is not None and m < lng_first else None)
            if alli is None or ts is None or lng is None:
                continue
            prod = mon[M_PROD[0]][m] + mon[M_PROD[1]][m]
            exports = sum(mm[b][m] for b in M_BORDERS) + alli + ts + lng
            demand = mm[M_DEMAND][m]
            implied = exports + demand - prod
            actual = mm[M_NS][m] + mon[M_AIT][m]
            vals = {"production_monthly": prod, "exports_monthly": exports, "demand_monthly": demand,
                    "implied_storage_monthly": implied, "storage_actual_monthly": actual,
                    "residual_monthly": actual - implied}
            res.obs += [Obs(PFX + k, m, v, as_of) for k, v in vals.items()]
            n += 1
        if not n:
            res.warnings.append("NO-RESULT balance (monthly): no month with every input stored")
        if fallback:
            res.warnings.append("T-South from the CER key points (no Westcoast OA month) for " + ", ".join(fallback))
        return res
