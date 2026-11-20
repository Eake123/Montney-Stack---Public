"""NGTL delivery stations that feed the GHGRP-validated gas plants (PLAN 5b, DECISIONS P5b rows 39-40, L-031, L-033).

Source: TC Customer Express "Station Percent Utilization – Delivery", https://www.tccustomerexpress.com/2883.html,
one file per year. The current year's file is year-to-date and is replaced monthly, about 20 days after month end.
Gas's 5a connector reads the receipt files. This one reads only the delivery stations configured in the spec, and
reuses Gas's sheet readers (`montney.connectors.ngtl_station_util`: read_sheet, _month, _num, parse_as_of), without
changing them.

Units (L-031). Deliveries are published as ENERGY: GJ in the month = %/100 x Demand + IT, where TC's "%" is the firm
allocation divided by the eligible monthly quantity ("Demand"). They are stored as **MMcf/d**:

    MMcf/d = GJ / HV (GJ/Mcf) / days in the month / 1000

* **HV** is the month's NGTL system-average *delivery* heat value. L-031 left the exact source for Gas to name at 5a,
  and 5a doesn't name one. TC's /heatValues only gives static per-area defaults, and 2881.html gives border values.
  So until a source is ruled, every month uses **L-031's flagged fallback, 39.0 MJ/m3 = 1.104 GJ/Mcf**.
* The HV actually used is stored as its own monthly series, `ngtl.heat_value.delivery` (GJ/Mcf), so the conversion
  can be reversed and audited. Its meta says `fallback: true`.
* Guard (L-032): an HV outside 37.5-39.8 MJ/m3 fails the fetch. The receipt "rd" value (~40.9) must never be used.

Series (freq M, ts = first of the month, naive; as_of = the file's "As of" stamp, a report vintage -> daily tier):
  ngtl.station.<tc_number>.delivery    MMcf/d, the station number as published (L-033: Gas's id scheme)
  ngtl.heat_value.delivery             GJ/Mcf, the HV used for that month
Plant sums are derives in sources/power_ngtl_deliveries.yaml: power.plant.<slug>.ngtl_delivery (MMcf/d).

Fail closed: a non-delivery file, month headers that don't match the link year, or missing %/Demand/IT columns
raise FormatDrift (nothing stored). A configured station missing from a file is a warning, and that plant's
derived sum then gets no value for it (derives need every input).
"""

from __future__ import annotations

import calendar
import html
import re
from datetime import date, datetime
from urllib.parse import urljoin

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors.gas_common import FormatDrift
from montney.connectors.ngtl_station_util import ANCHOR_RE, PAGE, _month, _num, parse_as_of, read_sheet
from montney.store import Obs, SeriesMeta

GROUP = "NGTL / Station deliveries (power plants)"
TEXT_RE = re.compile(r"^(?P<year>\d{4})\s+NGTL\s+Station\s+Percent\s+Utilization\s*[-–—]\s*Delivery$", re.I)
HV_FALLBACK_MJ_M3 = 39.0                     # L-031 fallback, flagged
HV_RANGE_MJ_M3 = (37.5, 39.8)                # L-032 range guard; the receipt rd value (~40.9) must fail
MJ_M3_TO_GJ_MCF = 0.0283168466               # 1 Mcf = 28.3168466 m3; MJ/m3 x m3/Mcf / 1000 = GJ/Mcf
STATION_MAX_MMCFD = 600.0                    # no single plant delivery station comes close (Shepard ~130)


def series_id(station: int) -> str:
    return f"ngtl.station.{station}.delivery"


HV_SERIES = "ngtl.heat_value.delivery"


def delivery_links(page_html: str, base: str = PAGE) -> dict[int, str]:
    """{year: absolute url} for every "<YYYY> NGTL Station Percent Utilization - Delivery" link."""
    out: dict[int, str] = {}
    for a in ANCHOR_RE.finditer(page_html):
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", a.group("text")))).strip()
        m = TEXT_RE.match(text)
        if not m:
            continue
        year = int(m.group("year"))
        if year in out:
            raise FormatDrift(f"two delivery links for {year} on the utilization page")
        out[year] = urljoin(base, html.unescape(a.group("href")))
    return out


