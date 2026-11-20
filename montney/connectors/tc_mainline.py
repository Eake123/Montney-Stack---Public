"""TC Canadian Mainline east to North Bay Junction (L-077, Erik): province areas and the bulletin "export spots".

Erik: "fill out Sask, Manitoba, NOL/Ontario before NBJ … don't save it granularly. Just collect it as one request to
their EBB … the saving should be area based by province. Also the main export spots based off the bulletin capability
reports. So prairie line, Emerson lateral, EDA (as that's the endpoint). It should calc similarly to the capability on
those days (it'll be a bit off)".

**1. Flows: TcMainline** (`tc_mainline`). One request per run to TC's public Mainline EBB, the GCTS "Gas Day Summary
Report" (https://www.gcts.tcenergy.com/#/app/ML/gasdaysummaryreport, linked from tccustomerexpress.com):

  GET https://www.gcts.tcenergy.com/nominations/pipelines/ML/gasdaysummaryreport?fromGasDay=…&toGasDay=…

It returns every Mainline point's **scheduled** quantity (GJ/d), receipt and delivery, for every gas day in the range
(~42 points a day; 2 years = 3.7 MB in 1.7 s). The body starts with the anti-JSON-hijacking line `)]}',`.
Points are summed **on ingest** into the areas below; no per-point row is stored.

| area (id)     | receipts                                          | deliveries                                    |
|---------------|---------------------------------------------------|-----------------------------------------------|
| ab (Empress)  | Empress, Suffield 2                               | Empress (backhaul to NGTL)                    |
| sk            | Success, Herbert, Richmound, Liebenthal, Bayhurst, Steelman, Welwyn | `* SSDA` (SaskEnergy, Centra)                 |
| mb            | Emerson 1/2 (imports), Spruce, `* MDA`            | `* MDA` (Centra), Spruce. Emerson exports are their own row |
| on (west of NBJ) | SS. Marie, `* WDA / NDA / SSMDA`               | `* WDA / NDA / SSMDA` (NOL towns, Sault)      |
| eda (endpoint)| `* EDA`, Iroquois, East Hereford, Lachenaie, …    | `* EDA`, Iroquois, East Hereford, Philipsburg, Napierville, Cornwall, … |

Everything else (Parkway, Kirkwall, Dawn, Niagara, Chippawa, St. Clair, CDA/SWDA/NCDA) is downstream of North Bay
Junction: not in the areas above, but (L-091, Ontario + Quebec, from the same request) summed into POINT_GROUPS:
  tcm.cda.deliveries          TC deliveries in the Central Delivery Area (Enbridge / Union CDA, NCDA, ECDA): the GTA off
                              the Mainline. Most GTA gas comes from Dawn on Enbridge Gas's Dawn-Parkway system, which
                              isn't public daily (its Gas Day Summary API needs a reCAPTCHA token), so this isn't GTA demand
  tcm.parkway.net_deliveries  Parkway Belt deliveries − receipts: TC's net hand-off to Enbridge Gas at Parkway (+ = into
                              Enbridge Gas: the GTA and, in injection season, back west toward Dawn)
  tcm.kirkwall.net_deliveries Kirkwall deliveries − receipts (+ = into Enbridge Gas toward Dawn)
  tcm.niagara.imports         Niagara Falls + Chippawa receipts − deliveries: US gas into Ontario at Niagara
  tcm.iroquois.exports        Iroquois deliveries − receipts: the export to Iroquois at Waddington (NY)
  tcm.energir.deliveries      Énergir EDA + NDA: Québec's distributor (≈ all of Québec's demand off TC)
  tcm.qc.exports              East Hereford + Philipsburg + Napierville: exports from Québec (PNGTS, Vermont, NY)
  demand.qc.implied_est       = tcm.energir.deliveries as Québec's demand (est., ±QC_DEMAND_BAND). Back-test against
                              StatCan 25-10-0086 (Québec deliveries to consumers, Nov 2025-Aug 2026, n 10): −5.8 % mean,
                              sd 4.8 %, worst −12.5 %; −10..−12 % Dec-Mar, when Québec's own storage (Pointe-du-Lac,
                              Saint-Flavien) and Énergir's LSR LNG plant supply the peaks off TC. → ±13 %.
A name in no list raises a warning (never a silent drop).

Through-flows (scheduled, before compressor fuel, so estimates): each area's **outflow** east =
upstream outflow + receipts − deliveries (MB also − Emerson exports). MB's outflow is the Northern Ontario Line east of
Station 41, the bulletin's NOL. ON's outflow is the gas arriving at North Bay Junction.

**2. Capability: TcMainlineCapability** (`tc_mainline_capability`). TC's weekly "TC Energy Planned Maintenance"
bulletin (Customer Express, provider Mainline) has an 11-day table in TJ/d at 38.5 MJ/m³:
- Prairies Line = receipts at Empress + Saskatchewan;
- Emerson Extension = deliveries to Emerson 1 + 2;
- Northern Ontario Line = flow east of Station 41;
- Eastern Delivery Area = flow east of Station 116 into the North Bay Shortcut + east of MLV 135 on the Montreal Line;
- Downstream Stn 148 (Les Cèdres).
The structured `/mainline/capabilities` endpoint stopped in 2020, so the bulletin it is. A newer bulletin
supersedes an older one day by day (as_of = its publication time).

**Units:** scheduled GJ → MMcf/d at HV_FLOW = 39.2 MJ/m³ (TC's posted Sep 2026 heat values: Empress 39.21, the main
delivery points 39.1-39.3). Capability TJ → MMcf/d at TC's own 38.5 MJ/m³, so both are volumes.

**Back-test** (2025-05-16..2026-11-02, 536 gas days with a bulletin; derived ÷ capability − 1, both as volume):
- NOL (full almost every day): |error| P50 1.9 %, P90 5.7 %. That's the "bit off": fuel burnt west of Station 41 and the
  heat-value spread. → OUTFLOW_BAND 0.06 on every outflow.
- Prairies Line: on the fullest 10 % of days, median −0.7 %, P90 |error| 1.6 %; 15 of 536 days are a little over.
- EDA: on its fullest days, median −6 %, P90 |error| 7.5 % (it's binding only on winter peaks).
- Emerson: never binding (flows ~60 % of capability); the row is two posted points summed.

Fails loudly (FormatDrift) if the report's shape changes, a range has no Empress receipts, or (capability) the newest
maintenance bulletins have no capability table at all.
Terms: TC's public postings, personal use (L-006). Nothing per point is saved.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import E3M3_TO_MMCF, TC_API, TC_SITE, FormatDrift, cache_load, cache_save
from montney.store import Obs, SeriesMeta

GCTS = "https://www.gcts.tcenergy.com/nominations/pipelines/ML/gasdaysummaryreport"
GROUP = "TC Mainline / scheduled (GCTS)"
CAP_GROUP = "TC Mainline / capability (bulletins)"
HV_FLOW, HV_CAP = 39.2, 38.5          # MJ/m³: scheduled energy → volume; TC's capability conversion
OUTFLOW_BAND = 0.06
DAYS_BACK = 7

SK_POINTS = {"Success", "Herbert", "Richmound", "Liebenthal", "Bayhurst 1", "Bayhurst 2", "Steelman", "Welwyn"}
EMERSON = {"Emerson 1", "Emerson 2"}
EDA_POINTS = {"Iroquois", "East Hereford", "Philipsburg", "Napierville", "Cornwall", "Lachenaie", "Ste. Genevieve",
              "St. Mathieu", "Sabrevois", "St. Lawrence"}
EAST = {"Union Parkway Belt", "Enbridge Parkway Belt", "Enbridge Parkway CDA", "Kirkwall", "Niagara Falls", "Chippawa",
        "Union Dawn", "Enbridge Dawn", "St. Clair", "Union CDA", "Enbridge CDA", "Union SWDA", "Enbridge SWDA",
        "Union NCDA", "Union ECDA", "Vic Square # 2 CDA"}
AREAS = ("ab", "sk", "mb", "on")
# L-091: named point groups downstream of North Bay Junction (summed per gas day alongside the areas)
POINT_GROUPS = {"cda": {"Enbridge CDA", "Union CDA", "Union NCDA", "Union ECDA", "Vic Square # 2 CDA",
                        "Enbridge Parkway CDA"},
                "parkway": {"Union Parkway Belt", "Enbridge Parkway Belt"}, "kirkwall": {"Kirkwall"},
                "niagara": {"Niagara Falls", "Chippawa"}, "iroquois": {"Iroquois"},
                "energir": {"Energir EDA", "Energir NDA"}, "qc_export": {"East Hereford", "Philipsburg", "Napierville"},
                # L-095: the Emerson export split by pipeline (Emerson 1 = Viking, Emerson 2 = Great Lakes)
                "viking": {"Emerson 1"}, "glgt": {"Emerson 2"}}
GROUP_OF = {n: g for g, names in POINT_GROUPS.items() for n in names}
# L-106 (nyc, upne): the three Québec export points one by one, besides their qc_export group (same request): East
# Hereford -> PNGTS (New England), Philipsburg -> Vermont Gas (New England), Napierville -> New York
SINGLE_OF = {"East Hereford": "east_hereford", "Philipsburg": "philipsburg", "Napierville": "napierville"}
QC_DEMAND_BAND = 0.13
AREA_LABEL = {"ab": "Empress (AB)", "sk": "Saskatchewan", "mb": "Manitoba",
              "on": "Ontario west of North Bay Junction (NOL, Sault)"}
OUTFLOW_LABEL = {"sk": "east into Manitoba", "mb": "east of Station 41 = the Northern Ontario Line",
                 "on": "arriving at North Bay Junction"}


def area_of(name: str) -> str | None:
    """The map area a Mainline point belongs to: ab | sk | mb | emerson | on | eda | east, or None (unknown)."""
    n = " ".join(name.split())
    if n in ("Empress", "Suffield 2"):
        return "ab"
    if n in EMERSON:
        return "emerson"
    if n in SK_POINTS or re.search(r"\bSSDA$", n):
        return "sk"
    if n == "Spruce" or re.search(r"\bMDA$", n):
        return "mb"
    if n == "SS. Marie" or re.search(r"\b(WDA|NDA|SSMDA)$", n):
        return "on"
    if n in EDA_POINTS or re.search(r"\bEDA$", n):
        return "eda"
    if n in EAST:
        return "east"
    return None


def gj_to_mmcfd(gj: float) -> float:
    return gj / HV_FLOW * E3M3_TO_MMCF          # GJ ÷ (MJ/m³) = 10³m³


def tj_to_mmcfd(tj: float) -> float:
    return tj * 1000 / HV_CAP * E3M3_TO_MMCF


def strip_xssi(text: str) -> str:
    return text.split("\n", 1)[1] if text.startswith(")]}'") else text


def area_sums(rows: list[dict]) -> tuple[dict[date, dict[str, float]], dict[str, float]]:
    """{gas day: {"<area>.r" / "<area>.d": GJ}} over the report rows, and {unknown point: GJ} (warned)."""
    out: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unknown: dict[str, float] = defaultdict(float)
    for r in rows:
        try:
            d = date.fromisoformat(str(r["gasDay"])[:10])
            name, flow, q = r["locationName"], r["flowDirection"], float(r["scheduledQty"] or 0)
        except (KeyError, TypeError, ValueError) as e:
            raise FormatDrift(f"GCTS gas day summary row: {e} in {r!r:.200}") from e
        if flow not in ("Receipt", "Delivery"):
            raise FormatDrift(f"GCTS flowDirection {flow!r}")
        a = area_of(name)
        if a is None:
            unknown[name] += q
            continue
        out[d][f"{a}.{flow[0].lower()}"] += q
        single = SINGLE_OF.get(" ".join(name.split()))
        if single:
            out[d][f"pt.{single}.{flow[0].lower()}"] += q
        grp = GROUP_OF.get(" ".join(name.split()))
        if grp:
            out[d][f"pt.{grp}.{flow[0].lower()}"] += q
    return out, dict(unknown)


def day_values(s: dict[str, float]) -> dict[str, float]:
    """One gas day's area sums (GJ) → the stored series (MMcf/d)."""
    g = lambda k: gj_to_mmcfd(s.get(k, 0.0))     # noqa: E731
    v = {f"tcm.area.{a}.{m}": g(f"{a}.{m[0]}") for a in AREAS for m in ("receipts", "deliveries")}
    v["tcm.emerson.deliveries"] = g("emerson.d")
    v["tcm.emerson.receipts"] = g("emerson.r")
    v["tcm.emerson.viking_deliveries"] = g("pt.viking.d")
    v["tcm.emerson.glgt_deliveries"] = g("pt.glgt.d")
    v["tcm.prairies.receipts"] = g("ab.r") + g("sk.r")
    v["tcm.eda.deliveries"] = g("eda.d") - g("eda.r")
    ab_out = g("ab.r") - g("ab.d")
    sk_out = ab_out + g("sk.r") - g("sk.d")
    mb_out = sk_out + g("mb.r") + g("emerson.r") - g("mb.d") - g("emerson.d")
    v["tcm.area.sk.outflow_est"] = sk_out
    v["tcm.area.mb.outflow_est"] = mb_out
    v["tcm.area.on.outflow_est"] = mb_out + g("on.r") - g("on.d")
    # L-091: Ontario + Québec points (see the docstring)
    p = lambda grp, m: g(f"pt.{grp}.{m}")       # noqa: E731
    v["tcm.cda.deliveries"] = p("cda", "d")
    v["tcm.parkway.net_deliveries"] = p("parkway", "d") - p("parkway", "r")
    v["tcm.kirkwall.net_deliveries"] = p("kirkwall", "d") - p("kirkwall", "r")
    v["tcm.niagara.imports"] = p("niagara", "r") - p("niagara", "d")
    v["tcm.iroquois.exports"] = p("iroquois", "d") - p("iroquois", "r")
    v["tcm.energir.deliveries"] = p("energir", "d")
    v["tcm.qc.exports"] = p("qc_export", "d")
    v["demand.qc.implied_est"] = p("energir", "d")
    for single in SINGLE_OF.values():
        v[f"tcm.{single}.exports"] = p(single, "d") - p(single, "r")
    return v


