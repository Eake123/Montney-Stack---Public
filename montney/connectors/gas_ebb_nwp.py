"""Northwest Pipeline (Williams): FERC Operationally Available Capacity by point, summed by region and category
(gas_ebb_common: categories, overrides, the location cache and the no-state guard). Gives the PacNW a measured
(scheduled) demand: NWP's LDC, power and industrial deliveries in WA / OR / ID, by point.

**Method** (`gas_ebb_nwp`; public EBB `www.northwest.williams.com/NWP_Portal/`, no login, no captcha; robots.txt is a
404, so no rules; probed from kraken's VPN exit 2026-11-13: 200):
- GET `CapacityResultsScrollable.action` (no parameter) = the current gas day's latest cycle; with
  `?StartGasFlowDate=MM-DD-YYYY` = that gas day's latest. One HTML table, ~350 KB, no CSV, no validators (gas_nwp reads
  the same page for its 8 named points). 765 rows: 420 DPQ (deliveries), 92 RPQ (receipts), 245 MLQ (mainline
  segments, dropped), Jackson Prairie / Plymouth LNG storage as SIQ (injection, a delivery) and SWQ (withdrawal, a
  receipt); STQ (their bi-directional net) is dropped so storage isn't counted twice. TSQ is in MMBtu (= Dth).
  Rows are keyed by **Loc Prop** (NWP's per-direction id; `Loc` is shared by a receipt and a delivery).
- Locations (weekly): GET `location.action` (FERC Location Data Download, ~390 KB HTML, 592 rows): Loc Prop, Loc Type
  Ind, Dir Flo, Up/Dn Name (counterparty), Loc St Abbrev. Found through the portal's own left menu (GWT
  `MenuService.loadMenuHtml`: "Locations | location.action").
- **Types** (NWP's own words -> the NAESB codes gas_ebb_common classifies): LDC -> LDC, Elec Gen -> EGN, End-User ->
  IND (not END: NWP names meters "... METER STATION", which POWER_RE would read as a power plant), P/L Inter -> INT,
  Stor and LNG -> STR (Plymouth LNG is peak-shaving storage), Compressor -> STA, Virtual -> VIR, Pool -> PPT, Plant ->
  PLT for a receipt (a processing plant: production) and IND for a delivery, Other -> RNG for a receipt (every NWP
  "Other" receipt is an RNG plant) and IND for a delivery. Every counterparty "Northwest Pipeline - Park & Loan" point
  is VIR (internal). A **blank type** (56 rows) borrows its counterparty's dominant type in the same direction (≥ 75 %
  of its typed rows: Intermountain's Nampa, 57 of 58 LDC -> LDC); what's left with gas is counted as int_in / int_out
  with a warning.
- Region by state (`state_regions()`): WA / OR / ID -> pnw, WY / UT / CO -> mtnw, NM -> sunbelt. **All of Idaho is
  pnw** (regions/pnw.yaml: US-ID, no clip), so NWP's southern Idaho deliveries (Boise, Nampa, Caldwell, Twin Falls,
  Idaho Falls, Idaho Power's Langley Gulch / Bennett / Elmore, the Reno lateral to Paiute) are pnw's, unlike
  pnw_gas's Baker boundary (which leaves southern Idaho out of `pnw.demand_est`).

**Series** (MMcf/d = Dth / 1,037, freq D, `day: gas`, ts = the gas day, as_of = the cycle's post time; all scheduled):
`ebb.nwp.<region>.<category>` for every category with a point in that region (ldc, power, industrial, production,
storage_inj, storage_wd, import_canada, int_in, int_out) and `ebb.nwp.<region>.net` (deliveries − receipts: NWP's net
import into the region, less fuel and linepack change). Today's postings are the latest intraday cycle; region lines
read ended gas days only (`gas: through: yesterday`).

**Cycles**: each run reads the current gas day's latest cycle. Once a day after FINAL_HOUR Central it reads the
previous gas day; NWP posts that day's final "Storage Realignment" cycle ~10:00 CT. Until it shows (and before
ACCEPT_HOUR CT) the pull is provisional: written, but the day is read again next run.

**Requests**: 1 per run (every 2 h: 12/day) + 1-2 a day for the previous gas day + 1 a week (location file):
~13-15/day, ~5 MB/day, on www.northwest.williams.com (with gas_nwp's ~13: ~28/day on the host, ≤ 3/h).

**Traps** (spec overrides, sources/gas_ebb_nwp.yaml):
- Kelso–Beaver: NWP delivers into the Kelso–Beaver pipeline at KELSO/BEAVER (229, "End-User") and takes the same gas
  back at KELSO BEAVER RECEIPT (971); PGE's plants (Beaver, Port Westward I/II) are then NWP delivery points of their
  own, typed "Plant". Both loop points are dropped, the plants are made power.
- Sumas (297, from Westcoast) is the Huntingdon export: import_canada, not int_in.
- NW Natural's receipts at Deer Island and Molalla (620, 672) are Mist storage gas coming back into NWP (NW Natural has
  no production): storage_wd. North Mist (981) is typed Stor already.
- 18 posted points aren't in the location file (in-line transfers, lateral / line constraint points, Blue Water LNG,
  DEX). The in-line transfers and line constraints pass the no-state guard by name (`unlocated_ok`); any other with gas
  fails the run until it's mapped.
Fails closed (FormatDrift): the OA or location table's header changes, the basis isn't MMBtu, the gas day isn't the
one asked for, an overridden point is no longer posted, or a point with gas has no state.
Terms: public FERC-required postings (18 CFR 284.13), personal use (L-006). Raw points are never stored.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime

from montney.connectors.base import Context, FetchResult
from montney.connectors.gas_ebb_common import (EbbConnector, Pt, Pull, _override, _overrides, central, ct_to_utc,
                                               num)
from montney.connectors.gas_common import FormatDrift

NWP_PORTAL = "https://www.northwest.williams.com/NWP_Portal/"
OA_PATH = "CapacityResultsScrollable.action"
LOC_PATH = "location.action"
OA_COLS = ["Loc Prop", "Loc Name", "Loc", "Loc Purp Desc", "Flow Ind Desc", "Loc/QTI", "Design Cap",
           "Operating Capacity", "Total Scheduled Qty", "Operationally Available Capacity", "IT"]
LOC_COLS = ("Loc Prop", "Loc Name", "Loc Type Ind", "Dir Flo", "Up/Dn Name", "Loc St Abbrev")
QTI_DIR = {"DPQ": "D", "SIQ": "D", "RPQ": "R", "SWQ": "R"}        # MLQ (segments), STQ (storage net) are dropped
PARK_LOAN = "Northwest Pipeline - Park & Loan"
TYPES = {"LDC": "LDC", "Elec Gen": "EGN", "End-User": "IND", "P/L Inter": "INT", "Stor": "STR", "LNG": "STR",
         "Compressor": "STA", "Virtual": "VIR", "Pool": "PPT"}
BY_DIR = {"Plant": {"R": "PLT", "D": "IND"}, "Other": {"R": "RNG", "D": "IND"}}
FINAL_CYCLE = "storage realignment"
BORROW_SHARE = 0.75                 # a blank type takes its counterparty's type when ≥ 75 % of its typed rows have it
ACCEPT_HOUR = 16                   # Central: after this, yesterday's newest cycle is taken as final even if not realigned


def _text(cell: str) -> str:
    t = re.sub(r"<[^>]+>", " ", cell).replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'")
    return re.sub(r"\s+", " ", t).strip()


def _cells(row: str, tag: str = "t[dh]") -> list[str]:
    return [_text(x) for x in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", row, re.S)]


def parse_oa(page: str) -> tuple[date, str, datetime, list[Pt]]:
    """(gas day, cycle, posted (naive UTC), points) from NWP's OA report. Receipts, deliveries and the storage
    injection / withdrawal rows; segments (MLQ) and the storage net (STQ) are dropped."""
    flat = _text(page)
    m_cyc = re.search(r"Cycle:\s*(.+?)\s+Post Date/Time:", flat)
    m_post = re.search(r"Post Date/Time:\s*(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)", flat)
    m_day = re.search(r"Eff Gas Day/Time:\s*(\d{1,2}/\d{1,2}/\d{4})", flat)
    if not (m_cyc and m_post and m_day):
        raise FormatDrift("NWP OA report: no 'Cycle / Post Date/Time / Eff Gas Day/Time' header")
    if "Meas Basis Desc: MMBtu" not in flat:
        raise FormatDrift("NWP OA report: measurement basis is no longer MMBtu")
    header, pts = None, {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        c = _cells(r)
        if c[:2] == ["Loc Prop", "Loc Name"]:
            if c != OA_COLS:
                raise FormatDrift(f"NWP OA report: columns changed ({c})")
            header = c
            continue
        if header is None or len(c) != len(OA_COLS):
            continue
        d = QTI_DIR.get(c[5])
        if d is None:
            continue
        p = Pt(c[0], c[1], d, num(c[8]))
        if p.key in pts:
            raise FormatDrift(f"NWP OA report: Loc Prop {c[0]} ({c[1]}) posted twice as {c[5]}")
        pts[p.key] = p
    if header is None:
        raise FormatDrift("NWP OA report: no 'Loc Prop, Loc Name, …' header row")
    if not pts:
        raise FormatDrift("NWP OA report: no receipt or delivery rows")
    try:
        day = datetime.strptime(m_day.group(1), "%m/%d/%Y").date()
        posted = ct_to_utc(datetime.strptime(m_post.group(1), "%m/%d/%Y %I:%M %p"))
    except ValueError as e:
        raise FormatDrift(f"NWP OA report: bad gas day / post time ({e})") from e
    return day, m_cyc.group(1).strip(), posted, list(pts.values())


def parse_locations(page: str) -> dict[str, tuple[str, str, str]]:
    """{Loc Prop: (state, NAESB type code, counterparty)} from NWP's Location Data Download (see TYPES / BY_DIR)."""
    i = page.find('id="grid"')
    hdr = _cells(page[i:], "th") if i >= 0 else []
    if any(c not in hdr for c in LOC_COLS):
        raise FormatDrift(f"NWP location.action: header changed ({hdr[:8]})")
    rows = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page[i:], re.S):
        c = _cells(r, "td")
        if len(c) == len(hdr):
            x = dict(zip(hdr, c))
            x["Up/Dn Name"] = x["Up/Dn Name"].strip('"').strip()
            rows.append(x)
    if not rows:
        raise FormatDrift("NWP location.action: no location rows")
    # a blank type borrows its counterparty's dominant type in the same direction (≥ BORROW_SHARE of its typed rows)
    by_cp: dict[tuple[str, str], Counter] = {}
    for x in rows:
        if x["Loc Type Ind"] and x["Up/Dn Name"]:
            by_cp.setdefault((x["Up/Dn Name"], x["Dir Flo"]), Counter())[x["Loc Type Ind"]] += 1
    out = {}
    for x in rows:
        raw, cp, dirn = x["Loc Type Ind"], x["Up/Dn Name"], x["Dir Flo"]
        if not raw and (cnt := by_cp.get((cp, dirn))):
            top, n = cnt.most_common(1)[0]
            raw = top if n >= BORROW_SHARE * sum(cnt.values()) else ""
        code = "VIR" if cp == PARK_LOAN else BY_DIR.get(raw, {}).get(dirn) or TYPES.get(raw, "")
        out[x["Loc Prop"]] = (x["Loc St Abbrev"], code, cp)
    return out


