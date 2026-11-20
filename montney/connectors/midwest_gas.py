"""Midwest gas (L-095). Erik: "I really mostly only care about the aggregate of Joliet and Bakken … whats getting dropped
off at chicago with Chicago demand. Wisconsin total demand, Michigan storage and demand, Bakken supply … Really limit the
requests". Sources, each public, no login, few requests:

1. **Northern Border (NBPL)** (`gas_nbpl`, 2 requests a run, every 12 h): TC eConnects' SSRS Operationally Available
   Capacity report (asset 3029). The viewer page's first GET already carries the rendered report's `ExportUrlBase`; one
   more GET of that + "CSV" returns every location for the default gas day and cycle (today, Timely). ~270 KB.
   Rows repeat per rate schedule with the location total in every copy, so a location counts once per
   (name, LocQTI, direction). Dth.
   - `nbpl.port_of_morgan.receipts`: Port of Morgan RPQ (Canadian gas off Foothills at Monchy).
   - `nbpl.bakken.net_receipts`: "Flow Past Glen Ullin" − "Flow Past Port Of Morgan": what NBPL picks up net in the
     Williston basin (Bakken receipts at Watford City, Killdeer, … less the few North Dakota deliveries).
   - `nbpl.glen_ullin.flow`: "Flow Past Glen Ullin", the mainline leaving the Bakken (the line value).
   - `nbpl.ventura.net_deliveries`: Ventura DPQ − RPQ, mostly to Northern Natural.
   - `nbpl.harper.flow`: "Flow Past Harper", the Chicago leg's flow into Illinois.
   - `nbpl.chicago.net_deliveries`: deliveries − receipts at the Chicago-market points CHICAGO (Troy Grove, Manhattan,
     Joliet, Minooka, Will County, Channahon, North Hayden). Channahon's receipts are Aux Sable's tailgate, which is
     Alliance gas; netting them keeps Alliance from counting twice in the Chicago drop-off.
2. **ANR** (`gas_anr`, 2 requests a run, every 12 h): the same SSRS report for ANR (asset 3005, report
   OperationallyAvailableCapacityANR, ~110 KB). ANR's quantity column is `TotalSchedQty`.
   - `anr.wisconsin.deliveries`: ANR's "WISCONSIN" delivery aggregate (DPQ), ANR's deliveries into Wisconsin.
   - `anr.alliance.net_receipts`: ALLIANCE/ANR INT RPQ − DPQ, Alliance gas ANR takes at Joliet toward WI/MI.
   - `anr.marshfield.viking_net_receipts`: MARSHFIELD/VIKING INT RPQ − DPQ, Viking into ANR at Marshfield.
3. **EIA monthly**: the IL / WI / MI deliveries, Michigan working gas and ND production (eia.<st>.*) now come from
   `gas_eia_states` (L-106, every state on five pages; montney/connectors/eia_states.py).
4. **The Chicago drop-off** (`gas_midwest_balance`, no requests): `midwest.chicago.dropoff` = Alliance (est.) + NBPL's
   Chicago-market net deliveries. What the northern pipes drop off at Chicago.
   Alliance's Chicago deliveries aren't stored, so its Canada receipts (alliance.receipts.total, scheduled) stand in for
   them, ±10 % (ALLIANCE_BAND, the Reviewer's figure on #381). The deliveries add the Tioga (Bakken) receipts and take
   off fuel and Aux Sable's liquids extraction. Each of these is roughly a tenth of throughput or less, they partly
   offset, and none is public daily from a request we already make. Alliance is ~83 % of the total, so the headline is
   ±8.5 % (DROPOFF_BAND ≈ ±150 MMcf/d).

Great Lakes' Emerson, Farwell and Muttonville points come from gas_glgt's existing request (ont_dawn.py); Viking's
Emerson receipts from tc_mainline's (Emerson 1). Units: MMcf/d; Dth at 1,037 Btu/cf. Gas day = the date.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, local_to_utc, read_stored
from montney.connectors.pnw_gas import BTU_PER_CF, CENTRAL, num
from montney.store import Obs, SeriesMeta

GROUP = "Midwest: Chicago, Bakken, WI, MI"
EBB = "https://ebb.tceconnects.com"
NBPL_URL = f"{EBB}/infopost/ReportViewer.aspx?/InfoPost/OperationallyAvailableCapacity&pAssetNbr=3029"
ANR_URL = f"{EBB}/infopost/ReportViewer.aspx?/InfoPost/OperationallyAvailableCapacityANR&pAssetNbr=3005"
CHICAGO = ("Troy Grove", "Manhattan", "Joliet", "Minooka", "Will County", "Channahon", "North Hayden")
ALLIANCE = "alliance.receipts.total"
ALLIANCE_BAND = 0.10      # Alliance's Canada receipts as its Chicago deliveries (docstring, item 4)
DROPOFF_BAND = 0.085      # ±10 % on Alliance's ~83 % share of the drop-off: ≈ ±150 MMcf/d
NBPL_CHICAGO = "nbpl.chicago.net_deliveries"
DROPOFF = "midwest.chicago.dropoff"


def mmcfd(dth: float) -> float:
    return dth / BTU_PER_CF


# ------------------------------------------------------------------ TC eConnects SSRS export (NBPL, ANR)
def ssrs_export(ctx: Context, url: str) -> str:
    """The report's CSV for its default gas day and cycle: GET the viewer (the rendered report's export URL is in it),
    then GET that URL + "CSV". Two requests; the session cookie carries over in ctx's client."""
    page = ctx.get(url, timeout=120).text
    m = re.search(r'"ExportUrlBase":"([^"]+)"', page)
    if not m:
        raise FormatDrift(f"TC eConnects viewer {url}: no ExportUrlBase (the report didn't render)")
    base = m.group(1).encode().decode("unicode_escape")
    r = ctx.get(EBB + base + "CSV", timeout=120)
    if "csv" not in r.headers.get("content-type", ""):
        raise FormatDrift(f"TC eConnects export: {r.headers.get('content-type')!r}, not CSV")
    return r.content.decode("utf-8-sig", "replace")


