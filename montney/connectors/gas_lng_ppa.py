"""LNG Canada feed gas from cargo departures at Kitimat (PLAN 5c, item 1; Q5 / L-041: personal use, at most 1 poll / 15 min).

Source: the Pacific Pilotage Authority's public live traffic feed (no auth, undocumented; "for information purposes
only"):

  GET https://ppaportal.portlink.co/api/pdams/GetCurrentVesselTraffic

The feed lists **active and upcoming** pilotage jobs only. A job shows as `Order` (planned, up to ~10 days ahead;
its draught is a placeholder) and then as `Dispatched` (under way, with the actual draught). It drops out when it's
done. On 2026-11-01 at 21:16Z the oldest job was from 13:00Z that day, and none were "Completed". So a departure is
only seen if we poll while it's dispatched, which is several hours for the KLG2 → sea transit. History is therefore
**accumulated in the store**, one point per departure.

**Loaded departure** (a cargo): a job **from** the LNG Canada berth `KLG2` **to** anywhere else, where:
- the vessel type is `Tanker`;
- the status is `Dispatched` or `Completed`;
- the actual draught is ≥ 10.5 m.

Calibration on PPA's quarterly 2025-26 records: loaded departures leave at 11.4-12.0 m and arrive at 8.9-9.7 m. The
8 light departures (9.1-9.6 m) were ballast or shift moves, and one was a cargo ship at 6.6 m.

Series:
  lng.kitimat.cargo      one point per loaded departure. ts = the departure (job order) time, value 1, as_of = ts.
                         A job seen again on later polls rewrites the same point, so change-only stores it once.
                         freq D (a sparse daily-tier series; the points carry their time of day).
  lng.kitimat.ppa_poll   heartbeat: ts = this poll's hour (UTC, floored), value = the jobs listed, as_of = ts. A
                         missing hour means we weren't polling, so a departure could have been missed. freq 1h.
  lng.kitimat.feed_30d   LNG Canada feed gas, MMcf/d, for the 30 days ending at the end of the latest complete Alberta
                         day (ts = that day, as_of = its end): cargoes × CARGO_BCF ÷ EXPORT_TO_FEED ÷ 30. freq D.
                         **NO-RESULT** (no point written, a warning instead) unless:
                         - at least MIN_POLL_COVER of the window's hours have a heartbeat;
                         - and no gap is longer than MAX_GAP, the shortest a departure stays visible.

Constants:
- **CARGO_BCF = 3.523 Bcf** per cargo: the mean of CER's per-vessel volumes for 125 Kitimat cargoes (sd 64 MMcf,
  ±1.8 %; docs/research/v2/gas-lng.md §4). Every vessel is ~174k m³ class.
- **EXPORT_TO_FEED = 0.90**: exported gas as a share of plant receipts (fuel ~4.7 %, flare ~1 %, plus boil-off and
  stock change).
  - Fitted on Jan-Jun 2026: monthly loaded departures in PPA's quarterly records × 3.523 vs BCER's reported LNG
    Canada receipts (facility BCLN3165257).
  - Tested out of sample on Jul-Sep 2026: −4.7 %, −1.6 %, +9.1 % (at 0.90).
- **Band ±10 %** at 30 days. Two sources: the monthly residual (P90 ≈ 10 %, Jan-Sep 2026), and the ±1 cargo
  (≈ 8 %) a window edge can move.

Reading back: the 30-day sum needs points stored by earlier polls. They're read through `gas_common.read_stored`, which
is read-only in both modes (Postgres: the reader role; DuckDB: the file opened `read_only=True`), and merged with this
poll's points. With no readable history (the first 30 days, an unreadable store), coverage is short, so it's a
NO-RESULT, never a guess.

Fails loudly (FormatDrift) if the payload isn't `{"result": "Succeed", "data": [...]}` with the expected job fields.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import AB, FormatDrift, local_to_utc, read_stored
from montney.store import Obs, SeriesMeta

log = logging.getLogger(__name__)

API = "https://ppaportal.portlink.co/api/pdams/GetCurrentVesselTraffic"
BERTH = "KLG2"
LOADED_STATUSES = {"dispatched", "completed"}
MIN_LOADED_DRAUGHT_M = 10.5
CARGO_BCF = 3.523
EXPORT_TO_FEED = 0.90
BAND = 0.10
WINDOW_DAYS = 30
MIN_POLL_COVER = 0.95                 # of the window's hours
MAX_GAP = timedelta(hours=4)          # a dispatched departure stays listed for several hours
GROUP = "LNG / Kitimat"
CARGO = "lng.kitimat.cargo"
POLL = "lng.kitimat.ppa_poll"
FEED = "lng.kitimat.feed_30d"
_FIELDS = {"jobOrderId", "vessel", "orderTime", "fromLocationShortCode", "toLocationShortCode", "status", "actualDraught"}


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def day_end(d: date) -> datetime:
    """End of the Alberta day d, as naive UTC."""
    return local_to_utc(datetime.combine(d + timedelta(days=1), datetime.min.time()), AB)


def alberta_date(t_utc: datetime) -> date:
    return t_utc.replace(tzinfo=timezone.utc).astimezone(AB).date()


def is_loaded_departure(j: dict) -> bool:
    v = j.get("vessel") or {}
    draught = j.get("actualDraught")
    return (j.get("fromLocationShortCode") == BERTH and j.get("toLocationShortCode") != BERTH
            and str(v.get("type", "")).lower() == "tanker"
            and str(j.get("status", "")).lower() in LOADED_STATUSES
            and draught is not None and float(draught) >= MIN_LOADED_DRAUGHT_M)


def feed_mmcfd(cargoes: int, days: int = WINDOW_DAYS) -> float:
    return cargoes * CARGO_BCF * 1000.0 / EXPORT_TO_FEED / days


def coverage(polls: set[datetime], start: datetime, end: datetime) -> tuple[float, timedelta]:
    """(share of the hours in [start, end) that have a heartbeat, longest stretch with none)."""
    hours = int((end - start).total_seconds() // 3600)
    have = sorted(t for t in polls if start <= t < end)
    share = len(have) / hours if hours else 0.0
    edges = [start - timedelta(hours=1)] + have + [end]
    gap = max((b - a - timedelta(hours=1) for a, b in zip(edges, edges[1:])), default=end - start)
    return share, gap


def read_history(start: datetime, end: datetime) -> dict[str, dict[datetime, float]]:
    """Stored cargo and heartbeat points in [start, end], read-only in both store modes (gas_common.read_stored)."""
    return read_stored([CARGO, POLL], start, end)


class GasLngPpa(Connector):
    history = staticmethod(read_history)    # tests inject a fake

    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(CARGO, self.id, "LNG Canada loaded cargo departure (Kitimat, PPA)", "cargo", "D", GROUP),
                SeriesMeta(POLL, self.id, "PPA traffic poll heartbeat (jobs listed)", "jobs", "1h", GROUP),
                SeriesMeta(FEED, self.id, "LNG Canada feed gas, 30-day estimate from cargoes (±10 %)", "MMcf/d", "D",
                           GROUP, extra={"band": BAND, "cargo_bcf": CARGO_BCF, "export_to_feed": EXPORT_TO_FEED,
                                         "window_days": WINDOW_DAYS})]

    def fetch(self, ctx: Context) -> FetchResult:
        url = self.spec.get("params", {}).get("api", API)
        body = ctx.get(url).json()
        jobs = body.get("data") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("result") != "Succeed" or not isinstance(jobs, list) or not jobs:
            raise FormatDrift(f"{self.id}: expected {{'result': 'Succeed', 'data': [...]}}")
        missing = _FIELDS - set(jobs[0])
        if missing:
            raise FormatDrift(f"{self.id}: job fields missing: {sorted(missing)}")
        res = FetchResult(meta=self.declared_series())

        hour = ctx.now.replace(minute=0, second=0, microsecond=0)
        res.obs.append(Obs(POLL, hour, float(len(jobs)), hour))
        cargo = {parse_utc(j["orderTime"]): 1.0 for j in jobs if is_loaded_departure(j)}
        res.obs.extend(Obs(CARGO, t, 1.0, t) for t in sorted(cargo))

        last_day = alberta_date(ctx.now) - timedelta(days=1)     # the latest complete Alberta day
        end = day_end(last_day)
        start = end - timedelta(days=WINDOW_DAYS)
        try:
            stored = self.history(start - timedelta(hours=1), end)
        except Exception as e:  # noqa: BLE001 - a read failure is a NO-RESULT, never a crash or a guess
            log.warning("%s: reading stored points failed: %s", self.id, e)
            stored = {}
        polls = {t for t in stored.get(POLL, {}) if start <= t < end} | ({hour} if start <= hour < end else set())
        departures = {t for t in stored.get(CARGO, {}) if start <= t < end} | {t for t in cargo if start <= t < end}
        share, gap = coverage(polls, start, end)
        if share < MIN_POLL_COVER or gap > MAX_GAP:
            res.warnings.append(f"NO-RESULT: polls cover {share:.0%} of the 30 days to {last_day} "
                                f"(need {MIN_POLL_COVER:.0%}), longest gap {gap} (max {MAX_GAP}); {FEED} not written")
            return res
        ts = datetime(last_day.year, last_day.month, last_day.day)
        res.obs.append(Obs(FEED, ts, feed_mmcfd(len(departures)), end))
        return res
