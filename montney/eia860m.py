"""EIA-860M, the monthly generator inventory: shared by the plant inventory (network/power/build_plants.py) and the
installed-capacity connector (montney/connectors/power_capacity_us.py).

* ``index(html)``: the monthly files listed on EIA's 860M page, {first of the month: absolute URL}. EIA keeps the next
  months' links in an HTML comment before they're released, so comments are stripped first.
* ``operating(content)``: the "Operating" sheet's generators, one dict per row keyed by EIA's header, read by streaming
  the sheet's XML (stdlib only: a file is ~14 MB zipped and ~56 MB of sheet XML, so no whole-sheet tree).
* ``EIA_TECH``: EIA's Technology -> (sprite fuel, FUELS group of montney/regions.py).
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import urljoin

INDEX_URL = "https://www.eia.gov/electricity/data/eia860m/"
EIA860M_URL = "https://www.eia.gov/electricity/data/eia860m/xls/{month}_generator{year}.xlsx"
EIA_CITE = ("EIA-860M Preliminary Monthly Electric Generator Inventory, {month} {year} (U.S. Energy Information "
            "Administration, public domain), https://www.eia.gov/electricity/data/eia860m/")

# EIA-860M Technology -> (sprite fuel, FUELS group). Gas plants in a CHP sector become cogeneration (build_plants).
EIA_TECH = {
    "Natural Gas Fired Combined Cycle": ("combined_cycle", "gas"),
    "Natural Gas Fired Combustion Turbine": ("simple_cycle", "gas"),
    "Natural Gas Internal Combustion Engine": ("simple_cycle", "gas"),
    "Natural Gas Steam Turbine": ("gas_fired_steam", "gas"),
    "Other Natural Gas": ("gas", "gas"), "Other Gases": ("gas", "gas"),
    "Natural Gas with Compressed Air Storage": ("other", "other"),
    "Conventional Steam Coal": ("coal", "coal"), "Coal Integrated Gasification Combined Cycle": ("coal", "coal"),
    "Petroleum Coke": ("oil", "oil"), "Petroleum Liquids": ("oil", "oil"),
    "Nuclear": ("nuclear", "nuclear"),
    "Conventional Hydroelectric": ("hydro", "hydro"),
    "Hydroelectric Pumped Storage": ("pumped_storage", "pumped_storage"),
    "Onshore Wind Turbine": ("wind", "wind"), "Offshore Wind Turbine": ("wind", "wind"),
    "Solar Photovoltaic": ("solar", "solar"), "Solar Thermal without Energy Storage": ("solar", "solar"),
    "Solar Thermal with Energy Storage": ("solar", "solar"),
    "Batteries": ("battery", "battery"), "Flywheels": ("battery", "battery"),
    "Wood/Wood Waste Biomass": ("biomass", "biomass"), "Landfill Gas": ("biomass", "biomass"),
    "Municipal Solid Waste": ("biomass", "biomass"), "Other Waste Biomass": ("biomass", "biomass"),
    "Geothermal": ("geothermal", "geothermal"), "All Other": ("other", "other"),
}
STATUSES = ("(OP)", "(SB)")          # operating, standby/backup: what the inventory and the capacity count

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_HREF = re.compile(r'href="(?P<href>[^"]*/(?P<m>[a-z]+)_generator(?P<y>\d{4})\.xlsx)"', re.I)
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class Eia860mError(ValueError):
    """The file or page no longer looks like EIA-860M (a connector re-raises it as FormatDrift)."""


def index(html: str, base: str = INDEX_URL) -> dict[datetime, str]:
    """{first of the month: absolute xlsx URL} for every monthly generator file the page links (comments stripped:
    EIA parks the coming months' links in an HTML comment)."""
    out: dict[datetime, str] = {}
    for m in _HREF.finditer(re.sub(r"<!--.*?-->", "", html, flags=re.S)):
        name = m.group("m").lower()
        if name in _MONTHS:
            out[datetime(int(m.group("y")), _MONTHS.index(name) + 1, 1)] = urljoin(base, m.group("href"))
    return out


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _sheet_path(z: zipfile.ZipFile, name: str) -> str:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    for sh in wb.iter(f"{_NS}sheet"):
        if sh.get("name") == name:
            t = rels[sh.get(_REL)]
            return t.lstrip("/") if t.startswith("/") else "xl/" + t
    raise Eia860mError(f"EIA-860M: no {name!r} sheet")


def operating(content: bytes) -> Iterator[dict[str, str]]:
    """Every generator row of the "Operating" sheet as {header: text} (header = the row holding "Plant ID"; cells as
    their text, numbers unparsed). Rows before the header and rows without a Plant ID (the notes) are skipped."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
                if el.tag == f"{_NS}si":
                    shared.append("".join(t.text or "" for t in el.iter(f"{_NS}t")))
                    el.clear()
        sheet = z.open(_sheet_path(z, "Operating"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise Eia860mError(f"EIA-860M: not a readable xlsx ({e})") from None
    header: dict[int, str] | None = None
    for _, el in ET.iterparse(sheet):
        if el.tag != f"{_NS}row":
            continue
        cells: dict[int, str] = {}
        for c in el.iter(f"{_NS}c"):
            t, v = c.get("t"), c.find(f"{_NS}v")
            if t == "inlineStr":
                cells[_col(c.get("r"))] = "".join(x.text or "" for x in c.iter(f"{_NS}t"))
            elif v is not None:
                cells[_col(c.get("r"))] = shared[int(v.text)] if t == "s" else (v.text or "")
        el.clear()
        if header is None:
            if "Plant ID" in cells.values():
                header = {i: h.strip() for i, h in cells.items() if h and h.strip()}
            continue
        row = {h: cells.get(i, "").strip() for i, h in header.items()}
        if row.get("Plant ID"):
            yield row
    if header is None:
        raise Eia860mError("EIA-860M: no header row with 'Plant ID' on the Operating sheet")


def as_of(content: bytes) -> datetime | None:
    """The month in the Operating sheet's title ("Inventory of Operating Generators as of September 2026"), or None."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
            if el.tag == f"{_NS}si":
                text = "".join(t.text or "" for t in el.iter(f"{_NS}t"))
                m = re.search(r"Operating Generators as of ([A-Za-z]+) (\d{4})", text)
                if m and m.group(1).lower() in _MONTHS:
                    return datetime(int(m.group(2)), _MONTHS.index(m.group(1).lower()) + 1, 1)
                el.clear()
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return None
    return None


def mw(text: str) -> float | None:
    """A capacity cell: a number, or None when blank (EIA writes ' ' for blanks)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise Eia860mError(f"EIA-860M: capacity {text!r} isn't a number") from None
