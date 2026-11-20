"""Bakken interstate EBBs: WBI Energy Transmission (`gas_ebb_wbi`) and Alliance Pipeline US (`gas_ebb_alliance_us`), summed per
region and category with the engine in gas_ebb_et.py (categories, overrides, borders, the no-state guard: see its
docstring). All values are SCHEDULED quantities (TSQ, MMBtu at 1,037 Btu/cf -> MMcf/d), freq D, gas day = ts. A row with
gas and no state fails that gas day (`require_states`).

## WBI Energy Transmission (`gas_ebb_wbi`): MT / ND / WY / SD

Source: WBI's own informational postings (ASP.NET WebForms, public, no login, no robots.txt):
  https://transmission.wbienergy.com/informational_postings/capacity/Operational_Capacity_Locations.aspx
1. GET the page for its `__VIEWSTATE` / `__EVENTVALIDATION` fields (~38 KB);
2. POST the form back with `btnDownload=Download` and `calPrvGasDay=MM/DD/YYYY` → a tab-separated
   `WBI_Tran_Operational_Capacity_Locations.txt` (~380 rows, ~550 KB, mostly a repeated comment column): TSP, Post
   Date, Post Time, Loc, Loc Name, Loc Purp, Loc/QTI, Flow Ind, ..., TSQ, ..., Eff Gas Day, Eff Time, Cycle, ...
   The same form state serves the second POST for yesterday (one GET per run).
The file carries the gas day, the cycle and the post time (Central): as_of = the post time.
Locations: `.../Locations/LocationsData.aspx`, GET + POST `drpLocSelection=Receipt/Delivery`, `butDownload` → NAESB CSV
(~78 KB: state, county, type, Up/Dn Name), at most weekly into `<home>/cache/ebb_locs_WBI.json`.
Traps: storage rows are aggregate, non-geographical (state "MU", Flow Ind SI inject / SW withdraw): the spec pins them
to bakken (WBI's storage is at Baker MT); "WBI-Agg Storage (Cap.)" is the certificated inventory, not a flow, and is
skipped. Most of WBI's ND plant gas goes to Northern Border *inside* ND (`to_nbpl`): an internal transfer, not an
export. Requests: 2 per run + 1 once a day (yesterday) + 2 a week (locations): ~25 a day at 2 h.

## Alliance Pipeline US (`gas_ebb_alliance_us`): the Canadian border, Tioga, Bantry, ND -> MN

Source: Pembina's informational-postings API behind ips.alliancepipeline.pembina.com/us (the site's own call; the same
backend as gas_alliance's Canadian side):
  POST https://content.alliancepipeline.pembina.com/api/operationallyavailablecapacities/search
       {"page": 0, "pageSize": 500, "sortFields": [], "startDate": "<YYYY-MM-DD>T00:00:00", "filterFields": []}
→ JSON `data` (~21 rows, ~10 KB): locId, locNm, gasDay, cycleDescr, postDt (Central), flowIndCd, totalSchdQty
(MMBtu), ... One gas day per request (its latest cycle); as_of = postDt. The state is the locId prefix (ND08, MN18,
IA06, IL01); `UX` "BORDER USA" is the Canadian border at Sherwood ND. The site's date picker goes back 3 years.
**robots.txt** (Cloudflare-managed): `User-agent: * Allow: /`, content signals `search=yes, ai-train=no,
use=reference`, and AI crawlers (ClaudeBot, GPTBot, ...) disallowed. This is a scheduled data connector, not an AI
crawler or a training use; it sends the runner's own plain User-Agent (montney.runner.USER_AGENT), one request per
run.
Border mirror: wcan's export line uses `alliance.receipts.total` (gas_alliance: Canadian receipts in **Mcf/d**, compressor
fuel included); bakken's import is `ebb.alliance.border.canada_nd` (the US border receipt in **MMBtu**/1,037). They
differ by Alliance's rich heating value (~1,100+ Btu/cf at the border) and its Canadian fuel, so they won't match
exactly: compare shapes, not levels. Requests: 1 per run + 1 once a day (yesterday): ~13 a day at 2 h.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_ebb_et import (
    NAESB_LOC, Row, aggregate, cached_locations, check_overrides, ct_to_utc, declared, gas_days, num, paced,
    parse_locations, read_csv, require_states, to_obs,
)
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save
from montney.store import SeriesMeta

WBI_OA = "https://transmission.wbienergy.com/informational_postings/capacity/Operational_Capacity_Locations.aspx"
WBI_LOC = "https://transmission.wbienergy.com/informational_postings/Locations/LocationsData.aspx"
WBI_NEED = {"Loc", "Loc Name", "Flow Ind", "TSQ", "Eff Gas Day", "Cycle", "Post Date", "Post Time"}
ALLIANCE_API = "https://content.alliancepipeline.pembina.com/api/operationallyavailablecapacities/search"
ALLIANCE_NEED = {"locId", "locNm", "gasDay", "cycleDescr", "postDt", "flowIndCd", "totalSchdQty"}


def form_fields(page: str) -> dict[str, str]:
    """An ASP.NET page's hidden inputs and text inputs (not its buttons), and its selects' chosen values."""
    form: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", page):
        n = re.search(r'name="([^"]*)"', tag)
        t = re.search(r'type="([^"]*)"', tag)
        if not n or (t and t.group(1).lower() in ("submit", "checkbox", "image", "button")):
            continue
        v = re.search(r'value="([^"]*)"', tag)
        form[n.group(1)] = html.unescape(v.group(1)) if v else ""
    for name, body in re.findall(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', page, re.S):
        sel = (re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', body)
               or re.search(r'<option[^>]*value="([^"]*)"', body))
        form[name] = html.unescape(sel.group(1)) if sel else ""
    if "__VIEWSTATE" not in form:
        raise FormatDrift("WBI: no __VIEWSTATE on the form page")
    return form


def read_tsv(text: str, need: set[str], where: str) -> list[dict[str, str]]:
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].lstrip().startswith("<"):
        raise FormatDrift(f"{where}: HTML instead of the tab-separated download")
    head = [h.strip() for h in lines[0].split("\t")]
    if not need <= set(head):
        raise FormatDrift(f"{where}: header lacks {sorted(need - set(head))}")
    return [dict(zip(head, (c.strip() for c in ln.split("\t")))) for ln in lines[1:] if ln.strip()]


def wbi_rows(table: list[dict[str, str]], locs: dict[str, list[str]]) -> tuple[list[Row], date, datetime, str]:
    """(rows, gas day, post time UTC, cycle) of one WBI download; a file mixing gas days is FormatDrift."""
    days = {r["Eff Gas Day"] for r in table}
    if len(days) != 1:
        raise FormatDrift(f"WBI: {len(days)} gas days in one download: {sorted(days)[:4]}")
    day = datetime.strptime(days.pop(), "%Y%m%d").date()
    posted = max(ct_to_utc(datetime.strptime(f"{r['Post Date']} {r['Post Time']}", "%Y%m%d %H:%M")) for r in table)
    rows = []
    for r in table:
        fl = {"SI": "D", "SW": "R"}.get(r["Flow Ind"].strip().upper(), r["Flow Ind"].strip().upper()[:1])
        if fl not in ("R", "D"):
            continue
        st, _cty, lt, cp = locs.get(r["Loc"], ["", "", "", ""])
        rows.append(Row(r["Loc"], r["Loc Name"], fl, num(r["TSQ"]), st, lt, cp))
    return rows, day, posted, table[0]["Cycle"]


class _Ebb(Connector):
    GROUP = "Interstate EBB"
    PIPE = ""

    def cfg(self) -> dict:
        return self.spec.get("params", {})

    def declared_series(self) -> list[SeriesMeta]:
        return declared(self.id, self.GROUP, {self.PIPE: self.cfg()})

    def days(self, ctx: Context) -> tuple[list[date], date | None, dict]:
        state = cache_load(f"{self.id}_state")
        d, mark = gas_days(ctx, state, int(self.cfg().get("yesterday_after_ct", 11)))
        return d, mark, state

    def done(self, mark: date | None, state: dict) -> None:
        if mark is not None:
            state["yesterday"] = mark.isoformat()
            cache_save(f"{self.id}_state", state)


class EbbWbi(_Ebb):
    GROUP = "Interstate EBB: WBI Energy Transmission"
    PIPE = "wbi"

    def _locations(self, ctx: Context, need: set[str]) -> dict[str, list[str]]:
        url = self.cfg().get("locations_url", WBI_LOC)

        def fetch():
            form = form_fields(paced(ctx, "GET", url, timeout=60).text)
            form.update({"drpLocSelection": "Receipt/Delivery", "butDownload": "Download"})
            r = paced(ctx, "POST", url, data=form, timeout=120)
            return parse_locations(read_csv(r.text, NAESB_LOC, "WBI locations"))
        return cached_locations(ctx, "WBI", fetch, need)

    def fetch(self, ctx: Context) -> FetchResult:
        url = self.cfg().get("url", WBI_OA)
        days, mark, state = self.days(ctx)
        res = FetchResult(meta=self.declared_series())
        form = form_fields(paced(ctx, "GET", url, timeout=60).text)
        for day in days:
            f = dict(form, calPrvGasDay=day.strftime("%m/%d/%Y"), btnDownload="Download")
            r = paced(ctx, "POST", url, data=f, timeout=120)
            table = read_tsv(r.text, WBI_NEED, f"WBI {day}")
            if not table:
                res.warnings.append(f"WBI {day}: no rows yet")
                continue
            locs = self._locations(ctx, {t["Loc"] for t in table})
            rows, got, posted, cycle = wbi_rows(table, locs)
            if got != day:
                res.warnings.append(f"WBI: asked for {day}, got gas day {got} (stored as {got})")
            check_overrides(rows, self.cfg(), f"WBI {got}")
            require_states(rows, self.cfg(), f"WBI {got}")
            res.obs += to_obs(aggregate(self.PIPE, rows, self.cfg()), got, posted)
            res.warnings.append(f"WBI {day}: {cycle}")
        self.done(mark, state)
        return res


def alliance_rows(data: list[dict]) -> tuple[list[Row], date, datetime, str]:
    if not data:
        return [], date.min, datetime.min, ""
    for r in data:
        if not ALLIANCE_NEED <= set(r):
            raise FormatDrift(f"Alliance US: row lacks {sorted(ALLIANCE_NEED - set(r))}")
    days = {r["gasDay"] for r in data}
    if len(days) != 1:
        raise FormatDrift(f"Alliance US: {len(days)} gas days in one answer")
    day = datetime.strptime(days.pop(), "%m/%d/%Y").date()
    posted = max(ct_to_utc(datetime.strptime(r["postDt"], "%m/%d/%Y %I:%M:%S %p")) for r in data)
    rows = []
    for r in data:
        loc = str(r["locId"]).strip()
        st = "ND" if loc == "UX" else loc[:2]
        fl = str(r["flowIndCd"]).strip().upper()[:1]
        if fl in ("R", "D"):
            rows.append(Row(loc, str(r["locNm"]).strip(), fl, float(r["totalSchdQty"] or 0), st))
    return rows, day, posted, data[0]["cycleDescr"]


class EbbAllianceUs(_Ebb):
    GROUP = "Interstate EBB: Alliance Pipeline US"
    PIPE = "alliance"

    def fetch(self, ctx: Context) -> FetchResult:
        url = self.cfg().get("api", ALLIANCE_API)
        days, mark, state = self.days(ctx)
        res = FetchResult(meta=self.declared_series())
        for day in days:
            body = {"page": 0, "pageSize": 500, "sortFields": [], "startDate": f"{day.isoformat()}T00:00:00",
                    "filterFields": []}
            j = paced(ctx, "POST", url, json=body, timeout=60).json()
            if not isinstance(j, dict) or "data" not in j:
                raise FormatDrift("Alliance US: no 'data' in the answer")
            rows, got, posted, cycle = alliance_rows(j["data"])
            if not rows:
                res.warnings.append(f"Alliance US {day}: no rows yet")
                continue
            if got != day:
                res.warnings.append(f"Alliance US: asked for {day}, got gas day {got} (stored as {got})")
            check_overrides(rows, self.cfg(), f"Alliance US {got}")
            require_states(rows, self.cfg(), f"Alliance US {got}")
            res.obs += to_obs(aggregate(self.PIPE, rows, self.cfg()), got, posted)
            res.warnings.append(f"Alliance US {day}: {cycle}")
        self.done(mark, state)
        return res
