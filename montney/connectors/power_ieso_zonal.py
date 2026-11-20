"""Ontario (IESO) hourly demand by zone (L-106, PLAN 8c sont + mbnwo): the split between the two Ontario regions.

Source (keyless): https://reports-public.ieso.ca/public/DemandZonal/PUB_DemandZonal_<YYYY>.csv, the year to date
(updated daily, ~0.55 MB; ETag/Last-Modified), one request a day; each past year from ``from_year`` is final and cached
in <home>/cache/ieso after one download. Columns: Date, Hour (hour ending, EST), Ontario Demand, then the ten zones
(Northwest, Northeast, Ottawa, East, Toronto, Essa, Bruce, Southwest, Niagara, West), Zone Total, Diff.

The regions: ``mbnwo`` holds Ontario north and west of the Sault Ste. Marie - Moosonee line = IESO's **Northwest +
Northeast** zones; ``sont`` the other eight. IESO publishes no generation or intertie split by zone.

Time: IESO's EST all year (``power_ieso.hour_start_utc``, fixed Etc/GMT+5). The file is about a day behind (yesterday's
24 hours land the next morning).

Series (MW, freq 1h): ieso.zone.<zone>.demand for the ten zones, ieso.zone.north.demand (Northwest + Northeast: the
mbnwo detail box, network/power/ontario_north.geojson), and ieso.demand.ontario (IESO's "Ontario Demand").
"Zone Total" - "Ontario Demand" is IESO's own "Diff" (zonal metering includes some embedded/loss terms): not stored.
Fail closed (FormatDrift): a header without the ten zones, a non-numeric value, or an hour outside 1-24.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.connectors.power_ieso import hour_start_utc, ieso_date
from montney.store import Obs, SeriesMeta

BASE = "https://reports-public.ieso.ca/public/DemandZonal/PUB_DemandZonal_{year}.csv"
ZONES = ("Northwest", "Northeast", "Ottawa", "East", "Toronto", "Essa", "Bruce", "Southwest", "Niagara", "West")
NORTH = ("northwest", "northeast")                 # mbnwo's Ontario part
GROUP = "IESO / Ontario demand by zone"
MAX_MW = 40_000


def parse(text: str) -> list[tuple[date, int, dict[str, float]]]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    h = next((i for i, r in enumerate(rows) if r[:2] == ["Date", "Hour"]), None)
    if h is None:
        raise FormatDrift("IESO DemandZonal: no Date,Hour header")
    head = [c.strip() for c in rows[h]]
    missing = [z for z in ZONES + ("Ontario Demand",) if z not in head]
    if missing:
        raise FormatDrift(f"IESO DemandZonal: missing columns {missing}")
    ix = {c: head.index(c) for c in ZONES + ("Ontario Demand",)}
    out = []
    for r in rows[h + 1:]:
        if len(r) < len(head) or not re.match(r"\d{4}-\d{2}-\d{2}$", r[0].strip()):
            continue
        he = int(r[1])
        if not 1 <= he <= 24:
            raise FormatDrift(f"IESO DemandZonal {r[0]}: hour {he}")
        try:
            vals = {c: float(r[i]) for c, i in ix.items()}
        except ValueError:
            raise FormatDrift(f"IESO DemandZonal {r[0]} HE{he}: a value that isn't a number") from None
        if not all(0 <= v <= MAX_MW for v in vals.values()):
            raise FormatDrift(f"IESO DemandZonal {r[0]} HE{he}: value out of range")
        out.append((date.fromisoformat(r[0].strip()), he, vals))
    if not out:
        raise FormatDrift("IESO DemandZonal: no rows (NO-RESULT)")
    return out


class IesoZonal(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"tier": "hourly", "tz": "EST (IESO market time)", "lag": "~1 day"}
        return ([SeriesMeta(f"ieso.zone.{z.lower()}.demand", self.id, f"IESO {z} zone demand", "MW", "1h", GROUP,
                            extra={**ex, "region": "mbnwo" if z.lower() in NORTH else "sont"}) for z in ZONES]
                + [SeriesMeta("ieso.zone.north.demand", self.id, "IESO Northwest + Northeast zone demand (mbnwo's "
                              "Ontario part; inside sont's all-Ontario lines)", "MW", "1h", GROUP,
                              extra={**ex, "region": "mbnwo"}),
                   SeriesMeta("ieso.demand.ontario", self.id, "IESO Ontario Demand (hourly)", "MW", "1h", GROUP,
                              extra=ex)])

    def _year(self, ctx: Context, year: int, this_year: int) -> str:
        url = BASE.format(year=year)
        if year == this_year:
            return ctx.get(url, timeout=120).text
        cache = paths.home() / "cache" / "ieso" / f"PUB_DemandZonal_{year}.csv"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        text = ctx.get(url, timeout=120).text
        parse(text)                                               # never cache a file that doesn't parse
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        this_year = ieso_date(ctx.now).year
        first = int(self.spec.get("params", {}).get("from_year", this_year))
        for year in range(first, this_year + 1):
            for d, he, vals in parse(self._year(ctx, year, this_year)):
                ts = hour_start_utc(d, he)
                for z in ZONES:
                    res.obs.append(Obs(f"ieso.zone.{z.lower()}.demand", ts, vals[z], ts))
                res.obs.append(Obs("ieso.zone.north.demand", ts, vals["Northwest"] + vals["Northeast"], ts))
                res.obs.append(Obs("ieso.demand.ontario", ts, vals["Ontario Demand"], ts))
        return res
