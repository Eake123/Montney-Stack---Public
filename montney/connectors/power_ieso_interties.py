"""Ontario (IESO) intertie flows: Quebec, Manitoba, Michigan, Minnesota, New York and net exports (L-103, Erik: "what
it's doing to IESO exports, specifically to Quebec, but also to others ... declining or staying the same").

Two public, keyless IESO reports (https://reports-public.ieso.ca/public/), both with ETag/Last-Modified:

* ``report: hourly`` (sources/power_ieso_interties.yaml, 1 h): ``IntertieScheduleFlow/PUB_IntertieScheduleFlow.xml``,
  today's report, re-issued through the day: per intertie zone, hourly schedules and the **actual flow every 5 min**.
  A complete hour (all twelve 5-min actuals of every zone) is stored as its mean, rounded to the MW like IESO's yearly
  file (the two agree to <= 0.52 MW before rounding, checked on 2026-11-04). 1 request/h.
* ``report: daily`` (sources/power_ieso_interties_daily.yaml, 1 d): ``IntertieScheduleFlowYear/
  PUB_IntertieScheduleFlowYear_<YYYY>.csv``, the hourly flow per zone for the year to date (updated daily, ~1 MB),
  plus each past year from ``from_year`` (final, cached in <home>/cache/ieso after one download). It re-emits the hourly
  history (change-only writes drop the repeats) and computes the trend series below. 1 request/day.

Sign (IESO's, kept as is): **flow > 0 = export from Ontario, < 0 = import into Ontario**. Zones: MANITOBA,
MANITOBA SK, MICHIGAN, MINNESOTA, NEW-YORK, and Quebec = the sum of every ``PQ.*`` sub-intertie (PQ.AT, PQ.B5D.B31L,
PQ.D4Z, PQ.D5A, PQ.H4Z, PQ.H9A, PQ.P33C, PQ.Q4C, PQ.X2Y; a new PQ.* path is summed too). Net = the sum of all zones
(equals the yearly file's own Total column; a mismatch over 2 MW is a warning).

Series (MW):
  ieso.intertie.<zone>.flow        hourly actual flow, freq 1h (ts = the hour's start, naive UTC)
  ieso.intertie.<zone>.flow_daily  the IESO date's mean flow, freq D (24 hours needed)
  ieso.intertie.<q|net>.flow_7d    trailing 7-day mean of the daily means ending that date, freq D (7 days needed)
  ieso.intertie.<q|net>.flow_30d   trailing 30-day mean, freq D (30 days needed)
with <zone> in quebec, manitoba, manitoba_sk, michigan, minnesota, new_york, net. IESO time is EST all year
(``Etc/GMT+5``, AGENTS.md exception, as in power_ieso). Fail closed: an intertie zone that isn't one of these (or
PQ.*), or a file without its header.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.connectors.power_ieso import hour_start_utc, ieso_date
from montney.store import Obs, SeriesMeta

BASE = "https://reports-public.ieso.ca/public"
ZONES = {"MANITOBA": "manitoba", "MANITOBA SK": "manitoba_sk", "MICHIGAN": "michigan", "MINNESOTA": "minnesota",
         "NEW-YORK": "new_york"}
KEYS = ("quebec", "manitoba", "manitoba_sk", "michigan", "minnesota", "new_york", "net")
TREND = ("quebec", "net")
NAMES = {"quebec": "Quebec (all PQ interties)", "manitoba": "Manitoba", "manitoba_sk": "Manitoba SK",
         "michigan": "Michigan", "minnesota": "Minnesota", "new_york": "New York", "net": "net (all interties)"}
GROUP = "IESO / Ontario intertie flows (+ export, - import)"
MAX_MW = 10_000
_ZONE = re.compile(r"<IntertieZone>\s*<IntertieZoneName>([^<]+)</IntertieZoneName>(.*?)</IntertieZone>", re.S)
_ACT = re.compile(r"<Actual>\s*<Hour>(\d+)</Hour>\s*<Interval>(\d+)</Interval>\s*<Flow>([^<]*)</Flow>")


def zone_key(name: str) -> str:
    n = name.strip().upper()
    if n.startswith("PQ."):
        return "quebec"
    if n in ZONES:
        return ZONES[n]
    raise FormatDrift(f"IESO interties: unknown zone {name!r} (map it in power_ieso_interties.ZONES first)")


def _rollup(per_zone: dict[str, float]) -> dict[str, float]:
    """{IESO zone name: MW} -> {key: MW}: PQ.* summed into quebec, net = all zones."""
    out = defaultdict(float)
    for z, v in per_zone.items():
        out[zone_key(z)] += v
    out["net"] = sum(per_zone.values())
    for k, v in out.items():
        if not -MAX_MW <= v <= MAX_MW:
            raise FormatDrift(f"IESO interties: {k} {v:,.0f} MW outside +-{MAX_MW:,}")
    return {k: out.get(k, 0.0) for k in KEYS}


def parse_daily_xml(text: str) -> tuple[date, dict[int, dict[str, float]]]:
    """Today's XML -> (date, {hour ending: {key: MW}}) for complete hours (12 actuals for every zone)."""
    m = re.search(r"<Date>(\d{4}-\d{2}-\d{2})</Date>", text)
    zones = _ZONE.findall(text)
    if not m or not zones:
        raise FormatDrift("IESO IntertieScheduleFlow: no <Date> or no intertie zones")
    acts: dict[str, dict[int, list[float]]] = {}
    for name, body in zones:
        zone_key(name)                                            # unknown zone: fail before storing anything
        by = defaultdict(list)
        for h, _i, f in _ACT.findall(body):
            if f.strip():
                by[int(h)].append(float(f))
        acts[name] = by
    hours = set.intersection(*[{h for h, v in by.items() if len(v) == 12} for by in acts.values()])
    out = {h: _rollup({z: round(sum(acts[z][h]) / 12) for z in acts}) for h in sorted(hours)}
    return date.fromisoformat(m.group(1)), out


