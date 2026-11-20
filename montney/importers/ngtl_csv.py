"""Import NGTL Gas Day Summary history from a TC Customer Express CSV export.

Writes the same series ids as the live `ngtl_gdsr` source, so history and live data
join seamlessly. The CSV stays on your machine; only the DuckDB file (in your
MONTNEY_HOME, never the repo) receives the numbers.

Handles these layouts (auto-detected):
  blocks  - repeated single-day reports:  Item,Prorated,Extrapolated,NextDayNominated
            ... rows ...,  Gas Day,<date>,<date>,<date+1>
  long    - one row per (gas day, item):  Gas Day,Item,Prorated,Extrapolated,...
  wide    - one row per gas day, one column per item (measure optional in name)
  matrix  - one row per item, one column per gas day

Run with --inspect first to see what was detected without importing.
"""

from __future__ import annotations

import csv
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from montney import units
from montney.connectors.base import to_float
from montney.store import Obs

# normalized-substring -> series key. Order matters: specific before generic.
ITEM_KEYS: list[tuple[str, str]] = [
    ("totalexportdeliveries", "total_export"),
    ("totalstorageintraprovincial", "storage_plus_intra"),
    ("storageintraprovincial", "storage_plus_intra"),
    ("netstorage", "net_storage"),
    ("storagedeliveries", "storage_deliveries"),
    ("storagereceipts", "storage_receipts"),
    ("totalngtldeliveries", "total_deliveries"),
    ("totaldeliveries", "total_deliveries"),
    ("totalngtlreceipts", "total_receipts"),
    ("totalreceipts", "total_receipts"),
    ("fieldreceipts", "field_receipts"),
    ("endofdaylinepack", "eod_linepack"),
    ("eodlinepack", "eod_linepack"),
    ("linepackrateofchange", "linepack_roc"),
    ("linepackchange", "linepack_change"),
    ("linepacktarget", "linepack_target"),
    ("sdaccount", "sd_account"),
    ("obaaccount", "oba_account"),
    ("empress", "empress"),
    ("mcneil", "mcneill"),
    ("albertabc", "alberta_bc"),
    ("abbc", "alberta_bc"),
    ("willowvalley", "willow_valley"),
    ("gordondale", "gordondale"),
    ("groundbirch", "groundbirch_east"),
    ("otherborder", "other_borders"),
    ("intraprovincial", "intraprovincial"),
]
ABS_KEYS = {"storage_deliveries", "empress", "mcneill", "alberta_bc", "willow_valley",
            "other_borders", "total_export", "intraprovincial", "storage_plus_intra",
            "total_deliveries"}
VOLUME_KEYS = {"eod_linepack", "linepack_roc", "linepack_change", "linepack_target"}  # not /d
TJ_KEYS = {"sd_account", "oba_account"}
NO_MEASURE_KEYS = {"linepack_change"}  # stored without a .flow suffix (matches ngtl_gdsr)

MEASURES: list[tuple[str, str]] = [
    ("prorated", "prorated"),
    ("nextday", "nom"), ("800nom", "nom"), ("8am", "nom"), ("nominat", "nom"), ("nom", "nom"),
    ("extrapolat", "flow"), ("volume", "flow"), ("actual", "flow"), ("flow", "flow"),
]
DATE_FORMATS = ["%Y-%b-%d", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y", "%b %d, %Y",
                "%d-%b-%y", "%Y%m%d", "%B %d, %Y", "%m/%d/%y"]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def item_key(label: str) -> str | None:
    n = norm(label)
    if not n:
        return None
    for needle, key in ITEM_KEYS:
        if n == needle:
            return key
    for needle, key in ITEM_KEYS:
        if needle in n:
            return key
    return None


def measure_of(label: str) -> str | None:
    n = norm(label)
    for needle, m in MEASURES:
        if needle in n:
            return m
    return None


def parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    s = re.split(r"[ T](?=\d{1,2}:\d{2})", s)[0].strip()  # drop a time part
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
            return datetime(d.year, d.month, d.day)
        except ValueError:
            continue
    return None


@dataclass
class Parsed:
    layout: str = "?"
    # (key, measure, gas_day) -> raw value in file units
    values: dict[tuple[str, str, datetime], float] = field(default_factory=dict)
    unknown_items: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)

    def put(self, key: str, measure: str, day: datetime, raw: str) -> None:
        v = to_float(raw)
        if v is not None:
            self.values[(key, measure, day)] = v


def read_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.excel
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        pass
    return [[c.strip() for c in r] for r in csv.reader(text.splitlines(), dialect) if any(c.strip() for c in r)]


def parse(path: Path) -> Parsed:
    rows = read_rows(path)
    p = Parsed()
    if not rows:
        p.notes.append("file is empty")
        return p
    first_cells = [norm(r[0]) for r in rows]
    if "item" in first_cells and any(c.startswith("gasday") for c in first_cells):
        _parse_blocks(rows, p)
        return p
    # find a header row: first row with >= 2 non-numeric cells
    hi = next((i for i, r in enumerate(rows[:20]) if sum(to_float(c) is None for c in r) >= 2), 0)
    header = rows[hi]
    body = rows[hi + 1:]
    hn = [norm(h) for h in header]
    date_col = next((i for i, h in enumerate(hn) if h in ("gasday", "date", "gasdate", "day", "flowdate")
                     or h.startswith("gasday")), None)
    item_col = next((i for i, h in enumerate(hn) if h in ("item", "location", "point", "description",
                                                          "name", "category")), None)
    if date_col is not None and item_col is not None:
        _parse_long(header, body, date_col, item_col, p)
    elif date_col is not None:
        _parse_wide(header, body, date_col, p)
    elif sum(parse_date(h) is not None for h in header[1:]) >= 1:
        _parse_matrix(header, body, p)
    else:
        p.notes.append(f"could not detect layout; header = {header[:12]}")
    return p


