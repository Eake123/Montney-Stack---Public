"""Daily gas demand for the US regions and four Canadian provinces, estimated (Gas, 2026-11-13; method and back-tests:
docs/research/pipelines/daily_demand_methods.md). **Zero requests:** it reads stored series only (gas_common.read_stored).

Per region <r> (the US regions of docs/REGIONS.md), MMcf/d, one value per **calendar day in the region's time zone**:

  gas.<r>.power_burn_est   Σ groups (base + HR × EIA-930 gas generation MWh / 1037). The MWh of a local day is the sum of
                           the stored hourly `eia930.<ba>.gen.gas` MW (power_eia930_gen) over the day's 23/24/25 UTC hours.
  gas.<r>.rescom_est       residential + commercial = a + b × HDD, where HDD is the population-weighted mean of
                           max(0, base − tmean) over the Weather agent's cities. The cities and weights are read from the
                           region's composite in sources/weather_<r>.yaml (`region.<r>`), so they stay the Weather agent's.
                           tmean is `wx.<city>.tmean.realized` for an ended day, otherwise the newest `wx.<city>.tmean`
                           (today: the forecast).
  gas.<r>.industrial_est   EIA monthly industrial consumption, the same month last year, per member state, held across
                           the month. It comes from the store (`eia.<st>.deliveries.industrial`, gas_eia_states) or, until
                           the store holds that month, from the spec's fitted table (`params.industrial`). A table cell
                           older than last year (EIA withholds NV and NM) is the newest published year, flagged in the
                           series' `extra.older_year` and in a run warning.
  gas.<r>.demand_est       the sum of the three, only on days where all three exist. Same definition as EIA's "delivered to
                           consumers" (minus vehicle fuel, ≤ 2 % in California and ~0 elsewhere). It excludes lease, plant
                           and pipeline fuel and LNG feed gas. `extra.band` is the monthly p90 miss of the back-test.

**Canadian provinces** (orchestrator, L-107): `gas.sk|mb|qc|on.*`, the same recipe fitted to StatCan 25-10-0086
deliveries to consumers. Industrial comes from `statcan.<pv>.deliveries.industrial` (gas_statcan_onqc, the same one
request) or the fitted `CA-<PV>` table.
- StatCan counts gas burned for power generation as industrial. So SK, MB and QC have no power series: it is held
  inside industrial at last year's month. SaskPower's gas output (`saskpower.gen.gas`) has no stored history to fit or
  split yet; Manitoba's and Hydro-Québec's thermal output is ~0.
- Ontario counts power daily: `ieso.gen.gas.daily` (MWh/d, IESO EST dates) × a heat rate fitted Apr-Oct on StatCan
  industrial vs IESO. With `industrial_less_power`, it takes last year's same-month power (the store's IESO daily, or
  the group's `power_ly` table) back out of last year's industrial, so it isn't counted twice.

**Current days: ISO real time for EIA-930's late hours** (2026-11-13). EIA-930 gas generation lands 11-38 h late, and a
day needs every BA hour, so the power term (and demand_est) ran ~2-3 days old. The ISOs' own fuel mixes, which Power
stores hourly within minutes of the hour (`ercot.gen.gas`, `nyiso.gen.gas` + `nyiso.gen.dual_fuel`, `isone.gen.gas`,
`sppw.gen.gas`), now fill the hours EIA-930 hasn't posted. A group names them per BA (`rt: {erco: [ercot.gen.gas]}`).
- **Per BA and hour:** EIA-930 where stored, else the RT feed (the sum of its series, all present) × the BA's
  RT -> EIA-930 ratio. EIA-930 stays the reference: each value is replaced by the actual once EIA posts it (as_of = the
  day).
- **Ratio** (`fit_rt`): Σ EIA / Σ RT over the overlap hours of the last 14 days, refitted every run, so it follows the
  feed and needs no spec update. It is **refused** with fewer than 72 overlap hours on 3 UTC days of ≥ 20 h, when any
  day's own ratio is more than 5 % off the overall one, or when the ratio leaves 0.8-1.25 (the two must measure the same
  plants). A refused feed isn't used, and the reason is a run warning.
  - Stored overlap on 2026-11-13: 23 hours. Hourly agreement: ERCO ratio 1.004, p90 |diff| 0.8 %; NYIS (gas + dual fuel)
    1.006, 1.5 % (gas alone 2.10: EIA's NG includes NYISO's dual-fuel units); ISNE (gas) 0.994, 3.2 % (adding landfill
    gas made it worse: 0.991); SWPW no overlap yet. So the feeds are accepted after ~3 days of overlap.
  - MISO's feed (`miso.gen.gas`) is one live 5-min snapshot every 2 h, and CAISO's hourly feed is live-tier (only the last
    fetch is kept), so neither can be fitted or summed into days. umw and cal stay on EIA-930.
- **Small BAs without a feed** (`power_plan`): a BA short of a day uses its mean daily MWh over its last 7 local days
  that were complete in EIA-930 for its whole group (≥ 5 such days in the last 14). This applies only if it was under
  `hold_max_share` (default 10 %) of the group's MWh on those days. It is listed in `extra.held`; `extra.hold` has each
  BA's share. EPE is 2.5 % of tx's group (Nov 2025-Nov 2026), so it qualifies. PACE (39 %) and PSCO (43 %) are not
  small, so mtnw waits for EIA-930 even with SPP West's feed.
- **Complete local days only.** Today is never written for power or demand_est. Scaling today's hours to 24 h would
  misread the daily shape: gas plants run hardest in the evening ramp. Yesterday completes within ~1-2 h of local
  midnight (the RT hours land ~5-15 min after they end, and this source runs hourly), so the box gets a daily value
  within hours of the day's end.
- `extra.rt_fit` (each BA's ratio, hours, days, max deviation, accepted/reason) and `extra.rt_days` (which days used how
  many RT hours) report it on power_burn_est.

**No partial sums.** A day where any balancing authority misses an hour, or any city misses its temperature, or any member
state misses its industrial value, gets no value for that term (and none for demand_est). Never a zero.

**Why fixed parameters, not a refit inside the connector.**
- A refit needs ≥ 12 months of EIA-930 gas generation and EIA's monthly end-use split per state. Kraken's store holds
  8 days of EIA-930 and 6 months of EIA state data (checked 2026-11-13).
- A monthly refit on a short or holey store (930 had multi-week gaps in Nov-Dec 2025) would swing the heat rate silently.
- So the parameters live in the spec, with `fitted:` dates, and are refitted by hand once a year or when the drift shows,
  using the scripts described in the research note (EIA-930 keyless daily history + the EIA dnav XLS downloads).
- Heat rates use the latest 12 published months (2025-09 .. 2026-08), so they carry recent fleet and BA changes. Texas
  uses an intercept fitted on 2023-01 .. 2026-08, which halves its miss. R+C uses 2021-01 .. 2026-08.

**Scope and caveats** (also in the labels):
- **umw:** MISO straddles 15 states. umw's power term is MISO's gas generation × a fitted share (as a heat rate).
- **bakken:** NorthWestern (MT) plus a fitted MISO + SPP share for North Dakota.
- **mtnw:** the 2026 drift. EIA's UT/CO/WY electric-power burn rose 30-80 % while EIA-930 output stayed flat. Part of it
  is LDWP's Intermountain gas units in Utah, which EIA-930 reports under LDWP (cal's list).
- **nyc / upne:** on the coldest days EIA-930 "gas" seems to include oil burned by dual-fuel units, so the power term reads
  high.
- **pnw and cal are cross-checks.** cal has its measured PG&E + SoCalGas demand (cal_gas.py); pnw has its
  pipeline-balance derivation (pnw_gas.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.store import Obs, SeriesMeta

GROUP = "Regions: gas demand (est.)"
BTU_PER_CF = 1037.0              # MMBtu per MMcf (pnw_gas / cal_gas use the same)
TERMS = ("power_burn_est", "rescom_est", "industrial_est", "demand_est")
LY_LOOKBACK = timedelta(days=430)
# Real-time ISO gas generation standing in for EIA-930's late hours (see the docstring, "Current days")
RT_FIT_WINDOW = timedelta(days=14)
RT_MIN_HOURS = 72
RT_MIN_DAYS = 3
RT_MAX_DEV = 0.05
RT_RATIO_RANGE = (0.8, 1.25)
HOLD_DAYS = 7                    # a small BA's stand-in: its mean over its last 7 complete EIA-930 days ...
HOLD_MIN_DAYS = 5                # ... of which at least 5 must exist in the last 14 days
HOLD_MAX_SHARE = 0.10            # ... and only if it's under 10 % of its group's MWh on those days
HISTORY = timedelta(days=15)     # how far back the power inputs are read (the fit and the hold need 14 days)


def sid(region: str, term: str) -> str:
    return f"gas.{region}.{term}"


def local_day_hours(day: datetime, tz: ZoneInfo) -> list[datetime]:
    """The naive-UTC hour starts of a local calendar day (23, 24 or 25 of them)."""
    start = day.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = (day + timedelta(days=1)).replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return [start + timedelta(hours=i) for i in range(int((end - start).total_seconds() // 3600))]


def group_mwh(g: dict, got: dict[str, dict[datetime, float]], day: datetime, hours: list[datetime]) -> float | None:
    """A power group's MWh for a local day: EIA-930 BAs (hourly MW, every hour required) or one daily MWh/d series
    (`series:`, e.g. ieso.gen.gas.daily, keyed by the day). None when anything is missing."""
    if g.get("series"):
        return (got.get(g["series"]) or {}).get(day)
    mwh = 0.0
    for ba in g["bas"]:
        pts = got.get(f"eia930.{ba}.gen.gas") or {}
        if any(h not in pts for h in hours):
            return None
        mwh += sum(pts[h] for h in hours)
    return mwh


def power_burn(groups: list[dict], got: dict[str, dict[datetime, float]], hours: list[datetime],
               day: datetime | None = None) -> float | None:
    """Σ groups (base + HR × MWh / 1037). None unless every group has its day complete (no partial sums)."""
    total = 0.0
    for g in groups:
        mwh = group_mwh(g, got, day if day is not None else hours[0], hours)
        if mwh is None:
            return None
        total += float(g.get("base", 0.0)) + float(g["hr"]) * mwh / BTU_PER_CF
    return total


def power_ids(groups: list[dict]) -> set[str]:
    ids = {g["series"] if g.get("series") else f"eia930.{ba}.gen.gas" for g in groups for ba in (g.get("bas") or [0])}
    return ids | {s for g in groups for parts in (g.get("rt") or {}).values() for s in parts}


def rt_value(rt_ids: list[str], got: dict[str, dict[datetime, float]], h: datetime) -> float | None:
    """The ISO real-time gas MW at hour h: the sum of the BA's RT series, only when every one has the hour."""
    pts = [(got.get(s) or {}).get(h) for s in rt_ids]
    return None if not rt_ids or any(v is None for v in pts) else sum(pts)


