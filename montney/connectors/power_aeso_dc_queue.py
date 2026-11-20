"""Alberta data-centre load in the AESO connection queue, monthly (PLAN 9 data centres, wcan / ab_nebc).

Alberta publishes no operating data-centre MW (no AESO or GoA figure; the AESO list has had no "Recently Energized"
data-load row through Nov 2026). What is public is the queue: the **AESO Connection Project List**, a monthly XLSX
(``<Month>-<YYYY>-Project-List.xlsx``) linked from
https://www.aeso.ca/grid/transmission-projects/connection-project-reporting/ (posted early in the month; lists back to
January 2023 are linked there).

Sheet "Connection Project List", one row per project: ``Status, Project Name, Planning Area, Cluster, Project Type,
MW Type, Stage, ..., EN1 STS MW, EN1 DTS MW, EN1 ISD, EN2 ..., EN3 ...``. A data-centre row has ``MW Type`` "Data Load"
or a combination ("Gas + Data Load", "Battery + Data Load", "Cogen + Data Load": co-located supply). Its load is the
**DTS** (demand transmission service) MW; STS is the supply side and is never counted.

The EN1-EN3 stages are **increments** (energization steps), so they add up: GLDC Load Phase 1.1 is 60 + 910 = 970 MW,
and Keephills Phase 1.1 115 + 115 = 230 MW, which are exactly AESO's Phase 1 allocations (970 + 230 = 1,200 MW). And
for the July 2026 list, Active + ISD Under Review = **19,565 MW**, exactly the Government of Alberta's "~19,565 MW of
new data centre load is seeking a connection (AESO list, 30 Jul 2026)". So:

  aeso.queue.data_load_mw          DTS MW (EN1 + EN2 + EN3) of the data-load rows still in the queue: every status
                                   except Recently Energized / Recently Cancelled (Active, ISD Under Review, Cluster N,
                                   On Hold). Requests, not approvals; not energized load.
  aeso.queue.data_load_projects    how many such rows
  aeso.queue.data_load_mw.active | .under_review | .cluster | .on_hold    the same MW by status
  aeso.queue.data_load_energized_mw    DTS MW of "Recently Energized" data-load rows (a rolling recent window, not a
                                   cumulative total; 0 through Nov 2026)

MW, freq M, ts = the list's month (first day). Only lists in the current layout (with ``Status``, "Data Load" and
``EN1 DTS MW``, from November 2024) are read; older lists are skipped. AESO's "Data Load" may include crypto mining.

TLS: the shared client (httpx, certifi's CA bundle, verification on) reads www.aeso.ca as-is (checked 2026-11-13, the
index and the lists). The research note's TLS failure was git-bash curl's OpenSSL setup, not this client. Verification
is never turned off.

Requests: the index page once a day (``ctx.get_if_changed``), and each list once: its parsed result is kept in
``<home>/cache/aeso_dc_queue/parsed.json`` (with the parser version), so a file is downloaded only when a new month's
list appears. The first run back-fills the lists from ``from`` (Nov 2024: ~24 files, ~50 KB each), at most
``backfill_per_run`` a run.

Fail closed (FormatDrift): the newest list not in the expected layout, an unknown Status, a DTS value that isn't a
number or is outside 0-10,000 MW.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from datetime import datetime
from urllib.parse import urljoin

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

INDEX = "https://www.aeso.ca/grid/transmission-projects/connection-project-reporting/"
SHEET = "Connection Project List"
GROUP = "AESO / Connection queue: data-centre load (monthly)"
PARSER = 1
MAX_MW = 10_000
MONTHS = {m: i for i, m in enumerate(("january february march april may june july august september october november "
                                      "december").split(), 1)}
LINK = re.compile(r'href="([^"]*/project-reporting/([A-Za-z]+)-(\d{4})-Project-List\.xlsx)"', re.I)
STATUS = {"active": "active", "isd under review": "under_review", "on hold": "on_hold",
          "recently energized": "energized", "recently cancelled": "cancelled"}
OPEN = ("active", "under_review", "cluster", "on_hold")
REQUIRED = ("Status", "Project Name", "MW Type", "EN1 DTS MW", "EN2 DTS MW", "EN3 DTS MW")


# ------------------------------------------------------------------ a small stdlib .xlsx reader
def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        n = n * 26 + ord(ch) - 64
    return n


def _text(xml: str) -> str:
    return html.unescape("".join(re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", xml, re.S)))


def xlsx_rows(content: bytes, sheet: str = SHEET) -> list[list[str]]:
    """The named sheet (else the first) as rows of cell text, by column position; blank cells are ""."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        names = set(z.namelist())
        ss = ([_text(si) for si in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
              if "xl/sharedStrings.xml" in names else [])
        path = "xl/worksheets/sheet1.xml"
        wb = z.read("xl/workbook.xml").decode("utf-8")
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        for name, rid in re.findall(r'<sheet\b[^>]*\bname="([^"]*)"[^>]*\br:id="([^"]+)"', wb):
            if html.unescape(name) == sheet:
                t = re.search(rf'<Relationship\b[^>]*\bId="{re.escape(rid)}"[^>]*\bTarget="([^"]+)"', rels) or \
                    re.search(rf'<Relationship\b[^>]*\bTarget="([^"]+)"[^>]*\bId="{re.escape(rid)}"', rels)
                if t:
                    path = "xl/" + t.group(1).lstrip("/").removeprefix("xl/")
        xml = z.read(path).decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        raise FormatDrift(f"AESO project list: not the expected .xlsx ({e})") from e
    out = []
    for row in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, re.S):
        cells: dict[int, str] = {}
        for m in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
            attrs, body = m.group(1), m.group(2) or ""
            ref = re.search(r'\br="([A-Z]+)\d+"', attrs)
            if not ref:
                continue
            t = re.search(r'\bt="(\w+)"', attrs)
            kind = t.group(1) if t else ""
            if kind == "inlineStr":
                val = _text(body)
            else:
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                if v is None:
                    continue
                val = ss[int(v.group(1))] if kind == "s" else html.unescape(v.group(1))
            cells[_col(ref.group(1))] = val.strip()
        out.append([cells.get(i, "") for i in range(1, max(cells, default=0) + 1)])
    return out


