"""Northern Natural Gas (NNG): FERC Operationally Available Capacity, summed for umw (gas_ebb_common). Research:
docs/research/pipelines/east_and_rockies.md §5. NNG's site is WordPress with a REST proxy; robots.txt allows it
(Disallow only /wp/wp-admin/). No login, no captcha.

**Method** (`gas_ebb_nng`):
- Cheap check: GET `www.northernnaturalgas.com/wp-json/api/capacity/oac` (~12 KB JSON) lists, per gas day, the cycles
  posted so far: `{"data": {"maxDate", "maxCycle", "data": [{"gasDay": "11/13/2026", "timely": 10, "evening": 11,
  "nonGridAM": 12, "intraday1": 13, ...}]}}`. The run takes the current gas day's newest usable cycle and skips the
  download when it was already read.
- OA CSV: POST `.../capacity/oac/report` with `{"gasDay", "cycle", "searchType": "All group and point locations", ...,
  "download": true}` returns `NNG_OperCap_<YYYYMMDD>.csv` (~357 KB, ~1,930 rows): Loc, Loc Zn, Loc Name, Flow Ind,
  Loc/QTI, Total Scheduled Quantity, Cycle Desc, Effective Gas Day/Time, Post Date/Time (Central), Meas Basis (BZ =
  MMBtu). as_of = the post time.
- Locations (weekly): POST `.../api/locations` (`download: true`, ~500 KB NAESB CSV: Loc St Abbrev, Loc Type Ind,
  Up/Dn Name; ~2,500 points).

**Cycles**: 10 Timely, 11 Evening, 13-15 Intraday 1-3 are used. 12 "Non-Grid A.M." re-posts only the non-grid points
(the rest read 0) and is never used. 16 "Final A.M." (posted ~08:40 CT the next morning) is not used either: on gas
day 2026-11-12 its Minnesota point loads rose 420k Dth over Intraday 3 while Zone EF and the supply into it rose
~155k, so its points don't reconcile with its own zone totals. The previous gas day's final is therefore its Intraday
3 (posted ~21:40 CT), read once a day after FINAL_HOUR (10:00 CT) like every other board.

**Trap: the market area is posted by allocation zone, not by point.** Most market-area deliveries are nominated to a
zone, so their points read 0 (Minneapolis, Duluth, Des Moines, Omaha, Sioux Falls all show 0) and the quantity sits on
the zone rows. The location file's zone accounts (VIR points named "ZONE E-F-<LDC>", "ZONE D-<LDC>", "ZONE ABC-<LDC>")
give each zone's states:
- **Zone EF** (group 453): Minnesota, northwest Wisconsin (NSP-WI, Superior, St. Croix, Viroqua, Midwest Natural,
  WG / WPL's NNG towns) and the Michigan Upper Peninsula (SEMCO): all umw.
- **Zone D** (456): eastern Iowa, northwest Illinois (JoCarroll) and southern Wisconsin (WE, WG, WPL, MG&E).
- **Zone ABC** (454): Nebraska, Iowa, South Dakota: outside every region.
The zone total includes the points posted one by one in it: Zone EF tracks the supply into the north system
(Ventura North + Welcome North + the Minnesota receipts) at ~98.4 % on two cycles (fuel), whereas zone + points would
be ~150 % of it. So `ebb.nng.zone.ef` is NNG's delivery total for most of umw, and the point categories
(`ebb.nng.umw.<category>`) are the part of it posted per point, a breakdown, never to be added to the zone. Their
`net` (deliveries − receipts) is not written: it would leave out the zone deliveries.

Other rows: group rows (no `Loc Zn`) are allocation groups, each the scheduled flow past a constraint "in the general
direction of physical flow" (NNG's note): stored only as named series (`<loc>:G` keys; never summed into a region).
Field-area groups list their D / R detail on rows without a Loc: skipped.

**Borders** (proxies, named in the spec): IA → MN = Ventura North (Ventura, IA, ~60 km south of the line: includes the
north-Iowa towns past it) + Welcome North (Welcome, MN, just north of the line, on the West Leg; includes Northern
Border's Welcome receipts). IL → WI = Galena East (the East Leg past Galena, IL, into southern Wisconsin's Zone D).

**Overrides** (spec): direct-connect industrials coded LDC (taconite, steel, ethanol), power plants coded LDC / END
(Mankato, Cottage Grove, the Baraga and Negaunee RICE plants), Wisconsin Gas master meters typed OTH, Pine Bend
refinery typed VIR. The no-state guard (`require_located`) refuses a run in which a point with gas has no state.

**Requests**: 1 GET per run (cheap check), + 1 POST when a new cycle is posted (~4 a day), + 1 POST a day (yesterday's
Intraday 3), + 1 POST a week (locations): ~17-18 a day on www.northernnaturalgas.com, ~2 MB a day.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from montney.connectors.base import Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.connectors.gas_ebb_common import EbbConnector, Pt, Pull, ct_to_utc, current_gas_day, num
from montney.store import SeriesMeta

API = "https://www.northernnaturalgas.com/wp-json/api/"
OAC = API + "capacity/oac"
REPORT = API + "capacity/oac/report"
LOCATIONS = API + "locations"
# listing key -> (cycle number, Cycle Desc in the CSV), newest last; nonGridAM (12) and finalAM (16) are never used
CYCLES = (("timely", 10, "Timely"), ("evening", 11, "Evening"), ("intraday1", 13, "Intraday 1"),
          ("intraday2", 14, "Intraday 2"), ("intraday3", 15, "Intraday 3"))
FINAL = "intraday3"
OA_COLS = ("Loc", "Loc Zn", "Loc Name", "Loc/QTI", "Total Scheduled Quantity", "Cycle Desc",
           "Effective Gas Day/Time", "Post Date/Time", "Meas Basis")
LOC_COLS = ("Loc", "Loc St Abbrev", "Loc Type Ind", "Up/Dn Name")
QTI_DIR = {"DPQ": "D", "RPQ": "R"}


def parse_listing(j) -> dict[date, dict[str, int]]:
    """{gas day: {"timely": 10, ...}} from the oac listing."""
    try:
        rows = j["data"]["data"]
        out = {datetime.strptime(r["gasDay"], "%m/%d/%Y").date(): {k: v for k, v in r.items() if k != "gasDay"}
               for r in rows}
    except (KeyError, TypeError, ValueError) as e:
        raise FormatDrift(f"NNG oac listing: not the expected JSON ({e})") from e
    if not out:
        raise FormatDrift("NNG oac listing: no gas days")
    return out


def parse_oa(text: str, day: date, cycle: str) -> Pull:
    rows = [r for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))) if r.get("Loc") is not None]
    if not rows or any(c not in rows[0] for c in OA_COLS):
        raise FormatDrift(f"NNG OA CSV: header changed ({list(rows[0])[:8] if rows else text[:60]!r})")
    data = [r for r in rows if (r["Loc"] or "").strip()]
    if {r["Cycle Desc"] for r in data} != {cycle}:
        raise FormatDrift(f"NNG OA CSV: cycle {sorted({r['Cycle Desc'] for r in data})} is not {cycle!r}")
    if {r["Effective Gas Day/Time"][:10] for r in data} != {day.isoformat()}:
        raise FormatDrift(f"NNG OA CSV: not gas day {day}")
    if {r["Meas Basis"] for r in data} != {"BZ"}:
        raise FormatDrift("NNG OA CSV: unit is not BZ (MMBtu)")
    pts: dict[str, Pt] = {}
    for r in data:
        loc, name = r["Loc"].strip(), r["Loc Name"].strip()
        q = num(r["Total Scheduled Quantity"])
        if not (r["Loc Zn"] or "").strip():
            p = Pt(loc, name, "G", q)                    # an allocation group / zone: named series only
        else:
            d = QTI_DIR.get(r["Loc/QTI"])
            if d is None:
                raise FormatDrift(f"NNG OA CSV: point {loc} has Loc/QTI {r['Loc/QTI']!r}")
            p = Pt(loc, name, d, q)
        pts.setdefault(p.key, p)                         # group 831 is listed twice (Demarcation, Field)
    posted = max(datetime.fromisoformat(r["Post Date/Time"][:19]) for r in data)
    return Pull(day, cycle, ct_to_utc(posted), list(pts.values()))


class EbbNng(EbbConnector):
    FAMILY = "nng"

    # ---- one listing and one location map per run
    def fetch(self, ctx: Context) -> FetchResult:
        self._days: dict[date, dict[str, int]] | None = None
        self._loc_map: dict[str, tuple[str, str, str]] = {}
        res = super().fetch(ctx)
        drop = {f"ebb.{b['pipe']}.{r}.net" for b in self.boards() for r in b.get("regions") or []}
        res.obs = [o for o in res.obs if o.series_id not in drop]
        res.meta = [m for m in res.meta if m.series_id not in drop]
        return res

    def _locs(self, ctx, board, warnings, *args, **kw):
        self._loc_map = super()._locs(ctx, board, warnings, *args, **kw)
        return self._loc_map

    def _listing(self, ctx: Context) -> dict[date, dict[str, int]]:
        if self._days is None:
            r = self.get(ctx, OAC, timeout=60)
            try:
                j = r.json()
            except ValueError as e:
                raise FormatDrift(f"NNG oac listing: not JSON ({r.text[:80]!r})") from e
            self._days = parse_listing(j)
        return self._days

    def _pull(self, ctx: Context, board: dict, day: date, n: int, cycle: str) -> Pull:
        body = {"gasDay": day.isoformat(), "cycle": n, "searchType": "All group and point locations",
                "searchString": "", "locationType": "All location types", "locationZone": "All Locations",
                "pagination": {"pageNumber": 1, "pageSize": 500}, "download": True}
        r = self.post(ctx, REPORT, json=body, timeout=120)
        pull = parse_oa(r.content.decode("utf-8-sig", "replace"), day, cycle)
        for p in pull.pts:
            if p.loc in self._loc_map and not p.state:
                p.state, p.ltype, p.cp = self._loc_map[p.loc]
        return pull

    def latest(self, ctx, board, bstate):
        day = current_gas_day(ctx.now)
        posted = self._listing(ctx).get(day) or {}
        have = [c for c in CYCLES if c[0] in posted]
        if not have:
            return None
        key, n, cycle = have[-1]
        if posted[key] != n:
            raise FormatDrift(f"NNG oac listing: {key} is cycle {posted[key]}, expected {n}")
        if bstate.get("cycle") == f"{day.isoformat()}|{cycle}":
            return None
        return self._pull(ctx, board, day, n, cycle)

    def final(self, ctx, board, day):
        posted = self._listing(ctx).get(day) or {}
        if FINAL not in posted:
            return None                                  # not posted yet: the next run tries again
        _, n, cycle = next(c for c in CYCLES if c[0] == FINAL)
        return self._pull(ctx, board, day, n, cycle)

    def locations(self, ctx, board):
        r = self.post(ctx, LOCATIONS, json={"searchType": "", "pointStatus": "A", "searchString": "",
                                            "pointTypeNew": "", "groupName": "", "zoneName": "",
                                            "stateAbbreviation": "", "download": True}, timeout=120)
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", "replace"))))
        if not rows or any(c not in rows[0] for c in LOC_COLS):
            raise FormatDrift(f"NNG locations CSV: header changed ({list(rows[0])[:6] if rows else 'empty'})")
        out = {x["Loc"].strip(): ((x["Loc St Abbrev"] or "").strip(), (x["Loc Type Ind"] or "").strip(),
                                  (x["Up/Dn Name"] or "").strip())
               for x in rows if (x.get("Loc") or "").strip()}
        if len(out) < 1000:
            raise FormatDrift(f"NNG locations CSV: only {len(out)} points (a filter answered?)")
        return out

    # ---- labels: the point categories are a breakdown of the zone totals
    def series_meta(self, sid: str, board: dict) -> SeriesMeta:
        m = super().series_meta(sid, board)
        if sid not in (board.get("named") or {}):
            m.label = m.label.replace(" (scheduled)", ", points posted one by one (inside the zone totals) (scheduled)")
        return m

    def declared_series(self) -> list[SeriesMeta]:
        return [self.series_meta(sid, b) for b in self.boards() for sid in (b.get("named") or {})]
