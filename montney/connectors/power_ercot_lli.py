"""ERCOT large loads energized since 2022 (mostly data centres and crypto), monthly (PLAN 9 data centres, tx).

ERCOT publishes no real-time data-centre or crypto load. The closest public, regular figure is one sentence on the
"Loads Approved to Energize – Observations" slide of the **ERCOT Monthly Operational Overview** (PDF, ~1.4 MB, posted
~mid-month for the month before):

  "Of the 9,456 MW that have received Approval to Energize, ERCOT has observed a non-simultaneous monthly peak
   consumption of 4,132 MW in September 2026 ... calculated as the sum of the maximum value for each individual load
   per month ... represents how much approved load ERCOT believes is now operational."

Scope: large loads (>= 75 MW) approved through ERCOT's Large Load Interconnection process since 2022, not all legacy
large load. More than 90 % of it is data centres + crypto mining (ERCOT/ESIG 2025; Feb 2025 split: crypto 3,700 MW,
data centres 1,400 MW observed).

Finding the PDF: its URL is dated and its filename varies (``...-September.pdf``, ``...-August-2026.pdf``,
``...-Final-May-2026.pdf``), so it's discovered, never templated. The index is the ERCOT Board of Directors page,
https://www.ercot.com/committees/board (this year's issues; the link title names the month), and
``/committees/board/<YYYY>`` for a past year (the December issue, posted in January, sits under its own year).
Requests: the board page once a day (``ctx.get_if_changed``: a 304 when unchanged), a past year's page only until that
year is complete (read once after 1 February), and each PDF once: what it said is kept in
``<home>/cache/ercot_lli/parsed.json`` (with the parser version), so a PDF is downloaded only when a new issue appears.

Text: the slides are PowerPoint exports with simple (TrueType, WinAnsi) fonts, so the text is read with a small stdlib
PDF reader (``pdf_text``: Flate content streams, Tj/TJ strings). No PDF library is added.

The month is the one the sentence names, not the issue's title: the Sep-Dec 2025 issues gave the month they were
posted in, to date (Sep 2025 issue: "4,126 MW in October 2025"; Dec 2025 issue: "3,349 MW in January 2026", later
4,049 MW for all of January in the January issue). **A value for the month the issue was posted in is month-to-date (to
about the 17th), so it is dropped** (Reviewer on #497): only a later issue's full-month figure is stored. When two
issues name the same complete month, the later issue wins. So Sep-Dec 2025 have no value. The slide starts with the
April 2025 issue.

Series (MW, freq M, ts = the month's first day):
  ercot.lli.observed_mw    the observed non-simultaneous monthly peak of the approved large loads
  ercot.lli.approved_mw    MW approved to energize (cumulative), as stated in the same sentence

Fail closed (FormatDrift): the newest issue without the sentence, a number that doesn't parse, observed > approved, or
values outside 0-100,000 MW. An older issue without it (before April 2025) is skipped.
"""

from __future__ import annotations

import json
import re
import zlib
from datetime import datetime

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

INDEX = "https://www.ercot.com/committees/board"
GROUP = "ERCOT / Large loads approved to energize (monthly)"
PARSER = 1                    # bump when the parse changes: cached results of an older parser are re-read
MAX_MW = 100_000
MONTHS = {m: i for i, m in enumerate(("january february march april may june july august september october november "
                                      "december").split(), 1)}
LINK = re.compile(r'<a\b[^>]*\bhref="([^"]+\.pdf)"[^>]*>([^<]*Monthly\s+Operational\s+Overview[^<]*)</a>', re.I)
POSTED = re.compile(r"/files/docs/(\d{4})/(\d{2})/(\d{2})/")
SENTENCE = re.compile(r"Of the ([\d,.]+) MW that have received Approval to Energize, ERCOT has observed a "
                      r"non-?simultaneous monthly peak consumption of ([\d,.]+) MW in ([A-Za-z]+) (\d{4})", re.I)

_STREAM = re.compile(rb"<<((?:[^<>]|<<(?:[^<>]|<<[^<>]*>>)*>>|<[0-9A-Fa-f\s]*>)*)>>\s*stream\r?\n")
_TOK = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|T[jJ*]|'|\"|Td|TD|ET", re.S)
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _literal(b: bytes) -> str:
    out, i = bytearray(), 1
    while i < len(b) - 1:
        c = b[i]
        if c != 0x5C:                                    # backslash
            out.append(c)
            i += 1
            continue
        e = b[i + 1:i + 2]
        m = re.match(rb"[0-7]{1,3}", b[i + 1:i + 4])
        if m:
            out.append(int(m.group(0), 8) & 0xFF)
            i += 1 + len(m.group(0))
            continue
        out += _ESC.get(e, b"" if e in (b"\r", b"\n") else e)
        i += 2
    return out.decode("cp1252", "replace")


def pdf_text(data: bytes) -> str:
    """The text of a PDF's Flate content streams (Tj/TJ strings), whitespace-collapsed. Enough for slides exported
    with simple fonts; a CID-keyed font gives garbage, so a caller's regex misses and fails closed."""
    parts = []
    for m in _STREAM.finditer(data):
        d = re.sub(rb"\s+", b"", m.group(1))
        if b"/FlateDecode" not in d or b"/Type/ObjStm" in d or b"/Subtype/Image" in d or b"/Type/XRef" in d:
            continue
        L = re.search(rb"/Length\s*(\d+)(?!\s*\d+\s*R)", m.group(1))
        raw = data[m.end():m.end() + int(L.group(1))] if L else data[m.end():data.find(b"endstream", m.end())]
        try:
            out = zlib.decompressobj().decompress(raw)
        except zlib.error:
            continue
        if b"BT" not in out:
            continue
        cur: list[str] = []
        for t in _TOK.finditer(out):
            s = t.group(0)
            if s[:1] == b"(":
                cur.append(_literal(s))
            elif s[:1] == b"<":
                h = re.sub(rb"\s", b"", s[1:-1]).decode()
                cur.append(bytes.fromhex(h + "0" * (len(h) % 2)).decode("cp1252", "replace"))
            elif s in (b"Tj", b"TJ", b"'", b'"'):
                parts.append("".join(cur))
                cur = []
            else:                                        # Td, TD, T*, ET: a new line or block
                parts.append(" ")
    text = re.sub(r"\s+", " ", " ".join(parts))
    return re.sub(r"\s*-\s*", "-", text)                # "non - simultaneous" -> "non-simultaneous"


