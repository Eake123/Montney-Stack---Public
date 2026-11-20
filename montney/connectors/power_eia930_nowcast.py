"""EIA-930 generation nowcast (PLAN 9): ``eia930.<ba>.net_gen_best`` and ``eia930.<ba>.interchange_best``, an hourly
series per balancing authority that runs up to the newest demand hour instead of stopping at yesterday's midnight.

ZERO requests: it reads the EIA-930 series already in the store (gas_common.read_stored, read-only), like
region_balance and power_ab_cushion.

**Why:** under Form EIA-930 a BA's hourly "same-day" file holds demand (D) only. Net generation (NG), generation by fuel
and total interchange (TI) come only in the **daily** file, posted ~15:00-16:00 UTC through local 23:00 yesterday
(montney/connectors/power_eia930.py). So D is ~1-2 h old while NG is 8-36 h old, and a region box summing NG shows
yesterday. The best series fill the gap with an estimate and hand over to the actual value once EIA posts it.

**Method**, per BA and UTC hour ``t`` from WINDOW before now up to the newest hour with demand:

* **actual:** ``net_gen(t)`` when it is stored.
* **BA with demand:** ``demand(t) + (net_gen - demand)(r)``, ``r`` the same UTC hour on the most recent earlier day
  (t - 24 h, - 48 h, - 72 h) where both are stored. The BA's net export is carried at the same hour of day, and demand
  carries the level. **A flat hold of the last NG is wrong**: the last NG hour is local 23:00, and a solar-heavy BA
  (CISO, AZPS) swings several GW between night and midday; the same-hour offset keeps that shape.
* **generation-only BA** (AVRN, GRID, GWA, DEAA: no demand, they export all they make): ``net_gen(r)``, the same hour on
  the most recent earlier day, only up to the newest hour ANY BA has demand ("now"), never into the future.
* otherwise no value: never a 0, never an estimate built on an estimate, nothing older than MAX_DAYS_BACK days.
  A demand <= 0, or one that jumps by more than 50 % from the hour before, counts as missing (``plausible``): EIA's
  newest hour is sometimes a placeholder 0 or a partial sum (2026-11-13: PNM 0, AZPS 1,220 MW against ~3,900).

``gen.gas_best`` (PLAN 9e, for Gas's ``gas_region_demand`` power-burn term, which otherwise waits a day for EIA's gas
generation): the actual where posted, else gas at the same UTC hour on the latest earlier day scaled by net_gen_best now
over net_gen then (GAS_METHOD). Gas-marginal BAs track it within ~1 % a day; hydro BAs (PGE, PSEI, Avista) less well,
but they burn little gas.

``demand_best`` is the BA's demand with the implausible hours dropped (``plausible`` below), so region load lines don't
show EIA's placeholder hours. A dropped hour has no value (never a 0).

``interchange_best`` keeps EIA's sign like ``eia930.<ba>.interchange``: **+ = net export** out of the BA (the region
files negate it into net imports). actual ``interchange(t)``, else ``net_gen_best(t) - demand(t)`` (sign-consistent:
NG - D = net export) less the BA's reported imbalance ``(net_gen - demand - interchange)(r)`` at the same hour on the
most recent earlier day that has all three (0 when none), else, for a generation-only BA, ``interchange(r)`` the same
hour on an earlier day as above. The imbalance is 0 for almost every BA (EIA-930 balances), but not for CISO (NG - D -
TI ~ -2.8 GW, 2026-11-05..13), MISO (+1.4 GW) and PJM (+1.1 GW): without it CISO's nowcast interchange was off by ~3.4 GW.

The values are always estimates where no actual exists (SeriesMeta quality: estimate). ``as_of`` = ``ts``, so the
actual value overwrites the nowcast in place when EIA posts it (same (series, ts, as_of)).
Backtest (2026-11-13; days 11-06..11-12 UTC, NG/TI hidden from 00 UTC, nowcast from the day before vs the actual):
net_gen MAE CISO 934 MW (5.1 % of mean NG), AZPS 314 (13.3 %), BPAT 484 (5.3 %), PACE 793 (10.8 %); NG(t - 24 h) alone
was 7.0 / 15.3 / 6.6 / 11.6 %, and a flat hold of the last NG offset 18.7 % for CISO (solar).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import read_stored
from montney.store import Obs, SeriesMeta

# Every EIA-930 BA a region file uses (regions/*.yaml power: parts). GEN_ONLY have no demand series.
DEMAND_BAS = ("ciso", "banc", "ldwp", "iid", "tidc", "bpat", "pacw", "pge", "psei", "scl", "tpwr", "ava", "chpd",
              "dopd", "gcpd", "ipco", "nwmt", "pace", "psco", "swpw", "azps", "srp", "tepc", "walc", "nevp", "pnm",
              "erco", "epe", "nyis", "isne")
GEN_ONLY = ("avrn", "grid", "gwa", "deaa")
BAS = DEMAND_BAS + GEN_ONLY
WINDOW = timedelta(hours=72)
MAX_DAYS_BACK = 3
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
DEMAND_JUMP = 1.5          # a BA's load never moves 50 % in an hour; EIA's partial newest hours do
# Hour-to-hour tolerance by the BA's size (Reviewer on #482: a 70 % partial hour of a 50 GW BA passes a 50 % test and
# would put tx ~15 GW low for an hour). Tested on a week of real EIA-930 demand (2026-11-06..13): solar-heavy 2-8 GW BAs
# ramp up to ~21 %/h (AZPS -19 %, SRP +21 %, PSCo -21 %), so they get 30 %; a 70 % partial hour fails every tier.
JUMP_TIERS = ((8000, 1.15), (2000, 1.30))   # (above MW, max ratio); else DEMAND_JUMP
GROUP = "EIA-930 / US balancing authorities (hourly)"
METHOD = ("actual EIA-930 net generation where posted; else demand(t) + (net_gen - demand) at the same UTC hour on the "
          "most recent earlier day (<= 3 days back); gen-only BAs: net_gen at the same hour on an earlier day, up to "
          "the newest demand hour")
GAS_METHOD = ("actual EIA-930 gas generation where posted; else gas at the same UTC hour on the most recent earlier day "
              "(<= 3 days back) x net_gen_best now / net_gen then (for Gas's power-burn model: backtest 11-06..12, daily "
              "error ERCO 0.3 %, NYIS 1.0 %, SRP 1.3 %, AZPS 0.7 %; a flat hold was 0.8 / 5.2 / 16 / 6.2 %)")
METHOD_TI = ("actual EIA-930 total interchange where posted; else net_gen_best - demand less the BA's own "
             "(net_gen - demand - interchange) at the same UTC hour on an earlier day; gen-only BAs: interchange at "
             "the same hour on an earlier day (<= 3 days back), up to the newest demand hour")


def _same_hour_before(t: datetime, ok) -> datetime | None:
    """The most recent earlier day's same UTC hour (t - 24 h, ... - MAX_DAYS_BACK days) where ``ok(r)``."""
    for k in range(1, MAX_DAYS_BACK + 1):
        r = t - k * DAY
        if ok(r):
            return r
    return None


