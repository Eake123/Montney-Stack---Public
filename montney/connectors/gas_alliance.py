"""Alliance Pipeline (Canada): daily receipts and capacity by zone (PLAN 4b item 3; promoted from P6 by L-058).

Source: Pembina's Alliance informational-postings API (no auth; the same backend as ips.alliancepipeline.pembina.com):
  POST https://content.alliancepipeline.pembina.com/api/dailyThroughputCapacities/search
       {"startDate": "<YYYY-MM-DD>T00:00:00", "sortFields": [], "filterFields": [], "page": 0, "pageSize": 50, ...}
One gas day per request: `endDate` is ignored (checked 2026-11-01), so the connector asks for each of the last few days.

**Rolling history:** each run also asks for `backfill_per_run` older days, stepping back through the last `history_days`
by the run's hour, so the whole window is re-read about once a day (100 days ÷ 4 per hourly run ≈ 25 h) at 4 extra
requests a run. The provincial production totals need a full regulator month of Zone 1 / Zone 2 (ngtl_split), and a
daily re-read also picks up Alliance's late revisions. Stateless: the days follow from ctx.now.
It returns one row per gas day and zone (`rdtRecType` "1R" = receipts):
- `rdtCapacityMcf`, `rdtSchedMcf`;
- `rdtDataCreateDtm` (Central time).

Alliance publishes **Mcf/d**, so MMcf/d = Mcf ÷ 1000, with no heating-value assumption.

**Zones** (CER key point KP0045 "Zone 2", near the Blueberry compressor station: "aggregates liquids rich gas primarily
from the Montney Basin in northeast BC & northwest AB. The majority of Alliance receipt points are upstream of Zone 2,
with smaller volumes received by the system south of Zone 2"):
- **Zone 1:** the receipts upstream of Blueberry, i.e. Montney gas from NE BC + NW AB (~1.3 Bcf/d capacity).
- **Zone 2:** the receipts south of it, in Alberta (~0.49 Bcf/d capacity).

Alliance publishes no per-receipt-point volumes, so **Zone 1 can't be split BC vs AB** from this source. The AB/BC
production totals (PLAN 4b item 4) don't split it: they carry Alliance inside each province's regulator remainder
(ngtl_split.py, "Provincial production today").

Series (MMcf/d, freq D, ts = gas day; as_of = Alliance's create time, converted to UTC; intraday re-postings are
vintages):
  alliance.zone1.receipts / .capacity
  alliance.zone2.receipts / .capacity
  alliance.receipts.total (Zone 1 + Zone 2 scheduled) / alliance.capacity.total

Fails loudly (FormatDrift) if a row lacks the expected fields, or a zone other than Zone 1/Zone 2 appears.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, local_to_utc
from montney.store import Obs, SeriesMeta

API = "https://content.alliancepipeline.pembina.com/api/dailyThroughputCapacities/search"
CENTRAL = ZoneInfo("America/Chicago")
GROUP = "Alliance Pipeline"
ZONES = {"Zone 1": "zone1", "Zone 2": "zone2"}
FIELDS = {"rdtGasDay", "rdtRecType", "rdtDescription", "rdtCapacityMcf", "rdtSchedMcf", "rdtDataCreateDtm"}
DAYS_BACK = 3        # a gas day is posted the next morning and revised intraday
HISTORY_DAYS, BACKFILL_PER_RUN = 100, 4


def backfill_days(now: datetime, days_back: int, history: int, per_run: int) -> list[int]:
    """Days-back offsets for this run's history requests: a block of `per_run` that steps by the hour through
    (days_back, history], so consecutive hourly runs cover the whole window."""
    span = max(history - days_back, 0)
    if not span or per_run <= 0:
        return []
    hours = int((now - datetime(1970, 1, 1)).total_seconds()) // 3600     # ctx.now is naive UTC: no local-time .timestamp()
    start = (hours * per_run) % span
    return sorted({days_back + 1 + (start + i) % span for i in range(min(per_run, span))})


def _ts(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%m/%d/%Y")


def _created(s: str) -> datetime:
    return local_to_utc(datetime.strptime(s.strip(), "%m/%d/%Y %I:%M:%S %p"), CENTRAL)


class GasAlliance(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for z, label in (("zone1", "Zone 1 (NE BC + NW AB, upstream of Blueberry)"),
                         ("zone2", "Zone 2 (Alberta, south of Blueberry)")):
            out += [SeriesMeta(f"alliance.{z}.receipts", self.id, f"Alliance {label} receipts (scheduled)", "MMcf/d", "D",
                               GROUP, day="gas"),
                    SeriesMeta(f"alliance.{z}.capacity", self.id, f"Alliance {label} capacity", "MMcf/d", "D", GROUP,
                               day="gas")]
        out += [SeriesMeta("alliance.receipts.total", self.id, "Alliance Canada receipts, Zone 1 + Zone 2 (scheduled)",
                           "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta("alliance.capacity.total", self.id, "Alliance Canada receipt capacity, Zone 1 + Zone 2",
                           "MMcf/d", "D", GROUP, day="gas")]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        rows: list[dict] = []
        days_back = int(p.get("days_back", DAYS_BACK))
        older = backfill_days(ctx.now, days_back, int(p.get("history_days", HISTORY_DAYS)),
                              int(p.get("backfill_per_run", BACKFILL_PER_RUN)))
        for back in [*sorted(older, reverse=True), *range(days_back, -1, -1)]:   # one gas day per request
            day = (ctx.now - timedelta(days=back)).date()
            body = {"startDate": f"{day}T00:00:00", "sortFields": [], "filterFields": [], "page": 0, "pageSize": 50,
                    "isFirstSearch": False}
            data = ctx.post(p.get("api", API), json=body).json().get("data")
            if not isinstance(data, list):
                raise FormatDrift("Alliance dailyThroughputCapacities: no data list")
            rows += data
        if not rows:
            raise FormatDrift("Alliance dailyThroughputCapacities: no rows in the window")
        res = FetchResult(meta=self.declared_series())
        totals: dict[datetime, dict[str, float]] = defaultdict(lambda: {"receipts": 0.0, "capacity": 0.0, "n": 0})
        stamp: dict[datetime, datetime] = {}
        for r in rows:
            if not FIELDS <= set(r):
                raise FormatDrift(f"Alliance row lacks fields: {sorted(FIELDS - set(r))}")
            if r["rdtRecType"] != "1R":
                continue
            zone = ZONES.get(str(r["rdtDescription"]).strip())
            if zone is None:
                raise FormatDrift(f"Alliance: unexpected zone {r['rdtDescription']!r}")
            ts, as_of = _ts(r["rdtGasDay"]), _created(r["rdtDataCreateDtm"])
            rec, cap = float(r["rdtSchedMcf"]) / 1000.0, float(r["rdtCapacityMcf"]) / 1000.0
            res.obs += [Obs(f"alliance.{zone}.receipts", ts, rec, as_of), Obs(f"alliance.{zone}.capacity", ts, cap, as_of)]
            t = totals[ts]
            t["receipts"] += rec
            t["capacity"] += cap
            t["n"] += 1
            stamp[ts] = max(stamp.get(ts, as_of), as_of)
        for ts, t in totals.items():
            if t["n"] == len(ZONES):                       # a total only when both zones are posted
                res.obs += [Obs("alliance.receipts.total", ts, t["receipts"], stamp[ts]),
                            Obs("alliance.capacity.total", ts, t["capacity"], stamp[ts])]
        return res
