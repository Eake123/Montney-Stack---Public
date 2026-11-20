"""BC gas production, monthly, from the BC Energy Regulator's facility reporting (PLAN 4b item 4).

Q6 / L-041: personal-use basis (L-006). Fetched daily, aggregated on ingest, never saved or redistributed.

Source: the same BCER "Facility Petrinex Reporting" APEX report as gas_lng_bcer (every BC facility):
  GET https://reports.bc-er.ca/ogc/r/app001/ams_reports/facility-petrinex-reporting?irin_prod_period=…  (session
      cookie + an interactive-report IN filter on the last WINDOW_MONTHS calendar months)
  GET  …/facility-petrinex-reporting?request=CSV         the filtered CSV
L-102: the unfiltered CSV is every month since Oct 2018 (96 months, ~8.7 MB, 20-100 s to build); this connector only
reads the last PLANT_MONTHS (24) of them, so the filter cuts the download to ~2.7 MB with the same output. APEX
matches the filter on the column's text, Oracle's padded "Month YYYY" ("May       2026"), so the labels are padded the
same way. If the filtered CSV has fewer than PLANT_MONTHS months (a moved format, a long reporting lag), the connector
fetches the unfiltered CSV instead (2 more requests), so it never stores less. APEX keeps the filter in the server
session and the runner shares one client across sources, so every read first drops the reports.bc-er.ca cookies
(gas_common.drop_cookies): a fresh session that can't see gas_lng_bcer's facility filter or our own window.
Columns used: Facility Type, Prod Period ("September 2026"), Ss Pgr Patr Productid (GAS rows only), Deliverablevolumes,
Receiptvolumes, in 10³m³ per month. Lag: about 1 month (Sep 2026 was there on 2026-11-01).

**BC production = what leaves the gas plants and batteries for pipelines** (the BC counterpart of Petrinex AB's masked
sales disposition, gas_petrinex):

    plant deliveries − plant receipts + battery deliveries − battery receipts

BCER doesn't publish where each facility's gas goes, so this is an identity, not a sum of pipeline meters:
- A plant's deliveries include gas sent on to another plant, and that gas is also another plant's receipts, so
  "plant deliveries − plant receipts" leaves the plants' own output to pipelines minus their shrink.
- Adding the batteries' net deliveries (what they send out, less what they take from other batteries) puts back the
  field gas that went into the plants, plus any battery gas sent straight to a pipeline.
- What's left is plant outlet gas plus battery gas sent straight to a pipeline. LNG plants and injection facilities
  only take gas from pipelines, so they're left out.

Aug 2026: plants 8,338 − 8,810, batteries 8,714 − 531 → 7,711 MMcf/d, **7,704** without the Aitken storage battery
(below; raw well production that month was 8,262).

**Storage is not production** (L-079, like gas_petrinex's STORAGE): Aitken Creek's storage wells report as a battery
(BCBT0000160) delivering to the Aitken Creek plant, so the identity counted every winter withdrawal as BC production
(Jan 2026: 421 MMcf/d; Nov-Mar 2025/26 mean 336). That battery is left out, so its gas cancels against the plant's
receipts. The net withdrawal stays its own series below. The supply/demand balance (docs/research/gas-balance.md) found it:
Aitken was counted once inside BC production and again as storage.

Series (MMcf/d = 10³m³ × 0.0353147 ÷ days in the month; freq M, ts = the first of the month):
  production.bc.monthly
  bcer.<px facility id>.plant_outlet   each gas processing plant's gas delivered out (Deliverablevolumes), the
                                       last PLANT_MONTHS months, for /api/top-producers (L-075). Plant-to-plant
                                       transfers make a few overlap, so they don't sum to BC production. Names,
                                       operators and locations go to <home>/facilities/bc.json (montney/top_producers.py).
  bcer.aitken_storage.net_withdrawal   Aitken Creek Gas Storage, + withdrawal / − injection (PLAN 6A):
                                       storage-well production (battery BCBT0000160) − injection receipts (injection
                                       facilities BCIF0000160 + BCIF0026827). Jan 2026 +421, Aug 2026 −228.

as_of = the start of the UTC day the value was first fetched: a report vintage. Change-only writes keep only months that moved.

Fails loudly (FormatDrift) if:
- the CSV header changes;
- the newest month has no gas plants;
- a month falls outside 4,000-14,000 MMcf/d (a unit or filter break).
"""

from __future__ import annotations

import calendar
import csv
import io
from collections import defaultdict
from datetime import date, datetime

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import E3M3_TO_MMCF, FormatDrift, drop_cookies
from montney.store import Obs, SeriesMeta
from montney.top_producers import save_registry