# ------------------------------------------------------------------ the list
def _mw(raw: str, where: str) -> float:
    raw = raw.strip()
    if not raw:
        return 0.0                                       # no such stage
    try:
        v = float(raw)
    except ValueError:
        raise FormatDrift(f"AESO project list {where}: DTS {raw!r} isn't a number") from None
    if not 0 <= v <= MAX_MW:
        raise FormatDrift(f"AESO project list {where}: DTS {v} MW is outside 0-{MAX_MW}")
    return v


def status_key(raw: str) -> str:
    s = re.sub(r"\s+", " ", raw.strip().lower())
    if re.fullmatch(r"cluster \d+", s):
        return "cluster"
    if s not in STATUS:
        raise FormatDrift(f"AESO project list: unknown Status {raw!r} (map it in STATUS)")
    return STATUS[s]


def parse_list(content: bytes) -> dict | None:
    """{mw: {status key: DTS MW}, n: {status key: rows}} of the data-load rows, or None for an old-layout list."""
    rows = xlsx_rows(content)
    hi = next((i for i, r in enumerate(rows[:10]) if "MW Type" in r), None)
    if hi is None:
        return None
    head = rows[hi]
    if any(c not in head for c in REQUIRED):
        return None
    ix = {c: head.index(c) for c in REQUIRED}
    mw = {k: 0.0 for k in (*OPEN, "energized", "cancelled")}
    n = {k: 0 for k in mw}
    for r in rows[hi + 1:]:
        r = r + [""] * (len(head) - len(r))
        if not r[ix["Project Name"]] and not r[ix["Status"]]:
            continue
        if "data load" not in r[ix["MW Type"]].lower():
            continue
        k = status_key(r[ix["Status"]])
        where = r[ix["Project Name"]]
        mw[k] += sum(_mw(r[ix[f"EN{i} DTS MW"]], where) for i in (1, 2, 3))
        n[k] += 1
    return {"mw": {k: round(v, 3) for k, v in mw.items()}, "n": n}


