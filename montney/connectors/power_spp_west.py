"""SPP West (the SWPW balancing area): load, generation by fuel and net interchange, hourly (PLAN 9 item 1, region
``mtnw``; budget region ``rockies``).

SWPW is SPP's RTO West BA, consolidated in April 2026: WAPA Rocky Mountain (formerly WACM), WAPA Upper Great Plains
West (formerly WAUW) and the other RTO West members, in the Western Interconnection (mostly CO/WY; it also reaches
NE/SD/MT/ND). Its AC neighbours are APS, BHBA, NWMT, PACE, PNM, PSCO and WALC, so PSCo is NOT inside it.

Sources (keyless SPP portal charts; marketplace.spp.org 302-redirects here, so the portal host is used directly):
  https://portal.spp.org/chart-api/gen-mix-swpw/asFile            CSV, ~2.6 KB: the last 2 h at 5 min
      GMT MKT Interval, BAA, Coal, Diesel Fuel Oil, Hydro, Natural Gas, Nuclear, Solar, Waste Disposal Services, Wind,
      Waste Heat, Energy Storage (signed), Other, Load (MW). Stamps are UTC INTERVAL ENDS (its Load equals
      load-forecast-swpw's Actual Load at GMTIntervalEnd), ~2 min behind, re-issued every 5 min.
  https://portal.spp.org/chart-api/interchange-trend-swpw/asFile  CSV, ~246 KB: 1-min rows from 24 h back to 24 h ahead
      GMTTime, SWPW NSI (scheduled), SWPW NAI (actual), APS, BHBA, NWMT, PACE, PNM, PSCO, WALC (actual tie flows),
      SWPW NSI Future. + = export. Rows after now hold only NSI Future (skipped). ~1 min behind.
Both send ``Cache-Control: no-store`` and no ETag/Last-Modified: there's no conditional GET.

Requests: the two files every 15 min = 8 req/h (~24 MB/day, nearly all the interchange file). gen-mix's 2 h window
holds one complete hour at a time, so an hour is written on the ~4 polls between its end and it leaving the window;
a runner outage of more than ~1 h loses those generation hours (SPP's 10 MB gen-mix-swpw-365 has the history, not
read here). The interchange file's 24 h window covers yesterday too (no second request).

Hours (ts = hour start, naive UTC): an interval-end stamp belongs to the hour it ends in, so hour H is gen-mix's
H:05 .. H+1:00 (12 rows) and the trend's H:01 .. H+1:00 (60 rows). Complete hours only.

Series (MW, freq 1h):
  sppw.gen.<fuel>           coal, oil (Diesel Fuel Oil), hydro, gas (Natural Gas), nuclear, solar, waste (Waste
                            Disposal Services), wind, waste_heat, battery (Energy Storage, + discharge), other
  sppw.gen.total            the 11 fuel columns (inside SWPW; imports never in it)
  sppw.load                 SWPW load
  sppw.interchange.net      net IMPORTS into SWPW (+ in) = -NAI (SPP's + is export)
  sppw.tie.<ba>.flow        actual flow on the AC ties to each neighbour, in SPP's chart sign. **The sign is INFERRED,
                            not published:** + = out of SWPW (as NAI) fits only as a group (the seven ties' sum tracks
                            NAI, correlation +0.51 over 24 h), and the seven don't close against NAI (NAI - sum = +48 MW
                            mean, sd 108 on 2026-11-13; SWPW's DC ties to SPP East aren't listed), so one tie with the
                            opposite sign would not show. The net (-NAI) is confirmed (Reviewer on #484).
Identity (2026-11-13 15:00Z hour): gen - load = +33 MW vs NAI +16 (export): gen + net imports ~ load within ~20 MW.
Fail closed (FormatDrift, nothing stored): a header without a known column or with an unknown one (a new fuel or tie
must be mapped), a stamp that doesn't parse, a value that isn't a number or is out of range, a BAA other than SWPW,
or no complete generation hour. An interchange request that fails on the network is a warning (generation still
written); an interchange file that fails its checks fails the run.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://portal.spp.org/chart-api"
GEN_URL = f"{BASE}/gen-mix-swpw/asFile"
TREND_URL = f"{BASE}/interchange-trend-swpw/asFile"
GROUP = "SPP West (SWPW) / load, generation and interchange"
FUELS = {"Coal": "coal", "Diesel Fuel Oil": "oil", "Hydro": "hydro", "Natural Gas": "gas", "Nuclear": "nuclear",
         "Solar": "solar", "Waste Disposal Services": "waste", "Wind": "wind", "Waste Heat": "waste_heat",
         "Energy Storage": "battery", "Other": "other"}
FUEL_NAMES = {"coal": "coal", "oil": "diesel fuel oil", "hydro": "hydro", "gas": "natural gas", "nuclear": "nuclear",
              "solar": "solar", "waste": "waste disposal services", "wind": "wind", "waste_heat": "waste heat",
              "battery": "energy storage (+ discharge, - charge)", "other": "other"}
TIES = {"APS": "aps", "BHBA": "bhba", "NWMT": "nwmt", "PACE": "pace", "PNM": "pnm", "PSCO": "psco", "WALC": "walc"}
TIE_NAMES = {"aps": "APS (Arizona Public Service)", "bhba": "Black Hills (BHBA)", "nwmt": "NorthWestern Montana",
             "pace": "PacifiCorp East", "pnm": "PNM", "psco": "PSCo (Xcel Colorado)", "walc": "WAPA Desert Southwest"}
GEN_TIME, TREND_TIME = "GMT MKT Interval", "GMTTime"
NSI, NAI, NSI_FUTURE = "SWPW NSI", "SWPW NAI", "SWPW NSI Future"
# Sane ranges (MW): SWPW's load is ~2.5-5 GW; storage and station service can be negative.
MAX_MW = 20_000
FUEL_MIN = -3_000
MAX_TIE_MW = 10_000


def _ts(s: str, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        raise FormatDrift(f"SPP {what}: stamp {s!r} doesn't parse") from None
    if t.tzinfo is None:
        raise FormatDrift(f"SPP {what}: stamp {s!r} has no UTC offset")
    return t.astimezone(timezone.utc).replace(tzinfo=None)


def _num(s: str, what: str) -> float:
    try:
        return float(s)
    except ValueError:
        raise FormatDrift(f"SPP {what}: {s!r} isn't a number") from None


def _hour(end: datetime, step: timedelta) -> datetime:
    """The hour start an interval ENDING at ``end`` belongs to."""
    return (end - step).replace(minute=0, second=0, microsecond=0)


def _reader(text: str, what: str, need: set[str], known: set[str]) -> list[dict]:
    r = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    head = [c.strip() for c in (r.fieldnames or [])]
    missing, unknown = sorted(need - set(head)), [c for c in head if c not in known]
    if missing or unknown:
        raise FormatDrift(f"SPP {what}: missing columns {missing}, unknown {unknown} (map them in power_spp_west)")
    r.fieldnames = head
    return list(r)


def parse_gen(text: str) -> dict[datetime, dict[str, float]]:
    """gen-mix-swpw CSV -> {hour start: {fuel..., total, load}} for hours with all twelve 5-min rows."""
    cols = {GEN_TIME, "BAA", *FUELS, "Load"}
    by: dict[datetime, list[dict[str, float]]] = defaultdict(list)
    for r in _reader(text, "gen-mix-swpw", cols, cols):
        stamp = r[GEN_TIME]
        if (r.get("BAA") or "").strip() != "SWPW":
            raise FormatDrift(f"SPP gen-mix-swpw {stamp}: BAA {r.get('BAA')!r}, not SWPW")
        cells = {c: (r.get(c) or "").strip() for c in (*FUELS, "Load")}
        if any(v == "" for v in cells.values()):
            continue                                              # a blank cell is not data, never 0
        row = {FUELS[c]: _num(cells[c], f"gen-mix-swpw {stamp} {c}") for c in FUELS}
        row["load"] = _num(cells["Load"], f"gen-mix-swpw {stamp} Load")
        for k, v in row.items():
            lo = 0 if k == "load" else FUEL_MIN
            if not lo <= v <= MAX_MW:
                raise FormatDrift(f"SPP gen-mix-swpw {stamp}: {k} {v:,.1f} MW outside {lo:,}..{MAX_MW:,}")
        by[_hour(_ts(stamp, "gen-mix-swpw"), timedelta(minutes=5))].append(row)
    out = {}
    for hr, rows in by.items():
        if len(rows) == 12:
            m = {k: sum(r[k] for r in rows) / 12 for k in rows[0]}
            m["total"] = sum(m[f] for f in FUELS.values())
            out[hr] = m
    return out


def parse_trend(text: str) -> dict[datetime, dict[str, float]]:
    """interchange-trend-swpw CSV -> {hour start: {net_imports, <tie>...}} for hours with all sixty 1-min rows."""
    need = {TREND_TIME, NAI, *TIES}
    by: dict[datetime, list[dict[str, float]]] = defaultdict(list)
    for r in _reader(text, "interchange-trend-swpw", need, need | {NSI, NSI_FUTURE}):
        stamp = r[TREND_TIME]
        cells = {c: (r.get(c) or "").strip() for c in (NAI, *TIES)}
        if any(v == "" for v in cells.values()):
            continue                                              # future rows: NSI Future only
        row = {TIES[c]: _num(cells[c], f"interchange-trend-swpw {stamp} {c}") for c in TIES}
        row["net_imports"] = -_num(cells[NAI], f"interchange-trend-swpw {stamp} NAI")
        for k, v in row.items():
            if not -MAX_TIE_MW <= v <= MAX_TIE_MW:
                raise FormatDrift(f"SPP interchange-trend-swpw {stamp}: {k} {v:,.1f} MW outside +-{MAX_TIE_MW:,}")
        by[_hour(_ts(stamp, "interchange-trend-swpw"), timedelta(minutes=1))].append(row)
    return {hr: {k: sum(r[k] for r in rows) / 60 for k in rows[0]} for hr, rows in by.items() if len(rows) == 60}


class SppWest(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "tz": "UTC (interval-end stamps)", "region": "mtnw"}
        gen = {**ex, "lag": "rows ~2 min; an hour lands ~2 min after it ends (+ the 15-min poll)"}
        ic = {**ex, "lag": "rows ~1 min; an hour lands ~1 min after it ends (+ the 15-min poll)"}
        return ([SeriesMeta(f"sppw.gen.{k}", self.id, f"SPP West (SWPW) {n} generation, hourly mean", "MW", "1h",
                            GROUP, extra=gen) for k, n in FUEL_NAMES.items()]
                + [SeriesMeta("sppw.gen.total", self.id, "SPP West (SWPW) total generation (all fuels, no imports), "
                              "hourly mean", "MW", "1h", GROUP, extra=gen),
                   SeriesMeta("sppw.load", self.id, "SPP West (SWPW) load, hourly mean", "MW", "1h", GROUP, extra=gen),
                   SeriesMeta("sppw.interchange.net", self.id, "SPP West (SWPW) net interchange (+ = net imports; "
                              "-NAI), hourly mean", "MW", "1h", GROUP, extra=ic)]
                + [SeriesMeta(f"sppw.tie.{k}.flow", self.id, f"SPP West (SWPW) - {n} tie flow (sign inferred: + = out "
                              "of SWPW), hourly mean", "MW", "1h", GROUP,
                              extra={**ic, "sign": "inferred, not published: the seven ties' sum tracks NAI (corr "
                                     "+0.51, 24 h on 2026-11-13) but doesn't close against it (DC ties to SPP East "
                                     "unlisted), so a single tie's sign is unverified"})
                   for k, n in TIE_NAMES.items()])

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        gen = parse_gen(ctx.get(GEN_URL).text)
        if not gen:
            raise FormatDrift("SPP gen-mix-swpw: no complete hour (NO-RESULT)")
        try:
            text = ctx.get(TREND_URL, timeout=60).text
        except Exception as e:                                    # noqa: BLE001 - generation still shows the box
            res.warnings.append(f"SPP interchange-trend-swpw not read ({e}); no interchange this run")
            text = None
        trend = parse_trend(text) if text is not None else {}
        for hr, row in sorted(gen.items()):
            for k in (*FUELS.values(), "total"):
                res.obs.append(Obs(f"sppw.gen.{k}", hr, round(row[k], 1), hr))
            res.obs.append(Obs("sppw.load", hr, round(row["load"], 1), hr))
        for hr, row in sorted(trend.items()):
            res.obs.append(Obs("sppw.interchange.net", hr, round(row["net_imports"], 1), hr))
            for k in TIES.values():
                res.obs.append(Obs(f"sppw.tie.{k}.flow", hr, round(row[k], 1), hr))
        if text is not None and not trend:
            res.warnings.append("SPP interchange-trend-swpw: no complete hour; no interchange this run")
        return res
