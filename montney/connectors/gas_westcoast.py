"""Westcoast (Enbridge BC Pipeline) critical notices: Station 2 South / Huntingdon capacity
(series) and every critical notice (events).

  GET https://infopost.enbridge.com/api/notices?businessUnit=WE&noticeType=CRI

This is the JSON behind Enbridge's informational-postings app. It returns ~12 months of critical
notices (~400 KB). The capacity numbers only exist as free text, e.g.

  Huntingdon Delivery Area Capacity: October 21, 2026: 1,858,760 GJ
  Station 2 South Capacity: October 17-18, 2026: 1,280,418 GJ October 19, 2026: 1,688,339 GJ
  Station 2 South Capacity November 1 - 2, 2026: 1,858,306 GJ
  Station 2 South Capacity October 25 ID1, 2026: ...   (ID1/ID2/ID3/EVE = intraday cycle)

Series (MMcf/d, freq D, ts = gas day, as_of = notice posting time, so a revision is a new
vintage and the newest notice wins):
  westcoast.station2_south.capacity
  westcoast.huntingdon.capacity

GJ -> MMcf: Westcoast's published heating value for Station 2 and Huntingdon is 40.000 MJ/m3
("Estimated Yearly Heat Content Values", effective 2026-11-01,
https://linkwc.enbridge.com/weheatingvalues/HV_2026_2027.pdf). 1 GJ / 40 MJ/m3 = 0.025 10^3 m3.
Override with params.heat_value_mj_m3 when Westcoast publishes a new year.

Times: Enbridge stamps Posted/Effective times in Central Clock Time (America/Chicago, with US
daylight saving), not Alberta time. See montney/connectors/ngtl_gasday.py for the evidence.

Polling: the API has no date filter and always returns ~12 months. That is ~400 KB raw but ~46 KB
gzipped on the wire (httpx asks for gzip), so a 30 min interval costs ~2 MB/day.

Format drift: if the newest Station 2 capacity notice cannot be parsed, fetch() raises
FormatDrift (nothing is stored, the runner logs the error). Older unparsable notices are
warnings only.

Events (kind "notice", id "westcoast:<NoticeId>"), for critical notices in the last
params.days (default 45). Severity rules:
  critical  text mentions force majeure, OFO, "unplanned", or linepack status "Critical"
  warn      Station 2 / Huntingdon capacity "below Firm Contracted Capacity", any other
            Capacity Constraint or maintenance notice, Pipeline Conditions (linepack)
  info      everything else
refs: Station 2 / Huntingdon notices -> gas.station2, gas.huntingdon; T-North items (Fort St
John mainline, Aitken Creek, Sunset Creek, 26" Alberta East/West) -> gas.westcoast.t_north;
linepack -> both Westcoast links.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import CCT, E3M3_TO_MMCF, FormatDrift, html_text, local_to_utc
from montney.store import Event, Obs, SeriesMeta

URL = "https://infopost.enbridge.com/api/notices"
PAGE = "https://infopost.enbridge.com/bu/WE/Notices/CRI"
HEAT_VALUE_MJ_M3 = 40.0
GROUP = "Westcoast / T-South"
POINTS = {
    "Station 2 South": ("westcoast.station2_south.capacity", "Westcoast Station 2 South capacity"),
    "Huntingdon Delivery Area": ("westcoast.huntingdon.capacity", "Westcoast Huntingdon delivery area capacity"),
}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_SECTION = re.compile(
    r"(Station 2 South|Huntingdon Delivery Area) Capacity:?\s*"
    rf"((?:{_MONTH}\s[^:]{{1,60}}?:\s*[\d,]+\s*GJ\s*)+)")
_ENTRY = re.compile(rf"({_MONTH}\s[^:]{{1,60}}?):\s*([\d,]+)\s*GJ")
_CYCLE = re.compile(r"\b(ID\s?\d|EVE|EVENING|TIMELY)\b", re.I)


def parse_days(spec: str) -> list[date]:
    """'October 17-18, 2026', 'November 1 - 2, 2026', 'October 25 ID1, 2026',
    'October 19 ID2 - 20, 2026', 'October 30 - November 2, 2026' -> list of gas days."""
    s = re.sub(r"\(?\brevised\b\)?", " ", spec, flags=re.I)
    s = _CYCLE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).replace(" ,", ",").strip()
    s = re.sub(r"\s*-\s*", " - ", s)
    m = re.fullmatch(r"([A-Za-z]+) (\d{1,2})(?:, (\d{4}))?(?: - (?:([A-Za-z]+) )?(\d{1,2}))?, (\d{4})", s)
    if not m:
        raise ValueError(f"unrecognised date {spec!r}")
    m1, d1, y1, m2, d2, y2 = m.groups()
    if m1.lower() not in MONTHS or (m2 and m2.lower() not in MONTHS):
        raise ValueError(f"unrecognised month in {spec!r}")
    end_year = int(y2)
    start = date(int(y1 or y2), MONTHS[m1.lower()], int(d1))
    if not d2:
        return [start]
    end = date(end_year, MONTHS[(m2 or m1).lower()], int(d2))
    if end < start and not y1:  # e.g. "December 30 - January 2, 2027"
        start = start.replace(year=end_year - 1)
    if end < start or (end - start).days > 62:
        raise ValueError(f"bad range {spec!r}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_capacity(text: str) -> dict[str, list[tuple[date, float]]]:
    """Free text -> {'Station 2 South': [(day, GJ), ...], 'Huntingdon Delivery Area': [...]}"""
    out: dict[str, list[tuple[date, float]]] = {}
    for point, block in _SECTION.findall(text):
        for spec, gj in _ENTRY.findall(block):
            for d in parse_days(spec):
                out.setdefault(point, []).append((d, float(gj.replace(",", ""))))
    return out


def gj_to_mmcf(gj: float, heat_value: float = HEAT_VALUE_MJ_M3) -> float:
    return gj / heat_value * E3M3_TO_MMCF


def notice_text(n: dict) -> str:
    t = n.get("NoticeText")
    if isinstance(t, dict):
        t = t.get("rawText") or t.get("html") or ""
    return html_text(t or "", limit=20000).replace("\n", " ")


def posted_utc(n: dict) -> datetime:
    return local_to_utc(datetime.strptime(f"{n['PostedDate']} {n['PostedTime']}", "%m/%d/%Y %I:%M:%S %p"), CCT)


def notice_refs(title: str, text: str) -> list[str]:
    t = f"{title} {text}"
    refs = []
    if re.search(r"Station 2 South|Huntingdon", t):
        refs += ["gas.station2", "gas.huntingdon"]
    if re.search(r"Fort St\.? John|Station #?1|Aitken Creek|Sunset Creek|Alberta (East|West)|T[- ]North", t):
        refs.append("gas.westcoast.t_north")
    if re.search(r"linepack", t, re.I):
        refs += [r for r in ("gas.westcoast.t_north", "gas.westcoast.t_south") if r not in refs]
    return refs


def notice_severity(n: dict, text: str) -> str:
    t = f"{n.get('NoticeTitle', '')} {text}"
    if re.search(r"force majeure|\bOFO\b|operational flow order|unplanned|Linepack Status - Critical", t, re.I):
        return "critical"
    if "below Firm Contracted Capacity" in t:
        return "warn"
    descr = n.get("NoticeTypeDescr", "")
    if re.search(r"Capacity Constraint|Maintenance|Pipeline Conditions", descr):
        if re.search(r"Station 2 South|Huntingdon", t) and "Firm Contracted" not in t:
            return "info"
        return "warn"
    return "info"


class WestcoastNotices(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        # day="gas" uses the NGTL gas-day cutoff (15:00 UTC). Westcoast's own gas day is 09:00 CCT
        # (America/Chicago): 15:00 UTC in winter (Nov-Mar), 14:00 UTC in summer. The 1 h summer
        # difference only shifts when "current" rolls over and is accepted for v1.
        return [SeriesMeta(sid, self.id, label, "MMcf/d", "D", GROUP, day="gas")
                for sid, label in POINTS.values()]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        hv = float(p.get("heat_value_mj_m3", HEAT_VALUE_MJ_M3))
        j = ctx.get(p.get("url", URL), params={"businessUnit": "WE", "noticeType": "CRI"},
                    timeout=60).json()
        notices = ((j or {}).get("Notices") or {}).get("Notice")
        if isinstance(notices, dict):
            notices = [notices]
        if not isinstance(notices, list) or not notices:
            raise FormatDrift(f"{self.id}: no Notices.Notice list in response")
        for n in notices:
            if not {"@Noticeid", "NoticeTitle", "PostedDate", "PostedTime"} <= set(n):
                raise FormatDrift(f"{self.id}: notice missing id/title/posted fields")
        notices.sort(key=posted_utc, reverse=True)
        res = FetchResult(meta=self.declared_series())
        cutoff = ctx.now - timedelta(days=int(p.get("days", 45)))

        newest_cap = True
        for n in notices:
            title = n["NoticeTitle"]
            text = notice_text(n)
            posted = posted_utc(n)
            if "Station 2 South Capacity" in title or "Huntingdon/Station 2" in title:
                try:
                    caps = parse_capacity(text)
                    if "Station 2 South" not in caps:
                        raise ValueError("no 'Station 2 South Capacity <date>: <n> GJ' found")
                except ValueError as e:
                    msg = f"{self.id}: notice {n['@Noticeid']} ({title!r}): {e}"
                    if newest_cap:
                        raise FormatDrift(msg) from e
                    res.warnings.append(msg)
                    caps = {}
                newest_cap = False
                for point, vals in caps.items():
                    sid = POINTS[point][0]
                    for d, gj in vals:
                        res.obs.append(Obs(sid, datetime(d.year, d.month, d.day), gj_to_mmcf(gj, hv), posted))
            if posted >= cutoff:
                res.events.append(Event(
                    id=f"westcoast:{n['@Noticeid']}",
                    source=self.id,
                    kind="notice",
                    title=title,
                    body=text[:4000],
                    url=PAGE,
                    starts=_eff(n, "NoticeEffFrom") or posted,
                    ends=None,
                    refs=notice_refs(title, text),
                    severity=notice_severity(n, text),
                    published=posted,
                    extra={"type": n.get("NoticeTypeDescr"), "prior_notice": n.get("PriorNoticeId")
                           if n.get("PriorNoticeId") not in (None, "None") else None},
                ))
        return res


def _eff(n: dict, prefix: str) -> datetime | None:
    try:
        return local_to_utc(datetime.strptime(f"{n[prefix + 'Date']} {n[prefix + 'Time']}",
                                              "%m/%d/%Y %I:%M:%S %p"), CCT)
    except (KeyError, ValueError):
        return None
