"""Kinder Morgan interstate EBBs (pipeline2.kindermorgan.com): daily scheduled flows by region and category.

Research: docs/research/pipelines/kinder_morgan.md. Every KM interstate posts the same FERC "Operationally Available"
pages (`/Capacity/OpAvailPoint.aspx?code=X`, `/Capacity/OpAvailSegment.aspx?code=X`) and a NAESB location-data CSV
(`/LocationDataDownload/LocDataDwnld.aspx?code=X`). Public, no login; robots.txt allows these paths.

**Method.** For each board in the spec (`params.boards`):
- the OA-by-point Excel downloads (Delivery and Receipt radios) give every point's **Total Scheduled Quantity** (TSQ, Dth,
  net of both directions at bidirectional points), for one gas day and the latest posted cycle;
- the location CSV (cached in `<home>/cache/ebb_km_loc_<CODE>.json`, refreshed weekly) gives each point's state, type
  (LDC, EGN, END, PLT, GTH, WHD, RNG, STR, LNG, INT, VIR, PPT, OTH) and counterparty. State → region comes from
  gas_ebb_common.state_regions (the region files' `US-XX` members); points in a state outside every region are dropped.
  There is no lat/lon in KM's data;
- **the no-state guard** (gas_ebb_common.require_located): a point with TSQ ≠ 0 and no state (blank in the location CSV,
  or not in it) fails the board like any other FormatDrift: it writes nothing that run, the other boards do. A board's
  `unlocated_ok` regex (on the OA or location-file name) lets known no-state points through (EPNG's Blanco balancing
  point). When the failing points are missing from the cached CSV, it is re-read early (once a day; below);
- categories: LDC (and NGPL's virtual "… CDP" citygate aggregates) → `ldc`, EGN → `power`, END → `industrial`
  (deliveries − receipts); PLT/GTH/WHD/RNG → `production` (receipts − deliveries); STR → `storage_inj` (deliveries) and
  `storage_wd` (receipts); LNG and INT to LNG-feed pipes → `lng`; INT to Mexican / Canadian counterparties →
  `export_mexico` / `export_canada` (deliveries − receipts). Other interconnects are not series (their totals go to the
  state file as a diagnostic); **interconnects with another KM board (or self-referencing ones) are dropped**, so the
  family roll-up `ebb.km.<region>.<category>` never counts a KM→KM transfer;
- borders (`params.borders`): named segments of the OA-by-segment download (Σ TSQ in the `forward` direction − Σ in the
  other), or listed points (loc ids; deliveries − receipts, × `sign`). A named segment or loc that disappears raises
  FormatDrift (gas_ebb_common.require_present), so a border is never a default 0.

Series are MMcf/d (Dth / 1,037: gas_ebb_common.mmcfd), daily, keyed by gas day (`day: gas`; the board's NAESB gas day, 9:00 CT), as_of = the
cycle's post time (Central clock → UTC), so every cycle is kept and the final one wins.

**Requests** (all through ctx.get/ctx.post, ≥ `pause_s` = 1 s apart, at most `max_requests` = 60 a run; a board that
won't fit waits for the next run):
- a board is only touched when something can have changed: a new gas day (CT midnight), a cycle boundary passed
  (`cycle_times_ct`, default ID1 14:00, ID2 17:30, ID3 22:00) with no post newer than it seen yet, or the final pull due.
  Then: 1 GET (the page carries the ASP.NET viewstate + session cookie; a cached viewstate is refused) + the Delivery
  POST as the **probe**: its header's (gas day, cycle, post time) equal to the state file's → stop there (the GET page
  itself has no cycle info). Changed → the Receipt POST + the segment page (GET + POST) for boards with borders.
  Segment-only boards (no categories, e.g. Stagecoach, MEP) probe with the segment POST.
- once a day after `final_after_ct` (09:00 CT): yesterday's final cycle (`dtePickerBegin` = D-1; EPNG returns "CYCLE 7")
  for every board: 2 point POSTs + 1 segment POST, sharing the run's GETs.
- the location CSV: GET + POST per board when missing, or older than `location_max_age_days` (7), or (first, at most
  once a day) when the board's guard failed on points it lacks: at most 2 refreshes a run.
Spacing: gas_ebb_common.host_wait, timed from the start of the previous request (this connector's own clock).
Measured cold start (empty cache, 10:50 CT): 60 requests (7 location files + 9 boards, 3 boards deferred) then 37.
Steady state (12 boards; 10 with points, 7 with border segments, 2 segment-only): per point board a day ≈ new-day pull 3
+ 3 cycles × (one changed pull 3 + about one early, unchanged probe 2) + final 3 ≈ 21, + 10 for its segment page; ≈ 300
requests/day (~13/h; never more than 60 in a run, most runs far fewer), ~20 MB (the ~165 KB page behind every GET
dominates; the Excel files are 8-48 KB).

**Caveats.** TSQ is *scheduled*, not metered, and LDC citygate takes include gas bound for LDC storage (the NGPL Chicago
lesson: citygate ≠ burn). Interconnects dominate TX and NY on KM pipes, so KM alone can't close those regions. Region by
state: EPNG's California deliveries (SoCalGas Ehrenberg, North Baja, PG&E Topock) sit at AZ-state points, so they're a
border series (`ebb.epng.border.sunbelt_cal`), not AZ demand. KM-internal removal relies on `Up/Dn Name` matching
`params.km_counterparties`.
"""

