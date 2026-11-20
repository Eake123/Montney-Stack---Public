"""Interstate pipeline EBBs (FERC Operationally Available Capacity) summed by region and category: the shared part.

Research: docs/research/pipelines/east_and_rockies.md. Every interstate pipeline posts, per nomination cycle, the
scheduled quantity (TSQ) at each receipt and delivery point. Joined to the pipeline's location master (state, NAESB
location type, counterparty) or a hand map, the points sum to region totals by category.

## Shared by all three EBB families

gas_ebb_km (Kinder Morgan), gas_ebb_et + gas_ebb_bakken + gas_ebb_lng_tx (Energy Transfer, WBI, Alliance US, Texas LNG)
and the EbbConnector boards below (gas_ebb_enbridge; gas_ebb_east: PNGTS, Iroquois, Transco, Millennium) use:
- `state_regions()`: US state -> region, from the region files' `US-XX` members (`montney.regions`);
- `mmcfd()`: Dth -> MMcf/d at DTH_PER_MMCF = 1,037 Btu/cf;
- per-host pacing: `host_wait()` / `paced_request()` keep ≥ a family's gap (PAUSE here, `gas_ebb_et.PACE`,
  KM's `pause_s`) between two requests to one host; the runner's per-host semaphore caps concurrency, not spacing;
- the weekly location-file cache (`loc_cache_read` / `loc_cache_due` / `loc_cache_lacks` / `loc_cache_write`): one
  `<home>/cache/<name>.json` per board, `{"fetched": <naive UTC>, <key>: {point: ...}}`, refreshed when older than
  LOC_MAX_AGE (7 days), or early (at most once a day) when it lacks a point that carries gas. Each family keeps its file
  names (`ebb_loc_<source>_<pipe>`, `ebb_locs_<ASSET>`, `ebb_km_loc_<CODE>`) and its map's shape;
- `require_present()`: an overridden, named or border point (or segment) missing from the posting is FormatDrift;
- **the no-state guard** `require_located()` (Reviewer on #478): a point with scheduled gas (TSQ > 0), no state and no
  override region is FormatDrift, because it would drop silently out of every region total (and a group or state
  border would read low or 0). Known no-state points pass by name with the board's `unlocated_ok` regex. A failure
  stops what the family's other FormatDrift stops: an ET pipe-day, a WBI / Alliance US run, a KM board for that run
  (the other boards still write), a whole EbbConnector source run.

## The EbbConnector boards (gas_ebb_enbridge, gas_ebb_east)

One module per EBB family fetches and parses; this part classifies and sums.

**Series** (MMcf/d, freq D, `day: gas`, ts = the gas day, as_of = the cycle's post time; all scheduled):
- `ebb.<pipe>.<region>.<category>` for every category with at least one point of that pipe in that region, plus
  `ebb.<pipe>.<region>.net` = deliveries − receipts over the non-internal points: the pipe's net import into the
  region (less fuel and linepack change, ~1–2 %). Interconnects between two pipes inside a region cancel when both
  pipes are summed.
- Named border points from the spec (`named:`), e.g. `ebb.pngts.east_hereford.receipts`.
Raw points are never stored. A series is written only from posted points: a missing named point is FormatDrift, never
a 0.

**Categories** (CATEGORIES): deliveries are ldc (LDC), power (EGN, or END whose name/counterparty reads like a
generator: POWER_RE), industrial (IND, other END), lng (LNG), storage_inj (STR), export_canada (override), int_out (INT
and anything else); receipts are production (WHD/PLT/GTH/RNG/BIO), storage_wd (STR), lng_in (LNG regas: Everett, the
Massachusetts Bay buoys), import_canada (override), int_in (INT and anything else). STA / VIR / PPT points and
segments are internal and dropped.

**Region** = the region whose `members` hold the point's state, via `state_regions()`. Points outside a board's
`regions:` are dropped.

**Overrides** (spec `params.boards[].overrides`, keyed by point id, optionally `"<id>:D"` / `"<id>:R"` for one
direction): `state`, `type` (NAESB location type), `region`, `category`, `cp` (counterparty), `drop: true`. They fix
meters coded in the wrong state (Tetco's Con Ed Manhattan meter is in NJ), misleading types (Millennium's Sanford and
Laser receipts are Pennsylvania gas, not New York production) and carry the whole location map for boards with no
location file (Iroquois). An overridden point that disappears from the posting raises FormatDrift, so does a missing
named point, and so does a receipt or delivery with TSQ > 0 and no state (location file or override `state`), no
override `region` and no `drop`, unless its name matches the board's `unlocated_ok`.

**Cycles** (EbbConnector.fetch): each run takes the board's newest cycle of the current gas day, skipping the download
when the board allows a cheap check and the cycle is unchanged (state in `<home>/cache/ebb_<source>.json`). Once a day,
after FINAL_HOUR Central, it also reads the previous gas day's last cycle (the final scheduled quantities); a board
whose final() marks the pull `extra["provisional"]` (NWP before its Storage Realignment cycle) is read again next run.
Location files are cached in `<home>/cache/ebb_loc_<source>_<pipe>.json` for LOC_MAX_AGE; when a point with gas is
missing from the file and the file is over a day old, it is read again once in that run before the guard decides.
Requests to one host are spaced PAUSE seconds apart.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save, local_to_utc
from montney.store import Obs, SeriesMeta

CENTRAL = ZoneInfo("America/Chicago")       # the NAESB clock every EBB posts in
DTH_PER_MMCF = 1037.0                       # Dth (= MMBtu) per MMcf at 1,037 Btu/cf, as pnw_gas.BTU_PER_CF
GAS_DAY_START_HOUR = 9                      # 09:00 CT
FINAL_HOUR = 10                             # previous gas day's last cycle is read after 10:00 CT
LOC_MAX_AGE = timedelta(days=7)             # location files are read at most weekly ...
LOC_RETRY = timedelta(days=1)               # ... or early, at most once a day, when one lacks a point with gas
PAUSE = 1.0                                 # seconds between two requests to one host (tests set 0)
GROUP = "Interstate pipelines (EBB, scheduled)"

CATEGORIES = ("production", "ldc", "power", "industrial", "storage_inj", "storage_wd", "lng", "lng_in", "export_canada",
              "import_canada", "int_in", "int_out", "net")
DELIVERY_CATS = {"ldc", "power", "industrial", "storage_inj", "lng", "export_canada", "int_out"}
RECEIPT_CATS = {"production", "storage_wd", "lng_in", "import_canada", "int_in"}
INTERNAL_TYPES = {"STA", "VIR", "PPT"}
PRODUCTION_TYPES = {"WHD", "PLT", "GTH", "RNG", "BIO"}
POWER_RE = re.compile(r"POWER|GENERAT|ENERGY CENTER|ELECTRIC|COGEN|CALPINE|\bCPV\b|\bNRG\b|KLEEN|DYNEGY|CONSTELLATION|"
                      r"VISTRA|PLANT\b|STATION\b", re.I)
CAT_LABEL = {"production": "production receipts", "ldc": "LDC (city-gate) deliveries", "power": "power-plant deliveries",
             "industrial": "industrial / end-user deliveries", "storage_inj": "storage injections",
             "storage_wd": "storage withdrawals", "lng": "LNG deliveries (feedgas, liquefaction)",
             "lng_in": "LNG regas receipts", "export_canada": "deliveries to Canada",
             "import_canada": "receipts from Canada", "int_in": "receipts from other pipelines",
             "int_out": "deliveries to other pipelines",
             "net": "net into the region (deliveries − receipts)"}


# ------------------------------------------------------------------ shared by all three families
def mmcfd(dth: float) -> float:
    """Dth/d -> MMcf/d at 1,037 Btu/cf."""
    return dth / DTH_PER_MMCF


@lru_cache(maxsize=1)
def state_regions() -> dict[str, str]:
    """{"NY": "nyc", "MA": "upne", ...} from the region files' US members (a later region file wins a shared state)."""
    from montney import regions
    out: dict[str, str] = {}
    for rid, reg in regions.load().items():
        for m in reg.members:
            if m.startswith("US-"):
                out[m[3:]] = rid
    return out


