"""The frozen vocabularies (docs/db/ERD.md "Frozen vocabularies", L-033). The single source.

The migrations are plain SQL, so they can't import this module. Instead, `sql_mismatches()` parses the
CHECK lists out of migrations/*.sql, and tests/test_db_vocab.py fails if they differ from these tuples.
Code that validates entities, aliases, links or events (runner, importers) imports from here.
Changing a list means a numbered migration plus a LOG entry, and this file in the same PR.
"""

from __future__ import annotations

import re

KEY_RE = r"^[a-z0-9_]+(\.[a-z0-9_]+)+$"   # entity.key and series.key (L-033): no hyphens

ENTITY_TYPES = (
    "station", "segment", "area", "facility", "operator", "well", "pipeline", "terminal",
    "ship", "gen_unit", "plant", "dam", "gauge", "river", "reach",
)

ALIAS_SYSTEMS = (
    "tc_station", "petrinex_facility", "bcer_facility", "petrinex_ba", "tc_segment",
    "tc_dop_area", "uwi", "bc_wa", "well_licence", "imo", "mmsi", "ppa_berth",
    "aeso_asset", "ghgrp", "npri", "bch_code", "usace", "wsc_station", "ab_station",
    "usgs_site", "nhn",
)

LINK_KINDS = (
    "delivers_to", "member_of", "operated_by", "generates_at", "measured_by",
    "on_reach", "flows_into", "derived_from",
)

EVENT_KINDS = ("outage", "bulletin", "notice", "alert", "cargo")

# (table, column) -> the frozen list its CHECK must equal
CHECKS = {
    ("entity", "type"): ENTITY_TYPES,
    ("entity_alias", "system"): ALIAS_SYSTEMS,
    ("entity_link", "kind"): LINK_KINDS,
    ("event", "kind"): EVENT_KINDS,
}


def sql_check_lists(sql: str) -> dict[tuple[str, str], tuple[str, ...]]:
    """Extract `<column> ... CHECK (<column> IN ('a', 'b', ...))` lists per CREATE TABLE montney.<table>."""
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for t in re.finditer(r"CREATE TABLE montney\.(\w+)\s*\((.*?)\n\);", sql, re.S):
        table, body = t[1], t[2]
        for c in re.finditer(r"CHECK\s*\(\s*(\w+)\s+IN\s*\((.*?)\)\s*\)", body, re.S):
            out[(table, c[1])] = tuple(re.findall(r"'([^']*)'", c[2]))
    return out


def sql_mismatches(sql: str) -> list[str]:
    """Differences between the CHECK lists in `sql` and CHECKS. Fails closed: a missing list is reported."""
    found = sql_check_lists(sql)
    problems = []
    for key, want in CHECKS.items():
        got = found.get(key)
        if got is None:
            problems.append(f"{key[0]}.{key[1]}: no CHECK list found")
        elif set(got) != set(want) or len(got) != len(want):
            problems.append(f"{key[0]}.{key[1]}: SQL has {sorted(set(got) ^ set(want))} differing "
                            f"(or duplicates); vocab.py is the source")
    if KEY_RE not in sql:
        problems.append(f"key grammar {KEY_RE} not found in the SQL")
    return problems
