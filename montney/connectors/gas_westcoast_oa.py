"""Westcoast (Enbridge) scheduled volumes by segment, from the informational posting's "Operationally Available Capacity"
(PLAN step 6 PR 6A; Erik: "I want the actual scheduled volumes. For Tnorth, Huntington, etc.").

Source (public, no login): https://rtba.enbridge.com/InformationalPosting/Default.aspx?bu=WE&Type=OA, an ASP.NET page.
The CSV is reached by four postbacks, carrying the page's hidden state each time:
  1. (only for a past gas day) the date picker `…$oaDefault$ucDate$rdpDate` → that day's cycle list;
  2. the cycle dropdown `…$oaDefault$ucSelector$ddlSelector` = the latest cycle (TIMELY / LATE / LATEC …_YYYY-MM-DD_HHMM);
  3. `…$lmapWestcoast` ("Operational Capacity Map");
  4. `…$mapWestcoast$Hyperlink36$LinkButton1` ("Download Csv") → `WE_OA_<date>_<cycle>.csv`:
       Gas Date: 2026-11-02  Cycle: LATE_2026-11-02_0901
       Station Name,Cap,Nom,Cap2
       Fort Nelson to Stn 2,2625401,1528445,
- **Units: GJ/d.** Westcoast's nominal 40 GJ per 10³m³ (its own convention; the gas's true heating value is a little
  under 40 MJ/m³, so physical volumes run slightly higher; Reviewer on #253) is exact for its own figures: Station 2 South Cap 1,858,306 GJ = 46,458 10³m³ =
  1,641 MMcf/d, its notice capacity (gas_westcoast); Huntingdon too. So MMcf/d = GJ ÷ 40 × 0.0353147.
- **Nom = the scheduled quantity** through the segment, signed in the segment's named direction: a negative value
  flows the other way, e.g. Fort St John → Stn 2 at −443k is eastbound, and Sunset Creek at −634k goes out to NGTL.
- **Segments** (on Westcoast's map): Fort Nelson → Stn 2 (T-North from the north, which the Aitken Creek Lateral joins
  at CSN4); Fort St John → Stn 2 (CS1 → CS2); 26" Alberta West / East (CS1 → CSA1, toward NGTL at Gordondale); Sunset
  Creek Interconnect (to NGTL Groundbirch East); Station 2 South (= T-South); Huntingdon Delivery Area.
- **Cross-check, 2026-11-02:** Sunset −634k + Alberta East +212k GJ = 747 MMcf/d into NGTL; NGTL's own Groundbirch
  East + Gordondale receipts were 718.

Series (MMcf/d; freq D, ts = gas day; as_of = the cycle's time stamp, Alberta clock assumed; a later cycle on the same
gas day is a newer vintage):
  westcoast.oa.<segment>.scheduled   Nom, signed as posted
  westcoast.oa.<segment>.capacity    Cap (operationally available)
  westcoast.oa.sunset_creek.to_ngtl  −Nom of Sunset Creek: Westcoast's delivery to NGTL there
  westcoast.oa.tnorth.out            T-North's scheduled deliveries out = Station 2 South + to NGTL at Sunset Creek +
                                     26" Alberta East (to NGTL at Gordondale): T-North receipts *including* Aitken Creek
                                     storage, the measured number
  westcoast.oa.aitken_storage_est    Aitken Creek storage today (est.), + withdrawal / − injection
  westcoast.oa.tnorth.production_est T-North production = T-North out − Aitken storage (est.)

**Aitken Creek storage, separated** (PLAN 6A; Erik: "Tnorth receipts include Aitken creek storage … Seperated out"):
- Westcoast posts only the Aitken Creek Lateral, which carries storage *and* the lateral's own production P.
- The anchor is measured: BCER's facility report gives Aitken Creek Gas Storage's monthly net withdrawal
  (`bcer.aitken_storage.net_withdrawal`, gas_bcer_production). NGTL's Aitken Creek Interconnect (3820) shows no
  receipts, so the storage moves on the Westcoast lateral.
- P(month) = the lateral's monthly mean (stored) − that month's storage net. Nov 2025-Sep 2026: 71-87 MMcf/d in winter,
  153-379 Apr-Sep.
- **P is seasonal** (winter 71-87, summer up to 379), so P is **anchored on the same calendar month last year** and never
  carried from another month (orchestrator on #254: September's 379 carried into November gave −13, where last November
  was +182). With last year's November, Nov 2 2026 reads +280 ±293: the right sign.
  - Band = max(164, |P(that month) − P(latest BCER month)|): the only same-month evidence is one year (n = 1), so the
    band is widened by the gap between the two Ps rather than measured.
  - The YoY shoulder-month adjustment the ruling allows needs two years of lateral history; there is one
    (from 2025-11), so it isn't applied yet.
  - **No same-month-last-year anchor → NO-RESULT** for storage and T-North production; the box shows T-North incl.
    Aitken only.
- The base band ±164 MMcf/d is the back-test of the carry within a season (P(M) − P(M-2), n 9, P90). It's absolute
  because storage passes through zero (`westcoast.oa.aitken_storage_band` stores today's value); on T-North production
  it's written as that run's share.
- No BCER month, or fewer than 90 % of that month's lateral days stored → NO-RESULT for both (the measured T-North out
  is still written).

Each run reads yesterday's and today's gas day (latest cycle). `from_date` (the back-fill importer) reads every day
from it to today; the page serves at least a year (2025-11-01 tested 2026-11-02).

Fails loudly (FormatDrift) if the page has no cycle list for today, the CSV header changes, or none of the known
segments is in it. An unknown new segment is a warning.
"""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import E3M3_TO_MMCF, FormatDrift, local_to_utc, read_stored
from montney.store import Obs, SeriesMeta

