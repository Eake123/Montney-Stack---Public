"""US state gas and oil, monthly, plus EIA's weekly storage regions (L-106 8b, REGIONS §8 "US state gas and oil").

EIA's dnav "summary" pages put every state on ONE page per measure (the last six months), so the whole US costs nine
public pages, ~35-45 KB each, every 3 days (`gas_eia_states`), plus one weekly page a day (`gas_eia_storage_weekly`).
No key, no API.

| page (www.eia.gov/dnav/…)                    | measure (EIA's)                               | series (per state <st>)            | unit   |
|----------------------------------------------|-----------------------------------------------|------------------------------------|--------|
| ng/ng_prod_sum_a_EPG0_VGM_mmcf_m.htm         | Natural Gas Marketed Production               | eia.<st>.marketed_production       | MMcf/d |
| ng/ng_cons_sum_a_EPG0_VGT_mmcf_m.htm         | Natural Gas Delivered to Consumers (N3060)    | eia.<st>.deliveries                | MMcf/d |
| ng/ng_cons_sum_a_EPG0_VRS_mmcf_m.htm         | Residential Consumption (N3010)               | eia.<st>.deliveries.residential    | MMcf/d |
| ng/ng_cons_sum_a_EPG0_VCS_mmcf_m.htm         | Deliveries to Commercial Consumers (N3020)    | eia.<st>.deliveries.commercial     | MMcf/d |
| ng/ng_cons_sum_a_EPG0_VIN_mmcf_m.htm         | Industrial Consumption (N3035)                | eia.<st>.deliveries.industrial     | MMcf/d |
| ng/ng_cons_sum_a_EPG0_VEU_mmcf_m.htm         | Deliveries to Electric Power Consumers (N3045)| eia.<st>.deliveries.electric       | MMcf/d |
| ng/ng_stor_sum_a_EPG0_SAO_mmcf_m.htm         | Working Gas in Underground Storage (N5020)    | eia.<st>.storage.working_gas       | Bcf    |
| ng/ng_stor_sum_a_EPG0_SAN_mmcf_m.htm         | Net Withdrawals from Underground Storage      | eia.<st>.storage.net_withdrawal    | MMcf/d |
| pet/pet_crd_crpdn_adc_mbblpd_m.htm           | Crude Oil Production (thousand b/d)           | eia.<st>.crude_production          | bbl/d  |
| ng/ng_stor_wkly_s1_w.htm (weekly)            | Weekly Working Gas in Underground Storage     | eia.storage.<region>.working_gas   | Bcf    |
|                                              |  (Lower 48, East, Midwest, Mountain, Pacific, | eia.storage.<region>.net_injection | MMcf/d |
|                                              |   South Central)                              |                                    |        |

- Monthly volumes (MMcf a month) are monthly means per day. Net withdrawal is + withdrawal / − injection, EIA's sign.
  Working gas is month end. ts = the month's first day (weekly: the week-ending Friday); as_of = the page's Release
  Date (a report vintage).
- Weekly net injection = (this week − last week) Bcf × 1000 ÷ 7: + injection, − withdrawal (REGIONS §3's storage sign).
- "Delivered to consumers" is residential + commercial + industrial + electric power + vehicle fuel. It excludes lease,
  plant and pipeline fuel. Marketed production includes the lease and plant fuel later burned, so a region's
  production − deliveries leaves those fuels in its balance residual.
- States: STATES, every member of the 9 US regions (docs/REGIONS.md §1) plus Illinois (Chicago). A state EIA doesn't
  list on a page (no gas production in Washington, Minnesota, Wisconsin or New England; no storage in most of the
  Sunbelt) gets no series: absent means none, never a written 0. A state that has had a series and drops off the page
  fails the run loudly (FormatDrift, MUST_LIST).
- "NA" and "W" (withheld) cells are skipped.
- This supersedes gas_eia_midwest's five single-state pages and gas_eia_mist's Oregon page (same EIA measures, same
  series ids: eia.il/wi/mi.deliveries, eia.nd.marketed_production, eia.mi.storage.working_gas,
  eia.or.storage.working_gas), so the whole US now costs fewer requests than the Midwest did.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

DNAV = "https://www.eia.gov/dnav/"
GROUP = "EIA state gas and oil (monthly)"
WGROUP = "EIA weekly storage regions"
STATES = {"WA": "Washington", "OR": "Oregon", "ID": "Idaho", "CA": "California", "MN": "Minnesota",
          "WI": "Wisconsin", "MI": "Michigan", "IL": "Illinois", "MT": "Montana", "ND": "North Dakota",
          "WY": "Wyoming", "UT": "Utah", "CO": "Colorado", "NV": "Nevada", "AZ": "Arizona", "NM": "New Mexico",
          "TX": "Texas", "NY": "New York", "ME": "Maine", "NH": "New Hampshire", "VT": "Vermont",
          "MA": "Massachusetts", "RI": "Rhode Island", "CT": "Connecticut"}
BY_NAME = {v: k for k, v in STATES.items()}
# page key -> (path, series suffix, label, unit); convert() turns the page's number into the unit
PAGES = {
    "production": ("ng/ng_prod_sum_a_EPG0_VGM_mmcf_m.htm", "marketed_production", "marketed gas production", "MMcf/d"),
    "deliveries": ("ng/ng_cons_sum_a_EPG0_VGT_mmcf_m.htm", "deliveries", "gas delivered to consumers", "MMcf/d"),
    # the end-use split (gas_region_demand's monthly inputs: industrial, and the refit of its weather / heat-rate terms)
    "residential": ("ng/ng_cons_sum_a_EPG0_VRS_mmcf_m.htm", "deliveries.residential", "residential gas consumption",
                    "MMcf/d"),
    "commercial": ("ng/ng_cons_sum_a_EPG0_VCS_mmcf_m.htm", "deliveries.commercial", "gas delivered to commercial "
                   "consumers", "MMcf/d"),
    "industrial": ("ng/ng_cons_sum_a_EPG0_VIN_mmcf_m.htm", "deliveries.industrial", "industrial gas consumption",
                   "MMcf/d"),
    "electric": ("ng/ng_cons_sum_a_EPG0_VEU_mmcf_m.htm", "deliveries.electric", "gas delivered to electric power "
                 "consumers", "MMcf/d"),
    "working": ("ng/ng_stor_sum_a_EPG0_SAO_mmcf_m.htm", "storage.working_gas",
                "underground storage working gas (month end)", "Bcf"),
    "net_withdrawal": ("ng/ng_stor_sum_a_EPG0_SAN_mmcf_m.htm", "storage.net_withdrawal",
                       "storage net withdrawal (+ withdrawal / − injection)", "MMcf/d"),
    "crude": ("pet/pet_crd_crpdn_adc_mbblpd_m.htm", "crude_production", "crude oil production", "bbl/d"),
}
# states that must appear on a page once they're known producers / storers (a dropped row is a format break, not a 0)
MUST_LIST = {"production": {"CA", "CO", "MT", "NM", "ND", "TX", "UT", "WY", "MI", "NY"},
             "deliveries": set(STATES),
             "residential": set(STATES), "commercial": set(STATES), "industrial": set(STATES),
             "electric": set(STATES),
             "working": {"CA", "CO", "MI", "MT", "NY", "OR", "TX", "UT", "WA", "WY"},
             "net_withdrawal": {"CA", "CO", "MI", "MT", "NY", "OR", "TX", "UT", "WA", "WY"},
             "crude": {"CA", "CO", "MT", "NM", "ND", "TX", "UT", "WY"}}
WEEKLY = "ng/ng_stor_wkly_s1_w.htm"
REGION_ROWS = {"Total Lower 48 States": "lower48", "East": "east", "Midwest": "midwest", "Mountain": "mountain",
               "Pacific": "pacific", "South Central": "south_central"}
REGION_LABEL = {"lower48": "Lower 48", "east": "East", "midwest": "Midwest", "mountain": "Mountain",
                "pacific": "Pacific", "south_central": "South Central"}
MON = {m: i for i, m in enumerate(calendar.month_abbr) if m}


def sid(st: str, suffix: str) -> str:
    return f"eia.{st.lower()}.{suffix}"


def summary_table(page: str) -> tuple[list[str], dict[str, list[str]], datetime | None]:
    """A dnav summary page -> (column headers, {row stub: [cell text per column]}, release date)."""
    heads = [re.sub(r"<[^>]+>|&nbsp;", " ", h).strip()
             for h in re.findall(r"<th[^>]*class=['\"]?Series5['\"]?[^>]*>(.*?)</th>", page, re.S)]
    rows: dict[str, list[str]] = {}
    for blk in page.split('<tr class="DataRow">')[1:]:
        stub = re.search(r'class="DataStub1">(.*?)</td>', blk, re.S)
        if not stub:
            continue
        name = re.sub(r"<[^>]+>|&nbsp;", " ", stub.group(1)).strip()
        cells = re.findall(r'<td[^>]*class="(?:DataB|Current2|Current|DataA)"[^>]*>([^<]*)</td>', blk)
        rows.setdefault(name, [c.strip().replace(",", "") for c in cells])
    rel = re.search(r"Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})", page)
    return heads, rows, datetime.strptime(rel.group(1), "%m/%d/%Y") if rel else None


def month_of(head: str) -> datetime:
    """'Aug-26' -> 2026-08-01."""
    m, y = head.split("-")
    return datetime(2000 + int(y), MON[m[:3].title()], 1)


def number(cell: str) -> float | None:
    return float(cell) if re.fullmatch(r"-?\d+(\.\d+)?", cell) else None


def convert(key: str, v: float, month: datetime) -> float:
    days = calendar.monthrange(month.year, month.month)[1]
    if key == "working":
        return v / 1000.0                       # MMcf -> Bcf
    if key == "crude":
        return v * 1000.0                       # thousand b/d -> b/d
    return v / days                             # MMcf a month -> MMcf/d


LABEL = {"eia.il.deliveries": "Illinois gas delivered to consumers (stands in for Chicago-area demand), EIA monthly"}


class GasEiaStates(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(sid(st, suffix), self.id, LABEL.get(sid(st, suffix), f"{name} {label}, EIA monthly"), unit,
                           "M", GROUP)
                for key, (_, suffix, label, unit) in PAGES.items() for st, name in STATES.items()]

    def fetch(self, ctx: Context) -> FetchResult:
        keep = int(self.spec.get("params", {}).get("months", 6))
        res = FetchResult(meta=self.declared_series())
        for key, (path, suffix, _, _) in PAGES.items():
            heads, rows, released = summary_table(ctx.get(DNAV + path, timeout=60).text)
            try:
                months = [month_of(h) for h in heads]
            except (ValueError, KeyError) as e:
                raise FormatDrift(f"EIA {path}: month headers {heads!r} ({e})") from e
            if len(months) < 3 or not rows:
                raise FormatDrift(f"EIA {path}: {len(months)} month columns, {len(rows)} rows")
            listed = {BY_NAME[n] for n in rows if n in BY_NAME}
            missing = MUST_LIST[key] - listed
            if missing:
                raise FormatDrift(f"EIA {path}: no row for {sorted(missing)}")
            for name, cells in rows.items():
                st = BY_NAME.get(name)
                if st is None:
                    continue
                for mo, cell in list(zip(months, cells))[-keep:]:
                    v = number(cell)
                    if v is not None:
                        res.obs.append(Obs(sid(st, suffix), mo, convert(key, v, mo), released or mo))
        return res


class GasEiaStorageWeekly(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for r, nm in REGION_LABEL.items():
            out += [SeriesMeta(f"eia.storage.{r}.working_gas", self.id, f"EIA {nm} working gas in storage (weekly)",
                               "Bcf", "W", WGROUP),
                    SeriesMeta(f"eia.storage.{r}.net_injection", self.id, f"EIA {nm} storage net injection, weekly "
                               "mean (+ injection / − withdrawal)", "MMcf/d", "W", WGROUP)]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        heads, rows, released = summary_table(ctx.get(DNAV + WEEKLY, timeout=60).text)
        try:
            weeks = [datetime.strptime(h, "%m/%d/%y") for h in heads]
        except ValueError as e:
            raise FormatDrift(f"EIA weekly storage: week headers {heads!r} ({e})") from e
        missing = set(REGION_ROWS) - set(rows)
        if len(weeks) < 2 or missing:
            raise FormatDrift(f"EIA weekly storage: {len(weeks)} weeks, missing rows {sorted(missing)}")
        res = FetchResult(meta=self.declared_series())
        for name, r in REGION_ROWS.items():
            vals = [number(c) for c in rows[name]][:len(weeks)]
            for i, (wk, v) in enumerate(zip(weeks, vals)):
                if v is None:
                    continue
                as_of = released if released and released >= wk else wk + timedelta(days=6)
                res.obs.append(Obs(f"eia.storage.{r}.working_gas", wk, v, as_of))
                prev = vals[i - 1] if i else None
                if prev is not None and (wk - weeks[i - 1]).days == 7:
                    res.obs.append(Obs(f"eia.storage.{r}.net_injection", wk, (v - prev) * 1000.0 / 7.0, as_of))
        return res
