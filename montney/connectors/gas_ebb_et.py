"""Interstate pipeline EBBs: daily scheduled quantities by point, summed per region and category (L-106 regions; research
docs/research/pipelines/energy_transfer_and_south.md).

FERC makes every interstate pipeline post Operationally Available Capacity by location for each nomination cycle, with
the Total Scheduled Quantity (TSQ, Dth/d) at every receipt and delivery point. Joined with the pipeline's NAESB location
file (state, county, location type, counterparty), the TSQs give each region's daily production receipts, LDC / power /
industrial deliveries, storage, LNG, and the flow across its borders, from the pipeline's own schedule.

This module holds this family's engine (rows -> categories -> series) and the **Energy Transfer** connector
(`gas_ebb_et`). WBI and Alliance US are in gas_ebb_bakken.py, the Texas LNG feeds in gas_ebb_lng_tx.py. What every EBB
family shares (state -> region, Dth -> MMcf/d, per-host pacing, the weekly location cache, the override / border and
no-state checks) is in gas_ebb_common.py.

## The engine (`Row` -> `aggregate`)

A `Row` is one OA line: loc id, name, flow (R receipt / D delivery), TSQ Dth, state, NAESB type, counterparty.
1. **Category** (`classify`), first match wins:
   - a point override in the spec's `params` (`points: {<loc>: {cat: ..., region: ...}}`); `cat: skip` drops it;
   - VIR / PPT (paper pools) and imbalance points: dropped;
   - LNG type or name: `lng`; storage type (STR/STA) or "STORAGE": `storage_inj` (D) / `storage_wd` (R);
   - receipts: WHD/PLT/GTH → `production`; LDC/END → `int_in`; anything else → the pipe's `receipt_default` when set
     (Transwestern: `production`, because even its INT-typed receipts are plant outlets; true pipeline receipts are
     overrides), else INT → `int_in` (`production` if the name says PLANT), untyped → `production` when the
     name/counterparty looks like a plant, gatherer or well, else `int_in`;
   - deliveries: LDC → `ldc`; END/IND/COM → `power` if the name or counterparty says POWER / GENERAT / ELECTRIC /
     ENERGY CENTER / SRP / PUBLIC SERVICE, `ldc` if the counterparty is a gas utility, else `industrial`; INT → `ldc`
     if the counterparty is a gas utility and not a pipeline, else `int_out`; WHD/PLT/GTH or FUEL → `other` (field
     and plant use, not demand); untyped → power / ldc / int_out by the same words, else `other`.
   The words misread a few big points (SoCal Needles and El Paso Blanco read as utilities by name; plants called
   "... PLANT" whose owner isn't a utility), so each spec carries those as overrides. The overrides are the contract:
   **an override point missing from the payload raises FormatDrift** (unless `optional: true`).
2. **Region**: the override's `region`, else the state's region (gas_ebb_common.state_regions: the region files'
   US-XX members); a state in no region (OH, IN, IL, ...) counts only toward the borders. A row with gas and neither
   fails the pipe-day (`require_states`, below).
3. **Series** (MMcf/d = Dth / 1037, freq D, `day: gas`, ts = gas day; every value is *scheduled*):
   - `ebb.<pipe>.<region>.<category>` for the (region, category) pairs the spec lists in `emit`; categories are
     production, ldc, power, industrial, storage_inj, storage_wd, lng, export_canada, import_canada, export_mexico,
     int_in, int_out, other, and `net` = receipts − deliveries over the region's counted rows (+ = the pipe carries gas
     out of the region along its own line);
   - `ebb.<pipe>.border.<name>`: either the sum of named points (`points`, `flow`), or a state mass balance
     (`state_net: [states]`, receipts − deliveries of every row in those states, `sign` to orient it). A border
     uses every row, `exclude`d ones included, because it measures physical flow;
   - `ebb.<pipe>.<region>.<group>`: a counterparty group (`groups`, e.g. WBI's deliveries to Northern Border);
   - a point's own series (`points: {<loc>: {series: <id>}}`).
   `exclude` (loc ids) leaves points out of the region sums when another source already stores them.

## Energy Transfer (`gas_ebb_et`): Transwestern, Panhandle, Trunkline, Rover, Florida Gas

All ET interstates run the same "Messenger" iPost site (`/ipost/...?asset=<CODE>`): public, no login, no captcha, no
robots.txt (404). Two OA layouts, both exported as CSV (`f=csv&extension=csv&max=ALL`), **one request per pipe and
gas day**:
- `by_location` (PEPL, TGC, ROVER): `/ipost/capacity/operationally-available-by-location?asset=X&gasDay=MM/DD/YYYY`;
  columns Loc, Loc Name, Loc Purp Desc, Loc/QTI, DC, OPC, TSQ, OAC, Loc Zn, IT, Flow Ind, State, County, Operator, ...
  Every location comes back (zeros included), so override points don't vanish.
- `oa` (TW, FGT): `/ipost/capacity/operationally-available?asset=X&gasDay=..&searchType=ALL&locType=ALL&locZone=ALL`;
  columns Loc, Loc Zn, Loc Name, Loc Purp Desc, Loc/QTI, Flow Ind, DC, OPC, TSQ (FGT: TSQ (Rec) + TSQ (Del)), OAC, ...
  No state: it comes from the location file. `searchType=ALL` lists all active locations (TW: ~300 rows, 32 KB),
  not just the ones with nominations, again so override points stay. FGT's segment rows have no Loc: skipped.
Traps:
- The CSV carries **neither the gas day nor the cycle**. The connector always passes `gasDay`; the site then serves
  that day's latest cycle (checked 11-13: 11/12 → "Final" after ~08:20 CT on TW, ~10:06 on PEPL). So as_of is the
  fetch hour, not a post time, and a later fetch of the same day (a later cycle) supersedes an earlier one.
- A not-yet-posted day comes back as a header-only CSV: a warning, not an error.
- Location files (`/ipost/locations/index?asset=X&gasDay=&f=csv&extension=csv`, NAESB layout, 30-320 KB) are read at
  most weekly into `<home>/cache/ebb_locs_<ASSET>.json` (parsed, never the raw file). A loc missing from the cache
  triggers one early refresh a day; still missing, a row with gas and no state fails that pipe-day (`require_states`;
  `unlocated_ok` names known no-state points), and groups / borders are written only when a matching row exists.
- Panhandle's Ojibway point (`UNION`) is already stored by gas_pepl_ojibway (`pepl.ojibway.deliveries`), so it is
  `exclude`d from `ebb.pepl.umw.*`; `ebb.pepl.border.oh_mi` (Panhandle into Michigan) still includes it, because that
  gas does enter Michigan before it leaves at Ojibway.
Days: every run asks for today's gas day (the Central-time date; its latest cycle) and, once a day after
`yesterday_after_ct` (11:00 CT, when yesterday's Final is up), yesterday's. The "yesterday done" date sits in
`<home>/cache/<source id>_state.json`, saved only after a successful run.
Requests: 5 per run (one per pipe), + 5 once a day for yesterday, + 4 location files a week: ~65 a day at 2 h.
Requests to one host are spaced ≥ `PACE` seconds (PEPL, TGC and ROVER share peplmessenger).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save
from montney.connectors.gas_ebb_common import (  # noqa: F401  (LOC_MAX_AGE, state_regions: this module's API too)
    LOC_MAX_AGE, loc_cache_due, loc_cache_lacks, loc_cache_read, loc_cache_write, mmcfd, paced_request,
    require_located, require_present, state_regions,
)
from montney.store import Obs, SeriesMeta

CENTRAL = ZoneInfo("America/Chicago")
PACE = 1.1                    # seconds between requests to one host (tests set 0)
CATS = ("production", "ldc", "power", "industrial", "storage_inj", "storage_wd", "lng", "export_canada",
        "import_canada", "export_mexico", "int_in", "int_out", "other", "net")
CAT_LABEL = {"production": "production receipts", "ldc": "LDC deliveries", "power": "power-plant deliveries",
             "industrial": "industrial deliveries", "storage_inj": "storage injection", "storage_wd": "storage withdrawal",
             "lng": "LNG feedgas deliveries", "export_canada": "exports to Canada", "import_canada": "imports from Canada",
             "export_mexico": "exports toward Mexico", "int_in": "receipts from other pipelines",
             "int_out": "deliveries to other pipelines", "other": "other deliveries (field / plant use)",
             "net": "net receipts (receipts − deliveries; + = gas carried out of the region on this pipe)"}

POWER = re.compile(r"POWER|GENERAT|ELECTRIC|ENERGY CENT|\bSRP\b|PUBLIC SERVICE", re.I)
LDC = re.compile(r"GAS CO|GAS CORP|UTILIT|SOUTHWEST GAS|UNS GAS|ATMOS|CITY OF|TOWN OF|MONTANA-DAKOTA|NORTHERN STATES"
                 r"|CENTERPOINT|DTE GAS|CONSUMERS ENERGY|SEMCO|MICHIGAN GAS|XCEL|BLACK HILLS ENERGY|CITY GATE", re.I)
PIPE = re.compile(r"PIPELINE|PIPE LINE|TRANSMISSION|NATURAL GAS CO|GAS PIPELINE|EXPRESS|STORAGE", re.I)
PROD = re.compile(r"PLANT|PROCESS|GATHER|MIDSTREAM|FIELD SERV|PRODUC|\bWELL\b", re.I)


def num(s) -> float:
    s = str(s or "").replace(",", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError as e:
        raise FormatDrift(f"not a number: {s!r}") from e


def ct_now(ctx: Context) -> datetime:
    return ctx.now.replace(tzinfo=timezone.utc).astimezone(CENTRAL).replace(tzinfo=None)


def ct_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=CENTRAL).astimezone(timezone.utc).replace(tzinfo=None)


def gas_days(ctx: Context, state: dict, after_hour: int) -> tuple[list[date], date | None]:
    """[today] (the Central-time date: its latest cycle), plus yesterday once a day after `after_hour` CT.
    Returns (days, the yesterday to mark done once the run succeeds, or None)."""
    now = ct_now(ctx)
    today = now.date()
    y = today - timedelta(days=1)
    if now.hour >= after_hour and state.get("yesterday") != y.isoformat():
        return [today, y], y
    return [today], None


def paced(ctx: Context, method: str, url: str, **kw) -> httpx.Response:
    """ctx.request with ≥ PACE s between requests to one host (gas_ebb_common.paced_request; the runner's per-host
    semaphore caps concurrency, not spacing)."""
    return paced_request(ctx, method, url, PACE, **kw)


# ------------------------------------------------------------------ the engine
@dataclass
class Row:
    loc: str
    name: str
    flow: str           # "R" receipt, "D" delivery
    dth: float
    state: str = ""
    ltype: str = ""     # NAESB Loc Type Ind
    cp: str = ""        # counterparty (Up/Dn Name or Operator)


def classify(r: Row, cfg: dict) -> str:
    ov = (cfg.get("points") or {}).get(r.loc) or {}
    if ov.get("cat"):
        return ov["cat"]
    t = (r.ltype or "").upper()
    who = f"{r.name} | {r.cp}"
    if t in ("VIR", "PPT") or "IMBALANCE" in r.name.upper():
        return "skip"
    if t == "LNG" or re.search(r"\bLNG\b", who, re.I):
        return "lng" if r.flow == "D" else "int_in"
    if t in ("STR", "STA") or re.search("STORAGE", who, re.I):
        return "storage_inj" if r.flow == "D" else "storage_wd"
    if r.flow == "R":
        if t in ("WHD", "PLT", "GTH"):
            return "production"
        if t in ("LDC", "END"):
            return "int_in"
        if cfg.get("receipt_default"):          # TW: even its INT-typed receipts are plant outlets (Zia II, S. Eddy)
            return cfg["receipt_default"]
        if t == "INT":
            return "production" if re.search(r"PLANT", r.name, re.I) else "int_in"
        return "production" if PROD.search(who) else "int_in"
    if t == "LDC":
        return "ldc"
    if t in ("END", "IND", "COM"):
        return "power" if POWER.search(who) else "ldc" if LDC.search(r.cp) else "industrial"
    if t == "INT":
        return "ldc" if LDC.search(r.cp) and not PIPE.search(r.cp) else "int_out"
    if t in ("WHD", "PLT", "GTH") or re.search(r"\bFUEL\b", r.name, re.I):
        return "other"
    if POWER.search(who):
        return "power"
    if LDC.search(r.cp) and not PIPE.search(r.cp):
        return "ldc"
    if PIPE.search(who):
        return "int_out"
    return "other"


def region_of(r: Row, cfg: dict) -> str | None:
    ov = (cfg.get("points") or {}).get(r.loc) or {}
    return ov.get("region") or state_regions().get((r.state or "").strip().upper())


def require_states(rows: list[Row], cfg: dict, where: str) -> None:
    """The no-state guard (gas_ebb_common.require_located; Reviewer on #478): FormatDrift if a row carrying gas
    (TSQ > 0) has no state and no override region. `unlocated_ok` (a regex on the point name) lets known no-state rows
    through, e.g. FGT's compressor-station fuel points the location file lacks."""
    pts = cfg.get("points") or {}
    require_located(((r.loc, r.name, r.dth, bool((r.state or "").strip() or (pts.get(r.loc) or {}).get("region")))
                     for r in rows), cfg.get("unlocated_ok"), where)


def check_overrides(rows: list[Row], cfg: dict, where: str) -> None:
    """FormatDrift if an override point (unless `optional`) or a border's listed point is missing from the payload."""
    need = {loc for loc, ov in (cfg.get("points") or {}).items() if not (ov or {}).get("optional")}
    for b in (cfg.get("borders") or {}).values():
        need |= set(b.get("points") or [])
    require_present(need, {r.loc for r in rows}, f"{where}: override / border point(s)")


def aggregate(pipe: str, rows: list[Row], cfg: dict) -> dict[str, float]:
    """{series id: Dth/d} for one pipe and gas day (see the module docstring)."""
    emit = cfg.get("emit") or {}
    excl = set(cfg.get("exclude") or [])
    sums: dict[tuple[str, str], float] = {}
    touched: set[tuple[str, str]] = set()
    out: dict[str, float] = {}
    for r in rows:
        ov = (cfg.get("points") or {}).get(r.loc) or {}
        if ov.get("series"):
            want = ov.get("flow") or "D"
            if r.flow == want:
                out[ov["series"]] = out.get(ov["series"], 0.0) + r.dth
        if r.loc in excl:
            continue
        cat = classify(r, cfg)
        reg = region_of(r, cfg)
        if cat == "skip" or reg not in emit:
            continue
        if cat not in CATS:
            raise FormatDrift(f"{pipe}: unknown category {cat!r} for {r.loc}")
        touched |= {(reg, cat), (reg, "net")}
        sums[(reg, cat)] = sums.get((reg, cat), 0.0) + r.dth
        sums[(reg, "net")] = sums.get((reg, "net"), 0.0) + (r.dth if r.flow == "R" else -r.dth)
        for g, gc in (cfg.get("groups") or {}).items():
            if (gc.get("region", reg) == reg and r.flow == gc.get("flow", "D")
                    and re.search(gc["counterparty"], f"{r.name} | {r.cp}", re.I)):
                out[f"ebb.{pipe}.{reg}.{g}"] = out.get(f"ebb.{pipe}.{reg}.{g}", 0.0) + r.dth
    for reg, cats in emit.items():
        for c in cats:
            if (reg, c) in touched:
                out[f"ebb.{pipe}.{reg}.{c}"] = sums.get((reg, c), 0.0)
    # A group (written above, per matching row) or a border is written only when a matching row exists, never a
    # default 0 (Reviewer on #478).
    for name, b in (cfg.get("borders") or {}).items():
        if b.get("points"):
            pts, fl = set(b["points"]), b.get("flow", "D")
            hits = [r.dth for r in rows if r.loc in pts and r.flow == fl]
        else:
            sts = {s.upper() for s in b["state_net"]}
            hits = [(r.dth if r.flow == "R" else -r.dth) for r in rows
                    if (r.state or "").upper() in sts and classify(r, cfg) != "skip"]
        if hits:
            out[f"ebb.{pipe}.border.{name}"] = sum(hits) * float(b.get("sign", 1))
    return out


def declared(source: str, group: str, pipes: dict[str, dict], extra: str = "scheduled") -> list[SeriesMeta]:
    out: list[SeriesMeta] = []
    for pipe, cfg in pipes.items():
        title = cfg.get("title", pipe.upper())
        for reg, cats in (cfg.get("emit") or {}).items():
            for c in cats:
                out.append(SeriesMeta(f"ebb.{pipe}.{reg}.{c}", source, f"{title} {reg}: {CAT_LABEL[c]} ({extra})",
                                      "MMcf/d", "D", group, day="gas"))
            for g, gc in (cfg.get("groups") or {}).items():
                if gc.get("region", reg) == reg:
                    out.append(SeriesMeta(f"ebb.{pipe}.{reg}.{g}", source, f"{title} {reg}: {gc.get('label', g)} "
                                          f"({extra})", "MMcf/d", "D", group, day="gas"))
        for name, b in (cfg.get("borders") or {}).items():
            out.append(SeriesMeta(f"ebb.{pipe}.border.{name}", source, f"{title} border: {b.get('label', name)} "
                                  f"({extra})", "MMcf/d", "D", group, day="gas"))
        for loc, ov in (cfg.get("points") or {}).items():
            if (ov or {}).get("series"):
                out.append(SeriesMeta(ov["series"], source, f"{title} {ov.get('label', loc)} ({extra})", "MMcf/d",
                                      "D", group, day="gas"))
    return out


def to_obs(values: dict[str, float], day: date, as_of: datetime) -> list[Obs]:
    ts = datetime(day.year, day.month, day.day)
    return [Obs(sid, ts, mmcfd(v), as_of) for sid, v in sorted(values.items())]


def read_csv(text: str, need: set[str], where: str) -> list[dict[str, str]]:
    """Rows of a CSV whose header must hold `need`; [] for a header-only file. HTML or a changed header: FormatDrift."""
    t = text.lstrip("﻿")
    if t.lstrip().startswith("<"):
        raise FormatDrift(f"{where}: HTML instead of CSV")
    rd = csv.DictReader(io.StringIO(t))
    head = {(h or "").strip() for h in (rd.fieldnames or [])}
    if not need <= head:
        raise FormatDrift(f"{where}: header lacks {sorted(need - head)} (got {sorted(head)[:12]})")
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items() if k} for r in rd]