from __future__ import annotations

import csv
import html
import io
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save, local_to_utc
from montney.connectors.gas_ebb_common import (
    host_wait, loc_cache_due, loc_cache_lacks, loc_cache_read, loc_cache_write, mmcfd, require_located, require_present,
    state_regions,
)
from montney.connectors.pnw_gas import CENTRAL, num
from montney.store import Obs, SeriesMeta

GROUP = "Pipelines: Kinder Morgan EBB (scheduled)"
BASE = "https://pipeline2.kindermorgan.com"
STATE = "ebb_km_state"
CATS = ("production", "ldc", "power", "industrial", "storage_inj", "storage_wd", "lng", "export_mexico", "export_canada")
CAT_LABEL = {"production": "production receipts (plants, gathering, wellhead, RNG)", "ldc": "LDC citygate deliveries",
             "power": "power-plant deliveries", "industrial": "industrial / end-user deliveries",
             "storage_inj": "storage injections", "storage_wd": "storage withdrawals", "lng": "LNG feedgas deliveries",
             "export_mexico": "exports to Mexico", "export_canada": "exports to Canada"}
TYPE_CAT = {"LDC": "ldc", "EGN": "power", "END": "industrial", "PLT": "production", "GTH": "production",
            "WHD": "production", "RNG": "production", "LNG": "lng"}
DEMAND_LIKE = {"ldc", "power", "industrial", "lng", "export_mexico", "export_canada"}      # deliveries − receipts
DATE_FIELD = "WebSplitter1_tmpl1_ContentPlaceHolder1_dtePickerBegin_clientState"
RADIO = "rbDelivery", "rbReceipt"
POINT_COLS = ("Loc", "Loc Name", "Total Scheduled Quantity")
SEG_COLS = ("Loc (Segment)", "Loc Name (Segment)", "Total Scheduled Quantity", "Flow Ind")
LOC_COLS = ("Loc", "Loc Name", "Loc St Abbrev", "Loc Type Ind", "Up/Dn Name", "Loc Stat Ind")