def ssrs_points(text: str) -> tuple[date, datetime, dict[tuple[str, str], float]]:
    """(gas day, posted UTC, {(location name, LocQTI[/direction]): scheduled Dth}), each location counted once."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "LocationName" not in rows[0] or "EffGasDay" not in rows[0]:
        raise FormatDrift(f"TC eConnects CSV: no LocationName/EffGasDay header ({list(rows[0])[:6] if rows else []})")
    qty = "TotalScheduledQuantity" if "TotalScheduledQuantity" in rows[0] else "TotalSchedQty"
    if qty not in rows[0]:
        raise FormatDrift("TC eConnects CSV: no TotalScheduledQuantity / TotalSchedQty column")
    days = {r["EffGasDay"] for r in rows}
    if len(days) != 1:
        raise FormatDrift(f"TC eConnects CSV: {len(days)} gas days in one export ({sorted(days)[:3]})")
    try:
        day = datetime.strptime(days.pop(), "%m/%d/%Y").date()
        posted = max(local_to_utc(datetime.strptime(r["PostingDate"], "%m/%d/%Y %I:%M %p"), CENTRAL) for r in rows)
    except (KeyError, ValueError) as e:
        raise FormatDrift(f"TC eConnects CSV: bad gas day / posting date ({e})") from e
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        qti = r.get("LocQTI", "").strip()
        if qti == "SGQ":
            qti += "/" + r.get("Direction", "").strip()
        out.setdefault((" ".join(r["LocationName"].split()), qti), num(r[qty]))   # rate-schedule copies: first only
    return day, posted, out


class _Ssrs(Connector):
    URL = ""
    NEED: tuple[tuple[str, str], ...] = ()

    def values(self, pts: dict[tuple[str, str], float]) -> dict[str, float]:
        raise NotImplementedError

    def fetch(self, ctx: Context) -> FetchResult:
        day, posted, pts = ssrs_points(ssrs_export(ctx, self.spec.get("params", {}).get("url", self.URL)))
        missing = [f"{n} {q}" for n, q in self.NEED if (n, q) not in pts]
        if missing:
            raise FormatDrift(f"{self.id}: {day} has no {', '.join(missing)}")
        ts = datetime(day.year, day.month, day.day)
        res = FetchResult(meta=self.declared_series())
        res.obs = [Obs(sid, ts, v, posted) for sid, v in self.values(pts).items()]
        return res


def _m(conn: Connector, sid: str, label: str, **kw) -> SeriesMeta:
    return SeriesMeta(sid, conn.id, label, "MMcf/d", "D", GROUP, day="gas", **kw)


class GasNbpl(_Ssrs):
    URL = NBPL_URL
    NEED = (("Flow Past Port Of Morgan", "SGQ/Forwardhaul"), ("Flow Past Glen Ullin", "SGQ/Forwardhaul"),
            ("Flow Past Harper", "SGQ/Forwardhaul"), ("Port of Morgan", "RPQ"), ("Ventura", "DPQ"),
            ("Will County", "DPQ"), ("North Hayden", "DPQ"))

    def declared_series(self) -> list[SeriesMeta]:
        return [_m(self, "nbpl.port_of_morgan.receipts", "Northern Border receipts at Port of Morgan (from Foothills, "
                                                         "Canadian gas; scheduled, Timely)"),
                _m(self, "nbpl.bakken.net_receipts", "Bakken gas on Northern Border: net receipts between Port of Morgan "
                                                     "and Glen Ullin (scheduled, Timely)"),
                _m(self, "nbpl.glen_ullin.flow", "Northern Border flow past Glen Ullin, ND (leaving the Bakken; "
                                                 "scheduled, Timely)"),
                _m(self, "nbpl.ventura.net_deliveries", "Northern Border net deliveries at Ventura, IA (mostly to "
                                                        "Northern Natural; scheduled, Timely)"),
                _m(self, "nbpl.harper.flow", "Northern Border flow past Harper, IA (the Chicago leg; scheduled, Timely)"),
                _m(self, NBPL_CHICAGO, "Northern Border Chicago-market net deliveries (Troy Grove, Manhattan, Joliet, "
                                       "Minooka, Will County, Channahon, North Hayden; less Aux Sable's receipts at "
                                       "Channahon; scheduled, Timely)")]

    def values(self, pts):
        g = lambda n, q: mmcfd(pts.get((n, q), 0.0))           # noqa: E731
        fw = "SGQ/Forwardhaul"
        return {"nbpl.port_of_morgan.receipts": g("Port of Morgan", "RPQ"),
                "nbpl.bakken.net_receipts": g("Flow Past Glen Ullin", fw) - g("Flow Past Port Of Morgan", fw),
                "nbpl.glen_ullin.flow": g("Flow Past Glen Ullin", fw),
                "nbpl.ventura.net_deliveries": g("Ventura", "DPQ") - g("Ventura", "RPQ"),
                "nbpl.harper.flow": g("Flow Past Harper", fw),
                NBPL_CHICAGO: sum(g(n, "DPQ") - g(n, "RPQ") for n in CHICAGO)}


class GasAnr(_Ssrs):
    URL = ANR_URL
    NEED = (("WISCONSIN", "DPQ"), ("ALLIANCE/ANR INT", "RPQ"), ("MARSHFIELD/VIKING INT", "RPQ"))

    def declared_series(self) -> list[SeriesMeta]:
        return [_m(self, "anr.wisconsin.deliveries", "ANR deliveries into Wisconsin (ANR's WISCONSIN aggregate; "
                                                     "scheduled, Timely)"),
                _m(self, "anr.alliance.net_receipts", "ANR net receipts from Alliance at Joliet (toward WI / MI; "
                                                      "scheduled, Timely)"),
                _m(self, "anr.marshfield.viking_net_receipts", "ANR net receipts from Viking at Marshfield, WI "
                                                               "(scheduled, Timely)")]

    def values(self, pts):
        g = lambda n, q: mmcfd(pts.get((n, q), 0.0))           # noqa: E731
        return {"anr.wisconsin.deliveries": g("WISCONSIN", "DPQ"),
                "anr.alliance.net_receipts": g("ALLIANCE/ANR INT", "RPQ") - g("ALLIANCE/ANR INT", "DPQ"),
                "anr.marshfield.viking_net_receipts": g("MARSHFIELD/VIKING INT", "RPQ")
                - g("MARSHFIELD/VIKING INT", "DPQ")}


# ------------------------------------------------------------------ the Chicago drop-off (derived from the store)
class GasMidwestBalance(Connector):
    stored = staticmethod(read_stored)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        return [_m(self, DROPOFF, f"Gas dropped off at Chicago from the north (est., ±{DROPOFF_BAND:.1%}): Alliance "
                                  f"(est. from its Canada receipts, ±{ALLIANCE_BAND:.0%}) + Northern "
                                  "Border's Chicago-market net deliveries (scheduled)", extra={"band": DROPOFF_BAND})]

    def fetch(self, ctx: Context) -> FetchResult:
        days = int(self.spec.get("params", {}).get("days", 7))
        end = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        got = self.stored([ALLIANCE, NBPL_CHICAGO], end - timedelta(days=days), end)
        a, n = got.get(ALLIANCE) or {}, got.get(NBPL_CHICAGO) or {}
        res = FetchResult(meta=self.declared_series())
        as_of = ctx.now.replace(minute=0, second=0, microsecond=0)
        for ts in sorted(set(a) | set(n)):
            if ts in a and ts in n:
                res.obs.append(Obs(DROPOFF, ts, a[ts] + n[ts], as_of))
            else:
                res.warnings.append(f"{ts:%Y-%m-%d}: no {ALLIANCE if ts not in a else NBPL_CHICAGO}; no Chicago total")
        return res
