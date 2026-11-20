"""LNG Canada monthly feed-gas receipts (BCER) and BC producer gas delivered straight into Coastal GasLink (PLAN 5c, item 2).

Q6 / L-041: personal-use basis (L-006). Fetched monthly-ish, never redistributed.

Source: BC Energy Regulator "Facility Petrinex Reporting" (an Oracle APEX interactive report), filtered to the
LNG Canada plant (BCER facility 00018315 = Petrinex BCLN3165257, "LNG Plant"):

  GET https://reports.bc-er.ca/ogc/r/app001/ams_reports/facility-petrinex-reporting                 (session cookie)
  GET  …/facility-petrinex-reporting?ir_facilityid=00018315&request=CSV                             (the CSV, ~5 KB)

Columns: Facilityid, Px Facility Id, …, Prod Period ("September 2026"), Ss Pgr Patr Productid (GAS/OIL), Flaredgas,
Ventedgas, Fuelgas, Deliverablevolumes, Productionvolumes, Receiptvolumes, in 10³m³ per month. Only GAS rows are used.
Lag: about 1 month (Sep 2026 was there on 2026-11-01).

Series (MMcf/d = 10³m³ × 0.0353147 ÷ days in the month; freq M, ts = the first of the month):
  lng.lng_canada.receipts   gas received by the plant: its feed gas (the monthly oracle for gas_lng_ppa)
  lng.lng_canada.fuel       fuel gas burned at the plant
  lng.lng_canada.flare      gas flared
  cgl.producer_direct       BC producer gas into Coastal GasLink that never touches NGTL:
                            LNG Canada receipts − the month's mean NGTL delivery at Willow Valley
                            (`ngtl.gdsr.willow_valley.flow`, read back from the store).
                            - **NO-RESULT** (not written, a warning instead) unless ≥ 90 % of the month's days have a
                              Willow Valley value.
                            - **Band ±3 %:** BCER's reported receipts (~±1 %) plus GDSR's extrapolated Willow Valley
                              flow (a ~150 MMcf/d term with a few % error).

A negative month is real, not an error: NGTL delivered more than the plant received while CGL was being line-filled
during commissioning (e.g. Nov 2024: receipts 0.9, Willow Valley 2.3 MMcf/d).

**Assumption:** all LNG Canada receipts arrive through CGL. CGL is LNG Canada's only supply line; its 4 receipt meters
are all at Wilde Lake (docs/research/v2/gas-lng.md §1). The CGL partner meters aren't published, which is why this is
a residual.

as_of = the start of the UTC day the value was first fetched: a report vintage. BCER's values can be amended; with
change-only writes a changed month adds a vintage and an unchanged one adds nothing.

Fails loudly (FormatDrift) if the CSV header changes, the LNG Canada facility is missing, or a month's receipts are
implausible (> 4 Bcf/d: more than twice Phase 1's nameplate, so a unit change).
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
from datetime import datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import (E3M3_TO_MMCF, FormatDrift, drop_cookies, forget_on_error, get_at_most_every,
                                           read_stored)
from montney.store import Obs, SeriesMeta

log = logging.getLogger(__name__)

PAGE = "https://reports.bc-er.ca/ogc/r/app001/ams_reports/facility-petrinex-reporting"
BCER_HOST = "reports.bc-er.ca"
FACILITY = "00018315"
CSV_MAX_AGE = timedelta(hours=12)      # PLAN 9: the monthly report, re-read twice a day at most
PX_ID = "BCLN3165257"
GROUP = "LNG / LNG Canada"
WILLOW = "ngtl.gdsr.willow_valley.flow"
RECEIPTS, FUEL, FLARE, DIRECT = ("lng.lng_canada.receipts", "lng.lng_canada.fuel", "lng.lng_canada.flare",
                                 "cgl.producer_direct")
COLUMNS = {"Facilityid", "Px Facility Id", "Prod Period", "Ss Pgr Patr Productid", "Flaredgas", "Fuelgas", "Receiptvolumes"}
MAX_MMCFD = 4000.0
MIN_WILLOW_DAYS = 0.90
BAND_DIRECT = 0.03
# LNG export as one daily number (PLAN 4b / L-058): Willow Valley (NGTL → CGL, measured daily) + the producer gas direct
# into CGL.
# **Direct-connect, daily regression** (PLAN 4c, L-060/L-061; no cap, Erik: "go with the data"):
#   direct(d) = a + b × Willow Valley(30 d to d) + c × Westcoast Sunset Creek, scheduled(30 d to d)
#   LNG export(d) = Willow Valley(d) + direct(d)
# - a, b, c are refit every run on the BCER months since TRAIN_FROM (monthly means of both flows vs BCER's monthly
#   direct). 2025 was LNG Canada's ramp (direct 0 → 800), a different regime, so it's left out.
# - Needs ≥ MIN_TRAIN months, each with ≥ 90 % of days of both flows; otherwise the latest BCER month is carried, as
#   before, with a warning.
# - **6B (PLAN step 6): Sunset Creek is now Westcoast's own scheduled delivery there** (`westcoast.oa.sunset_creek.to_ngtl`,
#   gas_westcoast_oa, back-filled from 2025-11), replacing NGTL's Groundbirch East receipt as the proxy. Westcoast's
#   schedule runs 520-660 MMcf/d where NGTL's Groundbirch East shows 140-250, and it tracks direct-connect much better
#   (part of Westcoast's Sunset Creek gas plausibly reaches CGL at Wilde Lake, next door).
# - **Back-test** (train on months ≤ M-2, predict M from M's flows; May-Sep 2026, n = 5):
#   | predictors                     | direct P90 | LNG export P90 |
#   | WV + NGTL Groundbirch East (4c) | 19.4 %     | 17.7 %         |
#   | WV + Westcoast Sunset (6B)      | 10.3 %     |  9.3 %         |  → ±11 % / ±10 %
#   Full fit on Jan-Sep 2026: direct = 142 + 2.97 × WV + 1.37 × Sunset.
# - **Thin:** n = 5 test months and a 3-parameter fit on 5-9 months; b > 0 is a level effect (both flows rise with LNG
#   demand), not a day-to-day trade-off. So the fit is fed trailing 30-day means (the scale it was trained on), not single days (Reviewer, #225):
#   direct moves as a level, and LNG export's day-to-day move is Willow Valley's own.
EXPORT, EXPORT_WV, EXPORT_DIRECT = "lng.export.total", "lng.export.willow_valley", "lng.export.direct"
# Erik (L-066): LNG export = Willow Valley (measured) + Sunset Creek (Westcoast scheduled, measured) + derived D/C (est.).
# The D/C is what the plant receives beyond the two measured flows: receipts − Willow Valley − Sunset, the same
# regression with Sunset moved to the measured side (coefficient on Sunset − 1). Adding Sunset to the old D/C (receipts −
# Willow Valley) would count it twice: +316 to +660 MMcf/d a month over LNG Canada's receipts, Nov 2025-Sep 2026.
# lng.export.direct stays "CGL-bound gas not via Willow Valley" = Sunset + D/C, so total export, BC production and the
# Montney box are unchanged in meaning.
EXPORT_SUNSET, EXPORT_DC = "lng.export.sunset", "lng.export.dc"
# The whole band is the D/C's: the back-test's largest absolute error (n 5, May-Sep 2026) is 166 MMcf/d (Jun). As shares
# today (~1.4 Bcf/d export, ~0.6 Bcf/d D/C): ±12 % of the export, ±28 % of the D/C.
DC_BAND_MMCFD, BAND_DC = 166.0, 0.28
BAND_EXPORT, BAND_EXPORT_DIRECT = 0.12, 0.11
BAND_EXPORT_CARRY, BAND_DIRECT_CARRY = 0.21, 0.23       # the fallback: carry-forward P90, Jan-Sep 2026
SUNSET = "westcoast.oa.sunset_creek.to_ngtl"            # Westcoast's scheduled Sunset Creek delivery (gas_westcoast_oa)
TRAIN_FROM, MIN_TRAIN = datetime(2026, 1, 1), 5
TRAILING_DAYS = 30      # Reviewer on #225: the monthly fit is applied to trailing 30-day means, so direct moves as a level
EXPORT_DAYS = 60


def mmcfd(e3m3: float, month: datetime) -> float:
    return e3m3 * E3M3_TO_MMCF / calendar.monthrange(month.year, month.month)[1]


def parse_csv(text: str) -> dict[datetime, dict[str, float]]:
    """{month: {"receipts", "fuel", "flare"} in 10³m³} for the LNG Canada GAS rows."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not COLUMNS <= set(rows[0]):
        raise FormatDrift(f"BCER Facility Petrinex Reporting: expected columns {sorted(COLUMNS)}")
    out: dict[datetime, dict[str, float]] = {}
    for r in rows:
        if r["Facilityid"] != FACILITY or r["Px Facility Id"] != PX_ID:
            raise FormatDrift(f"unexpected facility {r['Facilityid']}/{r['Px Facility Id']} in the filtered CSV")
        if r["Ss Pgr Patr Productid"].strip() != "GAS":
            continue
        m = datetime.strptime(" ".join(r["Prod Period"].split()), "%B %Y")
        num = lambda k: float(r[k]) if r.get(k) not in (None, "") else 0.0   # noqa: E731
        out[m] = {"receipts": num("Receiptvolumes"), "fuel": num("Fuelgas"), "flare": num("Flaredgas")}
    if not out:
        raise FormatDrift(f"no GAS rows for LNG Canada ({PX_ID})")
    return out


