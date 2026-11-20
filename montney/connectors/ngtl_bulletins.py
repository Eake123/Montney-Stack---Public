"""TC Customer Express bulletins for NGTL and Foothills (events).

  GET /bulletin/sent?page=1&page_size=N&...   newest first. Every query parameter is sent the
                                              way TC's own app sends it: shorter forms return 502.
  GET /bulletin/{id}                          body (HTML), fetched only for operational types

Kept: service providers NGTL, Foothills BC and Foothills Saskatchewan. Mainline and Great Lakes
are out of the v1 map.

Event id "ngtl_bulletins:<id>", kind "bulletin". Severity rules:
  critical  TC flags the bulletin critical, or its type is Curtailment
  warn      Capacity Constraint, Tolerance Change, or a subject mentioning a restriction,
            curtailment, service allowable, force majeure or OFO (whatever its type)
  info      everything else (Other, Event, News, Open Season, Project Update, ...)
refs: the NGTL areas named in the subject (EGAT/East Gate, WGAT/West Gate, USJR, OSDA, NEDA);
if the subject names none, the areas named in the body. Plus gas.ngtl.fhbc / gas.ngtl.fhsk for
Foothills bulletins.

Personal data: the list rows carry contactName/contactPhone and the detail carries publishedBy.
None of these are read or stored.
"""

from __future__ import annotations

import re

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import TC_API, TC_SITE, FormatDrift, cache_load, cache_save, html_text, parse_dt
from montney.store import Event

PROVIDERS = {"NGTL": [], "Foothills BC": ["gas.ngtl.fhbc"], "Foothills Saskatchewan": ["gas.ngtl.fhsk"]}
WARN_TYPES = {"Capacity Constraint", "Tolerance Change"}
CRITICAL_TYPES = {"Curtailment"}
BODY_TYPES = WARN_TYPES | CRITICAL_TYPES
BODY_CACHE, BODY_CACHE_MAX = "ngtl_bulletin_bodies", 500    # L-081: <home>/cache, newest 500 ids
AREA_PATTERNS = {
    "gas.ngtl.egat": r"\bEGAT\b|East Gate",
    "gas.ngtl.wgat": r"\bWGAT\b|West Gate",
    "gas.ngtl.usjr": r"\bUSJR\b|Upstream (of )?James River",
    "gas.ngtl.osda": r"\bOSDA\b|Oil Sands Delivery",
    "gas.ngtl.neda": r"\bNEDA\b|North (and|&) East Delivery",
}
LIST_PARAMS = {
    "sort_column": "publishedDateTimeUtc", "sort_order": "1", "search_text": "",
    "search_subject": "true", "search_body": "true", "exact_phrase": "false", "severity": "",
    "service_provider": "", "notice_type": "", "submission_date_start": "undefined",
    "submission_date_end": "undefined", "effective_date_start": "undefined",
    "effective_date_end": "undefined",
}


WARN_SUBJECT = re.compile(r"restrict|curtail|service allowable|force majeure|\bOFO\b", re.I)


def bulletin_severity(kind: str, is_critical, subject: str = "") -> str:
    if str(is_critical).lower() == "true" or kind in CRITICAL_TYPES:
        return "critical"
    if kind in WARN_TYPES or WARN_SUBJECT.search(subject or ""):
        return "warn"
    return "info"


def area_refs(text: str) -> list[str]:
    return [ref for ref, pat in AREA_PATTERNS.items() if re.search(pat, text or "", re.I)]


class NgtlBulletins(Connector):
    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        base = p.get("api", TC_API)
        res = FetchResult()
        j = ctx.get(f"{base}/bulletin/sent",
                    params={"page": 1, "page_size": int(p.get("page_size", 25)), **LIST_PARAMS}).json()
        rows = ((j or {}).get("data") or {}).get("data")
        if not isinstance(rows, list):
            raise FormatDrift(f"{self.id}: /bulletin/sent has no data.data list")
        bodies_left = int(p.get("max_bodies", 8))
        cache = cache_load(BODY_CACHE)        # L-081: {bulletin id: body text}; a sent bulletin's body doesn't change
        new: dict[str, str] = {}
        for r in rows:
            try:
                sub = r["subscription"]
                provider = sub["serviceProvider"]["name"]
                kind = sub["name"]
                bid, subject = r["id"], r["subject"]
            except (KeyError, TypeError) as e:
                raise FormatDrift(f"{self.id}: bulletin row missing {e}") from e
            if provider not in PROVIDERS:
                continue
            body = ""
            if kind in BODY_TYPES and str(bid) in cache:
                body = cache[str(bid)]
            elif kind in BODY_TYPES and bodies_left > 0:
                bodies_left -= 1
                try:
                    d = ctx.get(f"{base}/bulletin/{bid}", retries=1).json().get("data") or {}
                    body = html_text(d.get("body"))
                    new[str(bid)] = body
                except Exception as e:  # list item still useful without its body
                    res.warnings.append(f"bulletin {bid} body: {e}")
            named = area_refs(subject) or area_refs(body)
            refs = PROVIDERS[provider] + [x for x in named if x not in PROVIDERS[provider]]
            res.events.append(Event(
                id=f"{self.id}:{bid}",
                source=self.id,
                kind="bulletin",
                title=f"{provider}: {subject}" if provider != "NGTL" else subject,
                body=body,
                url=f"{TC_SITE}/#/Bulletin/{bid}",
                starts=parse_dt(r.get("startDateTimeUtc")),
                ends=None,
                refs=refs,
                severity=bulletin_severity(kind, r.get("isCritical"), subject),
                published=parse_dt(r.get("publishedDateTimeUtc")),
                extra={"provider": provider, "type": kind},
            ))
        if new:      # only now, after the whole run parsed: a failed run fetches them again next time
            keep = dict(list({**cache, **new}.items())[-BODY_CACHE_MAX:])
            try:
                cache_save(BODY_CACHE, keep)
            except OSError as e:
                res.warnings.append(f"bulletin body cache not written: {e}")
        return res
