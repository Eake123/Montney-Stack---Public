"""AESO ETS public reports (keyless CSV): unit-level generation, outages, wind/solar, event log.

`type: python` connector for the reports the declarative engine can't read (CSV with blocks,
local timestamps). One class; `params.report` picks the report:

  csd_units       Current Supply & Demand asset tables -> aeso.unit.<id>.{tng,mc}  (MW, 5min)
  outage_daily    Daily Outage report                  -> aeso.outage.<fuel>      (MW, D)
  outage_monthly  Monthly Outage report                -> aeso.outage.monthly.<fuel> (MW, M)
  wind_solar      Wind + solar forecast CSVs           -> aeso.{wind,solar}.{st,lt}.forecast.*
  event_log       AIES Event Log                       -> Events (kind alert)
  tx_outages      Transmission outages on interties    -> Events (kind outage, refs power.intertie.*)

ETS times are Alberta local clock (America/Edmonton, `params.tz`). `LocalClock` converts to UTC
and resolves a repeated fall-back hour if the zone has one (see its docstring on 2026-11-01).

Terms: aeso.ca/legal, personal use (docs/research/DECISIONS.md). Research: docs/research/power.md.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult, slug
from montney.store import Event, Obs, SeriesMeta

ETS = "http://ets.aeso.ca/ets_web/ip/Market/Reports"
WS = "http://ets.aeso.ca/Market/Reports/Manual/Operations/prodweb_reports/wind_solar_forecast"
URLS = {
    "csd_units": f"{ETS}/CSDReportServlet?contentType=csv",
    "outage_daily": f"{ETS}/DailyOutageReportServlet?contentType=csv",
    "outage_monthly": f"{ETS}/MonthlyOutageForecastReportServlet?contentType=csv",
    "event_log": f"{ETS}/RealTimeShiftReportServlet?contentType=csv",
}
TX = "http://ets.aeso.ca/outage_reports"
TX_REPORTS = [  # (key, page, intertie column); the 7-day approved plan wins over the 24-month list
    ("7day", "qryOpPlanTransmissionTable_1.html", "Interconnection"),
    ("24month", "Longterm_Critical_Outages.html", "Affected Intertie"),
]
INTERTIE_REFS = {"AB-BC": "power.intertie.bc", "AB-MATL": "power.intertie.matl", "AB-SK": "power.intertie.sk"}
NO_ATC_RE = re.compile(r"does not affect", re.I)
WS_FILES = {("wind", "st"): "wind_rpt_shortterm.csv", ("wind", "lt"): "wind_rpt_longterm.csv",
            ("solar", "st"): "solar_rpt_shortterm.csv", ("solar", "lt"): "solar_rpt_longterm.csv"}

# CSD fuel table names -> our fuel slugs. Gas sub-types have inline headers in the asset block.
GAS_HEADERS = {"Simple Cycle": "simple_cycle", "Cogeneration": "cogeneration",
               "Combined Cycle": "combined_cycle", "Gas Fired Steam": "gas_fired_steam"}
FUEL_TABLE = {"SIMPLE CYCLE": "simple_cycle", "COGENERATION": "cogeneration",
              "COMBINED CYCLE": "combined_cycle", "GAS FIRED STEAM": "gas_fired_steam",
              "HYDRO": "hydro", "ENERGY STORAGE": "energy_storage", "SOLAR": "solar",
              "WIND": "wind", "OTHER": "other"}
# Order of the headerless blocks after the gas block (titles only exist in the HTML version).
HEADERLESS_ORDER = ["hydro", "energy_storage", "solar", "wind", "other"]
GAS_FUELS = set(GAS_HEADERS.values())

EEA_RE = re.compile(r"\bEEA\b|energy emergency|grid alert|load shed|rotating outage", re.I)
EEA_END_RE = re.compile(r"terminat|ended|cancel|lifted", re.I)
WARN_RE = re.compile(r"offline|trip|forced|curtail", re.I)
ONLINE_RE = re.compile(r"\bon-?line\b", re.I)
UNIT_STATUS_RE = re.compile(r"^(?P<unit>.+?)\s+(?:is\s+)?(?:off-?line|on-?line|tripped|trip)\b", re.I)
EEA_TTL = timedelta(hours=12)   # fallback if no "EEA ... terminated" line follows
INFO_TTL = timedelta(hours=24)  # info/warn lines drop out of "ongoing" after a day


class CsdValidationError(ValueError):
    """The CSD block layout didn't match the fuel totals; refuse rather than mislabel units."""


