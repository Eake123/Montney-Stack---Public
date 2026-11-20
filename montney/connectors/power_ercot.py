"""Texas (ERCOT) generation by fuel, demand and DC-tie flows, hourly (PLAN 9 item 1: a box's generation < 2 h old).

Source: ERCOT's public dashboard JSONs (keyless; Imperva fronts the site, so a blocked request is a 403 HTML page),
``https://www.ercot.com/api/1/services/read/dashboards/<name>.json``, each re-issued every 5 min (``max-age=60``, an
``ETag`` that answers ``If-None-Match`` with 304):

  fuel-mix.json       ``data[<Central date>][<stamp>][<fuel>]["gen"]`` MW, 5-min, YESTERDAY + today (~155 KB).
                      Fuels: Coal and Lignite, Hydro, Nuclear, Other, Power Storage, Solar, Wind, Natural Gas, plus
                      Power Storage Charging / Discharging. "Power Storage" is already NET (charging + discharging,
                      signed): it is the battery fuel, and the other two are never added to it.
  supply-demand.json  ``data[]`` 5-min ``demand`` MW, today only; ``forecast: 1`` rows are forecasts (dropped).
  dc-tie-flows.json   ``data[]`` 5-min ``dcE`` / ``dcN`` (East, North: to SPP) and ``dcL`` / ``dcR`` (Laredo,
                      Railroad: to CFE Mexico) MW, today only, stamped ``updateTs`` (epoch seconds UTC). ERCOT's sign:
                      negative = import into ERCOT, positive = export. (Eagle Pass isn't on the dashboard.)
  system-wide-demand.json  hourly ``systemLoad`` (hour ending) for yesterday + today: read ONLY when the load series
                      has a gap before today's first 5-min hour (the first run, a runner that was down across Central
                      midnight, or - about once a day - yesterday's 23:00 hour, whose 23:55 row lands as supply-demand
                      rolls to the new day), so the last 24-48 h of load are always written. Its HE(h+1) matches the
                      mean of the 5-min demand over clock hour h to ~10 MW (2026-11-13).
                      The DC ties have no such file: a Central day's last hour may have no tie mean (never guessed).

Time: every stamp carries its own UTC offset (``2026-11-13 09:30:00-0600``; CDT is -0500) or is epoch UTC, so no zone
is guessed: the fall-back day's repeated 01:xx hour is two distinct stamps. Central prevailing time
(America/Chicago) only names the dashboards' days.

Requests: the three 5-min files every run (``interval: 15m``) = 12 req/h, each a conditional GET (the body and its ETag
are kept in ``<home>/cache/ercot``, and a 304 reuses them); system-wide-demand only after a gap (~1/day). One request
at a time, ``PAUSE_S`` apart. Lag (2026-11-13 15:46 UTC): fuel-mix and the ties ~6 min, demand ~1 min; an hour is
complete ~5 min after it ends.

Series (MW, freq 1h, ts = hour start, naive UTC; the mean of the hour's twelve 5-min rows, complete hours only):
  ercot.gen.<fuel>          gas, coal (coal and lignite), nuclear, hydro, wind, solar, battery (net: + discharge,
                            - charge), other
  ercot.gen.total           the sum of those eight (generation inside ERCOT, battery net, imports never in it)
  ercot.load                ERCOT demand (supply-demand.json; system-wide-demand's systemLoad only to fill a gap)
  ercot.tie.<tie>.flow      east, north, laredo, railroad: NET IMPORT into ERCOT (+ in), i.e. ERCOT's sign negated
  ercot.interchange.net     the four ties' sum, + = net imports into ERCOT (REGIONS §4)
Identity: gen.total + interchange.net = load to within ~1 MW (2026-11-13; tests). Not enforced in production.

Fail closed (FormatDrift, nothing stored): a body that isn't JSON, an unknown fuel or tie key (map it first), a fuel or
tie missing from every row, a stamp that doesn't parse (or disagrees with its epoch), a value outside a sane range, or
no complete generation hour. A blank or null cell skips its row (that hour isn't complete), never read as 0.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://www.ercot.com/api/1/services/read/dashboards/"
TZ = "America/Chicago"
FUELS = {"Natural Gas": "gas", "Coal and Lignite": "coal", "Nuclear": "nuclear", "Hydro": "hydro", "Wind": "wind",
         "Solar": "solar", "Power Storage": "battery", "Other": "other"}
IGNORED = ("Power Storage Charging", "Power Storage Discharging")      # already inside the net "Power Storage"
TIES = {"dcE": "east", "dcN": "north", "dcL": "laredo", "dcR": "railroad"}
TIE_NAMES = {"east": "East (SPP)", "north": "North (SPP)", "laredo": "Laredo (CFE)", "railroad": "Railroad (CFE)"}
TIE_META = ("repeatedHourFlag", "updateTs")
GROUP = "ERCOT / Texas generation, demand and DC ties"
STEP = timedelta(minutes=5)
PER_HOUR = 12
MAX_MW = 120_000                 # ERCOT's record demand is ~86 GW; installed capacity ~210 GW
MIN_LOAD_MW = 10_000             # ERCOT demand never falls near this (~25 GW minimum): a 0 is a blank, not data
MIN_GEN_MW = -2_000              # a fuel's small negative (station service) is real; -2 GW isn't
MAX_STORAGE_MW = 40_000          # ~21 GW of storage installed (2026-11)
MAX_TIE_MW = 2_000               # the four DC ties together are ~1.2 GW
PAUSE_S = 1.0                    # politeness: one request per second at most
EXTRA = {"tier": "hourly", "tz": "UTC offsets in the feed (Central prevailing time)", "lag": "~6 min (5-min rows)"}


def _stamp(s: str, epoch_ms=None) -> datetime:
    """'2026-11-13 09:30:00-0600' -> naive UTC; a given epoch (ms) must agree."""
    try:
        ts = datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        raise FormatDrift(f"ERCOT: stamp {s!r} doesn't parse") from None
    if epoch_ms is not None and datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).replace(tzinfo=None) != ts:
        raise FormatDrift(f"ERCOT: stamp {s!r} disagrees with its epoch {epoch_ms}")
    return ts


def _num(v, what: str, lo: float, hi: float) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise FormatDrift(f"ERCOT {what}: {v!r} isn't a number") from None
    if not lo <= x <= hi:
        raise FormatDrift(f"ERCOT {what}: {x:,.0f} MW outside {lo:,}..{hi:,}")
    return x


def _obj(payload, key: str, kind: type, what: str):
    v = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(v, kind):
        raise FormatDrift(f"ERCOT {what}: no `{key}` {kind.__name__}")
    return v


def parse_fuel_mix(payload) -> dict[datetime, dict[str, float]]:
    """{5-min UTC stamp: {fuel key: MW}} for rows with every fuel."""
    days = _obj(payload, "data", dict, "fuel-mix")
    out, seen = {}, set()
    for day, rows in days.items():
        if not isinstance(rows, dict):
            raise FormatDrift(f"ERCOT fuel-mix {day}: not a map of stamps")
        for s, cells in rows.items():
            ts = _stamp(s)
            unknown = set(cells) - set(FUELS) - set(IGNORED)
            if unknown:
                raise FormatDrift(f"ERCOT fuel-mix {s}: unknown fuel(s) {sorted(unknown)} (map them in "
                                  f"power_ercot.FUELS first)")
            seen |= set(cells)
            row = {}
            for name, key in FUELS.items():
                cell = cells.get(name)
                lo, hi = (-MAX_STORAGE_MW, MAX_STORAGE_MW) if key == "battery" else (MIN_GEN_MW, MAX_MW)
                v = _num(cell.get("gen") if isinstance(cell, dict) else None, f"fuel-mix {s} {name}", lo, hi)
                if v is not None:
                    row[key] = v
            if len(row) == len(FUELS):
                out[ts] = row
    missing = set(FUELS) - seen
    if missing:
        raise FormatDrift(f"ERCOT fuel-mix: fuel(s) {sorted(missing)} missing from every row")
    return out


def parse_demand(payload) -> dict[datetime, float]:
    out = {}
    for r in _obj(payload, "data", list, "supply-demand"):
        if "forecast" not in r or "timestamp" not in r:
            raise FormatDrift(f"ERCOT supply-demand: a row without forecast/timestamp: {r}")
        if r["forecast"] != 0:
            continue
        v = _num(r.get("demand"), f"supply-demand {r['timestamp']} demand", MIN_LOAD_MW, MAX_MW)
        if v is not None:
            out[_stamp(r["timestamp"], r.get("epoch"))] = v
    return out


def parse_ties(payload) -> dict[datetime, dict[str, float]]:
    """{5-min UTC stamp: {tie: MW, + = import into ERCOT}} for rows with all four ties."""
    out, seen = {}, set()
    for r in _obj(payload, "data", list, "dc-tie-flows"):
        unknown = set(r) - set(TIES) - set(TIE_META)
        if unknown:
            raise FormatDrift(f"ERCOT dc-tie-flows: unknown key(s) {sorted(unknown)} (a new tie? map it in TIES)")
        seen |= set(r)
        try:
            ts = datetime.fromtimestamp(int(r["updateTs"]), timezone.utc).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise FormatDrift(f"ERCOT dc-tie-flows: updateTs {r.get('updateTs')!r} doesn't parse") from None
        row = {}
        for k, tie in TIES.items():
            v = _num(r.get(k), f"dc-tie-flows {ts} {k}", -MAX_TIE_MW, MAX_TIE_MW)
            if v is not None:
                row[tie] = -v                                     # ERCOT: + export -> ours: + import
        if len(row) == len(TIES):
            out[ts] = row
    missing = set(TIES) - seen
    if missing:
        raise FormatDrift(f"ERCOT dc-tie-flows: tie(s) {sorted(missing)} missing from every row")
    return out


def parse_system_load(payload) -> dict[datetime, float]:
    """system-wide-demand.json -> {hour start (UTC): systemLoad MW} for previousDay + currentDay's actual hours."""
    out = {}
    for day in ("previousDay", "currentDay"):
        for r in _obj(_obj(payload, day, dict, "system-wide-demand"), "data", list, f"system-wide-demand {day}"):
            v = _num(r.get("systemLoad"), f"system-wide-demand {r.get('timestamp')}", MIN_LOAD_MW, MAX_MW)
            if v is not None:
                out[_stamp(r.get("timestamp", ""), r.get("epoch")) - timedelta(hours=1)] = v   # hour ending
    return out


