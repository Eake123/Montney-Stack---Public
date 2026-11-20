"""EIA Weekly Petroleum Status Report (WPSR) by PADD: refinery runs and stocks (REGIONS §5b oil groups `refinery_runs`
and `stocks`).

Four small public CSVs (no key, no API) at https://ir.eia.gov/wpsr/, each the WPSR table as printed: this week, last
week, a year ago, two years ago. 2-8 KB each.

| file        | WPSR table                                              | series (area <a>)                | unit    |
|-------------|---------------------------------------------------------|----------------------------------|---------|
| table4.csv  | 4: Stocks of crude oil by PADD                          | oil.<a>.crude_stocks (commercial, | kbbl    |
|             |   "Commercial (Excluding SPR)", + Cushing               |   excl. SPR; incl. Cushing row)  |         |
|             |   "SPR" (U.S. only)                                     | oil.us.spr_stocks                | kbbl    |
| table2.csv  | 2: Refiner inputs and utilization by PADD               | oil.<a>.refinery_crude_input     | kbbl/d  |
|             |   (Crude Oil Inputs, Gross Inputs, Operable Capacity,   | oil.<a>.refinery_gross_input     | kbbl/d  |
|             |    Percent Utilization)                                 | oil.<a>.refinery_capacity        | kbbl/d  |
|             |                                                         | oil.<a>.utilization              | %       |
| table5a.csv | 5A: Total motor gasoline stocks, with sub-PADDs         | oil.<a>.gasoline_stocks          | kbbl    |
| table6.csv  | 6: Distillate fuel oil stocks, with sub-PADDs           | oil.<a>.distillate_stocks        | kbbl    |

- Areas: us, padd1..padd5; padd1a/1b/1c (New England, Central Atlantic, Lower Atlantic) where EIA splits PADD 1
  (gasoline and distillate stocks only); cushing (crude stocks only). The weekly WPSR has no California / PADD 5
  ex-California split (that's monthly, in the PSM), and refinery data has no sub-PADDs.
- Units as EIA publishes them: inputs and capacity in thousand barrels a day (kbbl/d), utilization in % of operable
  capacity, stocks in thousand barrels (the CSVs print million barrels to 3 decimals, i.e. exact thousands; × 1000).
- ts = the week-ending Friday; as_of = the release date (the CSV's Last-Modified, as an Eastern date). Each release
  carries this week and last week, so last week's revision lands as a new vintage, and a release the runner missed is
  re-delivered by the next one.
- Cadence: EIA releases Wednesday 10:30 ET (Thursday after a federal holiday). The spec runs every 6 h; a run whose
  newest stored week is already the newest one released by the calendar makes NO request. When a release is due, the
  run reads table4.csv first (a conditional GET: ETag / Last-Modified) as the probe; only a new week there pulls the
  other three. A late release is probed again within 4 h of 10:30 ET, then once a day. ~4 requests a week (5-6 in a
  holiday week), never more than 4 a run.
- A row that drops off a table, a header that isn't two consecutive Fridays, or a U.S. total far outside its known range
  (a unit change) fails the run loudly (FormatDrift). "NA" / "W" / "-" cells are skipped, never written as 0.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save
from montney.store import Obs, SeriesMeta

BASE = "https://ir.eia.gov/wpsr/"
ET = ZoneInfo("America/New_York")
GROUP = "EIA weekly petroleum (WPSR) by PADD"
RELEASE = time(10, 30)                       # Wednesday 10:30 ET (Thursday after a holiday)
RETRY_WINDOW = timedelta(hours=4)            # a late file is probed again within 4 h of 10:30 ET, then daily

# area label in the CSV -> area id
AREAS = {"East Coast (PADD 1)": "padd1", "New England (PADD 1A)": "padd1a", "Central Atlantic (PADD 1B)": "padd1b",
         "Lower Atlantic (PADD 1C)": "padd1c", "Midwest (PADD 2)": "padd2", "Cushing": "cushing",
         "Gulf Coast (PADD 3)": "padd3", "Rocky Mountain (PADD 4)": "padd4", "West Coast (PADD 5)": "padd5"}
AREA_LABEL = {"us": "U.S.", "padd1": "PADD 1 East Coast", "padd1a": "PADD 1A New England",
              "padd1b": "PADD 1B Central Atlantic", "padd1c": "PADD 1C Lower Atlantic", "padd2": "PADD 2 Midwest",
              "cushing": "Cushing, OK", "padd3": "PADD 3 Gulf Coast", "padd4": "PADD 4 Rocky Mountain",
              "padd5": "PADD 5 West Coast"}
PADDS = ("us", "padd1", "padd2", "padd3", "padd4", "padd5")
SUB = ("padd1a", "padd1b", "padd1c")


class Item:
    def __init__(self, table, parent, suffix, label, unit, scale, areas, us_range):
        self.table, self.parent, self.suffix, self.label = table, parent, suffix, label
        self.unit, self.scale, self.areas, self.us_range = unit, scale, areas, us_range


# The probe table first. us_range: the U.S. row's plausible band in the STORED unit (a unit change fails loudly).
ITEMS = [
    Item("table4", "Commercial (Excluding SPR)", "crude_stocks", "commercial crude oil stocks (excl. SPR)", "kbbl",
         1000.0, PADDS + ("cushing",), (200_000, 800_000)),
    Item("table4", "SPR", "spr_stocks", "Strategic Petroleum Reserve crude stocks", "kbbl", 1000.0, ("us",),
         (100_000, 800_000)),
    Item("table2", "Crude Oil Inputs", "refinery_crude_input", "refinery crude oil inputs (net input)", "kbbl/d", 1.0,
         PADDS, (8_000, 25_000)),
    Item("table2", "Gross Inputs", "refinery_gross_input", "refinery gross inputs", "kbbl/d", 1.0, PADDS,
         (8_000, 25_000)),
    Item("table2", "Operable Capacity", "refinery_capacity", "refinery operable capacity (calendar day)", "kbbl/d",
         1.0, PADDS, (12_000, 25_000)),
    Item("table2", "Percent Utilization", "utilization", "refinery utilization (% of operable capacity)", "%", 1.0,
         PADDS, (40, 110)),
    Item("table5a", "Total Motor Gasoline", "gasoline_stocks", "total motor gasoline stocks", "kbbl", 1000.0,
         PADDS + SUB, (150_000, 300_000)),
    Item("table6", "Distillate Fuel Oil", "distillate_stocks", "distillate fuel oil stocks", "kbbl", 1000.0,
         PADDS + SUB, (60_000, 200_000)),
]
TABLES = list(dict.fromkeys(i.table for i in ITEMS))      # table4 (the probe), table2, table5a, table6
PROBE = TABLES[0]


def sid(area: str, suffix: str) -> str:
    return f"oil.{area}.{suffix}"


def parse_table(text: str, name: str) -> tuple[list[datetime], dict[tuple[str, str], list[str]]]:
    """A WPSR CSV -> ([this week, last week], {(parent row, area id): [this week's cell, last week's cell]}).

    Rows are nested by order: a row whose label isn't an area starts a parent (its own value is the U.S. figure); the
    area rows under it belong to it. The label is the last STUB_ column (table 2 and 5A put a section in STUB_1)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not rows[0] or rows[0][0] != "STUB_1":
        raise FormatDrift(f"EIA WPSR {name}: header {rows[0][:3] if rows else rows!r}, expected STUB_1 first")
    head = rows[0]
    nstub = sum(1 for h in head if h.startswith("STUB_"))
    try:
        weeks = [datetime.strptime(head[nstub + i].strip(), "%m/%d/%y") for i in (0, 1)]
    except (IndexError, ValueError) as e:
        raise FormatDrift(f"EIA WPSR {name}: week headers {head[nstub:nstub + 2]!r} ({e})") from e
    if weeks[0] - weeks[1] != timedelta(days=7) or weeks[0].weekday() != 4:
        raise FormatDrift(f"EIA WPSR {name}: weeks {weeks[0]:%Y-%m-%d} / {weeks[1]:%Y-%m-%d} aren't two consecutive "
                          "week-ending Fridays")
    out: dict[tuple[str, str], list[str]] = {}
    parent = ""
    for r in rows[1:]:
        if len(r) < nstub + 2:
            continue
        label = r[nstub - 1].strip()
        area = AREAS.get(label)
        if area is None:
            parent, area = label, "us"
        out.setdefault((parent, area), [c.strip() for c in r[nstub:nstub + 2]])
    return weeks, out


