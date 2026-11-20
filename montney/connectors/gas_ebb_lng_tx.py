"""Texas LNG feedgas from interstate pipeline EBBs (`gas_ebb_lng_tx`; L-106 tx exports). Both values are SCHEDULED quantities
(TSQ, MMBtu at 1,037 Btu/cf -> MMcf/d), freq D, gas day = ts, as_of = the pipeline's post time (Central -> UTC).

1. **Freeport LNG** = Gulf South's 24329 "Stratton Ridge (To Freeport Lng)" delivery (`lng.tx.freeport.feedgas`).
   Source: Boardwalk's GasQuest informational postings (public JSON API behind www.gasquest.com; no login):
     POST https://reporting.prod.bwpmlp.org/infopost/infopostdetails
          {"infoPostID": 1, "tspId": 1, "pageNumber": 1, "pageSize": 30, "sortBy": "datetimePostingEffective",
           "groupCode": "INFOPOST", "sortDescending": true}
     → the newest Operational Capacity postings: `description` "11/13/2026 TD1-10:00" (gas day first), `cycleCode`
       (TIM, EVE, ID1-3 and Gulf South's hourly TD1..TD11), `reportFiles[].infoPostTrackerID` (CSV + PDF);
     GET https://reporting.prod.bwpmlp.org/infopost/postings?postingsDocumentId=<CSV tracker id>
     → the CSV, **base64-encoded** (~190 KB decoded, ~780 rows): TSP Name, TSP, Post Date/Time, Effective Gas Day,
       ..., Loc, Loc Name, ..., Flow Ind, ..., Total Scheduled Quantity, ...
   The newest posting of each wanted gas day is downloaded only if its tracker id is new (state in
   `<home>/cache/gas_ebb_lng_tx_state.json`), so a run costs 1 request (the list) + 0-2 CSVs.
   **Interstate part only:** Freeport also takes some intrastate gas at Stratton Ridge; Gulf South's Coastal Bend
   Header is most of its feed, not all. (The CBH's 5-minute meter rate, measurement.prod.bwpmlp.org/infopost/cbh/
   strattonridge, is a real-time cross-check, not used here.)
2. **Golden Pass LNG** = Golden Pass Pipeline's "Terminal" 1097217 delivery (`lng.tx.golden_pass.feedgas`).
   Source: GPPL's public postings on gasnom.com (the platform gas_vector already reads):
     GET https://www.gasnom.com/ip/goldenpass/oauc.cfm?type=1
     → one HTML table, the current posting only (~40 KB): "Posting Date/Time: November 12, 2026 08:29 PM CT", "Eff Gas
       Day/Time: November 13, 2026 09:00 AM CT", "Cycle Indicator Description: Evening"; columns Location Name, Loc,
       Loc Zn, Loc Purp Desc, Loc/QTI, Flow Ind, All Qty Avail, Design Capacity, Operating Capacity, TSQ, OAC, ...
       A receipt row repeats its delivery row's Loc with a blank name.
   1 request per run. Labelled "interstate part only" like Freeport; in fact GPPL is the terminal's only feed line.
Corpus Christi (Cheniere's CCPL) is a separate source; Trans-Pecos (Mexico) comes from Transwestern in gas_ebb_et.

Days: Gulf South: today's gas day (the Central-time date) and, once a day after 11:00 CT, yesterday's last posting.
Golden Pass: whatever gas day the page shows. Requests: ~2-3 per run at 2 h (~30-35 a day), two hosts.
FormatDrift: a changed CSV / table header, a missing Stratton Ridge or Terminal row, an unreadable gas day.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import date, datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_ebb_et import ct_to_utc, gas_days, mmcfd, num, paced, read_csv
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save, html_text
from montney.store import Obs, SeriesMeta

GS_LIST = "https://reporting.prod.bwpmlp.org/infopost/infopostdetails"
GS_FILE = "https://reporting.prod.bwpmlp.org/infopost/postings"
GS_HEADERS = {"Origin": "https://www.gasquest.com", "Referer": "https://www.gasquest.com/"}
GS_NEED = {"Post Date/Time", "Effective Gas Day", "Loc", "Loc Name", "Flow Ind", "Total Scheduled Quantity"}
GP_URL = "https://www.gasnom.com/ip/goldenpass/oauc.cfm?type=1"
GP_HEAD = ["Location Name", "Loc", "Loc Zn", "Loc Purp Desc", "Loc/QTI", "Flow Ind", "All Qty Avail",
           "Design Capacity", "Operating Capacity", "TSQ", "OAC"]
FREEPORT, GOLDEN_PASS = "lng.tx.freeport.feedgas", "lng.tx.golden_pass.feedgas"
GROUP = "Texas LNG feedgas (interstate EBBs)"


def gs_latest(postings: list[dict], day: date) -> tuple[int, str] | None:
    """(CSV tracker id, cycle) of the newest posting for gas day `day`; postings come newest first."""
    best = None
    for p in postings:
        m = re.match(r"(\d{2}/\d{2}/\d{4})", str(p.get("description", "")))
        if not m or datetime.strptime(m.group(1), "%m/%d/%Y").date() != day:
            continue
        csvs = [f for f in p.get("reportFiles") or [] if str(f.get("fileName", "")).lower().endswith(".csv")]
        if not csvs:
            continue
        when = str(p.get("datetimePostingEffective", ""))
        if best is None or when > best[0]:
            best = (when, int(csvs[0]["infoPostTrackerID"]), str(p.get("cycleCode", "")))
    return (best[1], best[2]) if best else None


def gs_freeport(raw: bytes, loc: str) -> tuple[date, datetime, float]:
    """(gas day, post time UTC, Stratton Ridge delivery TSQ Dth) from a Gulf South OA CSV (base64 or plain)."""
    try:
        text = base64.b64decode(raw, validate=True).decode("utf-8-sig")
    except (binascii.Error, ValueError):
        text = raw.decode("utf-8-sig", errors="replace")
    rows = read_csv(text, GS_NEED, "Gulf South OA")
    hit = [r for r in rows if r["Loc"] == loc and r["Flow Ind"].upper().startswith("D")]
    if not hit:
        raise FormatDrift(f"Gulf South OA: no delivery row for loc {loc} (Stratton Ridge)")
    r = hit[0]
    try:
        day = datetime.strptime(r["Effective Gas Day"], "%Y%m%d").date()
        posted = ct_to_utc(datetime.strptime(r["Post Date/Time"], "%Y%m%d %H:%M:%S"))
    except ValueError as e:
        raise FormatDrift(f"Gulf South OA: bad date in {r['Effective Gas Day']!r} / {r['Post Date/Time']!r}") from e
    return day, posted, sum(num(x["Total Scheduled Quantity"]) for x in hit)


def gp_terminal(page: str, loc: str) -> tuple[date, datetime, str, float]:
    """(gas day, post time UTC, cycle, Terminal delivery TSQ Dth) from GPPL's gasnom OA page."""
    text = html_text(page, limit=200_000)
    m_day = re.search(r"Eff Gas Day/Time:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    m_post = re.search(r"Posting Date/Time:\s*([A-Z][a-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)", text)
    m_cyc = re.search(r"Cycle Indicator Description:\s*([A-Za-z0-9 ]+?)\s+Capacity", text)
    if not (m_day and m_post):
        raise FormatDrift("Golden Pass OA: no 'Eff Gas Day/Time' / 'Posting Date/Time'")
    day = datetime.strptime(m_day.group(1), "%B %d, %Y").date()
    posted = ct_to_utc(datetime.strptime(m_post.group(1), "%B %d, %Y %I:%M %p"))
    header, tsq = None, None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        c = [re.sub(r"\s+", " ", html_text(x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if c[:2] == ["Location Name", "Loc"]:
            if c[:len(GP_HEAD)] != GP_HEAD:
                raise FormatDrift(f"Golden Pass OA: header changed: {c[:12]}")
            header = c
            continue
        if header and len(c) >= len(GP_HEAD) and c[1] == loc and c[3].startswith("Delivery"):
            tsq = num(c[9])
    if header is None:
        raise FormatDrift("Golden Pass OA: no 'Location Name, Loc, ...' header row")
    if tsq is None:
        raise FormatDrift(f"Golden Pass OA: no delivery row for loc {loc} (Terminal)")
    return day, posted, (m_cyc.group(1).strip() if m_cyc else ""), tsq


class EbbLngTx(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(FREEPORT, self.id, "Freeport LNG feedgas: Gulf South at Stratton Ridge (scheduled, interstate "
                           "part only)", "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta(GOLDEN_PASS, self.id, "Golden Pass LNG feedgas: Golden Pass Pipeline to the terminal "
                           "(scheduled, interstate part only)", "MMcf/d", "D", GROUP, day="gas")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        state = cache_load(f"{self.id}_state")
        days, mark = gas_days(ctx, state, int(p.get("yesterday_after_ct", 11)))
        res = FetchResult(meta=self.declared_series())
        # Gulf South -> Freeport
        body = {"infoPostID": 1, "tspId": int(p.get("gs_tsp", 1)), "pageNumber": 1, "pageSize": 30,
                "sortBy": "datetimePostingEffective", "groupCode": "INFOPOST", "sortDescending": True}
        j = paced(ctx, "POST", p.get("gs_list", GS_LIST), json=body, headers=GS_HEADERS, timeout=60).json()
        if not isinstance(j, dict) or "postings" not in j:
            raise FormatDrift("Gulf South postings: no 'postings' list")
        seen = state.setdefault("gs", {})
        for day in days:
            hit = gs_latest(j["postings"] or [], day)
            if hit is None:
                res.warnings.append(f"Gulf South {day}: no posting yet")
                continue
            tracker, cycle = hit
            if seen.get(day.isoformat()) == tracker:
                continue                       # already stored this posting
            r = paced(ctx, "GET", p.get("gs_file", GS_FILE), params={"postingsDocumentId": tracker},
                      headers=GS_HEADERS, timeout=120)
            got, posted, dth = gs_freeport(r.content, str(p.get("freeport_loc", "24329")))
            if got != day:
                raise FormatDrift(f"Gulf South: posting for {day} holds gas day {got}")
            res.obs.append(Obs(FREEPORT, datetime(got.year, got.month, got.day), mmcfd(dth), posted))
            seen[day.isoformat()] = tracker
            res.warnings.append(f"Gulf South {day}: {cycle}")
        state["gs"] = {k: v for k, v in sorted(seen.items())[-6:]}
        # Golden Pass Pipeline -> Golden Pass LNG
        page = paced(ctx, "GET", p.get("gp_url", GP_URL), timeout=60).text
        gday, gposted, gcyc, gdth = gp_terminal(page, str(p.get("golden_pass_loc", "1097217")))
        res.obs.append(Obs(GOLDEN_PASS, datetime(gday.year, gday.month, gday.day), mmcfd(gdth), gposted))
        res.warnings.append(f"Golden Pass {gday}: {gcyc or 'cycle n/a'}")
        if mark is not None:
            state["yesterday"] = mark.isoformat()
        cache_save(f"{self.id}_state", state)
        return res