# ----------------------------------------------------------------- time
class LocalClock:
    """Local wall clock -> naive UTC, resolving a repeated fall-back hour if the zone has one.

    Recent tzdata (Alberta's change landed in 2026c; this project pins tzdata >= 2026.5) moves
    America/Edmonton to permanent UTC-6 on 2026-11-01, so there is no repeated hour from then on;
    AESO's files agree (01:50 -> 02:00).
    The fold handling stays for older tzdata and for any zone that still falls back.
    """

    def __init__(self, tz: str = "America/Edmonton"):
        self.tz = ZoneInfo(tz)

    def candidates(self, local: datetime) -> list[datetime]:
        out = []
        for fold in (0, 1):
            u = local.replace(tzinfo=self.tz, fold=fold).astimezone(timezone.utc).replace(tzinfo=None)
            if u not in out:
                out.append(u)
        return out

    def near(self, local: datetime, now: datetime) -> datetime:
        """Single stamp (report 'Last Update', event log): the candidate closest to `now`
        that isn't in the future (with 5 minutes of slack for clock skew)."""
        c = self.candidates(local)
        ok = [u for u in c if u <= now + timedelta(minutes=5)]
        return max(ok) if ok else min(c)

    def series(self, locals_: list[datetime]) -> list[datetime]:
        """Chronological local stamps: once the clock steps back inside an ambiguous hour,
        later ambiguous stamps take the second (standard-time) occurrence."""
        out, prev, second = [], None, False
        for t in locals_:
            c = self.candidates(t)
            if prev is not None and t <= prev and len(c) > 1:
                second = True
            out.append(c[-1] if (second and len(c) > 1) else c[0])
            prev = t
        return out


def alberta_day(now_utc: datetime, tz: ZoneInfo) -> date:
    """Alberta calendar day of a naive-UTC instant, from the zone (bundled tzdata), never a fixed offset:
    AGENTS.md; L-029's fixed UTC-6 is for SQL rollups only. From 2026-11-01 days end at 06:00Z."""
    return now_utc.replace(tzinfo=timezone.utc).astimezone(tz).date()


# ---------------------------------------------------------------- parsing
def _blocks(text: str) -> list[list[list[str]]]:
    """Split a CSV with blank-line separated blocks into parsed blocks."""
    out, cur = [], []
    for line in text.splitlines():
        if line.strip():
            cur.append(line)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [[[c.strip() for c in r] for r in csv.reader(b)] for b in out]


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


_ASSET_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<id>[A-Za-z0-9]+)\)\s*(?P<flag>[*^]*)\s*$")


def parse_csd(text: str) -> tuple[datetime, list[dict]]:
    """CSD CSV -> (local last-update time, units).

    Layout: summary, fuel table, interchange, then ONE gas block with inline
    `<center><b>Simple Cycle</b></center>` headers, then headerless blocks in the order
    HYDRO, ENERGY STORAGE, SOLAR, WIND, OTHER. Fuel for headerless blocks comes from block
    order, verified by summing unit MC per block against the fuel table. Any mismatch raises
    CsdValidationError.
    """
    m = re.search(r"Last Update\s*:\s*([A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2})", text)
    if not m:
        raise CsdValidationError("CSD: 'Last Update' line not found")
    updated = datetime.strptime(m.group(1), "%b %d, %Y %H:%M")
    blocks = _blocks(text)

    fuel_mc: dict[str, float] = {}
    gas_i = None
    for i, b in enumerate(blocks):
        if any(r and r[0].upper() in FUEL_TABLE for r in b) and any(r and r[0].upper() == "TOTAL" for r in b) \
                and not fuel_mc and len(b[0]) >= 3:
            fuel_mc = {FUEL_TABLE[r[0].upper()]: _num(r[1]) for r in b if r and r[0].upper() in FUEL_TABLE}
        if any(r and "<center>" in r[0].lower() for r in b):
            gas_i = i
            break
    if not fuel_mc or gas_i is None:
        raise CsdValidationError("CSD: fuel table or gas asset block not found")

    units: list[dict] = []

    def add(row: list[str], fuel: str) -> None:
        am = _ASSET_RE.match(row[0])
        if not am or len(row) < 3:
            raise CsdValidationError(f"CSD: unexpected asset row {row!r}")
        units.append({"id": am.group("id").upper(), "name": am.group("name").strip(),
                      "flag": am.group("flag"), "fuel": fuel,
                      "mc": _num(row[1]), "tng": _num(row[2]),
                      "dcr": _num(row[3]) if len(row) > 3 else None})

    fuel = None
    for r in blocks[gas_i]:
        if not r or not r[0]:
            continue
        if "<center>" in r[0].lower():
            title = re.sub(r"<[^>]+>", "", r[0]).strip()
            if title not in GAS_HEADERS:
                raise CsdValidationError(f"CSD: unknown gas header {title!r}")
            fuel = GAS_HEADERS[title]
            continue
        if r[0].upper() == "ASSET":
            continue
        if fuel is None:
            raise CsdValidationError("CSD: asset row before any gas header")
        add(r, fuel)

    rest = blocks[gas_i + 1:]
    if len(rest) != len(HEADERLESS_ORDER):
        raise CsdValidationError(
            f"CSD: expected {len(HEADERLESS_ORDER)} headerless blocks after gas, got {len(rest)}")
    for fuel, b in zip(HEADERLESS_ORDER, rest):
        for r in b:
            if r and r[0]:
                add(r, fuel)

    sums: dict[str, float] = {}
    for u in units:
        sums[u["fuel"]] = sums.get(u["fuel"], 0.0) + (u["mc"] or 0.0)
    bad = {f: (sums.get(f), mc) for f, mc in fuel_mc.items() if abs((sums.get(f) or 0.0) - (mc or 0.0)) > 0.5}
    if bad:
        raise CsdValidationError(f"CSD: unit MC sums don't match fuel table {bad}")
    return updated, units


