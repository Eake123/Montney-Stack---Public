"""Installed generating capacity by fuel for the nine US regions, monthly, from EIA-860M (PLAN 9, region page
`capacity_by_fuel`).

Source (keyless, public domain): EIA-860M, the Preliminary Monthly Electric Generator Inventory,
https://www.eia.gov/electricity/data/eia860m/. Each month EIA posts one ~14 MB workbook
(``<month>_generator<year>.xlsx``), about 3-4 weeks after the month (September 2026 posted 2026-10-23). The page lists
every month's file; links to months not yet released sit in an HTML comment, which ``montney.eia860m.index`` strips.

Requests: one GET of the 860M page per run (interval 1d, ~94 KB). A month's workbook is downloaded only when it isn't
in the cache yet: newest first, then the back-fill (``backfill_months``, 12), at most ``max_downloads`` (2) workbooks
a run, so a new install fills its 12 months over ~6 days and a normal month costs one 14 MB download. Each workbook is
parsed to region x fuel totals and kept in ``<home>/cache/eia860m_capacity.json`` (a few KB); the xlsx isn't kept.
Every run re-writes all cached months (idempotent).

What's counted: every generator on the "Operating" sheet with status (OP) operating or (SB) standby/backup, at its
**net summer capacity** (MW; nameplate where EIA leaves net summer blank, counted in a warning). (OA)/(OS)
out-of-service units aren't. EIA-860M covers every plant whose generators total >= 1 MW nameplate (its own threshold,
the note on the sheet), so this is a bit larger than the map's plant inventory, which keeps plants >= 5 MW.

Fuel: ``montney.eia860m.EIA_TECH`` (Technology -> FUELS group), the plant inventory's map, per generator (a plant with
gas and battery units counts in both). Region: the plant's state and coordinates through ``montney.regions.region_of``
(country US, member US-<state>), else the region holding its state (offshore wind beyond the coast tolerance): the
inventory's rule (network/power/build_plants.py). Plants in states outside every region (Ohio, ...) aren't counted.

Series (MW, freq M, ts = the inventory's month, naive UTC first of the month):
  cap.<region>.<fuel>   region in cal pnw bakken mtnw sunbelt tx nyc upne umw, every FUELS key (0 = none installed:
                        the inventory is complete above 1 MW, so a fuel with no generator is a real 0)
  cap.<region>.total    the sum of the fuels

Fail closed (FormatDrift, nothing stored): the page lists no monthly file; a download that isn't an xlsx; a workbook
whose title month differs from the link's; a missing column; a Technology not in EIA_TECH (a new one must be mapped);
a capacity that isn't a number or is negative; a region total outside 1 GW - 400 GW. A generator with a BLANK
Technology (Aug 2026: one 76 MW Queens peaker) counts as other, with a warning, up to MAX_BLANK_MW in all; more fails.
"""

from __future__ import annotations

import json
from datetime import datetime

from montney import eia860m
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, cache_load, cache_save
from montney.regions import FUELS
from montney.store import Obs, SeriesMeta

REGIONS = ("cal", "pnw", "bakken", "mtnw", "sunbelt", "tx", "nyc", "upne", "umw")
CACHE = "eia860m_capacity"
VERSION = 1                  # bump when the counting rules change: the cache is rebuilt from fresh downloads
GROUP = "EIA-860M / Installed capacity by fuel (monthly)"
COLUMNS = ("Plant ID", "Plant State", "Technology", "Status", "Net Summer Capacity (MW)", "Nameplate Capacity (MW)",
           "Latitude", "Longitude")
MIN_TOTAL_MW, MAX_TOTAL_MW = 1_000, 400_000
MAX_BLANK_MW = 2_000         # generators with a blank Technology count as "other" (warned) up to this much, then fail
FUEL_LABEL = {"pumped_storage": "pumped storage", "battery": "battery storage"}


def _region_names() -> dict[str, str]:
    from montney import regions
    regs = regions.cached()
    return {r: (regs[r].name if r in regs else r) for r in REGIONS}


def plant_region_fn(outlines: dict | None = None):
    """(state, lon, lat) -> region id or None: region_of(country US, member US-<state>), else the region holding the
    state (the plant inventory's rule, build_plants.build). Only the nine REGIONS count."""
    from montney import regions
    if outlines is None:
        outlines = json.loads(regions.OUTLINES.read_text(encoding="utf-8"))
    state_region = {m: rid for rid, reg in regions.load().items() for m in reg.members if m.startswith("US-")}
    memo: dict[tuple, str | None] = {}

    def fn(state: str, lon: float | None, lat: float | None) -> str | None:
        key = (state, lon, lat)
        if key not in memo:
            member = f"US-{state}"
            r = None
            if lon is not None and lat is not None:
                r = regions.region_of(lon, lat, country="US", member=member, outlines=outlines)
            r = r or state_region.get(member)
            memo[key] = r if r in REGIONS else None
        return memo[key]
    return fn


def _coord(text: str) -> float | None:
    try:
        return float(text) if text.strip() else None
    except ValueError:
        return None