PAGE = "https://reports.bc-er.ca/ogc/r/app001/ams_reports/facility-petrinex-reporting"
SERIES = "production.bc.monthly"
COLUMNS = {"Facility Type", "Prod Period", "Ss Pgr Patr Productid", "Deliverablevolumes", "Receiptvolumes"}
PLANT, BATTERY = "Gas Processing Plant", "Battery"
RANGE = (4000.0, 14000.0)
AITKEN = "bcer.aitken_storage.net_withdrawal"
AITKEN_OUT, AITKEN_IN = ("BCBT0000160",), ("BCIF0000160", "BCIF0026827")
AITKEN_MONTHS = 15
MONTHS_BACK = 6          # BCER amends recent months; older ones are kept as stored
PLANT_COLUMNS = {"Px Facility Id", "Fac Name", "Organizationname", "Legal Description"}
PLANT_MONTHS = 24        # the popup's history; change-only writes keep only months that moved
WINDOW_MONTHS = 30       # L-102: calendar months requested (PLANT_MONTHS + up to 6 months of reporting lag)
PLANT_GROUP = "BCER BC / gas plant outlet"


def window_labels(today: date, n: int) -> list[str]:
    """The last n calendar months up to `today`'s, as Oracle's padded 'Month YYYY' text (month names padded to 9)."""
    i = today.year * 12 + today.month - 1
    return [f"{calendar.month_name[k % 12 + 1]:<9} {k // 12}" for k in range(i - n + 1, i + 1)]


def csv_months(text: str) -> int:
    """How many distinct Prod Periods a report CSV holds."""
    return len({" ".join(r.get("Prod Period", "").split()) for r in csv.DictReader(io.StringIO(text))} - {""})


def plant_series_id(px: str) -> str:
    return f"bcer.{px.strip().lower()}.plant_outlet"


def bc_production(text: str) -> dict[datetime, float]:
    """{month: MMcf/d} from the unfiltered BCER facility CSV."""
    rdr = csv.DictReader(io.StringIO(text))
    if not COLUMNS <= set(rdr.fieldnames or []):
        raise FormatDrift(f"BCER facility report: expected columns {sorted(COLUMNS)}, got {rdr.fieldnames}")
    net: dict[datetime, float] = defaultdict(float)
    plants: dict[datetime, int] = defaultdict(int)
    for r in rdr:
        typ = r["Facility Type"].strip()
        if r["Ss Pgr Patr Productid"].strip() != "GAS" or typ not in (PLANT, BATTERY):
            continue
        if r.get("Px Facility Id", "").strip() in AITKEN_OUT:      # storage withdrawals aren't production (L-079)
            continue
        m = datetime.strptime(" ".join(r["Prod Period"].split()), "%B %Y")
        num = lambda k: float(r[k]) if r[k].strip() else 0.0   # noqa: E731
        net[m] += num("Deliverablevolumes") - num("Receiptvolumes")
        plants[m] += typ == PLANT
    return {m: v * E3M3_TO_MMCF / calendar.monthrange(m.year, m.month)[1] for m, v in net.items() if plants[m]}


def aitken_net(text: str) -> dict[datetime, float]:
    """{month: Aitken Creek storage net withdrawal, MMcf/d}: storage-well production − injection receipts."""
    net: dict[datetime, float] = defaultdict(float)
    for r in csv.DictReader(io.StringIO(text)):
        px = r.get("Px Facility Id", "").strip()
        if r["Ss Pgr Patr Productid"].strip() != "GAS" or px not in AITKEN_OUT + AITKEN_IN:
            continue
        m = datetime.strptime(" ".join(r["Prod Period"].split()), "%B %Y")
        k = E3M3_TO_MMCF / calendar.monthrange(m.year, m.month)[1]
        num = lambda c: float(r[c]) if r[c].strip() else 0.0   # noqa: E731
        net[m] += num("Productionvolumes") * k if px in AITKEN_OUT else -num("Receiptvolumes") * k
    return dict(net)