def links(page: str) -> dict[str, str]:
    """{"YYYY-MM": absolute url} of the project lists on the index page."""
    out = {}
    for href, mon, year in LINK.findall(page):
        m = MONTHS.get(mon.lower())
        if m:
            out[f"{int(year):04d}-{m:02d}"] = urljoin(INDEX, href)
    return out


class AesoDataCentreQueue(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"region": "wcan", "url": INDEX, "scope": "queue (requests, not approvals), not energized load"}
        lab = {"active": "Active", "under_review": "ISD Under Review", "cluster": "in a cluster study",
               "on_hold": "On Hold"}
        out = [SeriesMeta("aeso.queue.data_load_mw", self.id,
                          "Alberta data-centre load seeking connection (AESO project list, DTS MW)", "MW", "M", GROUP,
                          extra=ex),
               SeriesMeta("aeso.queue.data_load_projects", self.id,
                          "Alberta data-centre projects seeking connection (AESO project list)", "projects", "M",
                          GROUP, extra=ex),
               SeriesMeta("aeso.queue.data_load_energized_mw", self.id,
                          "Alberta data-centre load recently energized (AESO project list, DTS MW)", "MW", "M", GROUP,
                          extra=ex)]
        out += [SeriesMeta(f"aeso.queue.data_load_mw.{k}", self.id,
                           f"Alberta data-centre load seeking connection, {lab[k]} (AESO, DTS MW)", "MW", "M", GROUP,
                           extra=ex) for k in OPEN]
        return out

    def _cache_path(self):
        return paths.home() / "cache" / "aeso_dc_queue" / "parsed.json"

    def _load(self) -> dict:
        try:
            c = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        return c if c.get("parser") == PARSER else {"parser": PARSER, "lists": {}}

    def _save(self, c: dict) -> None:
        p = self._cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(p)

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        per_run = int(p.get("backfill_per_run", 60))
        first = str(p.get("from", "2024-11"))
        res = FetchResult(meta=self.declared_series())
        r = ctx.get_if_changed(INDEX, timeout=60)
        c = self._load()
        if r is None:
            return res                                   # index unchanged: no new list
        found = {m: u for m, u in links(r.text).items() if m >= first}
        if not found:
            raise FormatDrift("AESO connection-project page: no <Month>-<YYYY>-Project-List.xlsx links")
        newest = max(found)
        todo = [m for m in sorted(found, reverse=True) if c["lists"].get(m, {}).get("url") != found[m]]
        try:
            for i, m in enumerate(todo):
                if i >= per_run and m != newest:
                    res.warnings.append(f"AESO back-fill: {len(todo) - i} older lists still to read "
                                        f"({per_run} per run)")
                    break
                got = parse_list(ctx.get(found[m], timeout=60).content)
                if got is None and m == newest:
                    raise FormatDrift(f"AESO project list {m}: not the expected layout (Status, MW Type, "
                                      f"EN1-3 DTS MW)")
                c["lists"][m] = {"url": found[m], "result": got}
        finally:
            self._save(c)                                # what was read so far isn't downloaded again
        for m, entry in sorted(c["lists"].items()):
            got = entry.get("result")
            if not got or m < first:
                continue
            ts = datetime.strptime(m, "%Y-%m")
            mw, n = got["mw"], got["n"]
            res.obs.append(Obs("aeso.queue.data_load_mw", ts, round(sum(mw[k] for k in OPEN), 3), ts))
            res.obs.append(Obs("aeso.queue.data_load_projects", ts, float(sum(n[k] for k in OPEN)), ts))
            res.obs.append(Obs("aeso.queue.data_load_energized_mw", ts, mw["energized"], ts))
            for k in OPEN:
                res.obs.append(Obs(f"aeso.queue.data_load_mw.{k}", ts, mw[k], ts))
        return res