def number(cell: str) -> float | None:
    c = cell.replace(",", "")
    return float(c) if re.fullmatch(r"-?\d+(\.\d+)?", c) else None


def released_on(resp, now: datetime) -> datetime:
    """The release date: the file's Last-Modified as an Eastern date (midnight), else today's Eastern date."""
    lm = resp.headers.get("last-modified") if resp is not None else None
    try:
        at = parsedate_to_datetime(lm) if lm else None
    except (TypeError, ValueError):
        at = None
    at = at or now.replace(tzinfo=timezone.utc)
    d = at.astimezone(ET).date()
    return datetime(d.year, d.month, d.day)


def _utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)


def release_slot(week: date) -> datetime:
    """When the WPSR for the week ending Friday `week` is due: the next Wednesday, 10:30 ET (naive UTC)."""
    return _utc(week + timedelta(days=5), RELEASE)


def due_week(now: datetime) -> date:
    """The newest week-ending Friday whose regular Wednesday release time has passed at `now` (naive UTC)."""
    d = now.replace(tzinfo=timezone.utc).astimezone(ET).date() - timedelta(days=5)
    fri = d - timedelta(days=(d.weekday() - 4) % 7)
    return fri if release_slot(fri) <= now else fri - timedelta(days=7)


def next_try(now: datetime) -> datetime:
    """After a probe that found no new week: probe again on the next run while within RETRY_WINDOW of today's 10:30 ET,
    else at the next 10:30 ET (a holiday release on Thursday, or later)."""
    today = now.replace(tzinfo=timezone.utc).astimezone(ET).date()
    anchor = _utc(today, RELEASE)
    if anchor > now:                      # before today's 10:30 ET: yesterday's slot is the reference
        anchor = _utc(today - timedelta(days=1), RELEASE)
    if now < anchor + RETRY_WINDOW:
        return anchor + RETRY_WINDOW
    return _utc(today + timedelta(days=1) if _utc(today, RELEASE) <= now else today, RELEASE)