def _parse_blocks(rows: list[list[str]], p: Parsed) -> None:
    p.layout = "blocks"
    header: list[str] | None = None
    items: list[list[str]] = []
    dates: list[datetime | None] | None = None

    def flush():
        if header and items and dates:
            meas = [measure_of(h) for h in header]
            for r in items:
                key = item_key(r[0])
                if not key:
                    p.unknown_items[r[0]] += 1
                    continue
                for ci in range(1, len(r)):
                    if ci < len(meas) and meas[ci] and ci < len(dates) and dates[ci]:
                        p.put(key, meas[ci], dates[ci], r[ci])

    for r in rows:
        c0 = norm(r[0])
        if c0 == "item":
            flush()
            header, items, dates = r, [], None
        elif c0.startswith("gasday"):
            dates = [None] + [parse_date(c) for c in r[1:]]
            if items:
                flush()
                items = []
        elif header is not None:
            items.append(r)
    flush()


def _parse_long(header, body, date_col, item_col, p: Parsed) -> None:
    p.layout = "long"
    meas = [measure_of(h) if i not in (date_col, item_col) else None for i, h in enumerate(header)]
    if not any(meas):  # a single "Value" column
        meas = [("flow" if i not in (date_col, item_col) and norm(h) in ("value", "volume", "quantity")
                 else None) for i, h in enumerate(header)]
    for r in body:
        if len(r) <= max(date_col, item_col):
            continue
        d = parse_date(r[date_col])
        key = item_key(r[item_col])
        if d is None:
            continue
        if not key:
            p.unknown_items[r[item_col]] += 1
            continue
        for ci, m in enumerate(meas):
            if m and ci < len(r):
                p.put(key, m, d + timedelta(days=1) if m == "nom" else d, r[ci])
    p.notes.append("long layout: nominations shifted to gas day + 1 (next-day noms)")


def _parse_wide(header, body, date_col, p: Parsed) -> None:
    p.layout = "wide"
    cols = []
    for i, h in enumerate(header):
        if i == date_col:
            continue
        key = item_key(h)
        if not key:
            if h:
                p.unknown_items[h] += 1
            continue
        cols.append((i, key, measure_of(h) or "flow"))
    for r in body:
        if len(r) <= date_col:
            continue
        d = parse_date(r[date_col])
        if d is None:
            continue
        for ci, key, m in cols:
            if ci < len(r):
                p.put(key, m, d + timedelta(days=1) if m == "nom" else d, r[ci])


def _parse_matrix(header, body, p: Parsed) -> None:
    p.layout = "matrix"
    dates = [None] + [parse_date(h) for h in header[1:]]
    for r in body:
        key = item_key(r[0])
        if not key:
            p.unknown_items[r[0]] += 1
            continue
        for ci in range(1, len(r)):
            if ci < len(dates) and dates[ci]:
                p.put(key, "flow", dates[ci], r[ci])


def guess_units(p: Parsed, path: Path) -> str:
    """Return the file's volume unit: MMcf, e3m3 or e6m3."""
    if "imperial" in path.name.lower():
        return "MMcf"
    vals = [abs(v) for (k, m, _), v in p.values.items() if k == "total_receipts" and m == "flow"]
    if not vals:
        vals = [abs(v) for (k, _, _), v in p.values.items() if k not in TJ_KEYS | VOLUME_KEYS]
    if not vals:
        return "MMcf"
    med = statistics.median(vals)
    if med >= 100_000:
        return "e3m3"
    if med <= 2_000 and any(k == "total_receipts" for (k, _, _) in p.values):
        return "e6m3"
    return "MMcf"


def to_observations(p: Parsed, file_unit: str) -> list[Obs]:
    out = []
    for (key, m, day), raw in p.values.items():
        if key in TJ_KEYS:
            v = raw
        elif key in VOLUME_KEYS:
            v = units.convert(raw, file_unit, "MMcf")
        else:
            v = units.convert(raw, f"{file_unit}/d", "MMcf/d")
        if key in ABS_KEYS:
            v = abs(v)
        if key in NO_MEASURE_KEYS:
            if m != "flow":
                continue
            sid = f"ngtl.gdsr.{key}"
        else:
            sid = f"ngtl.gdsr.{key}.{m}"
        # Vintage just after the gas day, so any later live fetch supersedes it.
        out.append(Obs(sid, day, v, day + timedelta(days=1)))
    return out


def describe(p: Parsed, file_unit: str) -> str:
    days = sorted({d for (_, _, d) in p.values})
    keys = Counter(k for (k, _, _) in p.values)
    meas = Counter(m for (_, m, _) in p.values)
    lines = [
        f"layout:        {p.layout}",
        f"file units:    {file_unit}  (converted to MMcf/d, linepack to MMcf)",
        f"gas days:      {len(days)}" + (f"  ({days[0]:%Y-%m-%d} .. {days[-1]:%Y-%m-%d})" if days else ""),
        f"values:        {len(p.values)}",
        f"measures:      {dict(meas)}",
        f"items mapped:  {', '.join(sorted(keys))}",
    ]
    if p.unknown_items:
        lines.append("unmapped rows/cols (ignored): " + ", ".join(
            f"{k!r}" for k, _ in p.unknown_items.most_common(15)))
    lines += [f"note: {n}" for n in p.notes]
    return "\n".join(lines)