def fit_rt(eia: dict[datetime, float], rt_ids: list[str], got: dict[str, dict[datetime, float]], now: datetime) -> dict:
    """RT -> EIA-930 ratio on the stored overlap (hours in the last RT_FIT_WINDOW where both exist): Σ EIA / Σ RT.
    Accepted only with ≥ RT_MIN_HOURS overlap hours on ≥ RT_MIN_DAYS UTC days of ≥ 20 hours each, every such day's own
    ratio within RT_MAX_DEV of the overall one, and the ratio inside RT_RATIO_RANGE (the two measure the same plants)."""
    lo = now - RT_FIT_WINDOW
    hrs = [h for h in eia if lo <= h <= now and rt_value(rt_ids, got, h) is not None]
    out = {"hours": len(hrs), "accepted": False}
    if not hrs:
        return out | {"reason": "no overlap"}
    rt_sum = sum(rt_value(rt_ids, got, h) for h in hrs)
    if rt_sum <= 0:
        return out | {"reason": "RT sum <= 0"}
    ratio = sum(eia[h] for h in hrs) / rt_sum
    by_day: dict = {}
    for h in hrs:
        d = by_day.setdefault(h.date(), [0.0, 0.0, 0])
        d[0] += eia[h]
        d[1] += rt_value(rt_ids, got, h)
        d[2] += 1
    days = [e / r for e, r, n in by_day.values() if n >= 20 and r > 0]
    dev = max((abs(x / ratio - 1) for x in days), default=float("inf"))
    out |= {"ratio": round(ratio, 4), "days": len(days), "max_dev": round(dev, 4) if days else None}
    if len(hrs) < RT_MIN_HOURS or len(days) < RT_MIN_DAYS:
        return out | {"reason": f"overlap {len(hrs)} h / {len(days)} full days (needs {RT_MIN_HOURS} h, {RT_MIN_DAYS} d)"}
    if dev > RT_MAX_DEV:
        return out | {"reason": f"unstable: a day's ratio is {dev:.1%} off (max {RT_MAX_DEV:.0%})"}
    if not RT_RATIO_RANGE[0] <= ratio <= RT_RATIO_RANGE[1]:
        return out | {"reason": f"ratio {ratio:.3f} outside {RT_RATIO_RANGE} (not the same plants?)"}
    return out | {"accepted": True}


