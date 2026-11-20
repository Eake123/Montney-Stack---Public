"""Which network features a zoom tier's snapshot carries, and how (L-081, docs/ARCHITECTURE-REQUESTS.md §1-2).

The rule mirrors the map's (web/map.js featureTier/featureShown, web/callouts.js shownAt), read from the same config,
web/map.json, so the server and the client can't drift:

    a feature's minzoom = overrides[id].minzoom ?? tiers[overrides[id].tier ?? callout.priority ?? 2]
    a feature merged into another box (`merge_into`) shows inside that box from the box's minzoom, so it takes
    min(its own, the box's)

The tier→zoom table is web/map.json `"tiers"` ({"0": 0, "1": 6, "2": 6.75, "3": 7.5}); until that lands, DEFAULT_TIERS
(the same numbers as callouts.js TIER_MINZOOM).

A tier-t snapshot carries every feature with a value that may show somewhere in [tiers[t], tiers[t+1]): its minzoom
is below the NEXT tier's zoom (a per-feature minzoom between two tiers lands in the lower one, so a feature is never
missing at a zoom where it shows). Nodes (dots, sprites, smoke) follow their minzoom (web/style.js atLevel); links and
areas are drawn at every zoom, coloured by util, so they're always in. Then:
  hot  = it has a `callout` (a box) shown at this tier: the full entry (v, ts, prev, cap, next, stale, agg, ...);
  lite = otherwise (drawn only: sprite state, spin, smoke, link colour): [v, stale, cap], where cap is
         cap_current ?? cap and None where value_kind is capacity. That is everything paintNetwork reads: util =
         abs(v) / cap.
Click detail for any feature comes from ?features=<ids> (full entries).
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)
_warned: set = set()

DEFAULT_TIERS = [0.0, 6.0, 6.75, 7.5]
DEFAULT_TIER = 2            # a feature without a callout (compressors, plant links...): tier 2 (map.js featureTier)


def tiers(cfg: dict) -> list[float]:
    """The tier -> minzoom list from map.json `tiers`. Only integer keys count (a `_doc` or other note is skipped), and a
    malformed table falls back to DEFAULT_TIERS: a config edit must never turn every scoped snapshot into a 500."""
    t = cfg.get("tiers")
    if not isinstance(t, dict):
        return list(DEFAULT_TIERS)
    try:
        out = [float(t[k]) for k in sorted((k for k in t if str(k).lstrip("-").isdigit()), key=int)]
    except (TypeError, ValueError):
        return list(DEFAULT_TIERS)
    return out or list(DEFAULT_TIERS)


def _num(fid, key: str, v):
    """A map.json number (a numeric string is fine: the map's JS coerces it too), or None for junk, logged once per value.
    A bad override never 500s the snapshot; the caller falls back to the feature's tier, so it's never left out."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    if (fid, key, repr(v)) not in _warned:
        _warned.add((fid, key, repr(v)))
        log.warning("web/map.json: %s %s=%r is not a number; ignored (the tier's minzoom applies)", fid, key, v)
    return None


def minzooms(fc: dict, cfg: dict) -> dict[str, float]:
    T, ov = tiers(cfg), cfg.get("overrides")
    ov = {k: v for k, v in ov.items() if isinstance(v, dict)} if isinstance(ov, dict) else {}
    out = {}
    for f in fc["features"]:
        p = f["properties"]
        fid = f.get("id") or p.get("id")
        o, co = ov.get(fid) or {}, p.get("callout") if isinstance(p.get("callout"), dict) else {}
        t = _num(fid, "tier", o.get("tier"))
        t = _num(fid, "callout.priority", co.get("priority")) if t is None else t
        t = int(DEFAULT_TIER if t is None else t)
        mz = _num(fid, "minzoom", o.get("minzoom"))
        out[fid] = mz if mz is not None else T[max(0, min(t, len(T) - 1))]
    for fid, o in ov.items():
        dst = o.get("merge_into")
        if fid in out and dst in out:
            out[fid] = min(out[fid], out[dst])
    return out


def split(fc: dict, cfg: dict, refs: dict, tier: int) -> tuple[set[str], set[str]]:
    """(hot, lite) feature ids with a value ref in a tier-`tier` snapshot."""
    T = tiers(cfg)
    upper = T[tier + 1] if tier + 1 < len(T) else math.inf
    mz = minzooms(fc, cfg)
    props = {f.get("id") or f["properties"].get("id"): f["properties"] for f in fc["features"]}
    hot, lite = set(), set()
    for fid, r in refs.items():
        if "value" not in r:
            continue
        p = props.get(fid, {})
        in_tier = mz.get(fid, 0.0) < upper
        if p.get("callout") and in_tier:
            hot.add(fid)
        elif in_tier or p.get("kind") != "node":
            lite.add(fid)       # links and areas are drawn (and coloured by util) at every zoom (web/style.js)
    return hot, lite


def shared_series(fc: dict, cfg: dict, tier: int) -> list[str]:
    """The shared series the nodes drawn at a tier read besides their own value (L-106, Power #422): an inventory
    plant's `util_est` (eia930.<ba>.util.<fuel>, one per balancing authority and fuel, shared by every plant of that
    BA + fuel). The snapshot adds each once to `series`, so 4,784 plants cost at most a few hundred shared entries and
    the client's `extra` (capped at 400 ids, inside the request-line limit) doesn't grow."""
    T = tiers(cfg)
    upper = T[tier + 1] if tier + 1 < len(T) else math.inf
    mz = minzooms(fc, cfg)
    out = set()
    for f in fc["features"]:
        p = f["properties"]
        u = p.get("util_est")
        if isinstance(u, str) and p.get("kind") == "node" and mz.get(f.get("id") or p.get("id"), 0.0) < upper:
            out.add(u)
    return sorted(out)


def lite_entry(e: dict, value_kind: str | None) -> list:
    cap = None if value_kind == "capacity" else (e.get("cap_current") if e.get("cap_current") is not None else e.get("cap"))
    return [e.get("v"), bool(e.get("stale")), cap]
