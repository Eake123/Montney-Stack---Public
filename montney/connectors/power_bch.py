"""BC Hydro: balancing-authority load and intertie flows (L-106, PLAN 8c wcan). Keyless public files:

  https://www.bchydro.com/bctc/system_cms/actual_flow/data1.xls
      the last 7 days of BC-US and BC-AB actual flow, every 5 min (~180 KB, refreshed every 5 min). Read hourly
      (``report: interties``, 1 request/h) and stored as complete-hour means (12 samples).
  https://www.bchydro.com/content/dam/BCHydro/customer-portal/documents/corporate/suppliers/transmission-system/
  balancing_authority_load_data/CurrentHourlyBALoad.xls
      the month's hourly control-area load by hour ending (~35 KB). BC Hydro updates it each weekday with the previous
      day (Monday: Friday-Sunday), so it's 1-3.5 days behind. Read daily (``report: load``, 1 request/day).

BC Hydro publishes **no generation total or by fuel**. The wcan region derives BC generation as load + net exports
(regions/wcan.yaml, marked estimate).

Time: the two files run on **different clocks** (Power, PLAN 9, 2026-11-13):
  - data1.xls is on **Pacific Standard Time all year** (fixed UTC-8): its newest row is ~5 min before the file's
    Last-Modified only when read as UTC-8 (08:35 at 16:40Z on 2026-11-13), and its 2,016 five-minute rows cross the
    2026-11-01 fall-back with no repeated hour. So ``BCH_TZ = Etc/GMT+8`` (the "Etc" sign convention), a named exception
    like IESO and MISO.
  - CurrentHourlyBALoad.xls is on **BC local time**, ``America/Vancouver``: permanent UTC-7 from 2026-11-01 under the
    bundled tzdata (BC, like Alberta, stopped changing clocks; L-051 reads BC Hydro's hydromet files the same way). Read
    as PST until PLAN 9, every load hour was stamped **one hour late**: CAISO OASIS's BCHA actual load (explicit GMT
    interval stamps) matches this file to 0.8 % MAE only when HE h of day D = [D + h-1 h, D + h h) UTC-7, against 2.9 %
    read as PST. And BCHA's daily shape started to lead Seattle's (and the other Northwest WEIM areas') on 2026-11-01,
    as a clock change does (correlation of hourly changes, BCHA leading by 0 h / 1 h: 0.96 / 0.79 in October, 0.93 /
    0.93 in November: the clock-driven part moved an hour, the sunset-driven part didn't). The header names no zone. HE24 on 2026-11-01 was the 24th of 24 hours (no fall-back), which fits UTC-7 as well as PST.
``ts`` is the hour's start in naive UTC.

Sign: flows are as BC Hydro publishes them, **+ = export from BC** (BC-AB -146 matched AESO's BC intertie +150, AESO
signing + as an Alberta export, on 2026-11-07).

Series (MW, freq 1h):
  bch.intertie.bc_us   BC-US (BPA) actual flow, + export from BC
  bch.intertie.bc_ab   BC-Alberta actual flow, + export from BC
  bch.load             BC Hydro control-area load (province-wide gross load). The wcan region now takes BC's load from
                       CAISO OASIS (weim.bcha.load, power_caiso_weim_load: ~1 h late, not 1-4.5 days); this file
                       stays as its cross-check.
Fail closed (FormatDrift): a header that isn't Time / BC-US Actual / BC-AB Actual, a load file without the hourly table,
or a value out of range.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import xlrd

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

FLOW_URL = "https://www.bchydro.com/bctc/system_cms/actual_flow/data1.xls"
LOAD_URL = ("https://www.bchydro.com/content/dam/BCHydro/customer-portal/documents/corporate/suppliers/"
            "transmission-system/balancing_authority_load_data/CurrentHourlyBALoad.xls")
BCH_TZ = ZoneInfo("Etc/GMT+8")                     # data1.xls: Pacific Standard Time all year (fixed UTC-8)
LOAD_TZ = ZoneInfo("America/Vancouver")            # CurrentHourlyBALoad.xls: BC local time (UTC-7 since 2026-11-01)
GROUP = "BC Hydro / load and interties"
MAX_MW = 20_000


def _utc(local: datetime, tz: ZoneInfo = BCH_TZ) -> datetime:
    return local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _sheet(content: bytes, what: str):
    try:
        return xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    except Exception as e:                                        # noqa: BLE001 - not the file we know
        raise FormatDrift(f"BC Hydro {what}: not a readable .xls ({e})") from e


def parse_flows(content: bytes) -> dict[datetime, dict[str, float]]:
    """data1.xls -> {hour start UTC: {"bc_us", "bc_ab"}} means over complete hours (12 five-minute samples)."""
    sh = _sheet(content, "actual flow")
    head = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    if head[:3] != ["Time", "BC-US Actual", "BC-AB Actual"]:
        raise FormatDrift(f"BC Hydro actual flow: header {head[:3]}")
    by: dict[datetime, list[tuple[float, float]]] = defaultdict(list)
    for r in range(1, sh.nrows):
        t, us, ab = sh.cell_value(r, 0), sh.cell_value(r, 1), sh.cell_value(r, 2)
        if not isinstance(t, float) or not isinstance(us, float) or not isinstance(ab, float):
            continue
        local = xlrd.xldate_as_datetime(t, 0).replace(second=0, microsecond=0)
        by[_utc(local).replace(minute=0)].append((us, ab))
    out = {}
    for h, rows in by.items():
        if len(rows) < 12:
            continue                                              # the current hour, or a gap: not a full hour
        us, ab = sum(x[0] for x in rows) / len(rows), sum(x[1] for x in rows) / len(rows)
        if not (-MAX_MW <= us <= MAX_MW and -MAX_MW <= ab <= MAX_MW):
            raise FormatDrift(f"BC Hydro actual flow {h}: {us:,.0f} / {ab:,.0f} MW")
        out[h] = {"bc_us": round(us, 1), "bc_ab": round(ab, 1)}
    return out


def parse_load(content: bytes) -> dict[datetime, float]:
    """CurrentHourlyBALoad.xls -> {hour start UTC: MW}. Rows: Date (MM/DD/YYYY), HE (1-24, BC local time),
    Control Area Load."""
    sh = _sheet(content, "hourly load")
    start = next((r for r in range(min(sh.nrows, 20)) if str(sh.cell_value(r, 0)).strip() == "Date"
                  and "Load" in str(sh.cell_value(r, sh.ncols - 1))), None)
    if start is None:
        raise FormatDrift("BC Hydro hourly load: no Date / HE / Control Area Load header")
    out = {}
    for r in range(start + 1, sh.nrows):
        d, he, mw = sh.cell_value(r, 0), sh.cell_value(r, 1), sh.cell_value(r, 2)
        if not isinstance(he, float) or not isinstance(mw, float):
            continue
        day = datetime.strptime(str(d).strip(), "%m/%d/%Y")
        if not 1 <= he <= 24 or not 0 < mw <= MAX_MW:
            raise FormatDrift(f"BC Hydro hourly load {d} HE{he}: {mw}")
        out[_utc(day + timedelta(hours=int(he) - 1), LOAD_TZ)] = mw
    if not out:
        raise FormatDrift("BC Hydro hourly load: no rows (NO-RESULT)")
    return out


class BcHydro(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.report = spec.get("params", {}).get("report", "interties")
        if self.report not in ("interties", "load"):
            raise ValueError(f"{self.id}: params.report must be interties or load, not {self.report!r}")

    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "tz": "PST all year (UTC-8)"}            # data1.xls
        if self.report == "interties":
            return [SeriesMeta("bch.intertie.bc_us", self.id, "BC Hydro BC-US actual flow (+ export from BC)", "MW",
                               "1h", GROUP, extra=ex),
                    SeriesMeta("bch.intertie.bc_ab", self.id, "BC Hydro BC-Alberta actual flow (+ export from BC)",
                               "MW", "1h", GROUP, extra=ex)]
        return [SeriesMeta("bch.load", self.id, "BC Hydro control-area load (hourly, published 1-3.5 days late)", "MW",
                           "1h", GROUP, extra={**ex, "tz": "America/Vancouver (UTC-7)", "lag": "1-4.5 days"})]

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        if self.report == "interties":
            for h, row in sorted(parse_flows(ctx.get(FLOW_URL, timeout=60).content).items()):
                res.obs.append(Obs("bch.intertie.bc_us", h, row["bc_us"], h))
                res.obs.append(Obs("bch.intertie.bc_ab", h, row["bc_ab"], h))
        else:
            for h, mw in sorted(parse_load(ctx.get(LOAD_URL, timeout=60).content).items()):
                res.obs.append(Obs("bch.load", h, mw, h))
        if not res.obs:
            raise FormatDrift(f"BC Hydro {self.report}: no complete hour (NO-RESULT)")
        return res