def _num(raw: str, what: str) -> float:
    try:
        v = float(raw.replace(",", "").rstrip("."))
    except ValueError:
        raise FormatDrift(f"ERCOT LLI: {what} {raw!r} isn't a number") from None
    if not 0 <= v <= MAX_MW:
        raise FormatDrift(f"ERCOT LLI: {what} {v} MW is outside 0-{MAX_MW}")
    return v


def parse_issue(text: str) -> dict | None:
    """{month: "YYYY-MM", approved, observed} from an issue's text, or None when the sentence isn't there."""
    m = SENTENCE.search(text)
    if not m:
        return None
    mon = MONTHS.get(m.group(3).lower())
    if mon is None:
        raise FormatDrift(f"ERCOT LLI: month {m.group(3)!r}")
    approved, observed = _num(m.group(1), "approved"), _num(m.group(2), "observed")
    if observed > approved:
        raise FormatDrift(f"ERCOT LLI: observed {observed} MW > approved {approved} MW")
    return {"month": f"{int(m.group(4)):04d}-{mon:02d}", "approved": approved, "observed": observed}


def links(html: str) -> dict[str, tuple[str, str]]:
    """{pdf url: (posted "YYYY-MM-DD", title)} of the Monthly Operational Overview issues on an index page."""
    out = {}
    for href, title in LINK.findall(html):
        url = href if href.startswith("http") else "https://www.ercot.com" + href
        p = POSTED.search(url)
        if p:
            out[url] = ("-".join(p.groups()), re.sub(r"\s+", " ", title).strip())
    return out


def by_month(issues: dict[str, dict]) -> dict[str, dict]:
    """Month -> the result of the latest-posted issue that names it, for complete months only: an issue that names
    the month it was posted in gives a month-to-date peak (to about the 17th), never stored as the month's value."""
    out: dict[str, dict] = {}
    for _, r in sorted(issues.items(), key=lambda kv: kv[1]["posted"]):
        if r.get("month") and r["posted"][:7] != r["month"]:
            out[r["month"]] = r
    return out


class ErcotLargeLoads(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"region": "tx", "url": INDEX, "report": "ERCOT Monthly Operational Overview, Loads Approved to Energize",
              "scope": "large loads (>= 75 MW) energized via LLI since 2022; >90 % data centres + crypto"}
        return [SeriesMeta("ercot.lli.observed_mw", self.id,
                           "Large loads energized since 2022 (ERCOT, observed non-simultaneous monthly peak)", "MW",
                           "M", GROUP, extra=ex),
                SeriesMeta("ercot.lli.approved_mw", self.id,
                           "Large loads approved to energize since 2022 (ERCOT, cumulative)", "MW", "M", GROUP,
                           extra=ex)]

    def _cache_path(self):
        return paths.home() / "cache" / "ercot_lli" / "parsed.json"

    def _load(self) -> dict:
        try:
            c = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        if c.get("parser") != PARSER:
            c = {"parser": PARSER, "issues": {}, "years_done": []}
        return c

    def _save(self, c: dict) -> None:
        p = self._cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(p)

    def fetch(self, ctx: Context) -> FetchResult:
        first = int(self.spec.get("params", {}).get("from_year", 2025))
        res = FetchResult(meta=self.declared_series())
        c = self._load()
        found: dict[str, tuple[str, str]] = {}
        r = ctx.get_if_changed(INDEX, timeout=60)
        if r is not None:
            found.update(links(r.text))
        for y in range(first, ctx.now.year):
            if y in c["years_done"]:
                continue
            found.update(links(ctx.get(f"{INDEX}/{y}", timeout=60).text))
            if ctx.now >= datetime(y + 1, 2, 1):
                c["years_done"].append(y)                # complete: its December issue is out by mid-January
        if not found and r is None:
            self._save(c)
            return res                                   # nothing new
        if not found:
            raise FormatDrift("ERCOT board page: no Monthly Operational Overview links")
        newest = max(found, key=lambda u: found[u][0])
        try:
            for url, (posted, title) in sorted(found.items(), key=lambda kv: kv[1][0]):
                if url in c["issues"]:
                    continue
                got = parse_issue(pdf_text(ctx.get(url, timeout=120).content))
                if got is None and url == newest:      # never cached: retried (and failing) until it parses
                    raise FormatDrift(f"ERCOT LLI: the newest issue, {title} ({url}), has no 'Approval to Energize' "
                                      f"sentence")
                c["issues"][url] = {"posted": posted, "title": title, **(got or {"month": None})}
        finally:
            self._save(c)                                # what was read so far isn't downloaded again
        for month, got in sorted(by_month(c["issues"]).items()):
            ts = datetime.strptime(month, "%Y-%m")
            res.obs.append(Obs("ercot.lli.observed_mw", ts, got["observed"], ts))
            res.obs.append(Obs("ercot.lli.approved_mw", ts, got["approved"], ts))
        return res