URL = "https://rtba.enbridge.com/InformationalPosting/Default.aspx?bu=WE&Type=OA"
P = "ctl00$MainContent$ctl01$"
SEL, DATE = P + "oaDefault$ucSelector$ddlSelector", P + "oaDefault$ucDate$rdpDate"
MAP, CSV_LINK = P + "lmapWestcoast", P + "mapWestcoast$Hyperlink36$LinkButton1"
GJ_PER_E3M3 = 40.0
GROUP = "Westcoast / scheduled (OA)"
AITKEN_NET = "bcer.aitken_storage.net_withdrawal"
LATERAL = "westcoast.oa.aitken_lateral.scheduled"
AITKEN_BAND_MMCFD, TNORTH_PROD_BAND = 164.0, 0.07



SEGMENTS = {
    "Fort Nelson to Stn 2": ("fort_nelson", "Fort Nelson → Station 2 (T-North north)"),
    "Aitken Creek Lateral": ("aitken_lateral", "Aitken Creek Lateral (storage + lateral production)"),
    "Fort St John to Stn 2": ("fort_st_john", "Fort St. John → Station 2 (− = eastbound)"),
    '26" Alberta West': ("alberta_west", '26" Alberta West'),
    '26" Alberta East': ("alberta_east", '26" Alberta East (toward NGTL at Gordondale)'),
    "Sunset Creek Interconnect": ("sunset_creek", "Sunset Creek Interconnect (− = to NGTL)"),
    "Station 2 South": ("station2_south", "Station 2 South (T-South)"),
    "Huntingdon Delivery Area": ("huntingdon", "Huntingdon Delivery Area"),
}
CYCLE_RE = re.compile(r'<option[^>]*value="((?:TIMELY|EVENING|LATE|LATEC|ID\d|INTRADAY)[A-Z]*_\d{4}-\d{2}-\d{2}_\d{4})"')
HEADER_RE = re.compile(r"Gas Date:\s*(\d{4}-\d{2}-\d{2})\s+Cycle:\s*(\S+)")


def mmcfd(gj: float) -> float:
    return gj / GJ_PER_E3M3 * E3M3_TO_MMCF


def hidden(page: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'<input[^>]+type="hidden"[^>]*>', page):
        tag = m.group(0)
        n, v = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if n:
            out[html.unescape(n.group(1))] = html.unescape(v.group(1)) if v else ""
    return out