def plant_outlets(text: str) -> tuple[dict[str, dict[datetime, float]], dict[str, dict]]:
    """Gas processing plants: ({px id: {month: delivered MMcf/d}}, {px id: {name, operator, location, subtype}})."""
    rdr = csv.DictReader(io.StringIO(text))
    if not PLANT_COLUMNS <= set(rdr.fieldnames or []):
        return {}, {}
    vals: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    info: dict[str, dict] = {}
    for r in rdr:
        px = r["Px Facility Id"].strip()
        if r["Ss Pgr Patr Productid"].strip() != "GAS" or r["Facility Type"].strip() != PLANT or not px:
            continue
        m = datetime.strptime(" ".join(r["Prod Period"].split()), "%B %Y")
        v = r["Deliverablevolumes"].strip()
        vals[px][m] += (float(v) if v else 0.0) * E3M3_TO_MMCF / calendar.monthrange(m.year, m.month)[1]
        if px not in info or m >= info[px]["_m"]:          # the newest month's name/operator
            info[px] = {"name": " ".join(r["Fac Name"].split()), "operator": r["Organizationname"].strip(),
                        "location": r["Legal Description"].strip(), "subtype": PLANT, "_m": m}
    for f in info.values():
        f.pop("_m")
    return {k: dict(v) for k, v in vals.items()}, info


class GasBcerProduction(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(SERIES, self.id, "BC gas production, monthly (BCER plant + battery outlets)", "MMcf/d", "M",
                           "Gas production / totals"),
                SeriesMeta(AITKEN, self.id, "Aitken Creek storage net withdrawal, monthly (BCER; + withdrawal, − "
                           "injection)", "MMcf/d", "M", "Westcoast / scheduled (OA)")]

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        page = p.get("page", PAGE)
        window = int(p.get("window_months", WINDOW_MONTHS))
        text = self._csv(ctx, page, window_labels(local_today(), window) if window else None)
        if window and csv_months(text) < PLANT_MONTHS:
            text = self._csv(ctx, page, None)              # never store less than the unfiltered report gives
            warn = [f"BCER filtered CSV had fewer than {PLANT_MONTHS} months: read the unfiltered report"]
        else:
            warn = []
        months = bc_production(text)
        if not months:
            raise FormatDrift("BCER facility report: no month with gas plants")
        keep = sorted(months)[-int(p.get("months_back", MONTHS_BACK)):]
        for m in keep:
            if not RANGE[0] <= months[m] <= RANGE[1]:
                raise FormatDrift(f"{m:%Y-%m}: BC production {months[m]:,.0f} MMcf/d outside {RANGE}")
        as_of = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        res = FetchResult(meta=self.declared_series())
        res.obs = [Obs(SERIES, m, months[m], as_of) for m in keep]
        aitken = aitken_net(text)
        first_aitken = sorted(aitken)[-int(p.get("aitken_months", AITKEN_MONTHS)):] if aitken else []
        res.obs += [Obs(AITKEN, m, aitken[m], as_of) for m in first_aitken]      # 15 months: the same month last year
        if not aitken:
            res.warnings.append("Aitken Creek storage facilities not found in the BCER report")
        plants, info = plant_outlets(text)
        if plants:
            recent = set(sorted(months)[-PLANT_MONTHS:])
            for px, by in sorted(plants.items()):
                res.obs += [Obs(plant_series_id(px), m, v, as_of) for m, v in sorted(by.items()) if m in recent]
            res.meta += [SeriesMeta(plant_series_id(px), self.id, f"{info[px]['name']}: gas delivered out of the plant "
                                    "(BCER, monthly)", "MMcf/d", "M", PLANT_GROUP)
                         for px in sorted(plants) if recent & set(plants[px])]
            try:                     # L-075: the top-producers popup's registry, in the user's data dir (never the repo)
                save_registry("bc", {px: info[px] for px in plants if recent & set(plants[px])})
            except OSError as e:
                res.warnings.append(f"BC facility registry not written: {e}")
        else:
            res.warnings.append(f"BCER report: no gas plant rows with {sorted(PLANT_COLUMNS)}; no plant series")
        res.warnings += warn + [f"BC production {keep[-1]:%Y-%m}: {months[keep[-1]]:,.0f} MMcf/d"]
        return res

    @staticmethod
    def _csv(ctx: Context, page: str, labels: list[str] | None) -> str:
        """The report's CSV: the page (APEX session cookie; `labels` set the Prod Period IN filter), then the download.
        APEX keeps an IR filter in the session and the runner's client is shared, so each read starts a fresh session
        (gas_lng_bcer's facility filter, or our own window on the fallback, must not carry over; Reviewer on #394)."""
        drop_cookies(ctx, "reports.bc-er.ca")
        ctx.get(page, params={"irin_prod_period": ",".join(labels)} if labels else None, timeout=120)
        r = ctx.get(page, params={"request": "CSV"}, timeout=300)
        if "csv" not in r.headers.get("content-type", ""):
            raise FormatDrift(f"BCER report returned {r.headers.get('content-type')!r}, not CSV")
        return r.content.decode("utf-8-sig", errors="replace")
