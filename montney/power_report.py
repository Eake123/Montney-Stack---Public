"""Report-only checks owned by Power (L-051). No alarms: these print what the data says, with a NO-RESULT state.

Peace dams annual-energy bound (#146 follow-up, L-051)
------------------------------------------------------
BC Hydro publishes no plant MW, so Weather's flow x head estimate (`power.hydro.<plant>.mw_est/lo/hi`, hourly, each a
trailing 24 h mean) has no hourly oracle. Its checks are the Libby method check (power.md §2.1) and this one: annualize the
stored estimate and set it against BC Hydro's published average annual generation.

    annualized TWh/yr = mean(stored hourly MW over the window) x 8760 / 1e6       (lo and hi the same way)

* **n and coverage:** n = stored hourly points in the window; coverage = n / window hours. Below AGG_MIN_COVERAGE (75 %,
  the snapshot's agg floor) the bound is **NO-RESULT**: a partial window is reported as "to date" only, never as a pass.
* **Bound (report-only):** within ±BOUND (15 %) of the published figure. Hydrology moves a year by more than the model
  error, so this is a sanity bound, not a calibration. Per Wellsite §4.7, a bar is set only from the observed spread
  once a full year exists.
* **Chain total (GMS + Peace Canyon + Site C):** est = sum of the plant estimates. **The band adds every term linearly**:
  sum of lo .. sum of hi. ε_η (0.90 ± 0.05) is shared by all three plants (L-051). The flow error is shared too: all
  three flows hang off WSC 07FA004. A quadrature sum would understate the band.

Published annual generation (BC Hydro, via the plants' Wikipedia infoboxes, retrieved 2026-11-01):
GM Shrum 15 TWh, Peace Canyon 3.5 TWh, Site C 5.1 TWh.
"""

from __future__ import annotations

from datetime import datetime, timedelta

PLANTS = {"wac_bennett": "GM Shrum (WAC Bennett)", "peace_canyon": "Peace Canyon", "site_c": "Site C (John Horgan)"}
PUBLISHED_TWH = {"wac_bennett": 15.0, "peace_canyon": 3.5, "site_c": 5.1}
PUBLISHED_SOURCE = ("BC Hydro average annual generation via Wikipedia infoboxes (W._A._C._Bennett_Dam 15 TWh, "
                    "Peace_Canyon_Dam 3,500 GWh, John_Horgan_Dam 5,100 GWh), retrieved 2026-11-01")
AGG_MIN_COVERAGE = 0.75
BOUND = 0.15
HOURS_PER_YEAR = 8760
_CHAIN_ADD = "linear"          # seeded-red switch: "quadrature" understates the chain band and must be caught (tests)


def _twh(mean_mw: float | None) -> float | None:
    return None if mean_mw is None else mean_mw * HOURS_PER_YEAR / 1e6


def _window(store, ids: list[str], start: datetime, end: datetime) -> dict[str, dict[float, float]]:
    data = store.series(ids, start=start, end=end)
    return {sid: {t: v for t, v in zip(d["ts"], d["v"]) if v is not None} for sid, d in data.items()}