# ------------------------------------------------------------------ NAESB location files (weekly cache)
NAESB_LOC = {"Loc", "Loc Name", "Loc St Abbrev", "Loc Type Ind", "Up/Dn Name"}


def parse_locations(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    """{loc: [state, county, NAESB type, counterparty]}; the first (current) row of a loc wins."""
    out: dict[str, list[str]] = {}
    for r in rows:
        if r["Loc"] and r["Loc"] not in out:
            out[r["Loc"]] = [r.get("Loc St Abbrev", ""), r.get("Loc Cnty", ""), r.get("Loc Type Ind", ""),
                             r.get("Up/Dn Name", "")]
    return out


def cached_locations(ctx: Context, key: str, fetch, need: set[str] | None = None) -> dict[str, list[str]]:
    """The location map from `<home>/cache/ebb_locs_<key>.json` (gas_ebb_common's location cache), refreshed by
    `fetch()` (-> {loc: [...]}) when older than LOC_MAX_AGE, or when it lacks a loc in `need` and wasn't refreshed
    within a day."""
    name = f"ebb_locs_{key}"
    locs, got = loc_cache_read(name, "locs")
    if loc_cache_due(got, ctx.now) or loc_cache_lacks(locs, need, got, ctx.now):
        locs = fetch()
        if not locs:
            raise FormatDrift(f"{key}: empty location file")
        loc_cache_write(name, "locs", ctx.now, locs)
    return locs


# ------------------------------------------------------------------ Energy Transfer
BY_LOC = {"Loc", "Loc Name", "Flow Ind", "TSQ", "State", "County", "Operator"}
OA = {"Loc", "Loc Name", "Flow Ind"}


def et_rows(table: list[dict[str, str]], locs: dict[str, list[str]]) -> tuple[list[Row], int]:
    """Rows of an ET OA CSV (either layout) joined to the location map; (rows, rows without a state). Rows without a
    state are kept (state ""): `require_states` fails the pipe-day if one carries gas."""
    out, skipped = [], 0
    tsq = [c for c in ("TSQ", "TSQ (Rec)", "TSQ (Del)") if table and c in table[0]]
    if table and not tsq:
        raise FormatDrift("ET OA: no TSQ column")
    for r in table:
        loc = r["Loc"].strip()
        fl = r["Flow Ind"].strip().upper()[:1]
        if not loc or fl not in ("R", "D"):
            continue                                   # FGT's segment / group rows
        st, _cty, lt, cp = locs.get(loc, ["", "", "", ""])
        st = r.get("State", "").strip() or st
        skipped += not st                              # kept: require_states decides (Reviewer on #478)
        out.append(Row(loc, r["Loc Name"].strip(), fl, sum(num(r[c]) for c in tsq), st, lt,
                       r.get("Operator", "").strip() or cp))
    return out, skipped


class EbbEnergyTransfer(Connector):
    """Energy Transfer interstates (see the module docstring). params: `pipes: {<pipe>: {host, asset, layout, ...}}`."""

    GROUP = "Interstate EBB: Energy Transfer"

    def pipes(self) -> dict[str, dict]:
        return self.spec.get("params", {}).get("pipes", {})

    def declared_series(self) -> list[SeriesMeta]:
        return declared(self.id, self.GROUP, self.pipes())

    def _locations(self, ctx: Context, cfg: dict, need: set[str]) -> dict[str, list[str]]:
        if not cfg.get("locations"):
            return {}
        url = f"{cfg['host']}/ipost/locations/index"

        def fetch():
            r = paced(ctx, "GET", url, params={"asset": cfg["asset"], "gasDay": "", "f": "csv", "extension": "csv"},
                      timeout=120)
            return parse_locations(read_csv(r.text, NAESB_LOC, f"{cfg['asset']} locations"))
        return cached_locations(ctx, cfg["asset"], fetch, need)

    def _oa(self, ctx: Context, cfg: dict, day: date) -> list[dict[str, str]]:
        p = {"asset": cfg["asset"], "gasDay": day.strftime("%m/%d/%Y"), "f": "csv", "extension": "csv", "max": "ALL"}
        if cfg.get("layout") == "oa":
            url = f"{cfg['host']}/ipost/capacity/operationally-available"
            p.update({"searchType": "ALL", "locType": "ALL", "locZone": "ALL"})
            need = OA
        else:
            url = f"{cfg['host']}/ipost/capacity/operationally-available-by-location"
            need = BY_LOC
        r = paced(ctx, "GET", url, params=p, timeout=120)
        return read_csv(r.text, need, f"{cfg['asset']} OA {day}")

    def fetch(self, ctx: Context) -> FetchResult:
        params = self.spec.get("params", {})
        state_key = f"{self.id}_state"
        state = cache_load(state_key)
        days, mark = gas_days(ctx, state, int(params.get("yesterday_after_ct", 11)))
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        res = FetchResult(meta=self.declared_series())
        drift: list[str] = []
        for pipe, cfg in self.pipes().items():
            for day in days:
                table = self._oa(ctx, cfg, day)
                if not table:
                    res.warnings.append(f"{cfg['asset']} {day}: not posted yet (header-only CSV)")
                    continue
                locs = self._locations(ctx, cfg, {r["Loc"] for r in table if r["Loc"]})
                rows, _ = et_rows(table, locs)
                try:
                    check_overrides(rows, cfg, f"{cfg['asset']} {day}")
                    require_states(rows, cfg, f"{cfg['asset']} {day}")
                except FormatDrift as e:          # this pipe-day writes nothing; the other pipes still do
                    res.warnings.append(f"FormatDrift: {e}")
                    drift.append(str(e))
                    continue
                res.obs += to_obs(aggregate(pipe, rows, cfg), day, as_of)
        if drift and not res.obs:
            raise FormatDrift("; ".join(drift))
        if mark is not None and not drift:          # a skipped pipe-day retries yesterday's final on the next run
            state["yesterday"] = mark.isoformat()
            cache_save(state_key, state)
        return res
