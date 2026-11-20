"""CER key-point throughput and capacity: TC Mainline, Foothills, Westcoast, Alliance (PLAN step 5, PR B).

Source: the CER's open data "Pipeline Throughput and Capacity" (open.canada.ca dataset dc343c43-…, OGL-Canada), one CSV
per pipeline:
  https://www.cer-rec.gc.ca/open/energy/throughput-capacity/<pipeline>-throughput-and-capacity.csv
Columns: Date, Key Point, Direction Of Flow, Trade Type, Capacity (1000 m3/d), Throughput (1000 m3/d), …: **daily values,
released quarterly** (Group 1 companies, Guide BB). On 2026-11-02 the latest day was 2026-06-30, about 4 months behind.

So these are context, not live: the map draws them as capacity and in panels. The live flow on a line, where one exists,
comes from NGTL's GDSR/CSR (Empress, McNeill, the AB-BC border) or Alliance's own postings.

Series (MMcf/d = 10³m³/d × 0.0353147; freq D, ts = the day; as_of = the fetch day, a report vintage):
  cer.<pipeline>.<key point>.throughput / .capacity      for the KEYPOINTS below
  cer.<pipeline>.<key point>.throughput_q                freq Q, ts = the quarter's first day: the quarter's mean
                                                         (complete quarters only, ≥ 90 % of days), labelled
                                                         "quarter avg (CER)": the dated context the map shows
Only days since KEEP_FROM are kept (the CSVs go back to 2006; ~20 MB for the Mainline).

Fails loudly (FormatDrift) if a CSV lacks the expected columns, or a listed key point is missing from its file.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import E3M3_TO_MMCF, FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://www.cer-rec.gc.ca/open/energy/throughput-capacity/{}-throughput-and-capacity.csv"
GROUP = "CER key points (quarterly, ~4 months behind)"
COLUMNS = {"Date", "Key Point", "Direction Of Flow", "Capacity (1000 m3/d)", "Throughput (1000 m3/d)"}
KEEP_FROM = "2024-01-01"
# pipeline file → {series key: (label, [(CER key point, direction), …] summed)}
KEYPOINTS = {
    "tcpl-mainline": {
        "mainline.prairies": ("TC Mainline, Prairies (east of Empress)", [("Prairies", "east")]),
        "mainline.emerson": ("TC Mainline, Emerson exports (I + II)", [("Emerson I", "south"), ("Emerson II", "south")]),
        "mainline.nol": ("TC Mainline, Northern Ontario Line (east of Winnipeg)", [("Northern Ontario Line", "east")]),
    },
    "foothills": {
        "foothills.kingsgate": ("Foothills BC, Kingsgate export", [("Kingsgate", "south")]),
        "foothills.monchy": ("Foothills SK, Monchy export", [("Monchy", "southeast")]),
    },
    "westcoast": {
        "westcoast.huntingdon_export": ("Westcoast, Huntingdon export (to NWP)", [("Huntingdon Export", "south")]),
        "westcoast.fortisbc_lm": ("Westcoast, FortisBC Lower Mainland deliveries", [("FortisBC Lower Mainland", "west")]),
        "westcoast.kingsvale": ("Westcoast, Kingsvale (to FortisBC Southern Crossing)", [("Kingsvale", "west")]),
        "westcoast.sunset_creek_east": ("Westcoast, Sunset Creek east (to NGTL)", [("Sunset Creek", "east")]),
        "westcoast.gordondale_east": ("Westcoast, NOVA/Gordondale east (to NGTL)", [("NOVA/Gordondale", "east")]),
    },
    "alliance": {
        "alliance.border": ("Alliance, US border export", [("Border", "southeast")]),
        "alliance.zone2": ("Alliance, Zone 2 key point", [("Zone 2", "southeast")]),
    },
}


def parse(text: str, points: dict[str, tuple[str, list[tuple[str, str]]]]) -> dict[str, dict[datetime, list[float]]]:
    """{series key: {day: [throughput, capacity]}} in MMcf/d, summed over the key point/direction pairs."""
    rdr = csv.DictReader(io.StringIO(text))
    if not COLUMNS <= set(rdr.fieldnames or []):
        raise FormatDrift(f"CER throughput CSV: expected columns {sorted(COLUMNS)}")
    want = {(kp, d): key for key, (_, pairs) in points.items() for kp, d in pairs}
    out: dict[str, dict[datetime, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    seen = set()
    for r in rdr:
        key = want.get((r["Key Point"].strip(), r["Direction Of Flow"].strip()))
        if key is None or r["Date"] < KEEP_FROM:
            continue
        seen.add((r["Key Point"].strip(), r["Direction Of Flow"].strip()))
        num = lambda k: float(r[k]) if r[k].strip() else 0.0   # noqa: E731
        v = out[key][datetime.strptime(r["Date"][:10], "%Y-%m-%d")]
        v[0] += num("Throughput (1000 m3/d)") * E3M3_TO_MMCF
        v[1] += num("Capacity (1000 m3/d)") * E3M3_TO_MMCF
    missing = set(want) - seen
    if missing:
        raise FormatDrift(f"CER throughput CSV: key points missing since {KEEP_FROM}: {sorted(missing)}")
    return out


def quarter_means(by_day: dict[datetime, list[float]]) -> dict[datetime, float]:
    """{quarter start: mean daily throughput} for quarters with ≥ 90 % of their days."""
    acc: dict[datetime, list[float]] = defaultdict(list)
    for t, (thr, _) in by_day.items():
        acc[datetime(t.year, 3 * ((t.month - 1) // 3) + 1, 1)].append(thr)
    out = {}
    for q, vals in acc.items():
        end = datetime(q.year + (q.month == 10), (q.month + 2) % 12 + 1, 1)
        if len(vals) >= 0.9 * (end - q).days:
            out[q] = sum(vals) / len(vals)
    return out


class GasCerKeypoints(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for points in KEYPOINTS.values():
            for key, (label, _) in points.items():
                out += [SeriesMeta(f"cer.{key}.throughput", self.id, f"{label}, throughput (CER, ~4 months behind)",
                                   "MMcf/d", "D", GROUP),
                        SeriesMeta(f"cer.{key}.capacity", self.id, f"{label}, capacity (CER)", "MMcf/d", "D", GROUP),
                        SeriesMeta(f"cer.{key}.throughput_q", self.id, f"{label}, quarter avg (CER)", "MMcf/d", "Q",
                                   GROUP)]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        base = self.spec.get("params", {}).get("base", BASE)
        as_of = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        res = FetchResult(meta=self.declared_series())
        for pipe, points in KEYPOINTS.items():
            r = ctx.get(base.format(pipe), timeout=600)
            text = r.content.decode("utf-8-sig", errors="replace")
            if not text.lstrip().startswith("Date"):            # served as application/octet-stream; an HTML page isn't
                raise FormatDrift(f"CER {pipe}: not the CSV ({r.headers.get('content-type')!r}, {text[:40]!r})")
            data = parse(text, points)
            for key, by_day in data.items():
                for ts, (thr, cap) in by_day.items():
                    res.obs += [Obs(f"cer.{key}.throughput", ts, thr, as_of), Obs(f"cer.{key}.capacity", ts, cap, as_of)]
                for q, mean in quarter_means(by_day).items():
                    res.obs.append(Obs(f"cer.{key}.throughput_q", q, mean, as_of))
            last = max((max(d) for d in data.values()), default=None)
            res.warnings.append(f"CER {pipe}: {len(data)} series, latest day {last:%Y-%m-%d}" if last else f"CER {pipe}: empty")
        return res