def hdd(weights: dict[str, float], temps: dict[str, float | None], base_c: float) -> float | None:
    """Population-weighted HDD (°C-days) at base_c. None when any city has no temperature."""
    if not weights or any(temps.get(c) is None for c in weights):
        return None
    return sum(max(0.0, base_c - temps[c]) * w for c, w in weights.items()) / sum(weights.values())


def last_year(month: datetime) -> datetime:
    return month.replace(year=month.year - 1)


class GasRegionDemand(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params", {})
        self.regions: dict[str, dict] = p.get("regions", {})
        self.industrial: dict[str, list] = p.get("industrial", {})
        self.days = int(p.get("days", 10))
        self.fitted = p.get("fitted", {})
        for r, cfg in self.regions.items():     # YAML 1.1 reads a bare on / ON as true: fail loudly, never gas.True.*
            if not isinstance(r, str) or not isinstance(cfg.get("province", ""), str) or \
                    not all(isinstance(x, str) for x in cfg.get("states", [])):
                raise ValueError(f"{self.id}: region {r!r}: keys, province and states must be strings (quote on / ON)")

    # ------------------------------------------------------------------ weather weights (the Weather agent's)
    def weights(self, region: str, cfg: dict) -> dict[str, float]:
        from montney.connectors import load_specs

        w = cfg.get("weather", {})
        spec = load_specs(include_disabled=True).get(w.get("source", f"weather_{region}"))
        if spec is None:
            return {}
        comp = [c for c in spec.get("params", {}).get("composites", []) if c.get("id") == w.get("composite",
                                                                                              f"region.{region}")]
        keep = w.get("cities")
        return {k: float(v) for k, v in comp[0]["weights"].items() if keep is None or k in keep} if comp else {}

    # ------------------------------------------------------------------ industrial inputs
    @staticmethod
    def industrial_parts(cfg: dict) -> list[tuple[str, str]]:
        """(stored monthly series, fallback table key) per member: EIA states, or one StatCan province."""
        pv = cfg.get("province")
        if pv:
            return [(f"statcan.{pv.lower()}.deliveries.industrial", f"CA-{pv.upper()}")]
        return [(f"eia.{st.lower()}.deliveries.industrial", st) for st in cfg.get("states", [])]

    # ------------------------------------------------------------------ meta
    def _meta(self, region: str, cfg: dict, older: dict[str, int] | None = None, power_run: dict | None = None
              ) -> list[SeriesMeta]:
        name, role = cfg.get("name", region), cfg.get("role", "estimate")
        tag = " (cross-check; the region has a measured line)" if role == "cross_check" else ""
        note = f" {cfg['note']}" if cfg.get("note") else ""
        r = cfg.get("rescom", {})
        power = cfg.get("power", [])
        src = "; ".join(g.get("label") or ("EIA-930 " + "+".join(b.upper() for b in g["bas"])) for g in power)
        agency = "StatCan" if cfg.get("province") else "EIA"
        base = {"quality": "estimate", "role": role, "fitted": self.fitted, "tz": cfg.get("tz", "UTC")}
        m = lambda term, label, extra: SeriesMeta(sid(region, term), self.id, label, "MMcf/d", "D", GROUP,  # noqa
                                                  extra={**base, **extra})
        ind_extra = {"older_year": older} if older else {}
        held = (f" (older year where {agency} withholds: " + ", ".join(f"{k} {v}" for k, v in sorted(older.items()))
                + ")") if older else ""
        if cfg.get("industrial_less_power"):
            ind_how = f"{agency} monthly, the same month last year, less that month's gas-fired power (counted daily)"
        elif not power:
            ind_how = f"{agency} monthly, the same month last year (incl. gas burned for power generation)"
        else:
            ind_how = f"{agency} monthly, the same month last year"
        basis = f"{agency} {'deliveries to consumers' if agency == 'StatCan' else 'delivered to consumers'}"
        parts = ("power + " if power else "") + "residential/commercial + industrial"
        out = []
        if power:
            rt = sorted(f"{ba.upper()} ({'+'.join(ids)})" for g in power for ba, ids in (g.get("rt") or {}).items())
            rt_txt = f"; EIA-930's late hours from ISO real time × a fitted ratio: {', '.join(rt)}" if rt else ""
            hold_txt = ("" if any(g.get("series") for g in power) else
                        "; a small BA (< 10 % of its group) short of a day takes its 7-day mean (extra.held)")
            out.append(m("power_burn_est", f"{name} gas burned for power, est.: {src} gas generation × fitted heat rate"
                                           f"{rt_txt}{hold_txt}; complete local days only{tag}.{note}",
                         {"power": power, **(power_run or {})}))
        out += [
            m("rescom_est", f"{name} residential + commercial gas, est. from weather: {r.get('a')} + {r.get('b')} × "
                            f"HDD (base {r.get('base_c')} °C, Weather's population-weighted cities){tag}",
              {"rescom": r}),
            m("industrial_est", f"{name} industrial gas, {ind_how}{held}{tag}", ind_extra),
            m("demand_est", f"{name} gas demand, est. ({parts}; {basis} basis, ±{float(cfg.get('band', 0)):.0%} "
                            f"monthly p90){tag}.{note}",
              {"band": float(cfg.get("band", 0)), "band_basis": f"monthly p90 |miss| vs {basis}, back-test "
                                                                f"{cfg.get('band_window', '2024-01..2026-08')}",
               **ind_extra}),
        ]
        return out

    def declared_series(self) -> list[SeriesMeta]:
        return [m for r, cfg in self.regions.items() for m in self._meta(r, cfg)]

    # ------------------------------------------------------------------ fetch
    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        now = ctx.now
        plan = {}
        idsday, idsly = set(), set()
        for r, cfg in self.regions.items():
            tz = ZoneInfo(cfg.get("tz", "UTC"))
            today = now.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
            today = today.replace(hour=0, minute=0, second=0, microsecond=0)
            days = [today - timedelta(days=i) for i in range(self.days - 1, -1, -1)]
            w = self.weights(r, cfg)
            if not w:
                res.warnings.append(f"{r}: no weather composite weights (sources/{cfg.get('weather', {}).get('source')})")
            plan[r] = (tz, days, w)
            idsday |= power_ids(cfg.get("power", []))
            idsday |= {f"wx.{c}.{k}" for c in w for k in ("tmean", "tmean.realized")}
            idsly |= {s for s, _ in self.industrial_parts(cfg)}
            if cfg.get("industrial_less_power"):
                idsly |= {g["series"] for g in cfg.get("power", []) if g.get("series")}
        lo = min(d[0] for _, d, _ in plan.values()) - timedelta(days=1) if plan else now
        pids = {i for cfg in self.regions.values() for i in power_ids(cfg.get("power", []))}
        got = self.stored(sorted(idsday - pids), lo, now + timedelta(days=1))
        got.update(self.stored(sorted(pids), lo - HISTORY, now + timedelta(days=1)))   # the RT fit + hold need 14 d
        lyp = self.stored(sorted(idsly), lo - LY_LOOKBACK, now)      # monthly inputs + last year's daily power

        for r, cfg in self.regions.items():
            tz, days, w = plan[r]
            r_cfg = cfg.get("rescom", {})
            power = cfg.get("power", [])
            older: dict[str, int] = {}
            vals: dict[str, dict[datetime, float]] = {t: {} for t in TERMS}
            today = days[-1]
            pplan = self.power_plan(cfg, tz, today, got, now) if power else []
            held: dict[str, list[str]] = {}
            rt_days: dict[str, dict[str, int]] = {}
            for d in days:
                if not power:
                    pb = 0.0
                elif d >= today:      # complete local days only: today's partial hours never stand for a day
                    pb = None
                else:
                    pb, info = self.power_day(cfg, pplan, got, d, local_day_hours(d, tz))
                    if pb is not None:
                        for ba in info["held"]:
                            held.setdefault(ba, []).append(d.date().isoformat())
                        if info["rt_hours"]:
                            rt_days[d.date().isoformat()] = info["rt_hours"]
                temps = {}
                for c in w:
                    real = (got.get(f"wx.{c}.tmean.realized") or {}).get(d)
                    temps[c] = real if real is not None else (got.get(f"wx.{c}.tmean") or {}).get(d)
                h = hdd(w, temps, float(r_cfg.get("base_c", 18.0)))
                rc = None if h is None else float(r_cfg["a"]) + float(r_cfg["b"]) * h
                ind, older_d = self.industrial_day(cfg, d, lyp)
                if ind is not None and cfg.get("industrial_less_power"):
                    less = self.power_last_year(cfg, d, lyp)
                    ind = None if less is None else ind - less
                if ind is not None:
                    older.update(older_d)
                for term, v in (("power_burn_est", pb if power else None), ("rescom_est", rc),
                                ("industrial_est", ind)):
                    if v is not None:
                        vals[term][d] = v
                if pb is not None and rc is not None and ind is not None:
                    vals["demand_est"][d] = pb + rc + ind
            for term, pts in vals.items():
                # as_of = the day: today's value is re-stamped each run until the day is over (a daily actual)
                res.obs += [Obs(sid(r, term), d, v, d) for d, v in sorted(pts.items())]
            fits = {ba: f for p_ in pplan for ba, f in (p_.get("fits") or {}).items()}
            holds = {ba: h for p_ in pplan for ba, h in (p_.get("hold") or {}).items()}
            res.meta += self._meta(r, cfg, older, {"rt_fit": fits, "rt_days": rt_days, "held": held, "hold": holds})
            for ba, f in fits.items():
                if not f.get("accepted"):
                    res.warnings.append(f"{r}: {ba.upper()} real-time gas not used: {f.get('reason')}")
            if older:
                res.warnings.append(f"{r}: industrial uses an older year for "
                                    + ", ".join(f"{k} ({v})" for k, v in sorted(older.items())) + " (withheld)")
            if not vals["demand_est"]:
                res.warnings.append(f"{r}: no day with power, weather and industrial all stored")
        return res

    # ------------------------------------------------------------------ power: EIA-930, ISO real time, small-BA hold
    @staticmethod
    def power_plan(cfg: dict, tz: ZoneInfo, today: datetime, got: dict, now: datetime) -> list[dict]:
        """Per group: the accepted RT ratio of each BA with an `rt:` feed (fit_rt), and the hold stand-in of each small
        BA (its mean daily MWh over its last HOLD_DAYS local days that were complete in EIA-930 for every BA of the
        group, within the last 14 days, if it was under `hold_max_share` of the group's MWh on those days)."""
        plans = []
        for g in cfg.get("power", []):
            if g.get("series"):
                plans.append({})
                continue
            fits = {ba: fit_rt(got.get(f"eia930.{ba}.gen.gas") or {}, ids, got, now)
                    for ba, ids in (g.get("rt") or {}).items()}
            complete = []
            for i in range(14, 0, -1):
                d = today - timedelta(days=i)
                hours = local_day_hours(d, tz)
                mwh = {}
                for ba in g["bas"]:
                    pts = got.get(f"eia930.{ba}.gen.gas") or {}
                    if any(h not in pts for h in hours):
                        break
                    mwh[ba] = sum(pts[h] for h in hours)
                else:
                    complete.append(mwh)
            complete = complete[-HOLD_DAYS:]
            hold = {}
            if len(complete) >= HOLD_MIN_DAYS:
                tot = sum(sum(m.values()) for m in complete)
                max_share = float(g.get("hold_max_share", HOLD_MAX_SHARE))
                for ba in g["bas"]:
                    share = sum(m[ba] for m in complete) / tot if tot > 0 else 1.0
                    if share < max_share:
                        hold[ba] = {"mwh": sum(m[ba] for m in complete) / len(complete), "share": round(share, 4),
                                    "days": len(complete)}
            plans.append({"fits": fits, "hold": hold})
        return plans

    @staticmethod
    def power_day(cfg: dict, plans: list[dict], got: dict, day: datetime, hours: list[datetime]
                  ) -> tuple[float | None, dict]:
        """Σ groups (base + HR × MWh / 1037) for one complete local day, and what stood in for EIA-930.
        Per BA and hour: EIA-930 where stored, else the RT feed × its accepted ratio. A BA still short of an hour takes
        its hold stand-in if it has one; otherwise the day gets no value (never a partial sum)."""
        total, info = 0.0, {"rt_hours": {}, "held": []}
        for g, plan in zip(cfg.get("power", []), plans):
            if g.get("series"):
                mwh = (got.get(g["series"]) or {}).get(day)
                if mwh is None:
                    return None, info
            else:
                mwh = 0.0
                for ba in g["bas"]:
                    eia = got.get(f"eia930.{ba}.gen.gas") or {}
                    fit = plan["fits"].get(ba) or {}
                    rt_ids = (g.get("rt") or {}).get(ba) or []
                    s, n_rt = 0.0, 0
                    for h in hours:
                        if h in eia:
                            s += eia[h]
                            continue
                        v = rt_value(rt_ids, got, h) if fit.get("accepted") else None
                        if v is None:
                            s = None
                            break
                        s += v * fit["ratio"]
                        n_rt += 1
                    if s is None:
                        if ba not in plan["hold"]:
                            return None, info
                        s = plan["hold"][ba]["mwh"]
                        info["held"].append(ba)
                    elif n_rt:
                        info["rt_hours"][ba] = n_rt
                    mwh += s
            total += float(g.get("base", 0.0)) + float(g["hr"]) * mwh / BTU_PER_CF
        return total, info

    def industrial_day(self, cfg: dict, day: datetime, got: dict) -> tuple[float | None, dict[str, int]]:
        """Σ members' industrial MMcf/d, the same month last year: the store, else the fitted table."""
        month = day.replace(day=1)
        ly = last_year(month)
        total, older = 0.0, {}
        for series, key in self.industrial_parts(cfg):
            v = (got.get(series) or {}).get(ly)
            if v is None:
                row = self.industrial.get(key) or []
                cell = row[month.month - 1] if len(row) == 12 else None
                if not cell:
                    return None, {}
                v, year = float(cell[0]), int(cell[1])
                if year < ly.year:
                    older[key] = year
            total += v
        return total, older

    @staticmethod
    def power_last_year(cfg: dict, day: datetime, got: dict) -> float | None:
        """Σ groups HR × (mean daily MWh of the same month last year) / 1037: the gas-fired power inside last year's
        industrial (StatCan counts it there). The store's daily series (all but at most 2 days of the month), else
        the group's `power_ly` table ([MWh/d, year] per calendar month)."""
        month = day.replace(day=1)
        ly = last_year(month)
        nxt = (ly + timedelta(days=32)).replace(day=1)
        total = 0.0
        for g in cfg.get("power", []):
            pts = [v for t, v in (got.get(g.get("series", "")) or {}).items() if ly <= t < nxt]
            if len(pts) >= (nxt - ly).days - 2:
                mwh = sum(pts) / len(pts)
            else:
                row = g.get("power_ly") or []
                cell = row[month.month - 1] if len(row) == 12 else None
                if not cell:
                    return None
                mwh = float(cell[0])
            total += float(g["hr"]) * mwh / BTU_PER_CF
        return total