def read_willow(start: datetime, end: datetime) -> dict[datetime, float]:
    """Stored daily Willow Valley flows in [start, end], read-only in both store modes (gas_common.read_stored)."""
    return read_stored([WILLOW], start, end).get(WILLOW, {})


def read_sunset(start: datetime, end: datetime) -> dict[datetime, float]:
    return read_stored([SUNSET], start, end).get(SUNSET, {})


def ols(rows: list[list[float]], y: list[float]) -> list[float] | None:
    """Least squares by the normal equations (k = 3), Gauss-Jordan. None if singular."""
    k = len(rows[0])
    a = [[sum(r[i] * r[j] for r in rows) for j in range(k)] + [sum(r[i] * v for r, v in zip(rows, y))]
         for i in range(k)]
    for i in range(k):
        piv = max(range(i, k), key=lambda r: abs(a[r][i]))
        if abs(a[piv][i]) < 1e-9:
            return None
        a[i], a[piv] = a[piv], a[i]
        a[i] = [x / a[i][i] for x in a[i]]
        for r in range(k):
            if r != i:
                a[r] = [x - a[r][i] * z for x, z in zip(a[r], a[i])]
    return [row[k] for row in a]


def trailing_mean(daily: dict[datetime, float], t: datetime, n: int = TRAILING_DAYS) -> float | None:
    """Mean of the n gas days ending at t (≥ 90 % of them present), else None."""
    vals = [daily[t - timedelta(days=i)] for i in range(n) if t - timedelta(days=i) in daily]
    return sum(vals) / len(vals) if len(vals) >= MIN_WILLOW_DAYS * n else None