def plausible(d: dict[datetime, float]) -> dict[datetime, float]:
    """Demand points that can be trusted: > 0, and within DEMAND_JUMP of the accepted hour before (or else the accepted
    same hour the day before) when there is one; against the hour before, the tolerance tightens with the BA's size
    (JUMP_TIERS: 15 % above 8 GW, 30 % above 2 GW), since a big BA's load ramps a few % an hour. EIA's newest demand hour is
    sometimes a placeholder or a partial sum that is revised later (2026-11-13: PNM 0 MW, AZPS 1,220 MW against ~3,900
    an hour and a day earlier)."""
    out: dict[datetime, float] = {}
    for t in sorted(d):
        v = d[t]
        if t - HOUR in out:
            ref = out[t - HOUR]
            jump = next((j for mw, j in JUMP_TIERS if ref > mw), DEMAND_JUMP)
        else:
            ref, jump = out.get(t - DAY), DEMAND_JUMP
        if v > 0 and (ref is None or 1 / jump <= v / ref <= jump):
            out[t] = v
    return out


def nowcast(stored: dict[str, dict[datetime, float]], now: datetime) -> list[Obs]:
    """The best series for every BA in BAS, hours [now - WINDOW, newest demand hour]."""
    start = now.replace(minute=0, second=0, microsecond=0) - WINDOW
    dem = {ba: plausible(stored.get(f"eia930.{ba}.demand", {})) for ba in DEMAND_BAS}
    latest_d = max((t for ba in DEMAND_BAS for t in dem[ba] if t <= now), default=None)
    out: list[Obs] = [Obs(f"eia930.{ba}.demand_best", t, v, t) for ba in DEMAND_BAS for t, v in sorted(dem[ba].items())
                      if start <= t <= now]
    for ba in BAS:
        ng = stored.get(f"eia930.{ba}.net_gen", {})
        ti = stored.get(f"eia930.{ba}.interchange", {})
        d = dem.get(ba, {})
        gas = stored.get(f"eia930.{ba}.gen.gas", {})
        ends = [t for t in ng if t <= now]
        if ba in DEMAND_BAS:
            ends += [t for t in d if t <= now]
        elif latest_d is not None:
            ends.append(latest_d)
        if not ends:
            continue
        t, end = start, max(ends)
        while t <= end:
            g = x = None
            if t in ng:
                g = ng[t]
            elif ba in DEMAND_BAS:
                if t in d and (r := _same_hour_before(t, lambda r: r in ng and r in d)) is not None:
                    g = d[t] + ng[r] - d[r]
            elif latest_d is not None and t <= latest_d and (r := _same_hour_before(t, ng.__contains__)) is not None:
                g = ng[r]
            if t in ti:
                x = ti[t]
            elif ba in DEMAND_BAS:
                if g is not None and t in d:            # NG - D, less the BA's own NG - D - TI at the same hour before
                    r = _same_hour_before(t, lambda r: r in ng and r in d and r in ti)
                    x = g - d[t] - (ng[r] - d[r] - ti[r] if r is not None else 0.0)
            elif latest_d is not None and t <= latest_d and (r := _same_hour_before(t, ti.__contains__)) is not None:
                x = ti[r]
            if g is not None:
                out.append(Obs(f"eia930.{ba}.net_gen_best", t, round(g, 1), t))
            if t in gas:
                out.append(Obs(f"eia930.{ba}.gen.gas_best", t, gas[t], t))
            elif g is not None and gas:
                r = _same_hour_before(t, lambda r: r in gas and r in ng)
                if r is not None:     # the same hour's gas, scaled by this hour's generation vs then (GAS_METHOD)
                    v = gas[r] * g / ng[r] if ng[r] > 0 else gas[r]
                    out.append(Obs(f"eia930.{ba}.gen.gas_best", t, round(max(v, 0.0), 1), t))
            if x is not None:
                out.append(Obs(f"eia930.{ba}.interchange_best", t, round(x, 1), t))
            t += HOUR
    return out