def parse_outage_daily(text: str) -> list[tuple[datetime, dict[str, float]]]:
    rows = list(csv.reader(io.StringIO(text)))
    hi = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == "date"), None)
    if hi is None:
        raise ValueError("daily outage: header row not found")
    cols = [slug(c) for c in rows[hi]]
    out = []
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d-%b-%Y")
        except ValueError:
            continue
        out.append((d, {c: v for c, x in zip(cols[1:], r[1:]) if (v := _num(x)) is not None}))
    return out


def parse_outage_monthly(text: str) -> list[tuple[datetime, dict[str, float]]]:
    rows = list(csv.reader(io.StringIO(text)))
    hi = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == "month"), None)
    if hi is None:
        raise ValueError("monthly outage: header row not found")
    cols = [slug(c) for c in rows[hi]]
    out = []
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%b %Y")
        except ValueError:
            continue
        out.append((d, {c: v for c, x in zip(cols[1:], r[1:]) if (v := _num(x)) is not None}))
    return out


def parse_ws(text: str) -> list[dict]:
    """Wind/solar forecast CSV -> rows with local `t` and min/likely/max/actual/mcr."""
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for r in rd:
        try:
            t = datetime.strptime(r["Forecast Transaction Date"].strip(), "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        out.append({"t": t, "min": _num(r.get("Min")), "likely": _num(r.get("Most Likely")),
                    "max": _num(r.get("Max")), "actual": _num(r.get("Actual")),
                    "mcr": _num(r.get("MCR"))})
    return out


def parse_event_log(text: str) -> list[tuple[datetime, str]]:
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 2:
            continue
        try:
            t = datetime.strptime(r[0].strip(), "%m/%d/%Y %H:%M")
        except ValueError:
            continue
        msg = re.sub(r"\s+", " ", r[1]).strip()
        if msg:
            out.append((t, msg))
    return out


