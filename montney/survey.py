"""Survey-grid locations → approximate lat/lon: Alberta/BC DLS (incl. BC's Peace River Block) and BC NTS.

Moved here from network/gas/build_stations.py, which re-exports them, so the runtime (montney/top_producers.py) can
place facilities too.

The plain grid arithmetic ignores road allowances, so it drifts south with the township number. It was fitted against
260 AER ST102 NOVA meter stations at an "LSD Centre" in W6 (townships 50-99): lat +0.0521 km per township, lon
−0.753 km. Residual after the fit: p50 0.38 km, p90 0.74 km. Check: CGL Wilde Lake 04-33-078-19W6 lands 0.3 km from
BCER's point (tests/test_gas_stations.py). The fit is from W6; W4/W5 points (most of Alberta) are within a few km,
which is plenty for a facility dot.
"""

from __future__ import annotations

import math
import re

DLS_LAT_KM_PER_TWP = 0.0521
DLS_LON_KM = -0.753


def dls(s: str, calibrated: bool = True) -> tuple[float, float] | None:
    """LSD-SEC-TWP-RGE W<mer> (DLS, incl. BC's Peace River Block) → (lat, lon) of the LSD centre, calibrated (see above)."""
    m = re.match(r"^(?:\d{2}-)?(\d{1,2})-(\d{1,2})-(\d{3})-(\d{1,2})W(\d)$", s.strip())
    if not m:
        return None
    lsd, sec, twp, rge, mer = map(int, m.groups())
    if mer not in (4, 5, 6) or not (1 <= lsd <= 16 and 1 <= sec <= 36):
        return None
    r, c = (sec - 1) // 6, (sec - 1) % 6
    c = c if r % 2 == 0 else 5 - c
    lr, lc = (lsd - 1) // 4, (lsd - 1) % 4
    lc = lc if lr % 2 == 0 else 3 - lc
    lat = 49 + ((twp - 1) * 6 + r + (lr + 0.5) / 4) * 1.609344 / 111.2
    lon = {4: -110, 5: -114, 6: -118}[mer] - ((rge - 1) * 6 + c + (lc + 0.5) / 4) * 1.609344 / (111.32 * math.cos(math.radians(lat)))
    if calibrated:
        lat += DLS_LAT_KM_PER_TWP * twp / 111.2
        lon += DLS_LON_KM / (111.32 * math.cos(math.radians(lat)))
    return lat, lon


def _snake(idx: int, ncol: int) -> tuple[int, int]:
    r, c = idx // ncol, idx % ncol
    return r, (c if r % 2 == 0 else ncol - 1 - c)


def nts(s: str) -> tuple[float, float] | None:
    """BC NTS grid unit 'A-005-B/103-I-02' → (lat, lon) of the quarter-unit centre. Approx ±1 km."""
    m = re.match(r"^([A-D])-(\d{3})-([A-L])/(\d{3})-([A-P])-(\d{2})$", s.strip())
    if not m:
        return None
    q, unit, blk, ser, let, sh = m.groups()
    base = {82: (49, -112), 83: (53, -112), 92: (48, -120), 93: (52, -120), 94: (56, -120),
            102: (48, -128), 103: (52, -128), 104: (56, -128)}.get(int(ser))
    if not base or not (1 <= int(sh) <= 16 and 1 <= int(unit) <= 100):
        return None
    r, c = _snake("ABCDEFGHIJKLMNOP".index(let), 4)
    lat, lon = base[0] + r, base[1] - 2 * c
    r, c = _snake(int(sh) - 1, 4)
    lat, lon = lat + r * 0.25, lon - c * 0.5
    r, c = _snake("ABCDEFGHIJKL".index(blk), 4)
    lat, lon = lat + r * 5 / 60, lon - c * 7.5 / 60
    r, c = _snake(int(unit) - 1, 10)
    lat, lon = lat + r * 0.5 / 60, lon - c * 0.75 / 60
    dq = {"A": (0, 0), "B": (0, 1), "C": (1, 1), "D": (1, 0)}[q]
    return lat + (dq[0] + 0.5) * 0.25 / 60, lon - (dq[1] + 0.5) * 0.375 / 60


def locate(s: str) -> tuple[float, float] | None:
    """DLS or NTS, whichever the string is."""
    return dls(s) or nts(s)
