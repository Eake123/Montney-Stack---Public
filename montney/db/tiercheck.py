"""The spec-contract cross-check (docs/db/RETENTION.md "Spec contract"; L-037 (2)).

A series' declared tier (`retention:`) must fit its freq and its as_of mode, because the tier decides how its
rows are kept (60 d raw + rollups, 2 y, forever, or run vintages thinned and dropped at 1 y 7 d):

| tier       | freq                      | as_of                                  |
|------------|---------------------------|----------------------------------------|
| `fast`     | sub-hourly (`5min` ...)   | not `fetch`                            |
| `hourly`   | `1h`                      | not `fetch`                            |
| `daily`    | `D`, `W`, `M`, `Q`, `Y`   | not `fetch`                            |
| `forecast` | any                       | not `ts` (runs need a vintage per run) |
| `live`     | any                       | any, and only with `mode: live`        |

`mode: live` requires every series to be `live`. An unknown or missing freq fails closed (it can't be checked).
Python connectors declare series ids and freqs (`declared_series()`) but not an as_of mode, so only their
freqs are checked. Their run-time series are resolved per write by the runner (L-036).
"""

from __future__ import annotations

import re

from montney.db.backfill import RetentionConflict, spec_tier

SUB_HOURLY = re.compile(r"^\d+(s|min)$")
HOURLY = {"1h", "60min", "h", "H"}
DAILY_PLUS = re.compile(r"^\d*(D|W|M|MS|Q|QS|Y|A)$")


def freq_class(freq: str) -> str | None:
    if not freq:
        return None
    if freq in HOURLY:          # before SUB_HOURLY: "60min" is hourly
        return "hourly"
    if SUB_HOURLY.match(freq):
        return "sub_hourly"
    if DAILY_PLUS.match(freq):
        return "daily_plus"
    return None


ALLOWED_FREQ = {"fast": {"sub_hourly"}, "hourly": {"hourly"}, "daily": {"daily_plus"},
                "forecast": {"sub_hourly", "hourly", "daily_plus"}, "live": {"sub_hourly", "hourly", "daily_plus"}}


def _as_of_mode(a) -> str | None:
    if a in ("ts", "fetch"):
        return a
    if isinstance(a, dict):
        return "report"            # a published report time (e.g. GDSR asofDay): real revision vintages
    return None


def declares_retention(spec: dict) -> bool:
    return spec.get("retention") is not None or any(
        "retention" in b for b in spec.get("extract", []) or [] if isinstance(b, dict))


def _series(spec: dict):
    """(series id or None for data-keyed blocks, freq, as_of mode, block) for every series a spec declares."""
    out = []
    if spec.get("type", "http") == "http" or spec.get("extract"):
        for block in spec.get("extract", []) or []:
            mode = _as_of_mode(block.get("as_of"))
            if block.get("key_field"):          # ids depend on the data: check the block as a whole
                out.append((None, block.get("freq", ""), mode, block))
                continue
            for key, f in (block.get("fields") or {}).items():
                f = f if isinstance(f, dict) else {"field": f}
                sid = f.get("id", block.get("id", "{key}")).replace("{key}", key)
                out.append((sid, f.get("freq", block.get("freq", "")), _as_of_mode(f.get("as_of", block.get("as_of"))), block))
    else:
        from montney.connectors import build

        try:
            metas = build(spec).declared_series()
        except Exception as e:  # noqa: BLE001 - a spec that can't even build is a problem, reported
            return [("<build>", "", None, {"_error": f"{type(e).__name__}: {e}"})]
        out += [(m.series_id, m.freq, None, None) for m in metas]
    for d in spec.get("derive", []) or []:
        out.append((d["id"], d.get("freq", ""), None, None))
    return out


def problems(spec: dict) -> list[str]:
    """Every way `spec`'s declared tiers contradict its series' freq / as_of. Empty = consistent."""
    if not declares_retention(spec):
        return []
    sid_, live = spec.get("id", "?"), spec.get("mode") == "live"
    out = []
    for sid, freq, mode, block in _series(spec):
        if block and "_error" in block:
            out.append(f"{sid_}: can't build to list its series: {block['_error']}")
            continue
        where = f"{sid_}:{sid or '<keyed block>'}"
        try:
            if sid is None:
                tier = block.get("retention") or (spec.get("retention") if isinstance(spec.get("retention"), str) else None)
                if tier is None:
                    continue                    # a pattern map can't be resolved without ids: the runner does it
            else:
                tier = spec_tier(sid, spec)
        except RetentionConflict as e:
            out.append(f"{where}: {e}")
            continue
        if tier is None:
            out.append(f"{where}: no tier resolved although the spec declares retention:")
            continue
        if tier not in ALLOWED_FREQ:
            continue                            # events/grid: not an observation tier, checked elsewhere
        if live != (tier == "live"):
            out.append(f"{where}: tier {tier!r} with mode {spec.get('mode', 'cached')!r} (live <=> mode: live)")
        fc = freq_class(freq)
        if fc is None:
            out.append(f"{where}: freq {freq!r} unknown or missing: can't check tier {tier!r}")
        elif fc not in ALLOWED_FREQ[tier]:
            out.append(f"{where}: tier {tier!r} doesn't fit freq {freq!r}")
        if mode == "fetch" and tier not in ("forecast", "live"):
            out.append(f"{where}: as_of: fetch keeps every run, so it must be 'forecast', not {tier!r}")
        if mode == "ts" and tier == "forecast":
            out.append(f"{where}: 'forecast' needs a vintage per run, but as_of: ts gives one per point")
    return out
