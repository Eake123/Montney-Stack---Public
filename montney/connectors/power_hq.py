"""Quebec (Hydro-Quebec) demand and generation by source (L-091, Erik: "keep requests as limited as possible").

Source: Hydro-Quebec open data (Opendatasoft, keyless), https://donnees.hydroquebec.com, whole-dataset JSON exports
(``/api/explore/v2.1/catalog/datasets/<id>/exports/json``), one request per dataset:

  demande-electricite-quebec              rolling ~48 h, 15-min, ``valeurs_demandetotal`` (MW); future rows are null
  production-electricite-quebec           rolling ~48 h, hourly (stamped at :30), ``valeurs_hydraulique``,
                                          ``_eolien``, ``_solaire``, ``_thermique``, ``_autres``, ``_total`` (MW)
  historique-demande-electricite-quebec   hourly 2019-01-01 .. 2026-01-01, ``moyenne_mw``
  historique-production-electricite-quebec  hourly 2019-01-01 .. 2026-01-01, ``hydraulique`` ... ``total``

Stamps are UTC. Production stamps are mid-hour (05:30 = 00:00-01:00 EST): floored to the hour start. Demand stamps
are interval ends (the first 15-min row of a day is 00:15 local, and the history's first hour is stamped 06:00 UTC =
01:00 EST): the hourly mean is over the hour the intervals end in, ``ts`` = its start. Quebec days are
America/Toronto dates (EST/EDT).

Requests (L-081):
* ``report: hourly`` (sources/power_hq_hourly.yaml, ``mode: live``, every 15 min since PLAN 9): the two rolling
  exports = 2 req a run (8 req/h).
* ``report: daily`` (sources/power_hq_daily.yaml, 1 d): the two rolling exports once a day (yesterday's sums), plus
  the two history exports ONCE: their daily sums go into ``<home>/cache/hq/daily_sums.json`` and every run re-emits
  the cache (change-only writes drop the repeats). HQ publishes no history between 2026-01-01 and the rolling
  window, so that stretch has no daily values (said in a warning, never filled in).

* ``report: interchange`` (sources/power_hq_interchange.yaml, ``mode: live``, 1 h): the rolling
  ``importations-exportations-avec-transits`` export = 1 req/h (8c qc). HQ compiles it from the neighbours' public
  data (Ontario matches IESO's own Quebec intertie MW for MW, 2026-11-07); stamps are UTC hour starts. Per market,
  ``exportations_<m>`` is the NET export (negative = Quebec imports; ``importations_sources_<m>_total`` is its mirror).
  New Brunswick is always null so far, so the net covers Ontario + New York + New England only (the qc line's scope).
  Placeholder rows ahead of the data have every ``exportations_*`` null.

Series: hq.demand.hourly, hq.gen.<src>.hourly (MW, live) and hq.demand.daily, hq.gen.<src>.daily (MWh/d), with
<src> in hydro, wind, solar, thermal, other, total. A series gets a day only when it has every hour of it (23/24/25 with DST).
Interchange series (MW, live, + = Quebec exports): hq.intertie.<market>.flow for ontario, new_york, new_england,
new_brunswick (when HQ fills it), and hq.intertie.net.flow = Ontario + New York + New England, written only for an hour
that has all three. Churchill Falls (Labrador, ~5.4 GW, sold to HQ) isn't an import here: generation + net imports
balances demand to within ~1 GW (2026-11-06/07), so its output sits in HQ's hydro.
HQ's production history has ~7,300 rows without a date (essentially all of 2021): they can't be placed, so 2021 has
demand but no generation (a warning says so).
Freshness (2026-11-03): demand is ~25 min behind, production ~1.5-2.5 h (thermal ~6 h, emitted when it lands: PLAN 9),
so the hourly spec emits them separately (a merged row would hold demand back). Fail closed: an export that isn't a list of records, or none with a value
(NO-RESULT).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://donnees.hydroquebec.com/api/explore/v2.1/catalog/datasets"
TZ = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")
SOURCES = {"hydro": "hydraulique", "wind": "eolien", "solar": "solaire", "thermal": "thermique", "other": "autres",
           "total": "total"}
KEYS = ("demand", *SOURCES)
GROUP = "Hydro-Quebec / Quebec demand and generation"
MAX_MW = 60_000                          # Quebec's record demand is ~43 GW
MARKETS = {"ontario": "ontario", "new_york": "newyork", "new_england": "newengland", "new_brunswick": "newbrunswick"}
NET_MARKETS = ("ontario", "new_york", "new_england")   # New Brunswick: always null in HQ's export so far
MAX_TIE_MW = 10_000                      # all of Quebec's ties together are ~8 GW


def _sid(k: str, kind: str) -> str:
    return f"hq.demand.{kind}" if k == "demand" else f"hq.gen.{k}.{kind}"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)


def _records(payload, what: str) -> list[dict]:
    if not isinstance(payload, list):
        raise FormatDrift(f"Hydro-Quebec {what}: export is not a list of records")
    return payload


def demand_hours(recs: list[dict], field: str, step_min: int) -> dict[datetime, float]:
    """Interval-end stamps -> {hour start: mean MW} for complete hours (60 / step_min values)."""
    by: dict[datetime, list[float]] = defaultdict(list)
    for r in recs:
        v = r.get(field)
        if v is None or not r.get("date"):
            continue
        start = _ts(r["date"]) - timedelta(minutes=step_min)
        by[start.replace(minute=0, second=0)].append(float(v))
    need = 60 // step_min
    return {h: sum(v) / len(v) for h, v in by.items() if len(v) == need}


def production_hours(recs: list[dict], prefix: str) -> dict[datetime, dict[str, float]]:
    """{hour start: {source: MW}} with only the sources that have a value. HQ sends thermal 360 min late and the rest
    (incl. the total) 90 min late (dataset notes), so an hour's row has every source but thermal for ~4.5 h: each source
    is kept as it arrives instead of holding the whole row (and the total) back to thermal (PLAN 9: qc's box was 8 h
    old). A future placeholder (every source null, total 0) has no hydro, and a row is kept only when hydro is there."""
    out = {}
    for r in recs:
        vals = {k: r.get(prefix + f) for k, f in SOURCES.items()}
        if not r.get("date") or vals["hydro"] is None:
            continue                                              # a future placeholder (nulls, total 0)
        out[_ts(r["date"]).replace(minute=0, second=0)] = {k: float(v) for k, v in vals.items() if v is not None}
    return out


def interchange_hours(recs: list[dict]) -> dict[datetime, dict[str, float]]:
    """{hour start: {market: net export MW}} from HQ's import/export export; a market's null is left out (never 0)."""
    if recs and not all(f"exportations_{f}" in recs[0] for f in MARKETS.values()):
        raise FormatDrift("Hydro-Quebec importations-exportations: an exportations_<market> field is missing")
    out = {}
    for r in recs:
        if not r.get("date"):
            continue
        row = {m: float(r[f"exportations_{f}"]) for m, f in MARKETS.items() if r.get(f"exportations_{f}") is not None}
        if not row:
            continue                                              # a placeholder row ahead of the data
        for m, v in row.items():
            if not -MAX_TIE_MW <= v <= MAX_TIE_MW:
                raise FormatDrift(f"Hydro-Quebec {r['date']}: {m} flow {v:,.0f} MW outside +-{MAX_TIE_MW:,}")
        out[_ts(r["date"]).replace(minute=0, second=0)] = row
    return out


def merge(dem: dict[datetime, float], prod: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    """Union by hour: an hour can have demand, generation or both (HQ's 2021 production history has no dates)."""
    rows = {h: {**prod.get(h, {}), **({"demand": dem[h]} if h in dem else {})} for h in set(dem) | set(prod)}
    for h, r in rows.items():
        for k, v in r.items():
            if not -MAX_MW <= v <= MAX_MW:
                raise FormatDrift(f"Hydro-Quebec {h}: {k} {v:,.0f} MW outside +-{MAX_MW:,}")
    return rows


def local_day_hours(d: date) -> list[datetime]:
    a = datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(UTC)
    b = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=TZ).astimezone(UTC)
    n = int((b - a).total_seconds() // 3600)
    return [(a + timedelta(hours=i)).replace(tzinfo=None) for i in range(n)]


def daily_sums(rows: dict[datetime, dict[str, float]]) -> dict[str, dict[str, float]]:
    """{hour start: row} -> {Quebec date: {key: MWh}}: a key gets a day only when every hour of it has that key."""
    days = {h.replace(tzinfo=UTC).astimezone(TZ).date() for h in rows}
    out = {}
    for d in sorted(days):
        hrs = local_day_hours(d)
        day = {k: round(sum(rows[h][k] for h in hrs), 1) for k in KEYS if all(k in rows.get(h, {}) for h in hrs)}
        if day:
            out[d.isoformat()] = day
    return out


class HqGeneration(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.report = spec.get("params", {}).get("report", "daily")
        if self.report not in ("hourly", "daily", "interchange"):
            raise ValueError(f"{self.id}: params.report must be hourly, daily or interchange, not {self.report!r}")

    def declared_series(self) -> list[SeriesMeta]:
        names = {"demand": "demand", "hydro": "hydro generation", "wind": "wind generation",
                 "solar": "solar generation", "thermal": "thermal generation", "other": "other generation",
                 "total": "total generation"}
        if self.report == "interchange":
            names = {"ontario": "Ontario", "new_york": "New York", "new_england": "New England",
                     "new_brunswick": "New Brunswick"}
            return ([SeriesMeta(f"hq.intertie.{m}.flow", self.id, f"Quebec - {n} net flow (+ Quebec export), hourly",
                                "MW", "1h", GROUP, extra={"tier": "live"}) for m, n in names.items()]
                    + [SeriesMeta("hq.intertie.net.flow", self.id, "Quebec net exports to Ontario + New York + New "
                                  "England (+ export; excl. New Brunswick), hourly", "MW", "1h", GROUP,
                                  extra={"tier": "live"})])
        if self.report == "hourly":     # the tier is stated: the catalog is seeded from these metas (#292)
            return [SeriesMeta(_sid(k, "hourly"), self.id, f"Quebec (Hydro-Quebec) {n}, hourly mean", "MW", "1h",
                               GROUP, extra={"tier": "live"}) for k, n in names.items()]
        return [SeriesMeta(_sid(k, "daily"), self.id, f"Quebec (Hydro-Quebec) {n}, daily sum", "MWh/d", "D", GROUP,
                           extra={"tier": "daily", "day": "Quebec date (America/Toronto)"}) for k, n in names.items()]

    def _export(self, ctx: Context, ds: str, timeout: float = 60) -> list[dict]:
        return _records(ctx.get(f"{BASE}/{ds}/exports/json", timeout=timeout).json(), ds)

    def _rolling_parts(self, ctx: Context) -> tuple[dict[datetime, float], dict[datetime, dict[str, float]]]:
        dem = demand_hours(self._export(ctx, "demande-electricite-quebec"), "valeurs_demandetotal", 15)
        prod = production_hours(self._export(ctx, "production-electricite-quebec"), "valeurs_")
        return dem, prod

    def _rolling(self, ctx: Context) -> dict[datetime, dict[str, float]]:
        return merge(*self._rolling_parts(ctx))

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        if self.report == "interchange":
            hours = interchange_hours(self._export(ctx, "importations-exportations-avec-transits"))
            for h, row in sorted(hours.items()):
                for m, v in row.items():
                    res.obs.append(Obs(f"hq.intertie.{m}.flow", h, round(v, 1), h))
                if all(m in row for m in NET_MARKETS):
                    res.obs.append(Obs("hq.intertie.net.flow", h, round(sum(row[m] for m in NET_MARKETS), 1), h))
            if not any(o.series_id == "hq.intertie.net.flow" for o in res.obs):
                raise FormatDrift("Hydro-Quebec importations-exportations: no complete hour (NO-RESULT)")
            return res
        if self.report == "hourly":
            # Emitted separately: demand is ~25 min behind, production ~3 h (2026-11-03), so a merged row would hold
            # demand back to production's last hour.
            dem, prod = self._rolling_parts(ctx)
            merge({h: dem[h] for h in set(dem) & set(prod)}, {h: prod[h] for h in set(dem) & set(prod)})   # range check
            for h, v in sorted(dem.items()):
                if not -MAX_MW <= v <= MAX_MW:
                    raise FormatDrift(f"Hydro-Quebec {h}: demand {v:,.0f} MW outside +-{MAX_MW:,}")
                res.obs.append(Obs(_sid("demand", "hourly"), h, round(v, 1), h))
            for h, row in sorted(prod.items()):
                for k in SOURCES:
                    if k not in row:
                        continue                                  # thermal, until it lands (~6 h)
                    if not -MAX_MW <= row[k] <= MAX_MW:
                        raise FormatDrift(f"Hydro-Quebec {h}: {k} {row[k]:,.0f} MW outside +-{MAX_MW:,}")
                    res.obs.append(Obs(_sid(k, "hourly"), h, round(row[k], 1), h))
            if not dem or not prod:
                raise FormatDrift("Hydro-Quebec: no complete demand or generation hour (NO-RESULT)")
            return res
        start = date.fromisoformat(str(self.spec.get("params", {}).get("from_date", "2020-01-01")))
        cache = paths.home() / "cache" / "hq"
        cache.mkdir(parents=True, exist_ok=True)
        store = cache / "daily_sums.json"
        sums: dict[str, dict[str, float]] = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        if not any(d < "2026-01-01" for d in sums):              # the history exports: once
            hist = merge(demand_hours(self._export(ctx, "historique-demande-electricite-quebec", 180), "moyenne_mw", 60),
                         production_hours(self._export(ctx, "historique-production-electricite-quebec", 180), ""))
            sums.update({d: v for d, v in daily_sums(hist).items() if d >= start.isoformat()})
        today = datetime.now(TZ).date() if ctx.now is None else ctx.now.replace(tzinfo=UTC).astimezone(TZ).date()
        for d, v in daily_sums(self._rolling(ctx)).items():
            if d < today.isoformat():
                sums[d] = {**sums.get(d, {}), **v}
        store.write_text(json.dumps(sums, sort_keys=True), encoding="utf-8")
        hist_end = max((d for d in sums if d < "2026-01-01"), default=None)
        live_start = min((d for d in sums if d >= "2026-01-01"), default=None)
        if hist_end and live_start:
            res.warnings.append(f"Hydro-Quebec: no published data between {hist_end} and {live_start} (history ends, "
                                f"the rolling window is ~48 h): those days have no value")
        for ds, row in sorted(sums.items()):
            ts = datetime.fromisoformat(ds)
            for k, v in row.items():
                res.obs.append(Obs(_sid(k, "daily"), ts, v, ts))
        gen_gap = sorted(d for d, r in sums.items() if "total" not in r)
        if gen_gap:
            res.warnings.append(f"Hydro-Quebec: {len(gen_gap)} days with demand but no generation (e.g. {gen_gap[0]}): "
                                f"HQ's production history has rows without a date (all of 2021), never placed")
        if not res.obs:
            raise FormatDrift("Hydro-Quebec: no complete day (NO-RESULT)")
        return res