# ---- per-host pacing
_last_hit: dict[str, float] = {}            # host -> time.monotonic() of its last request, process-wide


def host_wait(url: str, gap: float, last: dict[str, float] | None = None, sleep=None) -> str:
    """Sleep until `gap` seconds have passed since the last request to `url`'s host (as stamped in `last`, by default
    the process-wide map); returns the host. A host never seen doesn't wait. The caller stamps `last[host]`."""
    last = _last_hit if last is None else last
    host = httpx.URL(url).host or ""
    if host in last:
        wait = last[host] + gap - time.monotonic()
        if wait > 0:
            (sleep or time.sleep)(wait)
    return host


def paced_request(ctx: Context, method: str, url: str, gap: float | None = None, **kw) -> httpx.Response:
    """ctx.request with ≥ `gap` s (default PAUSE) from the end of the last request to the same host."""
    host = host_wait(url, PAUSE if gap is None else gap)
    try:
        return ctx.request(method, url, **kw)
    finally:
        _last_hit[host] = time.monotonic()


# ---- the weekly location-file cache
def loc_cache_read(name: str, key: str) -> tuple[dict, datetime | None]:
    """(the location map stored under `key` in `<home>/cache/<name>.json`, {} if none; when it was fetched, or None)."""
    c = cache_load(name)
    return c.get(key) or {}, (datetime.fromisoformat(c["fetched"]) if c.get("fetched") else None)