def parse_year_csv(text: str) -> tuple[dict[tuple[date, int], dict[str, float]], list[str]]:
    """Yearly CSV -> ({(date, hour ending): {key: MW}}, warnings). Uses each zone's actual "Flow" column."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    hz = next((i for i, r in enumerate(rows) if len(r) > 2 and r[0] == "" and r[2]), None)
    if hz is None or hz + 1 >= len(rows) or rows[hz + 1][:2] != ["Date", "Hour"]:
        raise FormatDrift("IESO IntertieScheduleFlowYear: no zone / Date,Hour header")
    zones, kinds = rows[hz], rows[hz + 1]
    flow_cols = {i: z for i, (z, k) in enumerate(zip(zones, kinds)) if k.strip() == "Flow" and z.strip() != "Total"}
    total_col = next((i for i, (z, k) in enumerate(zip(zones, kinds)) if z.strip() == "Total" and k.strip() == "Flow"),
                     None)
    for z in set(flow_cols.values()):
        zone_key(z)
    out, warnings = {}, []
    for r in rows[hz + 2:]:
        if len(r) < 3 or not re.match(r"\d{4}-\d{2}-\d{2}$", r[0]):
            continue
        try:
            per = {flow_cols[i]: float(r[i]) for i in flow_cols if r[i].strip() != ""}
        except ValueError:
            raise FormatDrift(f"IESO IntertieScheduleFlowYear {r[0]} HE{r[1]}: a flow that isn't a number") from None
        if len(per) != len(flow_cols):
            continue                                              # an hour not filled in: not data
        d, he = date.fromisoformat(r[0]), int(r[1])
        roll = _rollup(per)
        if total_col is not None and r[total_col].strip() and abs(float(r[total_col]) - roll["net"]) > 2:
            warnings.append(f"IESO interties {d} HE{he}: zones sum {roll['net']:,.0f} != Total {r[total_col]}")
        out[(d, he)] = roll
    if not out:
        raise FormatDrift("IESO IntertieScheduleFlowYear: no hours (NO-RESULT)")
    return out, warnings


def daily_and_trend(hours: dict[tuple[date, int], dict[str, float]]) -> list[Obs]:
    by_day: dict[date, list[dict[str, float]]] = defaultdict(list)
    for (d, _he), row in hours.items():
        by_day[d].append(row)
    daily = {d: {k: sum(r[k] for r in rows) / 24 for k in KEYS} for d, rows in by_day.items() if len(rows) == 24}
    obs = []
    for d in sorted(daily):
        ts = datetime(d.year, d.month, d.day)
        for k in KEYS:
            obs.append(Obs(f"ieso.intertie.{k}.flow_daily", ts, round(daily[d][k], 1), ts))
        for n, sid in ((7, "flow_7d"), (30, "flow_30d")):
            win = [d - timedelta(days=i) for i in range(n)]
            if all(x in daily for x in win):
                for k in TREND:
                    obs.append(Obs(f"ieso.intertie.{k}.{sid}", ts, round(sum(daily[x][k] for x in win) / n, 1), ts))
    return obs


class IesoInterties(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.report = spec.get("params", {}).get("report", "hourly")
        if self.report not in ("hourly", "daily"):
            raise ValueError(f"{self.id}: params.report must be hourly or daily, not {self.report!r}")

    def declared_series(self) -> list[SeriesMeta]:
        ex = {"sign": "+ export from Ontario, - import", "tz": "EST (IESO market time)"}
        out = [SeriesMeta(f"ieso.intertie.{k}.flow", self.id, f"Ontario intertie {NAMES[k]}, actual flow, hourly",
                          "MW", "1h", GROUP, extra={**ex, "tier": "hourly"}) for k in KEYS]
        if self.report == "daily":
            out += [SeriesMeta(f"ieso.intertie.{k}.flow_daily", self.id, f"Ontario intertie {NAMES[k]}, daily mean flow",
                               "MW", "D", GROUP, extra={**ex, "tier": "daily"}) for k in KEYS]
            out += [SeriesMeta(f"ieso.intertie.{k}.flow_{n}", self.id,
                               f"Ontario intertie {NAMES[k]}, trailing {n.replace('d', '-day')} mean flow", "MW", "D",
                               GROUP, extra={**ex, "tier": "daily"}) for k in TREND for n in ("7d", "30d")]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        if self.report == "hourly":
            d, hours = parse_daily_xml(ctx.get(f"{BASE}/IntertieScheduleFlow/PUB_IntertieScheduleFlow.xml").text)
            for he, row in hours.items():
                ts = hour_start_utc(d, he)
                for k in KEYS:
                    res.obs.append(Obs(f"ieso.intertie.{k}.flow", ts, row[k], ts))
            if not res.obs:
                res.warnings.append(f"IESO interties {d}: no complete hour yet")
            return res
        this_year = ieso_date(ctx.now).year
        first = int(self.spec.get("params", {}).get("from_year", this_year))
        hours: dict[tuple[date, int], dict[str, float]] = {}
        for y in range(first, this_year + 1):
            h, w = parse_year_csv(self._year(ctx, y, this_year))
            hours.update(h)
            res.warnings += w[:5]
        for (d, he), row in sorted(hours.items()):
            ts = hour_start_utc(d, he)
            for k in KEYS:
                res.obs.append(Obs(f"ieso.intertie.{k}.flow", ts, row[k], ts))
        res.obs += daily_and_trend(hours)
        return res

    def _year(self, ctx: Context, year: int, this_year: int) -> str:
        url = f"{BASE}/IntertieScheduleFlowYear/PUB_IntertieScheduleFlowYear_{year}.csv"
        if year == this_year:
            return ctx.get(url, timeout=120).text
        cache = paths.home() / "cache" / "ieso" / f"PUB_IntertieScheduleFlowYear_{year}.csv"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        text = ctx.get(url, timeout=120).text
        parse_year_csv(text)                                      # never cache a file that doesn't parse
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text