def parse_deliveries(rows: list[list], year: int, stations: set[int]) -> tuple[list[dict], datetime]:
    """Rows of one delivery file -> ([{station, name, month, gj}], as_of), for the configured stations only."""
    title = " ".join(str(c) for r in rows[:2] for c in r if c).lower()
    if "delivery" not in title:
        raise FormatDrift("not a delivery utilization file (no 'delivery' in its title)")
    hi = next((i for i, r in enumerate(rows[:40]) if any(_month(c) for c in r)), None)
    if hi is None:
        raise FormatDrift("no month header row (e.g. 'Jan 2026') in the first 40 rows")
    hdr, sub = rows[hi], rows[hi + 1] if hi + 1 < len(rows) else []
    mcols = [(j, _month(c)) for j, c in enumerate(hdr) if _month(c)]
    if {m.year for _, m in mcols} != {year}:
        raise FormatDrift(f"month headers {sorted({m.year for _, m in mcols})} don't match the link year {year}")
    spans = []
    for k, (j, m) in enumerate(mcols):
        end = mcols[k + 1][0] if k + 1 < len(mcols) else len(sub)
        pos = {str(sub[x]).strip(): x for x in range(j, min(end, len(sub))) if sub[x] not in (None, "")}
        if not {"%", "Demand", "IT"} <= set(pos):
            raise FormatDrift(f"{m:%b %Y}: expected %/Demand/IT columns, found {sorted(pos)}")
        spans.append((m, pos["%"], pos["Demand"], pos["IT"]))
    as_of = parse_as_of(rows)
    out = []
    for r in rows[hi + 2:]:
        st = _num(r[0]) if r else None
        if st is None or st != int(st) or int(st) not in stations:
            continue
        for m, a, b, c in spans:
            vals = [_num(r[x]) if x < len(r) else None for x in (a, b, c)]
            if all(v is None for v in vals):
                continue
            pct, dem, it = (v or 0.0 for v in vals)
            out.append(dict(station=int(st), name=str(r[1]).strip() if len(r) > 1 and r[1] else "", month=m,
                            gj=pct / 100.0 * dem + it))
    return out, as_of


def gj_to_mmcfd(gj: float, month: date, hv_gj_mcf: float) -> float:
    return gj / hv_gj_mcf / calendar.monthrange(month.year, month.month)[1] / 1000.0


def _month_end(m: date) -> datetime:
    return datetime(m.year + (m.month == 12), m.month % 12 + 1, 1)


class NgtlDeliveries(Connector):
    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        page = p.get("page", PAGE)
        stations = {int(k): v for k, v in (p.get("stations") or {}).items()}
        if not stations:
            raise FormatDrift("no delivery stations configured (params.stations)")
        hv_mj = float(p.get("hv_mj_m3", HV_FALLBACK_MJ_M3))
        if not HV_RANGE_MJ_M3[0] <= hv_mj <= HV_RANGE_MJ_M3[1]:
            raise FormatDrift(f"delivery heat value {hv_mj} MJ/m3 outside {HV_RANGE_MJ_M3} (L-032; the receipt rd "
                              f"value ~40.9 is not a delivery heat value)")
        hv = hv_mj * MJ_M3_TO_GJ_MCF
        links = delivery_links(ctx.get(page).text, page)
        if not links:
            raise FormatDrift("no delivery links found on the utilization page")
        this_year = local_today().year
        if max(links) < this_year - 1:
            raise FormatDrift(f"newest delivery file is {max(links)}; expected {this_year - 1} or {this_year}")
        first = int(p["from_year"]) if p.get("from_year") else this_year - int(p.get("years_back", 1))
        res = FetchResult()
        names: dict[int, str] = {}
        for year in sorted(y for y in links if y >= first):
            url = links[year]
            recs, as_of = parse_deliveries(read_sheet(url, ctx.get(url).content), year, set(stations))
            missing = set(stations) - {x["station"] for x in recs}
            if missing:
                res.warnings.append(f"{year}: configured delivery stations {sorted(missing)} not in the file")
            months = set()
            for x in recs:
                if _month_end(x["month"]) > as_of:          # a month not over when the file was cut: not data
                    continue
                v = gj_to_mmcfd(x["gj"], x["month"], hv)
                if not 0.0 <= v <= STATION_MAX_MMCFD:
                    raise FormatDrift(f"{x['station']} {x['month']:%b %Y}: {v:,.1f} MMcf/d outside 0-{STATION_MAX_MMCFD}")
                ts = datetime(x["month"].year, x["month"].month, 1)
                res.obs.append(Obs(series_id(x["station"]), ts, v, as_of))
                months.add((ts, as_of))
                names[x["station"]] = x["name"]
            for ts, as_of_m in sorted(months):
                res.obs.append(Obs(HV_SERIES, ts, round(hv, 6), as_of_m))
        res.warnings.append(f"heat value: L-031 fallback {hv_mj} MJ/m3 = {hv:.4f} GJ/Mcf for every month (flagged; "
                            f"the system-average delivery heat value source is not ruled yet)")
        res.meta = [SeriesMeta(series_id(st), self.id, f"NGTL {names.get(st, '').title()} ({st}) deliveries", "MMcf/d",
                               "M", GROUP, extra={"station": st, "name": names.get(st, ""), "plant": stations[st],
                                                  "hv": "fallback 39.0 MJ/m3 (L-031)"})
                    for st in sorted(stations)]
        res.meta.append(SeriesMeta(HV_SERIES, self.id, "NGTL delivery heat value used for the conversion", "GJ/Mcf", "M",
                                   GROUP, extra={"fallback": True, "mj_m3": hv_mj, "source": "L-031 fallback"}))
        return res