def cycle_time(cycle: str) -> datetime:
    """'LATE_2026-11-02_0901' → naive UTC (the stamp read as Alberta time)."""
    _, d, hm = cycle.rsplit("_", 2)
    return local_to_utc(datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H%M"))


def parse_csv(text: str) -> tuple[date, str, dict[str, tuple[float, float]]]:
    """(gas day, cycle, {Station Name: (Cap GJ, Nom GJ)})."""
    m = HEADER_RE.search(text)
    if not m:
        raise FormatDrift("Westcoast OA CSV: no 'Gas Date: … Cycle: …' line")
    rows = list(csv.reader(io.StringIO(text[m.end():])))
    head = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Station Name"), None)
    if head is None or [c.strip() for c in rows[head][:3]] != ["Station Name", "Cap", "Nom"]:
        raise FormatDrift("Westcoast OA CSV: expected the header 'Station Name,Cap,Nom'")
    out = {}
    for r in rows[head + 1:]:
        if len(r) >= 3 and r[0].strip():
            num = lambda s: float(s.replace(",", "")) if s.strip() else 0.0   # noqa: E731
            out[r[0].strip()] = (num(r[1]), num(r[2]))
    return date.fromisoformat(m.group(1)), m.group(2), out


class GasWestcoastOa(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for key, label in SEGMENTS.values():
            out += [SeriesMeta(f"westcoast.oa.{key}.scheduled", self.id, f"Westcoast {label}, scheduled", "MMcf/d", "D",
                               GROUP, day="gas"),
                    SeriesMeta(f"westcoast.oa.{key}.capacity", self.id, f"Westcoast {label}, operationally available "
                               "capacity", "MMcf/d", "D", GROUP, day="gas")]
        out += [SeriesMeta("westcoast.oa.sunset_creek.to_ngtl", self.id, "Westcoast to NGTL at Sunset Creek, scheduled",
                           "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta("westcoast.oa.tnorth.out", self.id, "Westcoast T-North scheduled deliveries out (T-South + "
                           "NGTL at Sunset Creek + NGTL at Gordondale), incl. Aitken Creek storage", "MMcf/d", "D", GROUP,
                           day="gas"),
                SeriesMeta("westcoast.oa.aitken_storage_est", self.id, f"Aitken Creek storage (est.), + withdrawal / − "
                           f"injection, ±{AITKEN_BAND_MMCFD:.0f} MMcf/d", "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta("westcoast.oa.aitken_storage_band", self.id, "Aitken Creek storage estimate: today's band, ±",
                           "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta("westcoast.oa.tnorth.production_est", self.id, f"Westcoast T-North production (T-North out − "
                           f"Aitken Creek storage, est., ±{TNORTH_PROD_BAND:.0%})", "MMcf/d", "D", GROUP,
                           extra={"band": TNORTH_PROD_BAND}, day="gas")]
        return out

    def _aitken(self, ctx: Context, res: FetchResult, as_of_days: dict[datetime, datetime]) -> None:
        """Today's Aitken storage = lateral − P; T-North production = T-North out − storage (see the module docstring)."""
        try:
            st = self.stored([AITKEN_NET, LATERAL], ctx.now - timedelta(days=430), ctx.now)
        except Exception as e:  # noqa: BLE001
            res.warnings.append(f"NO-RESULT Aitken storage: store read failed ({e})")
            return
        net = st.get(AITKEN_NET) or {}
        lateral = {**(st.get(LATERAL) or {}), **{o.ts: o.value for o in res.obs if o.series_id == LATERAL}}
        if not net:
            res.warnings.append(f"NO-RESULT Aitken storage: no {AITKEN_NET} stored yet")
            return

        def p_of(m: datetime) -> float | None:
            """The lateral's own production in month m: its monthly mean − BCER's storage net (≥ 90 % of days)."""
            if m not in net:
                return None
            end = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1)
            days = [v for t, v in lateral.items() if m <= t < end]
            return sum(days) / len(days) - net[m] if len(days) >= 0.9 * (end - m).days else None

        out = {o.ts: o.value for o in res.obs if o.series_id == "westcoast.oa.tnorth.out"}
        latest = max(net)
        p_latest = p_of(latest)
        for ts, as_of in sorted(as_of_days.items()):
            if ts not in lateral or ts not in out:
                continue
            same = datetime(ts.year - 1, ts.month, 1)
            p_same = p_of(same)
            if p_same is not None:          # (b) the same calendar month last year, its gap to the latest month as band
                p, basis = p_same, f"{same:%Y-%m}"
                band = max(AITKEN_BAND_MMCFD, abs(p_same - p_latest) if p_latest is not None else 0.0)
            else:      # no same-month-last-year anchor: fail closed, never carry another month (orchestrator on #254)
                res.warnings.append(f"NO-RESULT Aitken storage {ts:%Y-%m-%d}: no same-month anchor ({same:%Y-%m} not "
                                    f"stored with ≥ 90 % lateral days); T-North shown incl. Aitken only")
                continue
            s_est = lateral[ts] - p
            res.obs += [Obs("westcoast.oa.aitken_storage_est", ts, s_est, as_of),
                        Obs("westcoast.oa.tnorth.production_est", ts, out[ts] - s_est, as_of),
                        Obs("westcoast.oa.aitken_storage_band", ts, band, as_of)]
            for m in res.meta:          # this run's band, absolute and as a share of T-North production
                if m.series_id == "westcoast.oa.tnorth.production_est" and out[ts] - s_est > 0:
                    m.extra["band"] = round(band / (out[ts] - s_est), 3)
            res.warnings.append(f"Aitken {ts:%Y-%m-%d}: lateral production {p:,.0f} MMcf/d from {basis}, storage "
                                f"{s_est:+,.0f} ±{band:,.0f}")

    def _post(self, ctx: Context, page: str, target: str, extra: dict[str, str]) -> str:
        data = hidden(page)
        data.update({"__EVENTTARGET": target, "__EVENTARGUMENT": ""})
        data.update(extra)
        return ctx.post(self.spec.get("params", {}).get("url", URL), data=data).text

    def _day(self, ctx: Context, day: date, today: date) -> str | None:
        """The latest cycle's CSV for one gas day, or None if the page lists no cycle for it."""
        url = self.spec.get("params", {}).get("url", URL)
        page = ctx.get(url).text
        if day != today:
            ds = f"{day:%Y-%m-%d}"
            page = self._post(ctx, page, DATE, {
                DATE: ds, DATE + "$dateInput": f"{day.month}/{day.day}/{day.year}",
                "ctl00_MainContent_ctl01_oaDefault_ucDate_rdpDate_dateInput_ClientState":
                    '{"enabled":true,"emptyMessage":"","validationText":"%s-00-00-00","valueAsString":"%s-00-00-00",'
                    '"lastSetTextBoxValue":"%d/%d/%d"}' % (ds, ds, day.month, day.day, day.year)})
        cycles = CYCLE_RE.findall(page)
        if not cycles:
            return None
        last = max(cycles, key=cycle_time)
        page = self._post(ctx, page, SEL, {SEL: last})
        page = self._post(ctx, page, MAP, {SEL: last})
        return self._post(ctx, page, CSV_LINK, {SEL: last})

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        today = local_today()
        start = date.fromisoformat(p["from_date"]) if p.get("from_date") else today - timedelta(days=1)
        res = FetchResult(meta=self.declared_series())
        days, unknown, as_of_days = 0, set(), {}
        d = start
        while d <= today:
            text = self._day(ctx, d, today)
            d += timedelta(days=1)
            if text is None:
                if d - timedelta(days=1) == today:
                    raise FormatDrift("Westcoast OA: no cycle list for today's gas day")
                continue
            gas_day, cycle, rows = parse_csv(text)
            known = {name: v for name, v in rows.items() if name in SEGMENTS}
            unknown |= set(rows) - set(SEGMENTS)
            if not known:
                raise FormatDrift(f"Westcoast OA {gas_day}: none of the known segments in {sorted(rows)}")
            ts, as_of = datetime(gas_day.year, gas_day.month, gas_day.day), cycle_time(cycle)
            as_of_days[ts] = as_of
            sched = {}
            for name, (cap, nom) in known.items():
                key = SEGMENTS[name][0]
                sched[key] = mmcfd(nom)
                res.obs += [Obs(f"westcoast.oa.{key}.scheduled", ts, mmcfd(nom), as_of),
                            Obs(f"westcoast.oa.{key}.capacity", ts, mmcfd(cap), as_of)]
            if "sunset_creek" in sched:
                res.obs.append(Obs("westcoast.oa.sunset_creek.to_ngtl", ts, -sched["sunset_creek"], as_of))
            if {"station2_south", "sunset_creek", "alberta_east"} <= set(sched):
                res.obs.append(Obs("westcoast.oa.tnorth.out", ts, sched["station2_south"] - sched["sunset_creek"]
                                   + sched["alberta_east"], as_of))
            days += 1
        self._aitken(ctx, res, as_of_days)
        if unknown:
            res.warnings.append(f"Westcoast OA: segments not mapped (skipped): {sorted(unknown)}")
        res.warnings.append(f"Westcoast OA: {days} gas day(s) from {start}")
        return res