def peace_energy(store, now: datetime, days: int = 365) -> dict:
    """Per plant and for the chain: n, coverage, annualized TWh/yr (est, lo, hi), the published TWh, and a status:
    NO-RESULT (coverage below the floor), within, or outside (±BOUND). `now` is naive UTC."""
    end, start = now, now - timedelta(days=days)
    hours = days * 24
    ids = [f"power.hydro.{p}.mw_{k}" for p in PLANTS for k in ("est", "lo", "hi")]
    pts = _window(store, ids, start, end)
    out = {"window": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "days": days, "hours": hours},
           "floor": AGG_MIN_COVERAGE, "bound": BOUND, "published_source": PUBLISHED_SOURCE, "plants": {}}
    for p, name in PLANTS.items():
        est, lo, hi = (pts.get(f"power.hydro.{p}.mw_{k}", {}) for k in ("est", "lo", "hi"))
        ts = sorted(set(est) & set(lo) & set(hi))          # only hours with the estimate AND its band
        n = len(ts)
        mean = {k: (sum(s[t] for t in ts) / n if n else None) for k, s in (("est", est), ("lo", lo), ("hi", hi))}
        out["plants"][p] = _verdict(name, n, n / hours, {k: _twh(v) for k, v in mean.items()}, PUBLISHED_TWH[p])
    # Chain: hours where all three plants have est, lo and hi
    common = None
    for p in PLANTS:
        s = set(pts.get(f"power.hydro.{p}.mw_est", {})) & set(pts.get(f"power.hydro.{p}.mw_lo", {})) \
            & set(pts.get(f"power.hydro.{p}.mw_hi", {}))
        common = s if common is None else common & s
    common = sorted(common or [])
    n = len(common)

    def chain(t):
        e = sum(pts[f"power.hydro.{p}.mw_est"][t] for p in PLANTS)
        dlo = [pts[f"power.hydro.{p}.mw_est"][t] - pts[f"power.hydro.{p}.mw_lo"][t] for p in PLANTS]
        dhi = [pts[f"power.hydro.{p}.mw_hi"][t] - pts[f"power.hydro.{p}.mw_est"][t] for p in PLANTS]
        if _CHAIN_ADD == "linear":
            return e, e - sum(dlo), e + sum(dhi)
        return e, e - sum(x * x for x in dlo) ** 0.5, e + sum(x * x for x in dhi) ** 0.5

    vals = [chain(t) for t in common]
    mean = {k: (sum(v[i] for v in vals) / n if n else None) for i, k in enumerate(("est", "lo", "hi"))}
    out["chain"] = _verdict("Peace chain (GMS + Peace Canyon + Site C)", n, n / hours,
                            {k: _twh(v) for k, v in mean.items()}, sum(PUBLISHED_TWH.values()))
    out["chain"]["band_rule"] = ("sum of lo .. sum of hi: every term linear (eps_eta shared by all three plants; the flow "
                                 "error is shared via 07FA004)")
    return out


def _verdict(name: str, n: int, coverage: float, twh: dict, published: float) -> dict:
    r = {"name": name, "n": n, "coverage": round(coverage, 4), "twh_per_year": twh, "published_twh": published,
         "ratio": None, "status": "NO-RESULT", "reason": None}
    if n == 0 or twh["est"] is None:
        r["reason"] = "no stored estimate in the window"
        return r
    r["ratio"] = twh["est"] / published
    if coverage < AGG_MIN_COVERAGE:
        r["reason"] = (f"coverage {coverage:.1%} < {AGG_MIN_COVERAGE:.0%} of the window: annualized 'to date' only "
                       f"(seasonal bias), not a bound")
        return r
    r["status"] = "within" if abs(r["ratio"] - 1) <= BOUND else "outside"
    r["published_in_band"] = twh["lo"] <= published <= twh["hi"]
    return r