def loc_cache_due(got: datetime | None, now: datetime, max_age: timedelta = LOC_MAX_AGE) -> bool:
    """True when the cached file is missing or older than `max_age`."""
    return got is None or now - got > max_age


def loc_cache_lacks(locs: dict, need: Iterable[str] | None, got: datetime | None, now: datetime) -> bool:
    """True when the cached map lacks a point of `need` and wasn't fetched within LOC_RETRY: worth one early refresh."""
    return bool(need and set(need) - set(locs)) and (got is None or now - got > LOC_RETRY)


def loc_cache_write(name: str, key: str, now: datetime, locs: dict) -> None:
    cache_save(name, {"fetched": now.isoformat(), key: locs})


# ---- fail-closed checks
def require_present(need: Iterable, seen: Iterable, what: str, tail: str = "missing from the payload") -> None:
    """FormatDrift "<what> [<gone>] <tail>" when an item of `need` (an overridden, named or border point, a segment) is
    not in `seen`: the series it feeds would read low or 0."""
    gone = sorted(set(need) - set(seen))
    if gone:
        raise FormatDrift(f"{what} {gone} {tail}")


def require_located(points: Iterable[tuple[str, str, float, bool]], unlocated_ok: str | None, where: str) -> None:
    """The no-state guard (Reviewer on #478). `points`: (point id, name, TSQ Dth, located) for every receipt and
    delivery, located = it has a state (location file or override) or an override region. FormatDrift when one with
    TSQ > 0 isn't located: it would drop out of every region total, and a group or state border would read low or 0 (a
    location-refresh miss once wrote WBI's deliveries to Northern Border as 0, not ~1,190). `unlocated_ok` (a regex on
    the name, case-insensitive) lets known no-state points through, e.g. FGT's compressor-station fuel points."""
    ok = re.compile(unlocated_ok, re.I) if unlocated_ok else None
    bad = sorted({loc for loc, name, dth, located in points
                  if dth > 0 and not located and not (ok and ok.search(name or ""))})
    if bad:
        raise FormatDrift(f"{where}: {len(bad)} point(s) with scheduled gas but no state (location file out of date?): "
                          f"{bad[:8]}")