def hourly(points: dict[datetime, dict[str, float]]) -> dict[datetime, dict[str, float]]:
    """5-min rows -> {UTC hour start: mean row} for hours with all twelve rows."""
    by: dict[datetime, list[dict]] = defaultdict(list)
    for ts, row in points.items():
        by[ts.replace(minute=0, second=0, microsecond=0)].append(row)
    return {h: {k: sum(r[k] for r in rows) / len(rows) for k in rows[0]}
            for h, rows in by.items() if len(rows) == PER_HOUR}


class ErcotGeneration(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        names = {"gas": "natural gas", "coal": "coal and lignite", "nuclear": "nuclear", "hydro": "hydro",
                 "wind": "wind", "solar": "solar", "battery": "storage, net (+ discharge, - charge)", "other": "other"}
        m = [SeriesMeta(f"ercot.gen.{k}", self.id, f"ERCOT {n} generation, hourly mean", "MW", "1h", GROUP,
                        extra=EXTRA) for k, n in names.items()]
        m.append(SeriesMeta("ercot.gen.total", self.id, "ERCOT generation, all fuels (storage net; no imports), "
                            "hourly mean", "MW", "1h", GROUP, extra=EXTRA))
        m.append(SeriesMeta("ercot.load", self.id, "ERCOT demand, hourly mean", "MW", "1h", GROUP, extra=EXTRA))
        m += [SeriesMeta(f"ercot.tie.{t}.flow", self.id, f"ERCOT DC tie {n}, net import into ERCOT (+ in, - out), "
                         "hourly mean", "MW", "1h", GROUP, extra=EXTRA) for t, n in TIE_NAMES.items()]
        m.append(SeriesMeta("ercot.interchange.net", self.id, "ERCOT net imports over the four published DC ties "
                            "(not Eagle Pass; + = into ERCOT), hourly mean", "MW", "1h", GROUP, extra=EXTRA))
        return m

    @staticmethod
    def _cache():
        d = paths.home() / "cache" / "ercot"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _json(self, ctx: Context, name: str, first: bool = False):
        """A dashboard file by conditional GET: a 304 reuses the body kept with its ETag."""
        if not first:
            time.sleep(PAUSE_S)
        f = self._cache() / f"{name}.json"
        kept = None
        try:
            kept = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except ValueError:
            kept = None
        head = {"If-None-Match": kept["etag"]} if kept and kept.get("etag") else None
        r = ctx.get(BASE + name + ".json", headers=head, allow=(304,))
        if r.status_code == 304 and kept:
            return kept["body"]
        try:
            body = r.json()
        except ValueError:
            raise FormatDrift(f"ERCOT {name}: not JSON ({r.headers.get('content-type')}; an Imperva block page?)") \
                from None
        if r.headers.get("etag"):
            f.write_text(json.dumps({"etag": r.headers["etag"], "body": body}), encoding="utf-8")
        return body

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        gen = hourly(parse_fuel_mix(self._json(ctx, "fuel-mix", first=True)))
        load = {h: r["v"] for h, r in hourly({t: {"v": v} for t, v in parse_demand(
            self._json(ctx, "supply-demand")).items()}).items()}
        ties = hourly(parse_ties(self._json(ctx, "dc-tie-flows")))
        if not gen:
            raise FormatDrift("ERCOT fuel-mix: no complete hour (NO-RESULT)")

        state_f = self._cache() / "state.json"
        try:
            last = datetime.fromisoformat(json.loads(state_f.read_text(encoding="utf-8"))["load_last"])
        except (OSError, ValueError, KeyError, TypeError):
            last = None
        first_5min = min(load, default=None)
        want = (first_5min or ctx.now.replace(minute=0, second=0, microsecond=0)) - timedelta(hours=1)
        if last is None or last < want:
            try:                                                  # a gap before today's 5-min hours: fill it
                for h, v in parse_system_load(self._json(ctx, "system-wide-demand")).items():
                    done = h + timedelta(hours=1) <= ctx.now      # an hour that has ended, never a future one
                    if done and (last is None or h > last) and (first_5min is None or h < first_5min):
                        load.setdefault(h, v)
            except FormatDrift:
                raise
            except Exception as e:                                # noqa: BLE001 - today's hours still stand
                res.warnings.append(f"ERCOT: system-wide-demand not read ({e}); load starts at today's first hour")

        for h, row in sorted(gen.items()):
            for k, v in row.items():
                res.obs.append(Obs(f"ercot.gen.{k}", h, round(v, 1), h))
            res.obs.append(Obs("ercot.gen.total", h, round(sum(row.values()), 1), h))
        for h, v in sorted(load.items()):
            res.obs.append(Obs("ercot.load", h, round(v, 1), h))
        for h, row in sorted(ties.items()):
            for t, v in row.items():
                res.obs.append(Obs(f"ercot.tie.{t}.flow", h, round(v, 1), h))
            res.obs.append(Obs("ercot.interchange.net", h, round(sum(row.values()), 1), h))
        if load:
            state_f.write_text(json.dumps({"load_last": max(load).isoformat()}), encoding="utf-8")
        return res