class TcMainline(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        m = []
        for a in AREAS:
            m += [SeriesMeta(f"tcm.area.{a}.receipts", self.id, f"TC Mainline {AREA_LABEL[a]}: receipts (scheduled)",
                             "MMcf/d", "D", GROUP, day="gas"),
                  SeriesMeta(f"tcm.area.{a}.deliveries", self.id, f"TC Mainline {AREA_LABEL[a]}: deliveries (scheduled)",
                             "MMcf/d", "D", GROUP, day="gas")]
            if a in OUTFLOW_LABEL:
                m.append(SeriesMeta(f"tcm.area.{a}.outflow_est", self.id,
                                    f"TC Mainline flow {OUTFLOW_LABEL[a]} (scheduled, before fuel; est., "
                                    f"±{OUTFLOW_BAND:.0%})", "MMcf/d", "D", GROUP, extra={"band": OUTFLOW_BAND},
                                    day="gas"))
        m += [SeriesMeta("tcm.prairies.receipts", self.id, "TC Mainline Prairies Line: receipts at Empress + "
                         "Saskatchewan (scheduled)", "MMcf/d", "D", GROUP, day="gas"),
              SeriesMeta("tcm.emerson.deliveries", self.id, "TC Mainline Emerson exports: Emerson 1 + 2 deliveries "
                         "(scheduled)", "MMcf/d", "D", GROUP, day="gas"),
              SeriesMeta("tcm.emerson.receipts", self.id, "TC Mainline Emerson imports: Emerson 1 + 2 receipts "
                         "(scheduled)", "MMcf/d", "D", GROUP, day="gas"),
              SeriesMeta("tcm.emerson.viking_deliveries", self.id, "TC Mainline deliveries to Viking at Emerson 1 "
                         "(scheduled)", "MMcf/d", "D", GROUP, day="gas"),
              SeriesMeta("tcm.emerson.glgt_deliveries", self.id, "TC Mainline deliveries to Great Lakes at Emerson 2 "
                         "(scheduled)", "MMcf/d", "D", GROUP, day="gas"),
              *self._onqc_meta(),
              SeriesMeta("tcm.eda.deliveries", self.id, "TC Mainline Eastern Delivery Area: deliveries − receipts "
                         "(scheduled; the endpoint)", "MMcf/d", "D", GROUP, day="gas")]
        return m

    def _onqc_meta(self) -> list[SeriesMeta]:
        g = "TC Mainline / Ontario + Québec (GCTS)"
        m = lambda sid, label, **kw: SeriesMeta(sid, self.id, label, "MMcf/d", "D", g, day="gas", **kw)  # noqa: E731
        return [m("tcm.cda.deliveries", "TC deliveries to the GTA (Central Delivery Area: Enbridge / Union CDA, NCDA; "
                                        "scheduled). Most GTA gas comes from Dawn on Enbridge Gas, not this"),
                m("tcm.parkway.net_deliveries", "TC net deliveries to Enbridge Gas at Parkway (scheduled)"),
                m("tcm.kirkwall.net_deliveries", "TC net deliveries to Enbridge Gas at Kirkwall (scheduled)"),
                m("tcm.niagara.imports", "US gas into Ontario at Niagara Falls + Chippawa (TC net receipts, "
                                         "scheduled)"),
                m("tcm.iroquois.exports", "Export to Iroquois at Waddington (TC net deliveries, scheduled)"),
                m("tcm.energir.deliveries", "TC deliveries to Énergir (EDA + NDA; scheduled)"),
                m("tcm.qc.exports", "Exports from Québec: East Hereford (PNGTS) + Philipsburg + Napierville "
                                    "(scheduled)"),
                m("tcm.east_hereford.exports", "TC export at East Hereford to PNGTS (New England; net, scheduled)"),
                m("tcm.philipsburg.exports", "TC export at Philipsburg to Vermont Gas (net, scheduled)"),
                m("tcm.napierville.exports", "TC export at Napierville to New York (net, scheduled)"),
                m("demand.qc.implied_est", f"Québec gas demand: Énergir off the TC Mainline (est., "
                                           f"±{QC_DEMAND_BAND:.0%}; low by ~10 % Dec-Mar, when Québec storage and LNG "
                                           "supply the peaks)",
                  extra={"band": QC_DEMAND_BAND})]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        today = local_today()
        start = date.fromisoformat(p["from_date"]) if p.get("from_date") else today - timedelta(
            days=int(p.get("days_back", DAYS_BACK)))
        r = ctx.get(p.get("url", GCTS), params={"fromGasDay": start.isoformat(),
                                                "toGasDay": (today + timedelta(days=1)).isoformat()}, timeout=180)
        try:
            j = json.loads(strip_xssi(r.text))
            rows = j["rows"]
        except (ValueError, KeyError, TypeError) as e:
            raise FormatDrift(f"GCTS gas day summary: not the expected JSON ({e})") from e
        days, unknown = area_sums(rows)
        if not days:
            raise FormatDrift(f"GCTS gas day summary: no rows for {start}..{today}")
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        for d in sorted(days):
            if not days[d].get("ab.r"):
                res.warnings.append(f"{d}: no Empress receipts scheduled; day skipped")
                continue
            ts = datetime(d.year, d.month, d.day)
            res.obs += [Obs(sid, ts, v, as_of) for sid, v in day_values(days[d]).items()]
        if not res.obs:
            raise FormatDrift("GCTS gas day summary: no gas day with Empress receipts")
        if unknown:
            res.warnings.append("Mainline points in no area (not counted): " + ", ".join(
                f"{n} {v / 1000:,.0f} TJ" for n, v in sorted(unknown.items(), key=lambda kv: -kv[1])))
        return res


# ------------------------------------------------------------------ capability (bulletins)
CAP_COLS = {"prairies": r"Prairies", "emerson": r"Emerson", "nol": r"Northern\s*Ontario", "eda": r"Eastern\s*Delivery",
            "stn148": r"148"}
CAP_LABEL = {"prairies": "Prairies Line (receipts at Empress + Saskatchewan)",
             "emerson": "Emerson Extension (deliveries to Emerson 1 + 2)",
             "nol": "Northern Ontario Line (flow east of Station 41)",
             "eda": "Eastern Delivery Area (flow east of Station 116 + MLV 135)",
             "stn148": "downstream of Station 148 (Les Cèdres)"}
BODY_CACHE, BODY_CACHE_MAX = "tc_mainline_bulletin_bodies", 2000   # L-081: <home>/cache
MAINT = re.compile(r"(?<![Uu]n)planned maintenance", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                          "dec"])}
LIST_PARAMS = {"sort_column": "publishedDateTimeUtc", "sort_order": "1", "search_text": "", "search_subject": "true",
               "search_body": "true", "exact_phrase": "false", "severity": "", "service_provider": "",
               "notice_type": "", "submission_date_start": "undefined", "submission_date_end": "undefined",
               "effective_date_start": "undefined", "effective_date_end": "undefined"}