def month_mean(daily: dict[datetime, float], m: datetime) -> float | None:
    vals = [x for t, x in daily.items() if t.year == m.year and t.month == m.month]
    return sum(vals) / len(vals) if len(vals) >= MIN_WILLOW_DAYS * calendar.monthrange(m.year, m.month)[1] else None


class GasLngBcer(Connector):
    willow = staticmethod(read_willow)        # tests inject a fake
    sunset = staticmethod(read_sunset)

    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(EXPORT, self.id, "LNG export, daily: Willow Valley + Sunset Creek + derived D/C (est.; ±166 "
                           "MMcf/d, the D/C's)",
                           "MMcf/d", "D", GROUP, extra={"band": BAND_EXPORT}, day="gas"),
                SeriesMeta(EXPORT_WV, self.id, "LNG export part: NGTL Willow Valley into CGL (measured)", "MMcf/d", "D",
                           GROUP, day="gas"),
                SeriesMeta(EXPORT_DIRECT, self.id, "LNG export part: producers direct into CGL, daily regression on "
                           "Willow Valley + Westcoast Sunset Creek scheduled (±11 %)",
                           "MMcf/d", "D", GROUP, extra={"band": BAND_EXPORT_DIRECT}, day="gas"),
                SeriesMeta(EXPORT_SUNSET, self.id, "LNG export part: Westcoast Sunset Creek, scheduled (measured)",
                           "MMcf/d", "D", GROUP, day="gas"),
                SeriesMeta(EXPORT_DC, self.id, f"LNG export part: derived D/C, producers direct into CGL (est., "
                           f"±{DC_BAND_MMCFD:.0f} MMcf/d)", "MMcf/d", "D", GROUP, extra={"band": BAND_DC}, day="gas"),
                SeriesMeta(RECEIPTS, self.id, "LNG Canada feed-gas receipts (BCER, monthly)", "MMcf/d", "M", GROUP),
                SeriesMeta(FUEL, self.id, "LNG Canada fuel gas (BCER, monthly)", "MMcf/d", "M", GROUP),
                SeriesMeta(FLARE, self.id, "LNG Canada flared gas (BCER, monthly)", "MMcf/d", "M", GROUP),
                SeriesMeta(DIRECT, self.id, "BC producer gas direct into Coastal GasLink (LNG Canada receipts − "
                                            "NGTL Willow Valley; ±3 %)", "MMcf/d", "M", GROUP, extra={"band": BAND_DIRECT})]

    @forget_on_error
    def fetch(self, ctx: Context) -> FetchResult:
        page = self.spec.get("params", {}).get("page", PAGE)

        def session() -> None:
            drop_cookies(ctx, BCER_HOST)    # a fresh APEX session: its IR filter sticks to the session (#394)
            ctx.get(page)                                        # APEX session cookie (the client keeps it)

        # PLAN 9: the source runs hourly so the daily LNG export follows Willow Valley within the hour; the monthly
        # BCER report is read at most every CSV_MAX_AGE (2 requests), the other runs reuse it.
        r = get_at_most_every(ctx, page, CSV_MAX_AGE, params={"ir_facilityid": FACILITY, "request": "CSV"},
                              before=session)
        if "csv" not in r.headers.get("content-type", ""):
            raise FormatDrift(f"BCER report returned {r.headers.get('content-type')!r}, not CSV")
        months = parse_csv(r.text)
        as_of = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        res = FetchResult(meta=self.declared_series())
        for m, v in sorted(months.items()):
            rec = mmcfd(v["receipts"], m)
            if rec > MAX_MMCFD:
                raise FormatDrift(f"{m:%b %Y}: LNG Canada receipts {rec:,.0f} MMcf/d > {MAX_MMCFD:,.0f} (a unit change?)")
            for sid, key in ((RECEIPTS, "receipts"), (FUEL, "fuel"), (FLARE, "flare")):
                res.obs.append(Obs(sid, m, mmcfd(v[key], m), as_of))

        first, last = min(months), max(months)
        last_end = last.replace(day=calendar.monthrange(last.year, last.month)[1])
        try:
            willow = self.willow(first, ctx.now)
        except Exception as e:  # noqa: BLE001 - a read failure is a NO-RESULT, never a crash or a guess
            log.warning("%s: reading Willow Valley failed: %s", self.id, e)
            willow = {}
        thin, latest_direct = [], None
        for m, v in sorted(months.items()):
            days = calendar.monthrange(m.year, m.month)[1]
            vals = [x for t, x in willow.items() if t.year == m.year and t.month == m.month]
            if len(vals) < MIN_WILLOW_DAYS * days:
                thin.append(f"{m:%Y-%m} ({len(vals)}/{days} d)")
                continue
            direct = mmcfd(v["receipts"], m) - sum(vals) / len(vals)
            res.obs.append(Obs(DIRECT, m, direct, as_of))
            latest_direct = (m, direct)
        if thin:
            res.warnings.append(f"NO-RESULT {DIRECT} for months with too few Willow Valley days: {', '.join(thin)}")

        # Daily LNG export for the gas days after the latest BCER month: Willow Valley (that day) + direct-connect, from the
        # daily regression (or, with too few training months, the latest BCER month carried).
        if not (latest_direct and latest_direct[0] == last):
            res.warnings.append(f"NO-RESULT {EXPORT}: the latest BCER month {last:%Y-%m} has no producer-direct value")
            return res
        try:
            sunset = self.sunset(first, ctx.now)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: reading Sunset Creek failed: %s", self.id, e)
            sunset = {}
        direct_m = {o.ts: o.value for o in res.obs if o.series_id == DIRECT}
        train = [(m, month_mean(willow, m), month_mean(sunset, m)) for m in sorted(direct_m) if m >= TRAIN_FROM]
        train = [(m, w, g) for m, w, g in train if w is not None and g is not None]
        coef = ols([[1.0, w, g] for _, w, g in train], [direct_m[m] for m, _, _ in train]) if len(train) >= MIN_TRAIN else None
        since = max(last_end, ctx.now - timedelta(days=EXPORT_DAYS))
        days, skipped = [], 0
        for t in sorted(t for t in willow if t > since):
            if coef:
                w30, s30 = trailing_mean(willow, t), trailing_mean(sunset, t)
                if w30 is None or s30 is None:
                    skipped += 1
                    continue
                # the month-scale fit on month-scale inputs, with Sunset on the measured side: D/C = fit − Sunset(30 d)
                dc = coef[0] + coef[1] * w30 + (coef[2] - 1.0) * s30
            else:
                s30 = trailing_mean(sunset, t)
                dc = latest_direct[1] - (s30 or 0.0)
            days.append(t)
            sun = sunset.get(t)
            if sun is None:                     # no Sunset that day: the part sums use its 30-day level
                sun = trailing_mean(sunset, t) or 0.0
            res.obs += [Obs(EXPORT, t, willow[t] + sun + dc, as_of), Obs(EXPORT_WV, t, willow[t], as_of),
                        Obs(EXPORT_SUNSET, t, sun, as_of), Obs(EXPORT_DC, t, dc, as_of),
                        Obs(EXPORT_DIRECT, t, sun + dc, as_of)]
        if coef:
            res.warnings.append(f"LNG export: {len(days)} gas days; D/C = {coef[0]:,.0f} + {coef[1]:.2f} × Willow Valley "
                                f"(30 d) + {coef[2] - 1.0:.2f} × Sunset Creek (30 d), fitted on {len(train)} BCER months"
                                + (f"; {skipped} day(s) lack a 30-day window" if skipped else ""))
        else:
            for m in res.meta:
                if m.series_id in (EXPORT, EXPORT_DIRECT):
                    m.extra["band"] = BAND_EXPORT_CARRY if m.series_id == EXPORT else BAND_DIRECT_CARRY
            res.warnings.append(f"LNG export: only {len(train)} training months (need {MIN_TRAIN}); direct-connect "
                                f"{latest_direct[1]:,.0f} MMcf/d carried from {latest_direct[0]:%Y-%m}")
        return res