# ------------------------------------------------------------------ EbbConnector: time helpers
def num(v) -> float:
    s = str(v if v is not None else "").replace(",", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def central(now_utc: datetime) -> datetime:
    """Naive UTC -> naive Central wall time."""
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(CENTRAL).replace(tzinfo=None)


def ct_to_utc(dt: datetime) -> datetime:
    return local_to_utc(dt, CENTRAL)


def current_gas_day(now_utc: datetime) -> date:
    return (central(now_utc) - timedelta(hours=GAS_DAY_START_HOUR)).date()


def last_final_day(now_utc: datetime) -> date:
    """The newest gas day whose final cycle is due (it ended at 09:00 CT; read after FINAL_HOUR)."""
    return (central(now_utc) - timedelta(hours=FINAL_HOUR)).date() - timedelta(days=1)


# ------------------------------------------------------------------ EbbConnector: points and classification
@dataclass
class Pt:
    """One posted location quantity. `dirn`: D (delivery), R (receipt) or S (segment / flow past)."""
    loc: str
    name: str
    dirn: str
    dth: float
    sub: str = ""                    # segment direction etc., part of the key for S points
    state: str = ""
    ltype: str = ""
    cp: str = ""

    @property
    def key(self) -> str:
        return f"{self.loc}:{self.dirn}" + (f":{self.sub}" if self.sub else "")


@dataclass
class Pull:
    """One cycle of one board."""
    day: date
    cycle: str
    posted: datetime                 # naive UTC
    pts: list[Pt]
    extra: dict = field(default_factory=dict)


def _override(ov: dict, p: Pt) -> dict:
    return {**(ov.get(p.loc) or {}), **(ov.get(f"{p.loc}:{p.dirn}") or {})}


def _overrides(board: dict) -> dict:
    return {str(k): v for k, v in (board.get("overrides") or {}).items()}


def located(p: Pt, o: dict) -> bool:
    """A receipt / delivery the guard accepts: dropped by override, or placed by a state or an override region."""
    return bool(o.get("drop") or o.get("region") or str(o.get("state", p.state) or "").strip())


def unlocated(board: dict, pts: list[Pt]) -> set[str]:
    """Ids of the receipts / deliveries with gas that no state, override region or `drop` places (`unlocated_ok`
    names aside): what require_located would refuse."""
    ov = _overrides(board)
    ok = re.compile(board["unlocated_ok"], re.I) if board.get("unlocated_ok") else None
    return {p.loc for p in pts if p.dirn in ("D", "R") and p.dth > 0 and not located(p, _override(ov, p))
            and not (ok and ok.search(p.name))}


def classify(p: Pt, o: dict, wanted: set[str]) -> tuple[str, str] | None:
    """(region, category) of a non-segment point, or None when it's internal, outside `wanted` or unplaceable."""
    if o.get("drop") or p.dirn not in ("D", "R"):
        return None
    ltype = str(o.get("type", p.ltype) or "").upper()
    region = o.get("region") or state_regions().get(str(o.get("state", p.state) or "").upper())
    if not region or region not in wanted:
        return None
    if o.get("category"):
        return region, o["category"]
    if ltype in INTERNAL_TYPES:
        return None
    if p.dirn == "D":
        if ltype == "LDC":
            return region, "ldc"
        if ltype == "EGN":
            return region, "power"
        if ltype == "IND":
            return region, "industrial"
        if ltype == "END":
            return region, "power" if POWER_RE.search(f"{p.name} {o.get('cp', p.cp)}") else "industrial"
        if ltype == "LNG":
            return region, "lng"
        if ltype == "STR":
            return region, "storage_inj"
        return region, "int_out"
    if ltype in PRODUCTION_TYPES:
        return region, "production"
    if ltype == "STR":
        return region, "storage_wd"
    if ltype == "LNG":
        return region, "lng_in"
    return region, "int_in"


def aggregate(board: dict, pts: list[Pt], warnings: list[str]) -> dict[str, float]:
    """{series id: MMcf/d} for one cycle of one board: region × category sums, nets and named points. FormatDrift for
    a missing overridden or named point, or a point with gas and no state (require_located)."""
    pipe = board["pipe"]
    ov = _overrides(board)
    wanted = set(board.get("regions") or state_regions().values())
    require_present({k.split(":")[0] for k in ov}, {p.loc for p in pts}, f"{pipe}: overridden point(s)",
                    "not in the posting")
    require_located(((p.loc, p.name, p.dth, located(p, _override(ov, p))) for p in pts if p.dirn in ("D", "R")),
                    board.get("unlocated_ok"), pipe)
    sums: dict[tuple[str, str], float] = {}
    for p in pts:
        rc = classify(p, _override(ov, p), wanted)
        if rc is None:
            continue
        region, cat = rc
        if cat not in CATEGORIES or cat == "net":
            raise FormatDrift(f"{pipe}: override category {cat!r} for {p.loc} is not one of {CATEGORIES}")
        sums[(region, cat)] = sums.get((region, cat), 0.0) + p.dth
        sign = 1.0 if cat in DELIVERY_CATS else -1.0
        sums[(region, "net")] = sums.get((region, "net"), 0.0) + sign * p.dth
    out = {f"ebb.{pipe}.{r}.{c}": mmcfd(v) for (r, c), v in sums.items()}
    by_key: dict[str, float] = {}
    for p in pts:
        by_key[p.key] = by_key.get(p.key, 0.0) + p.dth
    for sid, spec in (board.get("named") or {}).items():
        need = [str(k) for k in (*spec.get("plus", []), *spec.get("minus", []))]
        require_present(need, by_key, f"{pipe}: named point(s)", f"for {sid} not in the posting")
        out[sid] = mmcfd(sum(by_key[str(k)] for k in spec.get("plus", []))
                         - sum(by_key[str(k)] for k in spec.get("minus", [])))
    return out


# ------------------------------------------------------------------ the connector skeleton
class EbbConnector(Connector):
    """A source with `params.boards: [{pipe, regions, overrides, named, unlocated_ok, ...}]`. Subclasses implement
    latest(), final() and (optionally) locations()."""
    FAMILY = ""
    LABEL = ""                       # e.g. "Algonquin"; boards may set `label:`

    # ---- requests: spaced per host
    def _req(self, ctx: Context, method: str, url: str, **kw) -> httpx.Response:
        return paced_request(ctx, method, url, PAUSE, **kw)

    def get(self, ctx: Context, url: str, **kw) -> httpx.Response:
        return self._req(ctx, "GET", url, **kw)

    def post(self, ctx: Context, url: str, **kw) -> httpx.Response:
        return self._req(ctx, "POST", url, **kw)

    # ---- to implement
    def latest(self, ctx: Context, board: dict, bstate: dict) -> Pull | None:
        """The newest cycle of the current gas day, or None when the board's cheap check says nothing changed."""
        raise NotImplementedError

    def final(self, ctx: Context, board: dict, day: date) -> Pull | None:
        """The last cycle of gas day `day` (None: the board can't give it)."""
        return None

    def locations(self, ctx: Context, board: dict) -> dict[str, tuple[str, str, str]]:
        """{point id: (state, NAESB type, counterparty)} from the board's location file; {} when it has none."""
        return {}

    # ---- shared
    def boards(self) -> list[dict]:
        b = self.spec.get("params", {}).get("boards") or []
        if not b:
            raise ValueError(f"{self.id}: params.boards is empty")
        return b

    def _locs(self, ctx: Context, board: dict, warnings: list[str],
              need: set[str] | None = None) -> dict[str, tuple[str, str, str]]:
        """The board's location map from the weekly cache, or a fresh file when the cache is due, or lacks a point of
        `need` and is over a day old (loc_cache_lacks). A failed refresh falls back to the cached file."""
        name = f"ebb_loc_{self.id}_{board['pipe']}"
        cached, got = loc_cache_read(name, "points")
        if cached and not (loc_cache_due(got, ctx.now) or loc_cache_lacks(cached, need, got, ctx.now)):
            return {k: tuple(v) for k, v in cached.items()}
        try:
            pts = self.locations(ctx, board)
        except (httpx.HTTPError, FormatDrift) as e:
            if cached:
                warnings.append(f"{board['pipe']}: location file failed ({e}); using the cached one")
                return {k: tuple(v) for k, v in cached.items()}
            raise
        if pts:
            try:
                loc_cache_write(name, "points", ctx.now, {k: list(v) for k, v in pts.items()})
            except OSError as e:
                warnings.append(f"{board['pipe']}: location cache not written: {e}")
        return pts

    def series_meta(self, sid: str, board: dict) -> SeriesMeta:
        label = board.get("label") or board["pipe"].title()
        named = (board.get("named") or {}).get(sid)
        if named:
            text = f"{label}: {named.get('label', sid)} (scheduled)"
        else:
            _, _, region, cat = sid.split(".")
            text = f"{label}: {region} {CAT_LABEL.get(cat, cat)} (scheduled)"
        return SeriesMeta(sid, self.id, text, "MMcf/d", "D", GROUP, day="gas")

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for b in self.boards():
            for r in b.get("regions") or []:
                out.append(self.series_meta(f"ebb.{b['pipe']}.{r}.net", b))
            out += [self.series_meta(sid, b) for sid in (b.get("named") or {})]
        return out

    @staticmethod
    def _join(pulls: list[Pull], locs: dict[str, tuple[str, str, str]]) -> None:
        for pull in pulls:
            for p in pull.pts:
                if p.loc in locs and not p.state:
                    p.state, p.ltype, p.cp = locs[p.loc]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        state_name = f"ebb_{self.id}"
        state = cache_load(state_name)
        due = last_final_day(ctx.now)
        metas: dict[str, SeriesMeta] = {}
        for board in self.boards():
            bstate = state.setdefault(board["pipe"], {})
            locs = self._locs(ctx, board, res.warnings)
            pulls = []
            latest = self.latest(ctx, board, bstate)
            if latest is not None:
                pulls.append(latest)
            if bstate.get("final") != due.isoformat():
                fin = self.final(ctx, board, due)
                if fin is not None:
                    pulls.append(fin)
                    if not fin.extra.get("provisional"):     # e.g. NWP before its Storage Realignment cycle posts
                        bstate["final"] = due.isoformat()
            self._join(pulls, locs)
            need = {loc for pull in pulls for loc in unlocated(board, pull.pts)} - set(locs)
            if locs and need:            # a point with gas the cached file lacks: one early re-read, before the guard
                self._join(pulls, self._locs(ctx, board, res.warnings, need))
            for pull in pulls:
                vals = aggregate(board, pull.pts, res.warnings)
                ts = datetime(pull.day.year, pull.day.month, pull.day.day)
                for sid, v in sorted(vals.items()):
                    res.obs.append(Obs(sid, ts, v, pull.posted))
                    metas.setdefault(sid, self.series_meta(sid, board))
            if latest is not None:
                bstate["cycle"] = f"{latest.day.isoformat()}|{latest.cycle}"
        res.meta = list(metas.values()) or self.declared_series()
        try:
            cache_save(state_name, state)
        except OSError as e:
            res.warnings.append(f"{self.id}: state not written: {e}")
        return res
