"""Peace River dams: reservoir heads (BC Hydro hydromet + WSC) and estimated MW with a band (PLAN P5d, L-041).

**Estimates, not measurements.** BC Hydro publishes no plant MW. We estimate each plant's output from flow x head:

    MW = 9.81 * eta * Q * H / 1000        (Q m3/s, H m, eta the water-to-wire efficiency)

over a trailing 24 h window (Power: hourly is worse than +/-30 % for peaking plants, power.md §2.1). Every estimate is
written with its band: `mw_est`, `mw_lo`, `mw_hi`, where lo/hi = est * (1 -/+ 2*eps) and

    eps = sqrt(eps_Q^2 + eps_H^2 + eps_eta^2)                     (relative, 1 sigma; docs/research/v2/weather-p5d.md §1.4)

* eps_Q: DECISIONS P5d open discrepancy #2 (the Peace balance). eps_Q(daily) = |b| + sigma_g, with b = +1.4 % and
  sigma_g = 2.6 % (the approved 2016-2025 regime), plus the terms each plant's flow adds below.
* eps_H, eps_eta: **Power's terms (L-033). The values here are PROVISIONAL**, pending Power's confirmation.

Plants, upstream to downstream (Q4 = yes, L-041: BC Hydro hydromet fetched locally for personal use, never republished;
only these derived estimates go on the map):

| plant | head H = forebay - tailwater | flow Q |
|---|---|---|
| GM Shrum (WAC Bennett) | Williston (WSC 07EF003 + HYDAT 630.060, CGVD2013; else BC Hydro lst) - Dinosaur (BC Hydro dir) | = Peace Canyon's (Dinosaur pondage: error term only) |
| Peace Canyon | Dinosaur (dir) - (Site C reservoir (BC Hydro cta) + 0.5 m backwater) | 07FA004 - Halfway 07FA006 - Moberly 07FB008 + A_SiteC * dH_cta/dt |
| Site C | Site C reservoir (cta) - (07FA004 elevation + 1.5 m river slope over 6 km) | 07FA004 (6 km below the dam) |

If a head input is missing, the plant falls back to a fixed typical head (Q4 option (c)) with a much wider eps_H, and
the fetch warns. If a flow input is missing or thin, the plant gets no estimate (never a guess).

BC Hydro hydromet times are local Pacific time: the column label was "(PST)" until BC moved to permanent UTC-7 on
2026-11-01, and is "(PCT)" since. They're read on America/Vancouver (bundled tzdata 2026.6: PDT before the switch,
UTC-7 permanently after; AGENTS-compliant, no fixed offset). Evidence, 2026-11-01: lst (Williston, the same sensor as
WSC 07EF002) matched WSC at UTC-7 with 1.8 mm rms over Oct 25 - Nov 1 (a 5-min shift gave 4.3 mm). Two checks per fetch,
each failing closed:
* a future stamp refuses that file;
* a clock check of lst against WSC 07EF002 at lags -60/0/+60 min. If a shifted lag fits clearly better, WSC has caught a
  BC Hydro clock change (e.g. back to UTC-8, which the future guard can't see), and ALL hydromet files are refused, so
  the plants fall back to typical heads.
The raw BC Hydro levels are stored locally only. They are never referenced by network/ or the snapshot (Q4: the map
shows only our derived estimates); a test enforces it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.store import Obs, SeriesMeta

HYDROMET = "https://www.bchydro.com/info/res_hydromet/data/{code}.txt"
BCH_CLOCK = ZoneInfo("America/Vancouver")  # "(PCT)": Pacific time, permanent UTC-7 since 2026-11-01 (tzdata 2026.6)
LABELS = {"PCT", "PST", "PDT"}
CLOCK_GAIN = 0.7                            # a shifted lag must beat lag 0 by this rms ratio to count as a shift
CLOCK_FLOOR_M = 0.001                       # ...AND by >= 1 mm absolute (the gauges' resolution); a lake whose own
                                            # signal is under 1 mm is "inconclusive" (Reviewer, #146: float noise)
CLOCK_SPAN = timedelta(days=8)              # live 2026-11-01: 7 d gives 1.8 mm at lag 0 vs 5.2/5.7 at +/-60 min; 26 h can't tell
WINDOW = timedelta(hours=24)
MIN_POINTS = 144                            # half of a 5-min day: below this an input is thin -> NO-RESULT
FUTURE_TOL = timedelta(minutes=10)
STALE = timedelta(hours=3)

# Physical constants and provisional error terms (Power's to confirm, L-033)
ETA, EPS_ETA = 0.90, 0.05 / 0.90            # eta 0.90 +/- 0.05
B_Q, SIGMA_Q = 0.014, 0.026                 # DECISIONS P5d discrepancy #2: b = +1.4 %, sigma_g floor 2.6 %
EPS_TRIB = 0.05                             # tributary gauges, relative 1 sigma
EPS_LOCAL = 0.02                            # ungauged local inflow Peace Canyon -> Site C, relative to Q
A_SITEC_M2 = 93.3e6                         # Site C reservoir, 9,330 ha (BC Hydro via Wikipedia "John Horgan Dam")
EPS_AREA = 0.30                             # storage term: area at the operating level + a single-point level gauge
A_DINO_MAX_M2 = 10e6                        # Dinosaur Lake area: UNVERIFIED upper bound, error term only
DATUM_M = 0.12                              # BC Hydro vs CGVD2013 datum mix (07FA004: GSC vs CGVD2013 is 0.117 m)
PCN_BACKWATER, PCN_BACKWATER_SD = 0.5, 0.5  # Peace Canyon tailwater above the cta gauge
SITEC_SLOPE, SITEC_SLOPE_SD = 1.5, 1.5      # Site C tailwater above 07FA004 (6 km of river)
# Stored by weather_wsc; the .elevation series already carry the HYDAT 2026-10-19 CGVD2013 factors (07EF003 +630.060,
# 07FA004 +405.891, 07EF002 +630.018; DECISIONS P5d).
STORED = {"07FA004.q": "wsc.realtime.07fa004.discharge", "07FA004.elev": "wsc.realtime.07fa004.elevation",
          "07FA006.q": "wsc.realtime.07fa006.discharge", "07FB008.q": "wsc.realtime.07fb008.discharge",
          "07EF003.elev": "wsc.realtime.07ef003.elevation", "07EF002.elev": "wsc.realtime.07ef002.elevation"}
# Fixed typical heads for the fallback (power.md §2.3) with their relative 1 sigma (Williston's ~18 m range, etc.)
TYPICAL_HEAD = {"wac_bennett": (167.5, 0.06), "peace_canyon": (39.7, 0.03), "site_c": (51.0, 0.04)}
NAMEPLATE = {"wac_bennett": 2907.0, "peace_canyon": 694.0, "site_c": 1100.0}
PLANTS = {"wac_bennett": "GM Shrum (WAC Bennett Dam)", "peace_canyon": "Peace Canyon", "site_c": "Site C (John Horgan)"}
LEVELS = {"lst": "Williston Lake at Lost Cabin Creek", "dir": "Dinosaur Reservoir forebay",
          "cta": "Site C reservoir (Naache Mege) at Tea Creek"}


# ---------------------------------------------------------------- BC Hydro hydromet .txt
_ROW = re.compile(r"^(\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2})")


def parse_hydromet(text: str, now: datetime) -> tuple[dict[datetime, float], list[str]]:
    """{naive UTC: level m} from a hydromet file (the FIRST 'Water level' column), or ({}, [reason]).

    Columns are fixed-width with blanks for missing values, so each number is assigned to the header column whose
    span it falls under, never by its position in a whitespace split."""
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if "Date" in l and "Time" in l and "Water level" in l), None)
    if head is None:
        return {}, ["no 'Water level' column in the header"]
    hdr = lines[head]
    names = [(m.start(), m.end(), m.group(0).strip()) for m in re.finditer(r"\S+(?: \S+)*", hdr)]
    lvl = next((n for n in names if n[2] == "Water level"), None)
    units = lines[head + 1] if head + 1 < len(lines) else ""
    label = re.search(r"\(yyyy/mm/dd\) \(([A-Z]+)\)", units)
    if not label or label.group(1) not in LABELS:
        return {}, [f"time label {label.group(1) if label else None!r} isn't Pacific time: NO-RESULT"]
    if "(m)" not in units[lvl[0] - 4:lvl[1] + 6]:
        return {}, [f"'Water level' unit isn't (m): {units.strip()!r}"]
    out: dict[datetime, float] = {}
    for l in lines[head + 2:]:
        m = _ROW.match(l)
        if not m:
            continue
        stamp = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y/%m/%d %H:%M:%S")
        ts = stamp.replace(tzinfo=BCH_CLOCK).astimezone(timezone.utc).replace(tzinfo=None)
        for v in re.finditer(r"-?\d+\.\d+", l[m.end():]):
            a, b = v.start() + m.end(), v.end() + m.end()
            if a < lvl[1] + 8 and b > lvl[0] - 8:        # under the first Water level header
                out[ts] = float(v.group(0))
                break
    if not out:
        return {}, ["no level rows"]
    if max(out) > now + FUTURE_TOL:
        return {}, [f"newest stamp {max(out):%Y-%m-%d %H:%M}Z is in the future: the UTC-7 rule is broken, NO-RESULT"]
    warn = [f"newest level {max(out):%H:%M}Z is over 3 h old"] if now - max(out) > STALE else []
    return out, warn


def clock_check(lst: dict[datetime, float], wsc: dict[datetime, float]) -> tuple[str, str]:
    """("ok" | "shifted" | "inconclusive", detail): lst vs WSC 07EF002 (same sensor) at lags -60/0/+60 min.

    Levels are compared after removing each overlap's mean, so the two datums don't matter. A shifted lag counts only
    if it beats lag 0 clearly (rms ratio < CLOCK_GAIN). A flat calm lake is "inconclusive": the data are kept, with a
    warning."""
    rms = {}
    for lag in (-60, 0, 60):
        d = [v - wsc[t + timedelta(minutes=lag)] for t, v in lst.items() if t + timedelta(minutes=lag) in wsc]
        if len(d) < 100:
            continue
        m = sum(d) / len(d)
        rms[lag] = math.sqrt(sum((x - m) ** 2 for x in d) / len(d))
    if 0 not in rms or len(rms) < 2:
        return "inconclusive", f"too little overlap with WSC 07EF002 ({len(rms)} lags)"
    best = min(rms, key=rms.get)
    detail = ", ".join(f"{k:+d} min {v * 1000:.1f} mm" for k, v in sorted(rms.items()))
    vals = list(lst.values())
    mu = sum(vals) / len(vals)
    signal = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))
    if signal < CLOCK_FLOOR_M:      # a flat or mm-quantised calm lake: nothing to align, so rounding can't decide
        return "inconclusive", f"lake signal {signal * 1000:.2f} mm < {CLOCK_FLOOR_M * 1000:.0f} mm ({detail})"
    if best != 0 and rms[best] < CLOCK_GAIN * rms[0] and rms[0] - rms[best] >= CLOCK_FLOOR_M:
        return "shifted", f"BC Hydro stamps fit WSC best at {best:+d} min ({detail}): clock changed"
    if best != 0:
        return "inconclusive", f"no clear lag ({detail})"
    return "ok", detail


# ---------------------------------------------------------------- the estimate
def window_mean(pts: dict[datetime, float], end: datetime) -> float | None:
    v = [x for t, x in pts.items() if end - WINDOW < t <= end and x is not None]
    return sum(v) / len(v) if len(v) >= MIN_POINTS else None


def end_change(pts: dict[datetime, float], end: datetime) -> float | None:
    """Level change over the window: mean of its last hour minus mean of its first hour (m), or None."""
    first = [x for t, x in pts.items() if end - WINDOW < t <= end - WINDOW + timedelta(hours=1)]
    last = [x for t, x in pts.items() if end - timedelta(hours=1) < t <= end]
    return sum(last) / len(last) - sum(first) / len(first) if first and last else None


def mw(q: float, h: float) -> float:
    return 9.81 * ETA * q * h / 1000.0


def estimate(inputs: dict[str, dict[datetime, float]], end: datetime) -> tuple[dict[str, dict], list[str]]:
    """Per plant: {q, h, mw_est, mw_lo, mw_hi, eps, eps_q, eps_h, head_source} or absent (NO-RESULT), plus warnings."""
    w = {k: window_mean(v, end) for k, v in inputs.items()}
    warns, out = [], {}
    q_fa = w.get("07FA004.q")
    if q_fa is None:
        return {}, ["07FA004 discharge thin or missing: no Peace estimate (NO-RESULT)"]
    sec = WINDOW.total_seconds()

    def head(plant, fore, tail, sd_extra):
        if fore is not None and tail is not None:
            h = fore - tail
            return h, math.hypot(DATUM_M, sd_extra) / h, "measured"
        h, eps = TYPICAL_HEAD[plant]
        warns.append(f"{plant}: head input missing, using the fixed typical head {h} m (Q4 fallback (c))")
        return h, eps, "typical"

    # Site C: Q = 07FA004; tailwater = 07FA004 elevation + river slope
    eps_q_sc = B_Q + SIGMA_Q
    tail_sc = w.get("07FA004.elev")
    h_sc, eh_sc, src_sc = head("site_c", w.get("cta"), None if tail_sc is None else tail_sc + SITEC_SLOPE, SITEC_SLOPE_SD)
    out["site_c"] = (q_fa, eps_q_sc, h_sc, eh_sc, src_sc)

    # Peace Canyon: Q = 07FA004 - tributaries + Site C storage change
    q_trib = [w.get("07FA006.q"), w.get("07FB008.q")]
    if any(x is None for x in q_trib):
        warns.append("Halfway/Moberly discharge thin or missing: no Peace Canyon / GM Shrum estimate (NO-RESULT)")
    else:
        dh = end_change(inputs.get("cta", {}), end)
        storage = 0.0 if dh is None else A_SITEC_M2 * dh / sec
        if dh is None:
            warns.append("Site C level change unknown: storage term 0, band widened")
        q_pcn = q_fa - sum(q_trib) + storage
        sd = math.sqrt((eps_q_sc * q_fa) ** 2 + (EPS_TRIB * sum(q_trib)) ** 2 + (EPS_LOCAL * q_pcn) ** 2
                       + (EPS_AREA * storage) ** 2 + (0.0 if dh is not None else (A_SITEC_M2 * 0.2 / sec)) ** 2)
        cta = w.get("cta")
        h_pc, eh_pc, src_pc = head("peace_canyon", w.get("dir"), None if cta is None else cta + PCN_BACKWATER,
                                   PCN_BACKWATER_SD)
        out["peace_canyon"] = (q_pcn, sd / q_pcn, h_pc, eh_pc, src_pc)
        # GM Shrum: same flow at daily scale; Dinosaur pondage only widens the band (its area is unverified)
        dd = end_change(inputs.get("dir", {}), end)
        pond = abs(A_DINO_MAX_M2 * (dd if dd is not None else 0.2) / sec)
        sd_g = math.hypot(sd, pond)
        fore = w.get("07EF003.elev") if w.get("07EF003.elev") is not None else w.get("lst")
        h_g, eh_g, src_g = head("wac_bennett", fore, w.get("dir"), 0.05)
        out["wac_bennett"] = (q_pcn, sd_g / q_pcn, h_g, eh_g, src_g)

    res = {}
    for plant, (q, eq, h, eh, src) in out.items():
        est = mw(q, h)
        eps = math.sqrt(eq ** 2 + eh ** 2 + EPS_ETA ** 2)
        res[plant] = {"q": q, "h": h, "mw_est": est, "mw_lo": max(0.0, est * (1 - 2 * eps)), "mw_hi": est * (1 + 2 * eps),
                      "eps": eps, "eps_q": eq, "eps_h": eh, "head_source": src}
        if est > NAMEPLATE[plant] * 1.05:
            warns.append(f"{plant}: estimate {est:.0f} MW is above nameplate {NAMEPLATE[plant]:.0f} (spill or flow bias?)")
    return res, warns


# ---------------------------------------------------------------- connector
class PeaceDamsMW(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        g = "Rivers / Peace (BC Hydro)"
        out = [SeriesMeta(f"bchydro.hydromet.{c}.level", self.id, f"{n} level (BC Hydro hydromet {c})", "m", "5min", g)
               for c, n in LEVELS.items()]
        for k, n in PLANTS.items():
            gg = f"Power / Peace hydro (estimated)"
            out += [
                SeriesMeta(f"power.hydro.{k}.mw_est", self.id,
                           f"{n} MW, estimated from flow x head: 24 h mean ending at ts", "MW", "1h", gg),
                SeriesMeta(f"power.hydro.{k}.mw_lo", self.id,
                           f"{n} MW estimate, low bound (-2 sigma): 24 h mean ending at ts", "MW", "1h", gg),
                SeriesMeta(f"power.hydro.{k}.mw_hi", self.id,
                           f"{n} MW estimate, high bound (+2 sigma): 24 h mean ending at ts", "MW", "1h", gg),
                SeriesMeta(f"power.hydro.{k}.head", self.id, f"{n} head: 24 h mean ending at ts", "m", "1h", gg),
                SeriesMeta(f"power.hydro.{k}.flow", self.id, f"{n} flow: 24 h mean ending at ts", "m3/s", "1h", gg),
            ]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        inputs: dict[str, dict[datetime, float]] = {}
        for code in LEVELS:
            try:
                pts, w = parse_hydromet(ctx.get(HYDROMET.format(code=code)).text, ctx.now)
            except Exception as e:  # one file failing must not stop the others
                pts, w = {}, [f"{type(e).__name__}: {e}"]
            res.warnings += [f"bchydro {code}: {x}" for x in w]
            inputs[code] = pts
        # L-081: the five WSC inputs come from the store (weather_wsc already writes them every hour, with the HYDAT
        # CGVD2013 factors applied), not from five more API requests. Read-only via read_stored (#162). 07EF002 feeds
        # the clock check, which needs lst's whole ~7-day span (CLOCK_SPAN); its datum doesn't matter there (mean removed).
        try:
            stored = read_stored(list(STORED.values()), ctx.now - CLOCK_SPAN, ctx.now)
        except Exception as e:  # no store: the flows are NO-RESULT and the heads fall back, never a guess
            res.warnings.append(f"stored WSC inputs unreadable ({type(e).__name__}: {e}): flow NO-RESULT")
            stored = {}
        for k, sid in STORED.items():
            inputs[k] = stored.get(sid, {})
        state, detail = clock_check(inputs.get("lst", {}), inputs.get("07EF002.elev", {}))
        if state == "shifted":
            res.warnings.append(f"bchydro: {detail}; all hydromet levels refused (NO-RESULT), typical heads used")
            for code in LEVELS:
                inputs[code] = {}
        elif state == "inconclusive":
            res.warnings.append(f"bchydro clock check inconclusive: {detail}")
        for code in LEVELS:
            res.obs += [Obs(f"bchydro.hydromet.{code}.level", t, round(v, 3), t) for t, v in inputs[code].items()]
        end = ctx.now.replace(minute=0, second=0, microsecond=0)
        est, warns = estimate(inputs, end)
        res.warnings += warns
        for plant, e in est.items():
            for k in ("mw_est", "mw_lo", "mw_hi"):
                res.obs.append(Obs(f"power.hydro.{plant}.{k}", end, round(e[k], 1), end))
            res.obs.append(Obs(f"power.hydro.{plant}.head", end, round(e["h"], 3), end))
            res.obs.append(Obs(f"power.hydro.{plant}.flow", end, round(e["q"], 1), end))
        if not est and not any(inputs.get(c) for c in LEVELS):
            raise RuntimeError(f"no Peace inputs at all: {'; '.join(res.warnings)[:300]}")
        return res