def capability_table(body: str, published: datetime) -> dict[date, dict[str, int]]:
    """The bulletin's {gas day: {prairies|emerson|nol|eda|stn148: TJ}}; {} if it has no capability table."""
    t = html.unescape(body or "")
    names: list[str] = []
    for h in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S | re.I):
        h = re.sub(r"<sup>.*?</sup>|<[^>]+>", " ", h, flags=re.S)
        for k, pat in CAP_COLS.items():
            if re.search(pat, h, re.I) and k not in names:
                names.append(k)
                break
    if len(names) < 3:
        return {}
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
    out: dict[date, dict[str, int]] = {}
    for m in re.finditer(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2})((?: \d{3,5})+)", txt):
        mo, dy = MONTHS[m.group(1).lower()], int(m.group(2))
        nums = [int(x) for x in m.group(3).split()]
        if len(nums) != len(names):
            continue
        y = published.year + (1 if mo < published.month - 6 else 0) - (1 if mo > published.month + 6 else 0)
        out[date(y, mo, dy)] = dict(zip(names, nums))
    return out


class TcMainlineCapability(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(f"tcm.{k}.capability", self.id, f"TC Mainline capability, {CAP_LABEL[k]} (weekly "
                           "maintenance bulletin forecast; TJ at 38.5 MJ/m³)", "MMcf/d", "D", CAP_GROUP, day="gas")
                for k in CAP_COLS]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        base = p.get("api", TC_API)
        hdr = {"Origin": TC_SITE, "Referer": TC_SITE + "/"}
        want = int(p.get("max_bulletins", 3))
        found: list[dict] = []
        for page in range(1, int(p.get("pages", 1)) + 1):
            j = ctx.get(f"{base}/bulletin/sent", params={"page": page, "page_size": int(p.get("page_size", 100)),
                                                         **LIST_PARAMS}, headers=hdr).json()
            rows = ((j or {}).get("data") or {}).get("data")
            if not isinstance(rows, list):
                raise FormatDrift(f"{self.id}: /bulletin/sent has no data.data list")
            for r in rows:
                try:
                    prov = r["subscription"]["serviceProvider"]["name"]
                except (KeyError, TypeError) as e:
                    raise FormatDrift(f"{self.id}: bulletin row missing {e}") from e
                if prov == "Mainline" and MAINT.search(r.get("subject") or ""):
                    found.append(r)
            if len(found) >= want or not rows:
                break
        res = FetchResult(meta=self.declared_series())
        n_tables = 0
        cache = cache_load(BODY_CACHE)        # L-081: {bulletin id: body html}; a sent bulletin's body doesn't change
        new: dict[str, str] = {}
        for r in found[:want]:
            published = datetime.fromisoformat(r["publishedDateTimeUtc"])
            body = cache.get(str(r["id"]))
            if body is None:
                body = (ctx.get(f"{base}/bulletin/{r['id']}", headers=hdr).json().get("data") or {}).get("body") or ""
                new[str(r["id"])] = body
            table = capability_table(body, published)
            if not table:
                res.warnings.append(f"bulletin {r['id']} ({r.get('subject')}): no capability table")
                continue
            n_tables += 1
            for day, vals in table.items():
                ts = datetime(day.year, day.month, day.day)
                res.obs += [Obs(f"tcm.{k}.capability", ts, tj_to_mmcfd(v), published) for k, v in vals.items()]
        if found and not n_tables:
            raise FormatDrift(f"{self.id}: none of the newest {min(want, len(found))} maintenance bulletins has a "
                              "capability table")
        if not found:
            res.warnings.append("no Mainline 'Planned Maintenance' bulletin in the newest page")
        if new:      # only after the run parsed (a FormatDrift above leaves the cache as it was)
            try:
                cache_save(BODY_CACHE, dict(list({**cache, **new}.items())[-BODY_CACHE_MAX:]))
            except OSError as e:
                res.warnings.append(f"bulletin body cache not written: {e}")
        return res