def capacity(content: bytes, region_fn) -> tuple[dict[str, dict[str, float]], dict]:
    """A workbook -> ({region: {fuel: net summer MW}}, stats). Raises FormatDrift."""
    cap: dict[str, dict[str, float]] = {r: {f: 0.0 for f in FUELS} for r in REGIONS}
    stats = {"generators": 0, "nameplate_fallback": 0, "outside": 0, "blank_technology": 0,
             "blank_technology_mw": 0.0}
    try:
        for i, row in enumerate(eia860m.operating(content)):
            if i == 0:
                missing = [c for c in COLUMNS if c not in row]
                if missing:
                    raise FormatDrift(f"EIA-860M: missing columns {missing}")
            if not row["Status"].startswith(eia860m.STATUSES):
                continue
            tech = row["Technology"]
            if not tech:                    # a blank cell (Aug 2026: one NY peaker), not a new technology
                fuel = "other"
            elif tech in eia860m.EIA_TECH:
                fuel = eia860m.EIA_TECH[tech][1]
            else:
                raise FormatDrift(f"EIA-860M: unmapped Technology {tech!r} (add it to EIA_TECH, montney/eia860m.py)")
            mw = eia860m.mw(row["Net Summer Capacity (MW)"])
            fallback = mw is None
            if fallback:
                mw = eia860m.mw(row["Nameplate Capacity (MW)"]) or 0.0
            if mw < 0:
                raise FormatDrift(f"EIA-860M: negative capacity {mw} (plant {row['Plant ID']})")
            r = region_fn(row["Plant State"], _coord(row["Longitude"]), _coord(row["Latitude"]))
            if r is None:
                stats["outside"] += 1
                continue
            cap[r][fuel] += mw
            stats["generators"] += 1
            stats["nameplate_fallback"] += fallback
            if not tech:
                stats["blank_technology"] += 1
                stats["blank_technology_mw"] += mw
    except eia860m.Eia860mError as e:
        raise FormatDrift(str(e)) from None
    if stats["blank_technology_mw"] > MAX_BLANK_MW:
        raise FormatDrift(f"EIA-860M: {stats['blank_technology_mw']:,.0f} MW of generators with a blank Technology")
    stats["blank_technology_mw"] = round(stats["blank_technology_mw"], 1)
    for r, fuels in cap.items():
        total = sum(fuels.values())
        if not MIN_TOTAL_MW <= total <= MAX_TOTAL_MW:
            raise FormatDrift(f"EIA-860M: {r} total {total:,.0f} MW out of range")
    return {r: {f: round(v, 1) for f, v in fuels.items()} for r, fuels in cap.items()}, stats


class CapacityUS(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        names = _region_names()
        out = []
        for r in REGIONS:
            ex = {"tier": "daily", "region": r, "lag": "~4-8 weeks (EIA-860M, monthly)",
                  "coverage": "operating + standby generators at plants >= 1 MW", "measure": "net summer capacity"}
            for f in (*FUELS, "total"):
                what = "total" if f == "total" else FUEL_LABEL.get(f, f)
                out.append(SeriesMeta(f"cap.{r}.{f}", self.id,
                                      f"{names[r]} installed {what} capacity (EIA-860M, net summer MW, operating + "
                                      "standby)", "MW", "M", GROUP, extra=ex))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        backfill, max_dl = int(p.get("backfill_months", 12)), int(p.get("max_downloads", 2))
        months = eia860m.index(ctx.get(eia860m.INDEX_URL, timeout=60).text)
        if not months:
            raise FormatDrift("EIA-860M: the page lists no monthly generator file")
        wanted = sorted(months)[-backfill:]
        cache = cache_load(CACHE)
        if cache.get("v") != VERSION:
            cache = {"v": VERSION, "months": {}}
        res = FetchResult(meta=self.declared_series())
        todo = [m for m in reversed(wanted) if f"{m:%Y-%m}" not in cache["months"]]
        region_fn = plant_region_fn() if todo[:max_dl] else None
        for m in todo[:max_dl]:
            r = ctx.get(months[m], timeout=300)
            if not r.content.startswith(b"PK"):
                raise FormatDrift(f"EIA-860M {m:%Y-%m}: not an xlsx ({months[m]})")
            title = eia860m.as_of(r.content)
            if title != m:
                raise FormatDrift(f"EIA-860M {m:%Y-%m}: the workbook says {title:%Y-%m}" if title else
                                  f"EIA-860M {m:%Y-%m}: no 'Operating Generators as of <month>' title")
            cap, stats = capacity(r.content, region_fn)
            cache["months"][f"{m:%Y-%m}"] = {"url": months[m], "regions": cap, **stats}
            cache_save(CACHE, cache)            # each month as it's parsed: a failed back-fill keeps the newest
            if stats["nameplate_fallback"]:
                res.warnings.append(f"EIA-860M {m:%Y-%m}: {stats['nameplate_fallback']} generators without a net "
                                    "summer capacity counted at nameplate")
            if stats["blank_technology"]:
                res.warnings.append(f"EIA-860M {m:%Y-%m}: {stats['blank_technology']} generators "
                                    f"({stats['blank_technology_mw']:,.1f} MW) with a blank Technology: other")
        keep = {f"{m:%Y-%m}" for m in wanted}
        if set(cache["months"]) - keep:
            cache["months"] = {k: v for k, v in cache["months"].items() if k in keep}
            cache_save(CACHE, cache)
        left = len(todo) - len(todo[:max_dl])
        if left:
            res.warnings.append(f"EIA-860M: back-fill, {left} older months still to download ({max_dl} a run)")
        for key, month in sorted(cache["months"].items()):
            ts = datetime.strptime(key, "%Y-%m")
            for r, fuels in month["regions"].items():
                for f, v in fuels.items():
                    res.obs.append(Obs(f"cap.{r}.{f}", ts, v, ts))
                res.obs.append(Obs(f"cap.{r}.total", ts, round(sum(fuels.values()), 1), ts))
        return res