def format_peace_energy(rep: dict) -> str:
    """Plain ASCII: the Windows console (cp1252) can't print Greek letters or a plus-minus sign."""
    w = rep["window"]
    lines = [f"Peace dams annual-energy bound (report-only, L-051): window {w['start']} .. {w['end']} ({w['days']} d), "
             f"floor {rep['floor']:.0%} coverage, bound +/-{rep['bound']:.0%}",
             f"{'plant':42s} {'n':>6s} {'cover':>7s}  {'TWh/yr est [lo, hi]':>26s}  {'pub':>5s} {'ratio':>6s}  status"]
    for r in list(rep["plants"].values()) + [rep["chain"]]:
        t = r["twh_per_year"]
        band = "--" if t["est"] is None else f"{t['est']:.2f} [{t['lo']:.2f}, {t['hi']:.2f}]"
        ratio = "--" if r["ratio"] is None else f"{r['ratio']:.0%}"
        lines.append(f"{r['name']:42s} {r['n']:6d} {r['coverage']:7.1%}  {band:>26s}  {r['published_twh']:5.1f} {ratio:>6s}  "
                     f"{r['status']}" + (f" ({r['reason']})" if r["reason"] else ""))
    lines.append(f"chain band: {rep['chain']['band_rule']}")
    lines.append(f"published: {rep['published_source']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- monthly burn check (DECISIONS P5b, L-031, L-033)
# Monthly burn check for the 6 GHGRP-validated plants: est vs actual, in MMcf/d.
#
# * actual: NGTL delivered, power.plant.<slug>.ngtl_delivery (sources/power_ngtl_deliveries.yaml), monthly. It is
#   published about 20 days after month end.
# * est: the time-weighted mean of the stored 5-min power.plant.<slug>.gas_burn.est over the Alberta calendar month
#   (America/Edmonton boundaries). Storage is change-only, so each stored point holds its value until the next one,
#   for at most MAX_HOLD_H hours. Longer gaps count as missing, so runner downtime is a coverage shortfall, never a
#   fake mean.
# * gap = est / actual - 1, compared in GJ (est x 1.104 vs actual x the delivery HV actually used), so a later
#   heat-value change can't masquerade as a burn gap.
# * status:
#   - NO-RESULT: no delivery yet for the month, or coverage below AGG_MIN_COVERAGE;
#   - "not steady": the plant ran for less than STEADY_RUNNING of the covered time (starts and outages run +5-15 %,
#     DECISIONS), so the gap is reported but never flagged;
#   - "FLAG": a steady month with |gap| > FLAG_GAP;
#   - "ok": otherwise.
# Milner, Battle River, Genesee and the rest are context only (DECISIONS P5b), so they are not checked here.
# Month boundaries: Alberta calendar months, while TC allocates by NGTL gas day (which starts at 15:00 UTC, about
# 08:00-09:00 Alberta time). The offset is under ~1 % of a month's energy, well inside the 10 % flag (Reviewer, #177).
from datetime import timezone as _tz  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

VALIDATED = ("shepard", "calgary_energy_centre", "sheerness", "clover_bar", "carseland", "keephills")
# Cogen plants are compared like any other: since the cogen ruling (after #177) their burn estimate is total SITE
# fuel (Carseland's measured 11.3 GJ/MWh), which is what the delivery station meters.
EST_HHV = 1.104          # GJ/Mcf in the burn weights (docs/research/v2/power-heat-rates.md)
MAX_HOLD_H = 6.0
STEADY_RUNNING = 0.90
RUNNING_MMCFD = 0.5
FLAG_GAP = 0.10
AB = ZoneInfo("America/Edmonton")


def _month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    a = datetime(year, month, 1, tzinfo=AB)
    b = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=AB)
    return (a.astimezone(_tz.utc).replace(tzinfo=None), b.astimezone(_tz.utc).replace(tzinfo=None))


def _epoch(d: datetime) -> float:
    return (d - datetime(1970, 1, 1)).total_seconds()


def held_mean(ts: list[float], v: list[float], start: float, end: float, max_hold_s: float) -> dict:
    """Time-weighted mean of a step series on [start, end): each point holds until the next one or max_hold_s."""
    covered = acc = running = 0.0
    pts = sorted((t, x) for t, x in zip(ts, v) if x is not None)
    for k, (t, x) in enumerate(pts):
        nxt = pts[k + 1][0] if k + 1 < len(pts) else float("inf")
        a, b = max(t, start), min(nxt, t + max_hold_s, end)
        if b <= a:
            continue
        covered += b - a
        acc += x * (b - a)
        running += (b - a) if x > RUNNING_MMCFD else 0.0
    return {"mean": acc / covered if covered else None, "coverage": covered / (end - start),
            "running": running / covered if covered else None}


