"""Every source spec in sources/ must load, build, and declare sane series."""

import re

import pytest

from montney.connectors import build, load_specs, parse_interval

SPECS = load_specs(include_disabled=True)
ID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


@pytest.mark.parametrize("sid", sorted(SPECS))
def test_spec_builds(sid):
    spec = SPECS[sid]
    assert re.fullmatch(r"[a-z0-9_]+", sid), "source ids are lowercase [a-z0-9_]"
    assert parse_interval(spec.get("interval", "1h")) >= 60, "don't poll faster than once a minute"
    conn = build(spec)
    for m in conn.declared_series():
        assert ID_RE.match(m.series_id), f"bad series id {m.series_id!r}"
        assert m.unit is not None


def test_no_inline_secrets():
    for sid, spec in SPECS.items():
        text = repr({k: v for k, v in spec.items() if k != "_file"})
        assert not re.search(r"(api[-_]?key|token|password)['\"]?\s*:\s*['\"][A-Za-z0-9]{12,}", text, re.I), sid


# --- retention: (PLAN P3, LOG L-035 Q-P3 and L-036; tiers per docs/db/RETENTION.md "Spec contract") ---
# Database's tests cross-check each tier against the series' freq/as_of; this checks the spec shape only.
from fnmatch import fnmatchcase

TIERS = {"fast", "hourly", "daily", "forecast", "live", "events", "grid"}

# Specs that don't declare `retention:` yet. This list may only SHRINK: a new spec without retention fails, and a spec
# on this list that has gained retention fails until it's removed from here. Owners add the key to their own specs.
# It must be EMPTY before the Postgres default flips (L-037). The overlap check below uses declared_series(); series a
# python connector emits without declaring them are caught by the fail-closed write-time tier resolution (#74).
RETENTION_PENDING: set[str] = set()


def retention_problems(sid: str, spec: dict, series_ids: list[str] | None = None) -> list[str]:
    """Every way `spec`'s retention declaration breaks L-035/L-036. Empty list = valid."""
    out = []
    r = spec.get("retention")
    python = spec.get("type") == "python"
    if r is None:
        if sid not in RETENTION_PENDING:
            out.append(f"{sid}: no `retention:` (and not in RETENTION_PENDING)")
    elif sid in RETENTION_PENDING:
        out.append(f"{sid}: declares `retention:`, so remove it from RETENTION_PENDING")
    if isinstance(r, str):
        if r not in TIERS:
            out.append(f"{sid}: unknown tier {r!r}")
    elif isinstance(r, dict):
        if not python:
            out.append(f"{sid}: a pattern map is only allowed on type: python specs (L-036)")
        if r.get("default") not in TIERS:
            out.append(f"{sid}: a pattern map needs `default:` set to a known tier (L-036)")
        pats = {k: v for k, v in r.items() if k != "default"}
        for k, v in pats.items():
            if not isinstance(k, str) or not k.strip():
                out.append(f"{sid}: empty pattern key")
            if v not in TIERS:
                out.append(f"{sid}: pattern {k!r} → unknown tier {v!r}")
        for s in series_ids or []:
            hits = [k for k in pats if fnmatchcase(s, k)]
            if len(hits) > 1:
                out.append(f"{sid}: series {s} matches more than one pattern {hits} (L-036: no overlap)")
    elif r is not None:
        out.append(f"{sid}: `retention:` must be a tier name or (python only) a pattern map")
    for i, block in enumerate(spec.get("extract") or []):
        if isinstance(block, dict) and "retention" in block:
            if python:
                out.append(f"{sid}: extract[{i}] block override on a python spec")
            elif block["retention"] not in TIERS:
                out.append(f"{sid}: extract[{i}] unknown tier {block['retention']!r}")
    return out


@pytest.mark.parametrize("sid", sorted(SPECS))
def test_spec_retention(sid):
    spec = SPECS[sid]
    ids = [m.series_id for m in build(spec).declared_series()]
    assert retention_problems(sid, spec, ids) == []


def test_retention_pending_names_real_specs():
    assert RETENTION_PENDING <= set(SPECS), sorted(RETENTION_PENDING - set(SPECS))


# Seeded reds: each broken declaration must be caught (Wellsite §4.1).
@pytest.mark.parametrize("spec,series,needle", [
    ({"type": "http", "retention": "weekly"}, None, "unknown tier"),
    ({"type": "http", "retention": {"default": "fast"}}, None, "only allowed on type: python"),
    ({"type": "python", "retention": {"a.*": "fast"}}, None, "needs `default:`"),
    ({"type": "python", "retention": {"default": "fast", "a.*": "weekly"}}, None, "unknown tier"),
    ({"type": "python", "retention": {"default": "fast", "a.*": "fast", "*.x": "daily"}}, ["a.x"], "more than one pattern"),
    ({"type": "http", "retention": "hourly", "extract": [{"retention": "monthly"}]}, None, "extract[0] unknown tier"),
    ({"type": "python", "retention": "fast", "extract": [{"retention": "daily"}]}, None, "block override on a python spec"),
    ({"type": "http"}, None, "no `retention:`"),
    ({"type": "http", "retention": ["fast"]}, None, "must be a tier name"),
])
def test_retention_problems_catch_bad_specs(spec, series, needle):
    problems = retention_problems("seeded_red_spec", spec, series)
    assert any(needle in p for p in problems), problems


def test_retention_pending_must_shrink(monkeypatch):
    # A synthetic pending id, so this red keeps firing as the real list empties (L-037).
    monkeypatch.setitem(globals(), "RETENTION_PENDING", RETENTION_PENDING | {"seeded_red_pending_spec"})
    assert any("remove it from RETENTION_PENDING" in p
               for p in retention_problems("seeded_red_pending_spec", {"type": "http", "retention": "fast"}))