class OilEiaWpsr(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(sid(a, it.suffix), self.id, f"{AREA_LABEL[a]} {it.label}, EIA weekly (WPSR, week ending "
                           "Friday)", it.unit, "W", GROUP)
                for it in ITEMS for a in it.areas]

    def state_name(self) -> str:
        return f"{self.id}_state"

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        state = cache_load(self.state_name())
        weeks: dict[str, str] = dict(state.get("weeks", {}))
        due = due_week(ctx.now)
        have = min((date.fromisoformat(weeks[t]) for t in TABLES if t in weeks), default=None)
        if len(weeks) == len(TABLES) and have is not None and have >= due:
            return res                                      # current: no request until the next release
        wait = state.get("next_try")
        if wait and len(weeks) == len(TABLES) and ctx.now < datetime.fromisoformat(wait):
            res.warnings.append(f"EIA WPSR for {due} not out at the last probe; next probe after {wait}")
            return res
        for t in TABLES:
            if t != PROBE and t in weeks and date.fromisoformat(weeks.get(PROBE, "1970-01-01")) < due:
                continue                                    # the probe found no new week: leave the rest alone
            r = ctx.get_if_changed(BASE + f"{t}.csv", timeout=60)
            if r is None:                                   # 304: what it says is already stored
                continue
            wk, cells = parse_table(r.text, t)
            as_of = released_on(r, ctx.now)
            res.obs += self.observations(t, wk, cells, as_of)
            weeks[t] = wk[0].date().isoformat()
        state = {"weeks": weeks}
        newest = min(date.fromisoformat(weeks[t]) for t in TABLES) if len(weeks) == len(TABLES) else None
        if newest is None or newest < due:
            state["next_try"] = next_try(ctx.now).isoformat()
            res.warnings.append(f"EIA WPSR for the week ending {due} not out yet (have {newest})")
        cache_save(self.state_name(), state)
        return res

    @staticmethod
    def observations(table: str, weeks: list[datetime], cells: dict, as_of: datetime) -> list[Obs]:
        out = []
        for it in (i for i in ITEMS if i.table == table):
            missing = [a for a in it.areas if (it.parent, a) not in cells]
            if missing:
                raise FormatDrift(f"EIA WPSR {table}: no {it.parent!r} row for {missing}")
            for a in it.areas:
                for wk, cell in zip(weeks, cells[(it.parent, a)]):
                    v = number(cell)
                    if v is None:
                        continue
                    v = round(v * it.scale, 3)
                    lo, hi = it.us_range
                    if a == "us" and not lo <= v <= hi:
                        raise FormatDrift(f"EIA WPSR {table}: U.S. {it.parent} = {v:g} {it.unit} on {wk:%Y-%m-%d}, "
                                          f"outside {lo:g}-{hi:g} (a unit change?)")
                    out.append(Obs(sid(a, it.suffix), wk, v, max(as_of, wk)))
        return out
