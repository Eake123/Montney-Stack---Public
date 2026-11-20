"""Unit conversion for gas volumes and flows.

Units are strings like "e3m3", "MMcf", "Bcf", optionally with a rate suffix "/d".
Conversion is only allowed between the same dimension (volume -> volume, or
volume/d -> volume/d). Anything not in the table (MW, $/MWh, degC, TJ...) only
converts to itself.

Factors are cubic feet per unit. 1 m3 = 35.3146667 ft3 (this matches TC Energy's
own MMcf display to the unit).
"""

from __future__ import annotations

FT3 = {
    "cf": 1.0,
    "Mcf": 1e3,
    "MMcf": 1e6,
    "Bcf": 1e9,
    "Tcf": 1e12,
    "m3": 35.3146667,
    "e3m3": 35.3146667e3,
    "e6m3": 35.3146667e6,
}


def _split(unit: str) -> tuple[str, str]:
    if "/" in unit:
        base, rate = unit.split("/", 1)
        return base, "/" + rate
    return unit, ""


def factor(from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return 1.0
    fb, fr = _split(from_unit)
    tb, tr = _split(to_unit)
    if fr != tr:
        raise ValueError(f"cannot convert {from_unit} -> {to_unit}: rate mismatch")
    if fb not in FT3 or tb not in FT3:
        raise ValueError(f"cannot convert {from_unit} -> {to_unit}: unknown unit")
    return FT3[fb] / FT3[tb]


def convert(value: float, from_unit: str, to_unit: str) -> float:
    return value * factor(from_unit, to_unit)