# --------------------------------------------------------------- connector
class AesoEts(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params", {})
        self.report = p.get("report", "")
        if self.report not in (*URLS, "wind_solar", "tx_outages"):
            raise ValueError(f"{self.id}: unknown params.report {self.report!r}")
        self.fuels = set(p.get("fuels", sorted(GAS_FUELS)))
        self.extra_units = {u.upper() for u in p.get("extra_units", [])}
        self.unit_names: dict[str, str] = {k.upper(): v for k, v in p.get("unit_names", {}).items()}
        self.horizons = p.get("horizons", ["st", "lt"])
        self.refs = p.get("refs", {})  # event-log keyword -> network feature ids
        self.clock = LocalClock(p.get("tz", "America/Edmonton"))
        # csd_units: warn about any asset of these fuels that the map neither shows nor lists as unlocated
        w = p.get("watch_assets") or {}
        self.watch_fuels = set(w.get("fuels", []))
        self.watch_files = [paths.REPO_ROOT / f for f in w.get("network", [])]

    # ---------------------------------------------------------------- meta
    def declared_series(self) -> list[SeriesMeta]:
        if self.report == "outage_daily":
            g = "AESO / Outages (daily)"
            fuels = ("sc", "cogen", "cc", "gfs", "hydro", "wind", "solar", "energy_storage",
                     "biomass_and_other", "mbo", "load")
            return ([SeriesMeta(f"aeso.outage.{c}", self.id, f"AESO {c.replace('_', ' ')} MW out", "MW", "D", g)
                     for c in fuels]
                    + [SeriesMeta(f"aeso.outage.{c}.realized", self.id,
                                  f"AESO {c.replace('_', ' ')} MW out (realized: last value known on the day)",
                                  "MW", "D", g) for c in fuels])
        if self.report == "outage_monthly":
            g = "AESO / Outages (monthly)"
            fuels = ("sc", "cogen", "cc", "gfs", "hydro", "wind", "solar", "energy_storage",
                     "biomass_and_other", "mbo")
            return ([SeriesMeta(f"aeso.outage.monthly.{c}", self.id, f"AESO {c.replace('_', ' ')} MW out (monthly)",
                                "MW", "M", g) for c in fuels]
                    + [SeriesMeta(f"aeso.outage.monthly.{c}.realized", self.id,
                                  f"AESO {c.replace('_', ' ')} MW out (monthly, realized: last value known in the month)",
                                  "MW", "M", g) for c in fuels])
        if self.report == "wind_solar":
            out = []
            for kind in ("wind", "solar"):
                g = f"AESO / {kind.title()} forecast"
                for h in self.horizons:
                    freq = "10min" if h == "st" else "1h"
                    hl = "short-term" if h == "st" else "7-day"
                    for q in ("min", "likely", "max"):
                        out.append(SeriesMeta(f"aeso.{kind}.{h}.forecast.{q}", self.id,
                                              f"AESO {kind} {hl} forecast ({q})", "MW", freq, g))
                out.append(SeriesMeta(f"aeso.{kind}.actual", self.id, f"AESO {kind} actual", "MW", "10min", g))
            return out
        if self.report == "csd_units":
            out = []
            for uid, name in self.unit_names.items():
                out += self._unit_meta(uid, name, "")
            return out
        return []

    def _unit_meta(self, uid: str, name: str, fuel: str) -> list[SeriesMeta]:
        g = "AESO / Units"
        x = {"asset_id": uid, "fuel": fuel} if fuel else {"asset_id": uid}
        lid = uid.lower()
        return [SeriesMeta(f"aeso.unit.{lid}.tng", self.id, f"{name} ({uid}) net gen", "MW", "5min", g, dict(x)),
                SeriesMeta(f"aeso.unit.{lid}.mc", self.id, f"{name} ({uid}) max capability", "MW", "5min", g, dict(x))]

    # --------------------------------------------------------------- fetch
    def fetch(self, ctx: Context) -> FetchResult:
        return getattr(self, f"_fetch_{self.report}")(ctx)

    def _fetch_csd_units(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        updated, units = parse_csd(ctx.get(URLS["csd_units"]).text)
        ts = self.clock.near(updated, ctx.now)
        res.warnings += self.unknown_assets(units)
        for u in units:
            if u["fuel"] not in self.fuels and u["id"] not in self.extra_units:
                continue
            res.meta += self._unit_meta(u["id"], self.unit_names.get(u["id"], u["name"]), u["fuel"])
            lid = u["id"].lower()
            for k in ("tng", "mc"):
                if u[k] is not None:
                    res.obs.append(Obs(f"aeso.unit.{lid}.{k}", ts, u[k], ts))
        return res

    def known_assets(self) -> set[str]:
        """AESO asset ids the map accounts for: mapped (`aeso_assets`) or listed in the file's `unlocated`."""
        known: set[str] = set()
        for f in self.watch_files:
            fc = json.loads(f.read_text(encoding="utf-8"))
            known |= {a for feat in fc.get("features", []) for a in feat["properties"].get("aeso_assets", [])}
            known |= {u["asset"] for u in fc.get("unlocated", [])}
        return known

    def unknown_assets(self, units: list[dict]) -> list[str]:
        """Warnings for watched-fuel assets AESO lists that the map doesn't account for (new farms).
        Fails closed: an unreadable or empty watch list is itself a warning, never silence."""
        if not self.watch_fuels:
            return []
        try:
            known = self.known_assets()
        except (OSError, ValueError, KeyError) as e:
            return [f"asset watch list unreadable ({e}); new {'/'.join(sorted(self.watch_fuels))} farms can't be detected"]
        if not known:
            return ["asset watch list is empty; new farms can't be detected"]
        return [f"new {u['fuel']} asset {u['id']} ({u['name']}, MC {u['mc']:g} MW) is neither mapped in network/power "
                f"nor listed as unlocated" for u in units if u["fuel"] in self.watch_fuels and u["id"] not in known]

    def _fetch_outage_daily(self, ctx: Context) -> FetchResult:
        """Every fetch is a vintage (as_of = fetch time, forecast tier). The row for the Alberta day the fetch
        falls in is also written as `aeso.outage.<fuel>.realized` with as_of = ts (daily tier, kept forever).
        The last fetch of the day overwrites it, so it ends as the latest vintage with as_of before the end of
        that day, which is the L-036 definition. The Alberta day comes from the zone (06:00Z day ends from 2026-11).
        A day on which no fetch ran gets no realized row: missing, never a post-day value."""
        res = FetchResult(meta=self.declared_series())
        today = alberta_day(ctx.now, self.clock.tz)
        for d, vals in parse_outage_daily(ctx.get(URLS["outage_daily"]).text):
            for c, v in vals.items():
                res.obs.append(Obs(f"aeso.outage.{c}", d, v, ctx.now))
                if d.date() == today:
                    res.obs.append(Obs(f"aeso.outage.{c}.realized", d, v, d))
        return res

    def _fetch_outage_monthly(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        rows = parse_outage_monthly(ctx.get(URLS["outage_monthly"]).text)
        # Realized (L-036/L-037): the latest vintage known by month end. The live report starts at NEXT month
        # (fetched 2026-10-31 23:04 local, first row "Nov 2026"), so a month stops being listed before it begins.
        # Writing the earliest listed month on every fetch (as_of = ts, last write wins) therefore leaves each
        # month with the last vintage that listed it, which is the latest one before its end. If the report ever
        # lists the current month, the same rule keeps updating it until the month ends.
        today = alberta_day(ctx.now, self.clock.tz)
        # Earliest listed month that hasn't ended: a late report roll that still lists an ended month must not
        # overwrite that month's realized value with a post-end vintage.
        first = min((d for d, _ in rows if (d.year, d.month) >= (today.year, today.month)), default=None)
        for d, vals in rows:
            for c, v in vals.items():
                res.obs.append(Obs(f"aeso.outage.monthly.{c}", d, v, ctx.now))
                if d == first:
                    res.obs.append(Obs(f"aeso.outage.monthly.{c}.realized", d, v, d))
        return res

    def _fetch_wind_solar(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        for kind in ("wind", "solar"):
            for h in self.horizons:
                rows = parse_ws(ctx.get(f"{WS}/{WS_FILES[(kind, h)]}").text)
                for r, ts in zip(rows, self.clock.series([r["t"] for r in rows])):
                    if h == "st" and r["actual"] is not None:
                        res.obs.append(Obs(f"aeso.{kind}.actual", ts, r["actual"], ts))
                    if ts < ctx.now - timedelta(minutes=10):
                        continue  # past forecast slots were stored by earlier fetches
                    for q in ("min", "likely", "max"):
                        if r[q] is not None:
                            res.obs.append(Obs(f"aeso.{kind}.{h}.forecast.{q}", ts, r[q], ctx.now))
        return res

    def _fetch_tx_outages(self, ctx: Context) -> FetchResult:
        """Intertie-flagged transmission outages (7-day approved plan + 24-month significant list)
        as list/ticker Events. The flag does NOT mean transfer capability is cut (rows often say
        "does not affect the Alberta ATC"), so these never colour lines; severity is info when the
        text says so, warn otherwise. Both reports are fetched in full each time, so the result
        is the complete current set: `event_scopes` withdraws outages AESO drops or reschedules.
        """
        res = FetchResult()
        seen: dict[str, Event] = {}
        for key, page, col in TX_REPORTS:  # any failure raises: never withdraw on a partial set
            link = tx_csv_link(ctx.get(f"{TX}/{page}").text)
            csv_url = f"{TX}/{link}"
            for r in parse_tx_csv(ctx.get(csv_url).text):
                tie = r.get(col, "").upper()
                if tie not in INTERTIE_REFS:
                    continue
                try:
                    t0 = datetime.strptime(r["From"], "%d-%b-%y %H:%M")
                    t1 = datetime.strptime(r["To"], "%d-%b-%y %H:%M")
                except (KeyError, ValueError):
                    res.warnings.append(f"tx_outages: unparsed dates in {r!r}"[:200])
                    continue
                native = hashlib.sha1(f"{r.get('Element')}|{r['From']}|{r['To']}".encode()).hexdigest()[:12]
                eid = f"{self.id}:{native}"
                if eid in seen:  # same outage on both lists: keep the approved 7-day row
                    seen[eid].extra["also_in"] = key
                    continue
                activity = r.get("Scheduled Activity", "")
                comments = r.get("Date/Time Comments") or r.get("Duration", "")
                seen[eid] = Event(
                    id=eid, source=self.id, kind="outage",
                    title=f"{tie} {r.get('Type') or 'Outage'}: {r.get('Element', '')}"[:200],
                    body=f"{activity} ({comments}). Owner: {r.get('Owner', '')}. "
                         "Intertie flag only; not a confirmed ATC change.",
                    url=f"{TX}/{page}",
                    starts=self.clock.candidates(t0)[0], ends=self.clock.candidates(t1)[0],
                    refs=[INTERTIE_REFS[tie]],
                    severity="info" if NO_ATC_RE.search(activity) else "warn",
                    extra={"report": key, "intertie": tie, "owner": r.get("Owner", ""),
                           "type": r.get("Type", ""), "element": r.get("Element", ""),
                           "duration": comments, "local_from": r["From"], "local_to": r["To"],
                           "csv": csv_url})
        res.events = list(seen.values())
        res.event_scopes = [(self.id, "outage")]
        return res

    def _fetch_event_log(self, ctx: Context) -> FetchResult:
        """Log lines -> alert Events, each with an `ends` so nothing stays "ongoing" forever:

        * EEA declaration: critical, ends at the next EEA-ended line, else starts + 12 h.
        * EEA ended/terminated/cancelled: info (the all-clear), ends starts + 24 h.
        * "<unit> offline/trip": warn, ends at the next "<unit> online" line, else starts + 24 h.
        * anything else: info, ends starts + 24 h.
        """
        res = FetchResult()
        rows = sorted(((self.clock.near(t, ctx.now), t, msg)
                       for t, msg in parse_event_log(ctx.get(URLS["event_log"]).text)),
                      key=lambda r: r[0])
        for i, (ts, t, msg) in enumerate(rows):
            later = rows[i + 1:]
            native = hashlib.sha1(f"{t:%Y-%m-%d %H:%M}|{msg}".encode()).hexdigest()[:12]
            unit = _unit_key(msg)
            if EEA_RE.search(msg) and EEA_END_RE.search(msg):
                severity, ends = "info", ts + INFO_TTL
            elif EEA_RE.search(msg):
                severity = "critical"
                ends = next((lt for lt, _, lm in later if EEA_RE.search(lm) and EEA_END_RE.search(lm)),
                            ts + EEA_TTL)
            elif WARN_RE.search(msg):
                severity = "warn"
                ends = next((lt for lt, _, lm in later
                             if unit and ONLINE_RE.search(lm) and _unit_key(lm) == unit), ts + INFO_TTL)
            else:
                severity, ends = "info", ts + INFO_TTL
            refs = sorted({r for k, ids in self.refs.items() if k.lower() in msg.lower() for r in ids})
            if EEA_RE.search(msg):
                refs = sorted(set(refs) | {"power.region.ab"})
            res.events.append(Event(
                id=f"{self.id}:{native}", source=self.id, kind="alert", title=msg[:200], body=msg,
                url=URLS["event_log"].replace("?contentType=csv", ""), starts=ts, ends=ends,
                published=ts, refs=refs, severity=severity,
                extra={"local_time": f"{t:%Y-%m-%d %H:%M}", **({"unit": unit} if unit else {})}))
        return res


def parse_tx_csv(text: str) -> list[dict]:
    return [{k.strip(): (v or "").strip() for k, v in r.items() if k}
            for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff")))]


def tx_csv_link(page_html: str) -> str:
    """The timestamped CSV link on an outage page; AESO writes it with a backslash."""
    m = re.search(r'href="(csvData[\\/][^"]+\.csv)"', page_html, re.I)
    if not m:
        raise ValueError("transmission outage page: CSV link not found")
    return m.group(1).replace("\\", "/")


def _unit_key(msg: str) -> str | None:
    """'Keephills #2 Online' / 'Keephills 2 offline.' -> 'keephills 2'."""
    m = UNIT_STATUS_RE.match(msg)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group("unit").replace("#", " ")).strip().lower()