class Eia930Nowcast(Connector):
    stored = staticmethod(read_stored)                            # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        metas = []
        for ba in BAS:
            metas.append(SeriesMeta(f"eia930.{ba}.net_gen_best", self.id,
                                    f"EIA-930 {ba.upper()} net generation, best (actual, else est. nowcast)", "MW",
                                    "1h", GROUP, extra={"tier": "hourly", "quality": "estimate", "method": METHOD}))
            metas.append(SeriesMeta(f"eia930.{ba}.interchange_best", self.id,
                                    f"EIA-930 {ba.upper()} total interchange, best (+ net export; actual, else est.)",
                                    "MW", "1h", GROUP, extra={"tier": "hourly", "quality": "estimate",
                                                              "method": METHOD_TI}))
        metas += [SeriesMeta(f"eia930.{ba}.gen.gas_best", self.id, f"EIA-930 {ba.upper()} gas generation, best (actual, "
                             f"else est. nowcast)", "MW", "1h", GROUP,
                             extra={"tier": "hourly", "quality": "estimate", "method": GAS_METHOD}) for ba in BAS]
        metas += [SeriesMeta(f"eia930.{ba}.demand_best", self.id, f"EIA-930 {ba.upper()} demand, implausible hours "
                             f"dropped (a placeholder 0 or a >50 % jump)", "MW", "1h", GROUP,
                             extra={"tier": "hourly", "method": "EIA-930 demand where > 0 and within 1.5x of the hour "
                                                               "before (or the same hour the day before)"})
                  for ba in DEMAND_BAS]
        return metas

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        ids = [f"eia930.{ba}.{k}" for ba in BAS for k in ("net_gen", "interchange", "gen.gas")]
        ids += [f"eia930.{ba}.demand" for ba in DEMAND_BAS]
        try:
            data = self.stored(ids, ctx.now - WINDOW - MAX_DAYS_BACK * DAY - HOUR, ctx.now + HOUR)
        except Exception as e:                                    # noqa: BLE001 - a store problem: NO-RESULT, no guess
            res.warnings.append(f"EIA-930 nowcast: store not readable ({e})")
            return res
        res.obs = nowcast(data, ctx.now)
        if not res.obs:
            res.warnings.append("EIA-930 nowcast: no EIA-930 demand or net generation stored yet")
        return res