def burn_check(store, now: datetime, months: int = 12) -> dict:
    """Per validated plant and completed Alberta month: {est, actual, gap, coverage, running, status, reason}."""
    local_now = now.replace(tzinfo=_tz.utc).astimezone(AB)
    y, m = local_now.year, local_now.month
    span = []
    for _ in range(months):                       # the last `months` COMPLETED months, newest first
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        span.append((y, m))
    start_all = _month_bounds_utc(*span[-1])[0] - timedelta(hours=MAX_HOLD_H)
    end_all = _month_bounds_utc(*span[0])[1]
    hv_id = "ngtl.heat_value.delivery"
    ids = [f"power.plant.{p}.{k}" for p in VALIDATED for k in ("gas_burn.est", "ngtl_delivery")] + [hv_id]
    data = store.series(ids, start=start_all, end=end_all)
    hv = dict(zip(data[hv_id]["ts"], data[hv_id]["v"]))
    out = {"months": [f"{a}-{b:02d}" for a, b in span], "flag_gap": FLAG_GAP, "floor": AGG_MIN_COVERAGE,
           "steady_running": STEADY_RUNNING, "max_hold_h": MAX_HOLD_H, "plants": {}}
    for p in VALIDATED:
        est_s, act_s = data[f"power.plant.{p}.gas_burn.est"], data[f"power.plant.{p}.ngtl_delivery"]
        act = dict(zip(act_s["ts"], act_s["v"]))
        rows = {}
        for a, b in span:
            s, e = _month_bounds_utc(a, b)
            key, mts = f"{a}-{b:02d}", _epoch(datetime(a, b, 1))
            h = held_mean(est_s["ts"], est_s["v"], _epoch(s), _epoch(e), MAX_HOLD_H * 3600)
            r = {"est": h["mean"], "actual": act.get(mts), "coverage": round(h["coverage"], 4),
                 "running": None if h["running"] is None else round(h["running"], 4), "gap": None,
                 "status": "NO-RESULT", "reason": None}
            if r["actual"] is None:
                r["reason"] = "no NGTL delivery for the month yet (TC publishes ~20 days after month end)"
            elif h["coverage"] < AGG_MIN_COVERAGE or h["mean"] is None:
                r["reason"] = f"burn-estimate coverage {h['coverage']:.1%} < {AGG_MIN_COVERAGE:.0%} of the month"
            else:
                gj_act = r["actual"] * hv.get(mts, EST_HHV)
                r["gap"] = (h["mean"] * EST_HHV / gj_act - 1) if gj_act > 0 else None
                if h["running"] < STEADY_RUNNING:
                    r["status"] = "not steady"
                    r["reason"] = f"ran {h['running']:.0%} of the covered time (starts/outages)"
                elif r["gap"] is None:
                    r["status"], r["reason"] = "FLAG", "estimate > 0 but nothing delivered"
                else:
                    r["status"] = "FLAG" if abs(r["gap"]) > FLAG_GAP else "ok"
            rows[key] = r
        out["plants"][p] = rows
    return out


def _f(x, spec="{:7.1f}") -> str:
    return "--".rjust(7) if x is None else spec.format(x)


def format_burn_check(rep: dict) -> str:
    """Plain ASCII (the Windows console is cp1252)."""
    lines = [f"Monthly burn check, est vs NGTL delivered (MMcf/d; report-only, DECISIONS P5b): flag steady months with "
             f"|gap| > {rep['flag_gap']:.0%}; NO-RESULT below {rep['floor']:.0%} coverage; steady = running >= "
             f"{rep['steady_running']:.0%}; a stored point holds at most {rep['max_hold_h']:g} h",
             f"{'plant':24s} {'month':8s} {'est':>7s} {'actual':>7s} {'gap':>7s} {'cover':>6s} {'run':>5s}  status"]
    for p, rows in rep["plants"].items():
        for mth, r in rows.items():
            gap = "--" if r["gap"] is None else format(r["gap"], "+.1%")
            run = "--" if r["running"] is None else format(r["running"], ".0%")
            lines.append(f"{p:24s} {mth:8s} {_f(r['est'])} {_f(r['actual'])} {gap:>7s} {r['coverage']:6.0%} {run:>5s}  "
                         f"{r['status']}" + (f" ({r['reason']})" if r["reason"] else ""))
    return "\n".join(lines)