# ------------------------------------------------------------------ parsing
def xlsx_rows(content: bytes, what: str) -> list[dict[str, str]]:
    """First sheet of an .xlsx as [{column letter: text}] (shared and inline strings). Not an .xlsx → FormatDrift."""
    if content[:2] != b"PK":
        raise FormatDrift(f"{what}: not an .xlsx (the site answered with a page: session, date range or layout)")
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        names = z.namelist()
        ss = ([html.unescape(re.sub(r"<[^>]+>", "", si)) for si in
               re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
              if "xl/sharedStrings.xml" in names else [])
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        raise FormatDrift(f"{what}: unreadable .xlsx ({e})") from e
    out = []
    for row in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        cells = {}
        for ref, attrs, body in re.findall(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', row, re.S):
            v = re.search(r"<v>(.*?)</v>", body or "")
            t = re.search(r"<t[^>]*>(.*?)</t>", body or "", re.S)
            if v is not None:
                cells[ref] = (ss[int(v.group(1))] if 't="s"' in attrs else html.unescape(v.group(1))).strip()
            elif t is not None:
                cells[ref] = html.unescape(t.group(1)).strip()
        out.append(cells)
    return out


def _table(rows: list[dict[str, str]], cols: tuple[str, ...], what: str) -> tuple[dict, list[dict[str, str]]]:
    """(header values by label, data rows by column name). Checks the 3-row KM header; drift → FormatDrift."""
    if len(rows) < 3:
        raise FormatDrift(f"{what}: {len(rows)} rows, no header")
    labels, values, names = rows[0], rows[1], rows[2]
    head = {labels.get(k, k): v for k, v in values.items()}
    for need in ("Eff Gas Day/Eff Time", "CycleDesc", "Post Date/Post Time", "Meas Basis Desc"):
        if need not in head:
            raise FormatDrift(f"{what}: header moved, no {need!r} ({labels})")
    if "Dth" not in head["Meas Basis Desc"]:
        raise FormatDrift(f"{what}: measurement basis {head['Meas Basis Desc']!r}, not Dth")
    col = {v: k for k, v in names.items()}
    missing = [c for c in cols if c not in col]
    if missing:
        raise FormatDrift(f"{what}: column(s) {missing} gone ({list(col)})")
    data = []
    for r in rows[3:]:
        loc = r.get(col[cols[0]], "")
        if not loc.strip().isdigit():          # footer: "Row Count: N", "Comments and Notes", footnotes
            continue
        data.append({c: r.get(k, "") for c, k in col.items()})
    return head, data


def header_key(head: dict, what: str) -> tuple[date, str, datetime]:
    """(gas day, cycle, post time naive UTC) from a download's header row."""
    try:
        day = datetime.strptime(head["Eff Gas Day/Eff Time"].split()[0], "%m/%d/%Y").date()
        posted = local_to_utc(datetime.strptime(head["Post Date/Post Time"], "%m/%d/%Y %I:%M %p"), CENTRAL)
    except (KeyError, ValueError, IndexError) as e:
        raise FormatDrift(f"{what}: bad gas day / post time ({e})") from e
    return day, head.get("CycleDesc", ""), posted


def point_rows(content: bytes, side: str, what: str) -> tuple[tuple[date, str, datetime], list[dict[str, str]]]:
    head, data = _table(xlsx_rows(content, what), POINT_COLS, what)
    if side not in head.get("Loc Purp Desc", ""):
        raise FormatDrift(f"{what}: {head.get('Loc Purp Desc')!r}, not {side} points")
    return header_key(head, what), data


def segment_rows(content: bytes, what: str) -> tuple[tuple[date, str, datetime], list[dict[str, str]]]:
    head, data = _table(xlsx_rows(content, what), SEG_COLS, what)
    return header_key(head, what), data


def parse_locations(text: str, what: str) -> dict[str, dict[str, str]]:
    """NAESB location CSV → {loc id: {name, st, type, cp, active}}."""
    rd = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in LOC_COLS if c not in (rd.fieldnames or [])]
    if missing:
        raise FormatDrift(f"{what}: location CSV lacks {missing} ({(rd.fieldnames or [])[:12]})")
    out = {}
    for r in rd:
        out[r["Loc"].strip()] = {"name": r["Loc Name"].strip(), "st": r["Loc St Abbrev"].strip(),
                                 "type": r["Loc Type Ind"].strip(), "cp": r["Up/Dn Name"].strip().upper(),
                                 "active": r["Loc Stat Ind"].strip() == "A"}
    if not out:
        raise FormatDrift(f"{what}: empty location CSV")
    return out


def form(page: str, radio: str | None = None, day: date | None = None) -> dict[str, str]:
    """The page's postback fields (checked radios, selects), `radio` forced on, the date picker on `day`, + Download."""
    data: dict[str, str] = {}
    radios: dict[str, list[str]] = {}
    for m in re.finditer(r"<input[^>]+>", page):
        tag = m.group(0)
        n, v = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if not n or 'type="image"' in tag or 'type="submit"' in tag:
            continue
        name, val = html.unescape(n.group(1)), html.unescape(v.group(1)) if v else ""
        if 'type="radio"' in tag:
            radios.setdefault(name, []).append(val)
            if "checked" not in tag:
                continue
        if 'type="checkbox"' in tag and "checked" not in tag:
            continue
        data[name] = val
    for m in re.finditer(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        sel = re.search(r'<option selected="selected" value="([^"]*)"', m.group(2))
        data[html.unescape(m.group(1))] = sel.group(1) if sel else ""
    if radio is not None:
        group = next((g for g, vals in radios.items() if radio in vals), None)
        if group is None:
            raise FormatDrift(f"OA page: no {radio!r} option ({radios})")
        data[group] = radio
    btn = next((html.unescape(b) for b in re.findall(r'name="([^"]*btnDownload)"', page)), None)
    if btn is None or "__VIEWSTATE" not in data:
        raise FormatDrift("KM page: no Download button or no viewstate")
    data[btn + ".x"], data[btn + ".y"] = "10", "10"
    data["ctl00$hdnIsDownload"] = "true"
    if day is not None:
        if DATE_FIELD not in data:
            raise FormatDrift("KM page: no date picker")
        s = f"01{day.year}-{day.month}-{day.day}-0-0-0-0"
        data[DATE_FIELD] = f'|0|{s}||[[[[]],[],[]],[{{}},[]],"{s}"]'
    return data


# ------------------------------------------------------------------ classification
us_state_regions = state_regions          # {"TX": "tx", ...} from the region files' US-XX members (gas_ebb_common)


def region_names() -> dict[str, str]:
    from montney import regions
    return {rid: reg.name for rid, reg in regions.load().items()}


class Classifier:
    def __init__(self, params: dict, states: dict[str, str]):
        self.states = states
        up = lambda xs: tuple(x.upper() for x in xs)      # noqa: E731
        self.km = up(params.get("km_counterparties", []))
        self.mexico = up(params.get("mexico_counterparties", []))
        self.canada = up(params.get("canada_counterparties", []))
        self.lng = up(params.get("lng_counterparties", []))

    def category(self, m: dict) -> str | None:
        """Category of a point, "km" (KM-internal), "int" (other interconnect) or None (paper/other)."""
        t, cp = m["type"], m["cp"]
        if t in TYPE_CAT:
            return TYPE_CAT[t]
        if t == "STR":
            return "storage"
        if t == "VIR" and m["name"].upper().endswith("CDP"):      # NGPL's citygate aggregates ("NICORGAS/NGPL CDP")
            return "ldc"
        if t == "INT":
            if any(k in cp for k in self.km):
                return "km"
            if any(k in cp for k in self.mexico):
                return "export_mexico"
            if any(k in cp for k in self.canada):
                return "export_canada"
            if any(k in cp for k in self.lng):
                return "lng"
            return "int"
        return None

    def totals(self, sides: dict[str, list[dict[str, str]]], locs: dict[str, dict]) -> tuple[dict, dict, list[str]]:
        """({(region, cat): MMcf/d}, diagnostics {region: {...}}, unknown loc ids) from the Delivery/Receipt rows."""
        tot: dict[tuple[str, str], float] = {}
        diag: dict[str, dict[str, float]] = {}
        unknown = []
        for side, rows in sides.items():
            sign_d = 1.0 if side == "Delivery" else -1.0         # +deliveries, −receipts
            for r in rows:
                q = mmcfd(num(r["Total Scheduled Quantity"]))
                if not q:
                    continue
                m = locs.get(r["Loc"].strip())
                if m is None:
                    unknown.append(r["Loc"])
                    continue
                reg = self.states.get(m["st"])
                if reg is None:
                    continue
                cat = self.category(m)
                d = diag.setdefault(reg, {})
                if cat == "storage":
                    key = "storage_inj" if side == "Delivery" else "storage_wd"
                    tot[(reg, key)] = tot.get((reg, key), 0.0) + q
                elif cat in DEMAND_LIKE:
                    tot[(reg, cat)] = tot.get((reg, cat), 0.0) + sign_d * q
                elif cat == "production":
                    tot[(reg, cat)] = tot.get((reg, cat), 0.0) - sign_d * q
                elif cat == "km":
                    d["km_internal"] = d.get("km_internal", 0.0) + q
                elif cat == "int":
                    k = "int_out" if side == "Delivery" else "int_in"
                    d[k] = d.get(k, 0.0) + q
                else:
                    d["other"] = d.get("other", 0.0) + q
        return tot, diag, unknown


def point_locations(sides: dict[str, list[dict[str, str]]], locs: dict[str, dict]) -> list[tuple[str, str, float, bool]]:
    """(loc, OA name | location-file name, |TSQ| Dth, has a state) of every Delivery / Receipt row, for the no-state
    guard (gas_ebb_common.require_located). |TSQ|: KM nets both directions at a bidirectional point, so a no-state
    point's quantity counts whichever its sign. A loc missing from the location file has no state."""
    out = []
    for rows in sides.values():
        for r in rows:
            m = locs.get(r["Loc"].strip())
            name = " | ".join(x for x in (r.get("Loc Name", "").strip(), (m or {}).get("name", "")) if x)
            out.append((r["Loc"].strip(), name, abs(num(r["Total Scheduled Quantity"])), bool(m and m["st"].strip())))
    return out


def segment_border(rows: list[dict[str, str]], b: dict, what: str) -> float:
    """Σ TSQ of the named segments in the `forward` direction − Σ in the other (MMcf/d). A name gone → FormatDrift."""
    fwd = b.get("forward", "TD1")
    want = {(str(n), str(nm)) for n, nm in b["segments"]}
    seen, v = set(), 0.0
    for r in rows:
        key = (r["Loc (Segment)"].strip(), r["Loc Name (Segment)"].strip())
        if key in want:
            seen.add(key)
            q = mmcfd(num(r["Total Scheduled Quantity"]))
            v += q if r["Flow Ind"].strip() == fwd else -q
    require_present(want, seen, f"{what}: border {b['id']}: segment(s)", "gone")
    return v


def point_border(sides: dict[str, list[dict[str, str]]], locs: dict, b: dict, what: str) -> float:
    """(deliveries − receipts) × sign at the listed locs. Only listed locs (Reviewer on #476): a counterparty-name match
    would drop a point silently when its name is reworded or its loc re-coded; a listed loc that's gone is FormatDrift."""
    ids = {str(x) for x in b.get("locs", [])}
    if not ids:
        raise ValueError(f"{what}: border {b['id']}: needs `segments` or `locs`")
    seen, v = set(), 0.0
    for side, rows in sides.items():
        for r in rows:
            loc = r["Loc"].strip()
            if loc in ids:
                seen.add(loc)
                q = mmcfd(num(r["Total Scheduled Quantity"]))
                v += q if side == "Delivery" else -q
    require_present(ids, seen, f"{what}: border {b['id']}: point(s)", "gone")
    return v * float(b.get("sign", 1))


# ------------------------------------------------------------------ connector
class GasEbbKm(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        p = spec.get("params", {})
        self.p = p
        self.base = p.get("base", BASE)
        self.boards: dict[str, dict] = p.get("boards", {})
        self.borders: list[dict] = p.get("borders", [])
        self.sleep = time.sleep          # tests replace this
        self._hits: dict[str, float] = {}           # host -> when the last request to it started (host_wait)
        self._loc_miss: dict[str, set[str]] = {}    # board -> locs with gas its cached location file lacks (this run)

    # ----- series
    def _board_cats(self, code: str) -> dict[str, list[str]]:
        return self.boards[code].get("regions", {}) or {}

    def declared_series(self) -> list[SeriesMeta]:
        names = region_names()
        out, fam = [], {}
        for code, b in self.boards.items():
            pipe = b.get("name", code)
            for reg, cats in self._board_cats(code).items():
                for c in cats:
                    out.append(SeriesMeta(f"ebb.{code.lower()}.{reg}.{c}", self.id,
                                          f"{pipe}: scheduled {CAT_LABEL[c]}, {names.get(reg, reg)} (KM OA, net TSQ)",
                                          "MMcf/d", "D", GROUP, day="gas"))
                    fam.setdefault((reg, c), []).append(code)
        for (reg, c) in sorted(fam):
            out.append(SeriesMeta(f"ebb.km.{reg}.{c}", self.id,
                                  f"Kinder Morgan pipes: scheduled {CAT_LABEL[c]}, {names.get(reg, reg)} "
                                  f"({' + '.join(fam[(reg, c)])}; KM-to-KM transfers removed)",
                                  "MMcf/d", "D", GROUP, day="gas"))
        for b in self.borders:
            out.append(SeriesMeta(b["id"], self.id, b.get("label", b["id"]) + " (scheduled, + = from → to)",
                                  "MMcf/d", "D", GROUP, day="gas"))
        return out

    def _family(self) -> dict[tuple[str, str], list[str]]:
        fam: dict[tuple[str, str], list[str]] = {}
        for code in self.boards:
            for reg, cats in self._board_cats(code).items():
                for c in cats:
                    fam.setdefault((reg, c), []).append(code)
        return fam

    # ----- HTTP (≥ pause_s between the starts of two requests to the host, all counted by ctx)
    def _pace(self, url: str):
        host = host_wait(url, float(self.p.get("pause_s", 1.0)), self._hits, self.sleep)
        self._hits[host] = time.monotonic()

    def _get(self, ctx: Context, url: str) -> str:
        self._pace(url)
        return ctx.get(url, timeout=180, retries=1).text

    def _post(self, ctx: Context, url: str, data: dict) -> bytes:
        self._pace(url)
        return ctx.post(url, data=data, timeout=180, retries=1).content

    # ----- schedule
    def _due_current(self, st: dict, now_ct: datetime) -> bool:
        cur = st.get("cur")
        if not cur or cur[0] != now_ct.date().isoformat():
            return True
        posted_ct = datetime.fromisoformat(cur[2]).replace(tzinfo=timezone.utc).astimezone(CENTRAL).replace(tzinfo=None)
        for hm in self.p.get("cycle_times_ct", ["14:00", "17:30", "22:00"]):
            h, m = map(int, hm.split(":"))
            b = now_ct.replace(hour=h, minute=m, second=0, microsecond=0)
            if b <= now_ct and posted_ct < b - timedelta(minutes=int(self.p.get("post_slack_min", 60))):
                return True
        return False

    def _final_day(self, st: dict, now_ct: datetime) -> date | None:
        h, m = map(int, self.p.get("final_after_ct", "09:00").split(":"))
        if (now_ct.hour, now_ct.minute) < (h, m):
            return None
        y = now_ct.date() - timedelta(days=1)
        return None if st.get("final") == y.isoformat() else y

    # ----- fetch
    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        state = cache_load(STATE)
        bstate: dict = state.setdefault("boards", {})
        vals: dict = state.setdefault("vals", {})
        diags: dict = state.setdefault("diag", {})
        cls = Classifier(self.p, us_state_regions())
        now_ct = ctx.now.replace(tzinfo=timezone.utc).astimezone(CENTRAL).replace(tzinfo=None)
        budget = int(self.p.get("max_requests", 60))
        start = ctx.n_requests
        loc_age = timedelta(days=float(self.p.get("location_max_age_days", 7)))
        touched: set[str] = set()
        errors: list[Exception] = []
        attempted = 0
        self._loc_miss = {}

        order = sorted(self.boards, key=lambda c: bstate.get(c, {}).get("ok", ""))
        for code in order:
            st = bstate.setdefault(code, {})
            cfg = self.boards[code]
            has_points = bool(self._board_cats(code)) or any(b["board"] == code and "segments" not in b
                                                             for b in self.borders)
            segs = [b for b in self.borders if b["board"] == code and "segments" in b]
            due_cur = self._due_current(st, now_ct)
            final = self._final_day(st, now_ct)
            locs = loc_cache_read(f"ebb_km_loc_{code}", "locs")[0] if has_points else {}
            need_loc = has_points and not locs
            if not (due_cur or final or need_loc):
                continue
            worst = 2 * need_loc + (1 + 2 * bool(due_cur) + 2 * bool(final) if has_points else 0) + \
                (1 + bool(due_cur) + bool(final) if segs else 0)
            if ctx.n_requests - start + worst > budget:
                res.warnings.append(f"{code}: deferred to the next run (request budget {budget})")
                continue
            attempted += 1
            try:
                if need_loc:
                    locs = self._locations(ctx, code)
                    loc_cache_write(f"ebb_km_loc_{code}", "locs", ctx.now, locs)
                obs, pulls = self._board(ctx, code, cfg, st, segs, has_points, due_cur, final, locs, cls, diags, res)
                res.obs.extend(obs)
                for day, tot, as_of in pulls:
                    vals.setdefault(day, {})[code] = {"as_of": as_of, "v": tot}
                    touched.add(day)
                st["ok"] = ctx.now.isoformat()
            except Exception as e:  # noqa: BLE001 - one board's failure must not lose the others'
                errors.append(e)
                res.warnings.append(f"{code}: {type(e).__name__}: {e}")
        failed = bool(attempted and len(errors) == attempted)

        # location refresh, at most 2 a run, budget permitting: first a board whose guard failed on locs its cached file
        # lacks (once a day; even when every board failed, or a lone failing board would never get it), then weekly
        # (stale, not missing; only when some board succeeded)
        cached = {c: loc_cache_read(f"ebb_km_loc_{c}", "locs") for c in self.boards}
        early = [c for c, need in self._loc_miss.items() if loc_cache_lacks(cached[c][0], need, cached[c][1], ctx.now)]
        stale = [] if failed else [c for c in self.boards
                                   if cached[c][0] and loc_cache_due(cached[c][1], ctx.now, loc_age)]
        for code in (early + [c for c in stale if c not in early])[:2]:
            if ctx.n_requests - start + 2 > budget:
                break
            try:
                loc_cache_write(f"ebb_km_loc_{code}", "locs", ctx.now, self._locations(ctx, code))
            except Exception as e:  # noqa: BLE001
                res.warnings.append(f"{code}: location refresh: {type(e).__name__}: {e}")
        if failed:
            raise errors[0]

        res.obs.extend(self._rollup(vals, touched))
        for d in sorted(vals)[:-4]:          # keep 4 gas days of per-board totals for the roll-up
            del vals[d]
        cache_save(STATE, state)
        return res

    def _locations(self, ctx: Context, code: str) -> dict[str, dict[str, str]]:
        url = f"{self.base}/LocationDataDownload/LocDataDwnld.aspx?code={code}"
        page = self._get(ctx, url)
        body = self._post(ctx, url, form(page))
        return parse_locations(body.decode("utf-8-sig", errors="replace"), f"{code} locations")

    def _board(self, ctx, code, cfg, st, segs, has_points, due_cur, final, locs, cls, diags, res):
        """Pull one board: current (probe first) and/or yesterday's final. Returns (obs, [(day, totals, as_of)])."""
        obs: list[Obs] = []
        pulls = []
        updates: dict = {}
        purl = f"{self.base}/Capacity/OpAvailPoint.aspx?code={code}"
        surl = f"{self.base}/Capacity/OpAvailSegment.aspx?code={code}"
        ppage = self._get(ctx, purl) if has_points else None
        spage = None

        def seg_rows(day: date | None):
            nonlocal spage
            if spage is None:
                spage = self._get(ctx, surl)
            return segment_rows(self._post(ctx, surl, form(spage, day=day)), f"{code} segments")

        jobs = []
        if due_cur:
            jobs.append(None)
        if final:
            jobs.append(final)
        for day in jobs:
            what = f"{code} {'current' if day is None else day.isoformat()}"
            sides: dict[str, list] = {}
            srows = None
            if has_points:
                key, sides["Delivery"] = point_rows(self._post(ctx, purl, form(ppage, RADIO[0], day)), "Delivery", what)
            else:
                key, srows = seg_rows(day)
            gas_day, cycle, posted = key
            if day is not None and gas_day != day:
                raise FormatDrift(f"{what}: asked for gas day {day}, got {gas_day}")
            kst = [gas_day.isoformat(), cycle, posted.isoformat()]
            if day is None and st.get("cur") == kst:
                continue                                   # unchanged since the last run: skip the other POSTs
            if has_points:
                key_r, sides["Receipt"] = point_rows(self._post(ctx, purl, form(ppage, RADIO[1], day)), "Receipt", what)
                if key_r[0] != gas_day:
                    raise FormatDrift(f"{what}: Delivery gas day {gas_day}, Receipt {key_r[0]}")
            if segs and srows is None:
                skey, srows = seg_rows(day)
                if skey[0] != gas_day:
                    raise FormatDrift(f"{what}: points gas day {gas_day}, segments {skey[0]}")
            ts = datetime(gas_day.year, gas_day.month, gas_day.day)
            pts = point_locations(sides, locs)
            try:
                require_located(pts, cfg.get("unlocated_ok"), what)       # the board writes nothing this run
            except FormatDrift:
                miss = {loc for loc, _, q, _ in pts if q > 0 and loc not in locs}
                if miss:                                                 # re-read its location file early
                    self._loc_miss[code] = miss
                raise
            tot, diag, unknown = cls.totals(sides, locs)
            if unknown:
                res.warnings.append(f"{what}: {len(unknown)} loc(s) not in the location file (e.g. {unknown[:3]})")
            declared = {(r, c) for r, cs in self._board_cats(code).items() for c in cs}
            for (reg, c) in sorted(declared):
                obs.append(Obs(f"ebb.{code.lower()}.{reg}.{c}", ts, round(tot.get((reg, c), 0.0), 3), posted))
            stray = {k: round(v, 1) for k, v in tot.items() if k not in declared and abs(v) >= 1}
            if stray:
                res.warnings.append(f"{what}: undeclared region/category flows {stray}")
            for b in self.borders:
                if b["board"] != code:
                    continue
                v = segment_border(srows, b, what) if "segments" in b else point_border(sides, locs, b, what)
                obs.append(Obs(b["id"], ts, round(v, 3), posted))
            bd = diags.setdefault(code, {})
            bd[gas_day.isoformat()] = {"cycle": cycle, "regions": diag}
            for d in sorted(bd)[:-4]:
                del bd[d]
            pulls.append((gas_day.isoformat(), {f"{r}.{c}": v for (r, c), v in tot.items() if (r, c) in declared},
                          posted.isoformat()))
            updates["cur" if day is None else "final"] = kst if day is None else day.isoformat()
        st.update(updates)          # only once every job of the board succeeded (a failure retries them all)
        return obs, pulls

    def _rollup(self, vals: dict, days: set[str]) -> list[Obs]:
        """ebb.km.<region>.<cat> for the touched gas days, once every board that carries (region, cat) has that day."""
        out = []
        for day in sorted(days):
            have = vals.get(day, {})
            ts = datetime.fromisoformat(day)
            for (reg, c), codes in sorted(self._family().items()):
                if not all(code in have for code in codes):
                    continue
                v = sum(have[code]["v"].get(f"{reg}.{c}", 0.0) for code in codes)
                as_of = max(datetime.fromisoformat(have[code]["as_of"]) for code in codes)
                out.append(Obs(f"ebb.km.{reg}.{c}", ts, round(v, 3), as_of))
        return out
