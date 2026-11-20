"""Where Alberta sales gas goes: each Petrinex facility attributed to its nearest pipeline meter station (L-080 item 4).

Petrinex masks the target of every sales disposition (`FromToID = "***"`, a pipeline meter station), and meter stations
don't report in the public volumetrics. The facility list does give every meter station's location and the pipeline it
belongs to (`MeterStationPipelineLink` → that pipeline facility's name). So each sales facility is assigned to the
**nearest active meter station** (survey-grid locations, montney/survey.py) and that station's pipeline system.

Checks (docs/research/gas-residual.md, Aug 2025-Sep 2026):
- Systems with no station in TC's NGTL receipt data (Gas Alberta, AEC Suffield, Suncor Albersun, AltaGas Cold Lake,
  ICG, ...) are off NGTL by construction.
- 30 of the ~190 ATCO Pipelines stations that facilities use **are** in TC's NGTL receipts (the integrated Alberta
  System counts them), so they go to `atco_in_ngtl`, not off-system. Detected from the stored TC station receipts
  (`ngtl.station.<n>.receipt`, number = the meter station id's digits).
- The off-system total tracks the measured-basis AB gap: 1,017 vs 1,035 MMcf/d in Aug 2026.

It's an attribution, not a meter. A plant near two systems may ship to the farther one: the unambiguous share (no
NGTL station within 2 km) is ~60 % of the off-system total.
"""

from __future__ import annotations

import csv
import math

from montney.survey import dls

# pipeline system (substring of the meter station's pipeline name, upper case) → group; first match wins
GROUP_RULES = (("NOVA", "ngtl"), ("ALLIANCE", "alliance"), ("ATCO PIPELINE", "atco_pipelines"),
               ("GAS ALBERTA", "gas_alberta"), ("SUFFIELD", "tc_mainline_suffield"), ("ALBERSUN", "suncor_albersun"),
               ("COLD LAKE", "altagas_cold_lake"), ("BRAZEAU", "pioneer_brazeau"))
OFF_GROUPS = ("gas_alberta", "atco_pipelines", "tc_mainline_suffield", "pioneer_brazeau", "suncor_albersun",
              "altagas_cold_lake", "other_systems")
ALL_GROUPS = ("ngtl", "alliance", "atco_in_ngtl") + OFF_GROUPS + ("unattributed",)
GROUP_LABEL = {
    "ngtl": "NGTL meter stations", "alliance": "Alliance meter stations",
    "atco_in_ngtl": "ATCO Pipelines stations that TC counts as NGTL receipts",
    "gas_alberta": "Gas Alberta (rural gas co-ops)", "atco_pipelines": "ATCO Pipelines (stations outside NGTL's receipts)",
    "tc_mainline_suffield": "AEC Suffield pipeline → TC Mainline at Suffield 2 (an export path that bypasses NGTL)",
    "pioneer_brazeau": "Pioneer Brazeau pipeline", "suncor_albersun": "Suncor Albersun pipeline (oil sands)",
    "altagas_cold_lake": "AltaGas Cold Lake system", "other_systems": "other systems (ICG, Medicine Hat, Montana, ...)",
    "unattributed": "no meter station within reach (or no location)"}
MAX_KM = 40.0
ACTIVE = ("ACTIVE", "NEW")


def meter_stations(stream) -> list[tuple[str, str, float, float]]:
    """[(meter station id, its pipeline's name, lat, lon)] for active meter stations with a DLS location."""
    rows = list(csv.DictReader(stream))
    name = {r["FacilityID"].strip(): r["FacilityName"].strip() for r in rows}
    out = []
    for r in rows:
        if r["FacilityType"].strip() != "MS" or r["FacilityOperationalStatus"].strip() not in ACTIVE:
            continue
        p = dls(r["FacilityLocation"]) if r.get("FacilityLocation") else None
        link = r.get("MeterStationPipelineLink", "").strip()
        if p:
            out.append((r["FacilityID"].strip(), name.get(link, link), p[0], p[1]))
    return out


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371.0 * math.hypot(x, math.radians(b[0] - a[0]))


def nearest(locations: dict[str, str], ms: list[tuple[str, str, float, float]]) -> dict[str, tuple[str, str, float]]:
    """{facility: (meter station id, pipeline name, km)} for the nearest active station within MAX_KM."""
    out = {}
    for fid, loc in locations.items():
        p = dls(loc) if loc else None
        if not p:
            continue
        best = min(((_km(p, (lat, lon)), mid, pipe) for mid, pipe, lat, lon in ms), default=None)
        if best and best[0] <= MAX_KM:
            out[fid] = (best[1], best[2], best[0])
    return out


def group_of(pipe: str, ms_id: str, tc_counted: set[int]) -> str:
    up = pipe.upper()
    for key, g in GROUP_RULES:
        if key in up:
            if g == "atco_pipelines" and station_number(ms_id) in tc_counted:
                return "atco_in_ngtl"
            return g
    return "other_systems"


def station_number(ms_id: str) -> int | None:
    digits = ms_id[4:]
    return int(digits) if digits.isdigit() else None