class EbbNwp(EbbConnector):
    FAMILY = "nwp"
    LABEL = "Northwest Pipeline"

    def _url(self, board: dict, path: str) -> str:
        return (board.get("portal") or NWP_PORTAL) + path

    def latest(self, ctx: Context, board: dict, bstate: dict) -> Pull | None:
        day, cycle, posted, pts = parse_oa(self.get(ctx, self._url(board, OA_PATH), timeout=120).text)
        return Pull(day, cycle, posted, pts)

    def final(self, ctx: Context, board: dict, day: date) -> Pull | None:
        r = self.get(ctx, self._url(board, OA_PATH), params={"StartGasFlowDate": day.strftime("%m-%d-%Y")},
                     timeout=120)
        gday, cycle, posted, pts = parse_oa(r.text)
        if gday != day:
            raise FormatDrift(f"NWP OA report: asked for {day}, got {gday}")
        realigned = cycle.lower().startswith(FINAL_CYCLE)
        return Pull(gday, cycle, posted, pts,
                    {"provisional": not realigned and central(ctx.now).hour < ACCEPT_HOUR})

    def locations(self, ctx: Context, board: dict) -> dict[str, tuple[str, str, str]]:
        return parse_locations(self.get(ctx, self._url(board, LOC_PATH), timeout=120).text)

    # ---- a warning for points with gas whose type nothing resolves (counted as int_in / int_out)
    def _join(self, pulls: list[Pull], locs: dict[str, tuple[str, str, str]]) -> None:
        EbbConnector._join(pulls, locs)
        ov = _overrides(self._board)
        for pull in pulls:
            for p in pull.pts:
                o = _override(ov, p)
                if p.dth > 0 and p.state and not (p.ltype or o.get("type") or o.get("category") or o.get("drop")):
                    self._untyped.add(f"{p.loc} {p.name}")

    def fetch(self, ctx: Context) -> FetchResult:
        boards = self.boards()
        if len(boards) != 1:
            raise ValueError(f"{self.id}: one NWP board expected, got {len(boards)}")
        self._board, self._untyped = boards[0], set()
        res = super().fetch(ctx)
        if self._untyped:
            res.warnings.append(f"nwp: {len(self._untyped)} point(s) with gas but no location type, counted as "
                                f"int_in / int_out (add an override): {sorted(self._untyped)[:8]}")
        return res
