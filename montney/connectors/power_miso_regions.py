"""MISO by region: Midwest (North + Central) and South load (L-100, Erik: "seperate miso regionally ... so Midwest miso
should be its own thing so it's not including say Louisiana").

MISO's public real-time API (power_miso.py) is MISO-wide only, and no regional fuel mix is published anywhere. Regional
load is in one daily market report, "Forecasted and Actual Load" (keyless, legacy .xls, ETag + Last-Modified):

  https://docs.misoenergy.org/marketreports/<YYYYMMDD>_df_al.xls

The file for day D (published ~01:20 EST on D) has, per hour ending (EST) and Local Resource Zone group, the MTLF
forecast (D-1 .. D+5) and the actual load (D-1 only), in MWh per hour = mean MW:

  Midwest = LRZ1 + LRZ2_7 + LRZ3_5 + LRZ4 + LRZ6   (MISO North + Central: MN, WI, IA, IL, IN, MI, MO, the Dakotas)
  South   = LRZ8_9_10                               (LA, AR, MS, East TX)

The report's last day is a placeholder (one constant for all 24 hours, e.g. 53,378 MW for every hour of 2026-11-08 in
the 2026-11-03 file): a flat day is skipped with a warning, never shown as a forecast.
The zone columns add up to the file's own "MISO" columns; a mismatch over ``TOLERANCE_MW`` fails the run. A new or
renamed LRZ column also fails the run rather than leaving a zone out of a region.

Requests: today's file, once it's published; it's cached in ``<home>/cache/miso`` (one day kept), so the other runs of
the day cost nothing. Before publication, yesterday's cached file is used (its forecast covers today). ~1 request/day.

Series (live, ``mode: live``; MW):
  miso.<region>.load.forecast       hourly MTLF, freq 1h: the box value is this hour's FORECAST, never shown as actual
  miso.<region>.load.forecast_peak  the day's forecast peak (EST date), freq D
  miso.<region>.load.actual_mean    yesterday's actual load, mean of 24 hours, freq D (ts = that EST date)
  miso.<region>.load.actual_peak    yesterday's actual peak hour, freq D
  miso.<region>.load.actual         yesterday's actual load per hour, freq 1h (PLAN 9: a real, day-late load line)
with <region> in midwest, south, and umw (LRZ1 + LRZ2_7, a sub-group of midwest: the umw map region's zones + ND; not
in the sum-to-MISO check). MISO time is EST all year (``Etc/GMT+5``, AGENTS.md exception, as for power_miso).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import xlrd

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://docs.misoenergy.org/marketreports/{d:%Y%m%d}_df_al.xls"
MISO_TZ = ZoneInfo("Etc/GMT+5")          # EST all year (MISO market time)
REGIONS = {"midwest": ("LRZ1", "LRZ2_7", "LRZ3_5", "LRZ4", "LRZ6"), "south": ("LRZ8_9_10",)}
ZONES = {z for zs in REGIONS.values() for z in zs}
# Sub-groups inside a region, not part of the sum-to-MISO check (PLAN 9 umw): LRZ1 (MN, ND, W. WI) + LRZ2_7 (E. WI +
# the UP, Lower Michigan) = MISO's zones under the umw map region (MN + WI + MI), plus North Dakota.
SUBS = {"umw": ("LRZ1", "LRZ2_7")}
GROUPS = {**REGIONS, **SUBS}
TOLERANCE_MW = 2.0
GROUP = "MISO / load by region"
NAMES = {"midwest": "Midwest MISO (North + Central)", "south": "MISO South",
         "umw": "MISO LRZ1 + LRZ2_7 (MN, WI, MI + ND)"}


def est_date(now_utc: datetime) -> date:
    return now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(MISO_TZ).date()


def hour_start_utc(d: date, he: int) -> datetime:
    t = datetime(d.year, d.month, d.day, tzinfo=MISO_TZ) + timedelta(hours=he - 1)
    return t.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_df_al(content: bytes) -> dict[date, dict[int, dict[str, dict[str, float]]]]:
    """df_al.xls -> {EST date: {hour ending: {"fc"|"act": {region or "miso": MW}}}}; blank actuals are left out."""
    try:
        sh = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    except Exception as e:                                        # noqa: BLE001 - not the report we know
        raise FormatDrift(f"MISO df_al: not a readable .xls ({e})") from e
    cols = None
    out: dict[date, dict[int, dict[str, dict[str, float]]]] = {}
    for r in range(sh.nrows):
        row = [sh.cell_value(r, c) for c in range(sh.ncols)]
        if row[0] == "Market Day" and len(row) > 1 and row[1] == "HourEnding":
            cols = {}
            for c, name in enumerate(row[2:], start=2):
                zone, _, kind = str(name).partition(" ")
                if not zone:
                    continue
                if zone != "MISO" and zone not in ZONES:
                    raise FormatDrift(f"MISO df_al: unknown zone column {name!r} (map it in REGIONS first)")
                cols[c] = (zone, "fc" if kind.startswith("MTLF") else "act" if kind.startswith("ActualLoad") else None)
                if cols[c][1] is None:
                    raise FormatDrift(f"MISO df_al: unknown column {name!r}")
            if {z for z, _ in cols.values()} != ZONES | {"MISO"}:
                raise FormatDrift(f"MISO df_al: zone columns {sorted({z for z, _ in cols.values()})}")
            continue
        if cols is None or not isinstance(row[0], float) or not isinstance(row[1], float):
            continue
        d = xlrd.xldate_as_datetime(row[0], 0).date()
        he = int(row[1])
        vals: dict[str, dict[str, float]] = {"fc": {}, "act": {}}
        for c, (zone, kind) in cols.items():
            if isinstance(row[c], float):
                vals[kind][zone] = row[c]
        cell = {}
        for kind, z in vals.items():
            if not z:
                continue
            if set(z) != ZONES | {"MISO"}:
                raise FormatDrift(f"MISO df_al {d} HE{he}: {kind} has zones {sorted(z)}, not all of them")
            reg = {k: sum(z[x] for x in zs) for k, zs in REGIONS.items()}
            if abs(sum(reg.values()) - z["MISO"]) > TOLERANCE_MW:
                raise FormatDrift(f"MISO df_al {d} HE{he}: regions {sum(reg.values()):,.0f} != MISO {z['MISO']:,.0f}")
            cell[kind] = {**reg, **{k: sum(z[x] for x in zs) for k, zs in SUBS.items()}, "miso": z["MISO"]}
        if cell:
            out.setdefault(d, {})[he] = cell
    if not out:
        raise FormatDrift("MISO df_al: no data rows (NO-RESULT)")
    return out


class MisoRegions(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "live", "tz": "EST (MISO market time)", "source_file": "MISO df_al (Forecasted and Actual Load)"}
        out = []
        for k, n in NAMES.items():
            out += [SeriesMeta(f"miso.{k}.load.forecast", self.id, f"{n} load, MTLF forecast for the hour", "MW", "1h",
                               GROUP, extra={**ex, "zones": list(GROUPS[k])}),
                    SeriesMeta(f"miso.{k}.load.forecast_peak", self.id, f"{n} load, forecast peak for the day", "MW", "D",
                               GROUP, extra={**ex, "zones": list(GROUPS[k])}),
                    SeriesMeta(f"miso.{k}.load.actual_mean", self.id, f"{n} load, actual mean for the day", "MW", "D",
                               GROUP, extra={**ex, "zones": list(GROUPS[k])}),
                    SeriesMeta(f"miso.{k}.load.actual_peak", self.id, f"{n} load, actual peak hour for the day", "MW",
                               "D", GROUP, extra={**ex, "zones": list(GROUPS[k])}),
                    SeriesMeta(f"miso.{k}.load.actual", self.id, f"{n} load, actual hourly (yesterday's 24 hours, "
                               "posted ~01:20 EST the next day)", "MW", "1h", GROUP,
                               extra={**ex, "zones": list(GROUPS[k]), "lag": "~1-2 days"})]
        return out

    def _file(self, ctx: Context, d: date) -> bytes | None:
        c = paths.home() / "cache" / "miso"
        f = c / f"{d:%Y%m%d}_df_al.xls"
        if f.exists():
            return f.read_bytes()
        r = ctx.get(URL.format(d=d), timeout=60, retries=1, allow=(404,))   # counted in fetch_run (L-081)
        if r.status_code == 404:
            return None
        parse_df_al(r.content)                                    # never cache a file that doesn't parse
        c.mkdir(parents=True, exist_ok=True)
        f.write_bytes(r.content)
        for old in c.glob("*_df_al.xls"):
            if old.name < f"{d - timedelta(days=1):%Y%m%d}":      # keep today's and yesterday's
                old.unlink(missing_ok=True)
        return r.content

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        today = est_date(ctx.now)
        content = self._file(ctx, today)
        if content is None:                                       # not published yet (~01:20 EST): yesterday's file
            res.warnings.append(f"MISO df_al for {today} not published yet; using {today - timedelta(days=1)}'s")
            content = self._file(ctx, today - timedelta(days=1))
        if content is None:
            raise FormatDrift(f"MISO df_al: no file for {today} or the day before (NO-RESULT)")
        days = parse_df_al(content)
        for d, hours in sorted(days.items()):
            fc_vals = {round(c["fc"]["miso"], 1) for c in hours.values() if "fc" in c}
            flat = len(fc_vals) == 1 and len(hours) > 1
            if flat:                                              # the report's last day: one constant, a placeholder
                res.warnings.append(f"MISO df_al {d}: the forecast is one value for every hour (placeholder); skipped")
            for he, cell in sorted(hours.items()):
                ts = hour_start_utc(d, he)
                if "act" in cell:
                    for k in GROUPS:
                        res.obs.append(Obs(f"miso.{k}.load.actual", ts, round(cell["act"][k], 1), ts))
                if "fc" in cell and not flat:
                    for k in GROUPS:
                        res.obs.append(Obs(f"miso.{k}.load.forecast", ts, round(cell["fc"][k], 1), ts))
            fc = [c["fc"] for c in hours.values() if "fc" in c]
            act = [c["act"] for c in hours.values() if "act" in c]
            ts = datetime(d.year, d.month, d.day)
            if len(fc) == 24 and d >= today and not flat:
                for k in GROUPS:
                    res.obs.append(Obs(f"miso.{k}.load.forecast_peak", ts, round(max(x[k] for x in fc), 1), ts))
            if len(act) == 24:
                for k in GROUPS:
                    res.obs.append(Obs(f"miso.{k}.load.actual_mean", ts, round(sum(x[k] for x in act) / 24, 1), ts))
                    res.obs.append(Obs(f"miso.{k}.load.actual_peak", ts, round(max(x[k] for x in act), 1), ts))
            elif act:
                res.warnings.append(f"MISO df_al {d}: {len(act)} of 24 actual hours; no actual day values")
        return res
