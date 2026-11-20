"""Corpus Christi LNG feed gas, scheduled, from Cheniere Corpus Christi Pipeline's public capacity posting (Gas,
2026-11-13; docs/research/pipelines/daily_demand_methods.md §4).

Source (keyless JSON): the FERC informational postings behind Cheniere's LNG Connection site
(https://lngconnection.cheniere.com, CCPL is company 400):

  https://lngconnectionapi.cheniere.com/api/Capacity/GetCapacity?tspNo=400&beginDate=MM/DD/YYYY&cycleId=166&locationId=0

It returns {"report": [...]}, one row per location, with `loc`, `loC_NAME`, `opeR_CAP`, `scheD_QTY` (Dth),
`floW_IND` and `postinG_DT_TIME` (Central clock). `locationId=0` means every location; cycle 166 is Intraday 3, the
last scheduling cycle of a gas day. The delivery into the plant is `CC200221` ("CORPUS CHRISTI-CCLIQ-D").
- No robots.txt on the API or EBB host. www.cheniere.com's robots disallows only /form-submit. No terms-of-use page in
  the EBB's page list.
- Checked from kraken's VPN exit on 2026-11-13: HTTP 200.
- The posting keeps only recent gas days (2026-06-01 already answers empty), so history builds from the first run.

One request per run, every 12 h: **yesterday's gas day (Central clock), Intraday 3**. After an outage, the gas days
missed since the last success are fetched too, at most BACKFILL (remembered in <home>/cache, never the repo).

Series: `lng.tx.corpus_christi.feedgas`, MMcf/d, gas day (NAESB: 9:00 Central). The value is CCPL's **scheduled**
delivery to Corpus Christi Liquefaction at 1,037 Btu/cf; the posted gas runs ~1,003 Btu/cf, so the volume is ~3 %
higher. A feed that reaches the plant without crossing CCPL isn't in it. as_of is the cycle's posting time (UTC).
`extra.capacity` is the posted operating capacity at that point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save, local_to_utc
from montney.store import Obs, SeriesMeta

URL = "https://lngconnectionapi.cheniere.com/api/Capacity/GetCapacity"
TSP = 400                       # Cheniere Corpus Christi Pipeline on LNG Connection
CYCLE = 166                     # Intraday 3: the gas day's last schedule
LOC = "CC200221"                # CORPUS CHRISTI-CCLIQ-D (delivery to the liquefaction plant)
SID = "lng.tx.corpus_christi.feedgas"
BTU_PER_CF = 1037.0
CENTRAL = ZoneInfo("America/Chicago")
BACKFILL = 3
MAX_MMCFD = 6000.0              # CCPL is 2.75 Bcf/d; anything past this is a unit change, not gas
GROUP = "LNG feed gas (US)"


def parse(payload, day: datetime) -> tuple[float, float, datetime] | None:
    """-> (scheduled MMcf/d, operating capacity MMcf/d, posted naive UTC), or None when the day isn't posted yet."""
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), list):
        raise FormatDrift("Cheniere GetCapacity: not {report: [...]}")
    rows = payload["report"]
    if not rows:
        return None
    hit = [r for r in rows if r.get("loc") == LOC]
    if not hit:
        raise FormatDrift(f"Cheniere GetCapacity: no row for {LOC} (CCLIQ-D) among {len(rows)} locations")
    r = hit[0]
    try:
        sched, cap = float(r["scheD_QTY"]), float(r["opeR_CAP"])
        posted = datetime.fromisoformat(str(r["postinG_DT_TIME"]).split(".")[0])
    except (KeyError, TypeError, ValueError) as e:
        raise FormatDrift(f"Cheniere GetCapacity: {LOC} row without scheD_QTY / opeR_CAP / postinG_DT_TIME ({e})") from e
    if r.get("floW_IND") not in (None, "D"):
        raise FormatDrift(f"Cheniere GetCapacity: {LOC} is no longer a delivery (floW_IND {r.get('floW_IND')!r})")
    v = sched / BTU_PER_CF                               # Dth = MMBtu; MMBtu / (MMBtu per MMcf) = MMcf
    if not 0 <= v <= MAX_MMCFD:
        raise FormatDrift(f"Cheniere GetCapacity: {LOC} {sched} Dth ({v:,.0f} MMcf/d) out of range")
    return v, cap / BTU_PER_CF, local_to_utc(posted, CENTRAL)


class GasCheniereCcl(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(SID, self.id, "Corpus Christi LNG feed gas, scheduled on Cheniere Corpus Christi Pipeline, "
                           "at 1,037 Btu/cf (CCLIQ-D, Intraday 3; a feed bypassing CCPL isn't in it)", "MMcf/d", "D", GROUP,
                           extra={"quality": "scheduled"}, day="gas")]

    def fetch(self, ctx: Context) -> FetchResult:
        today = ctx.now.replace(tzinfo=timezone.utc).astimezone(CENTRAL).replace(tzinfo=None)
        yesterday = today.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        cache = cache_load(self.id)
        days = [yesterday]
        last = cache.get("last_day")
        if last:
            d = datetime.fromisoformat(last) + timedelta(days=1)
            days = [d + timedelta(days=i) for i in range((yesterday - d).days + 1)][-BACKFILL:]
            if not days:
                days = [yesterday]          # already have it: re-read (a 12 h later read can carry a revision)
        res = FetchResult(meta=self.declared_series())
        done = None
        for day in days:
            payload = ctx.get(URL, params={"tspNo": TSP, "beginDate": day.strftime("%m/%d/%Y"), "cycleId": CYCLE,
                                           "locationId": 0}, timeout=60).json()
            got = parse(payload, day)
            if got is None:
                res.warnings.append(f"CCPL {day:%Y-%m-%d} Intraday 3 not posted yet")
                continue
            v, cap, posted = got
            res.obs.append(Obs(SID, day, v, max(posted, day)))
            res.meta[0].extra["capacity"] = round(cap, 1)
            done = day
        if done is not None and (not last or done.isoformat() > last):
            cache_save(self.id, {"last_day": done.isoformat()})
        return res
