"""Regions (L-106, docs/REGIONS.md): load + validate `regions/<id>.yaml`, find which region a point is in, and build the
region box features the map draws at tier 0.

One code path for every region: a region is a YAML file. Nothing here (or in web/) special-cases a region id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from montney import paths

REGIONS_DIR = paths.REPO_ROOT / "regions"
OUTLINES = paths.REPO_ROOT / "network" / "region" / "outlines.geojson"
BOXES = paths.REPO_ROOT / "network" / "region" / "boxes.geojson"

ID_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
MEMBER_RE = re.compile(r"^(CA|US|MX)-[A-Z]{2}$")
SERIES_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{1,120}$")

# The only fuel keys (docs/REGIONS.md §4). Each maps to a plant sprite kind (web/sprites.js FUEL_KIND).
FUELS = ("gas", "coal", "nuclear", "hydro", "pumped_storage", "wind", "solar", "battery", "oil", "biomass",
         "geothermal", "other")
GAS_LINES = ("production", "imports", "exports", "demand", "storage", "inventory")
POWER_LINES = ("load", "generation", "interchange")
OIL_LINES = ("production",)
QUALITIES = ("measured", "scheduled", "estimate")
FILLS = ("month", "latest", "seasonal")
LATEST_MAX_DAYS = 150      # `fill: latest` holds the newest month forward at most this long (EIA lags 2-3 months)
SEASONAL_MAX_YEARS = 1     # `fill: seasonal` borrows the same calendar month at most this many years back
# Reviewer on #434/#435: holding August's storage injection into November flips its sign. A storage FLOW must be the
# real month; an inventory (a stock) may be held forward; a seasonal stand-in is only for demand-type flows.
LINE_FILLS = {"storage": ("month",), "inventory": ("month", "latest")}
TOP_KEYS = {"id", "name", "color", "members", "clips", "anchor", "tz", "budget_region", "coverage", "detail", "box",
            "gas", "power", "oil", "weather", "watch", "note"}
LINE_KEYS = {"parts", "covers", "quality", "unit", "note", "none", "scope", "derived", "lag", "unproven", "held"}
# The region page (REGIONS.md §5b): `detail: {gas|power|oil: [group, ...]}`, each group a table of rows.
TABS = ("gas", "power", "oil")
TAB_LINES = {"gas": GAS_LINES + ("net_imports",), "power": POWER_LINES, "oil": OIL_LINES}
TAB_UNIT = {"gas": "MMcf/d", "power": "MW", "oil": "bbl/d"}
GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
GROUP_KEYS = {"id", "title", "of", "unit", "note", "rows"}
ROW_KEYS = {"label", "series", "value", "unit", "as_of", "source", "note", "quality"}
BOX_DIRS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


class RegionError(ValueError):
    pass


@dataclass(frozen=True)
class Part:
    series: str
    sign: float = 1.0
    scale: float = 1.0
    fill: str = ""            # "" | "month" | "latest" | "seasonal"


@dataclass
class Line:
    parts: list[Part]
    covers: list[str] = field(default_factory=list)
    quality: str = "measured"
    unit: str = ""
    note: str = ""
    none: bool = False        # `none: true`: known to be ~0 (no parts); counts as 0 in the implied storage
    scope: str = ""           # short footprint label shown in the box when it isn't the region: "CAISO", "all ON"
    derived: bool = False     # computed from the region's other balance lines (pnw demand): no implied storage
    lag: int = 0              # seconds this line's slowest part publishes late (`lag:` on the line; else the section's)
    unproven: bool = False    # a model not yet checked against daily data: shown (est.), but no implied storage from it
    held: bool = False        # a part is its source's latest value held forward by its connector (`*_best`): tagged "held"

    @property
    def estimate(self) -> bool:
        return self.quality == "estimate" or any(p.fill for p in self.parts)

    @property
    def stand_in(self) -> bool:
        """True when the line isn't a reading of the day itself: a part fills unpublished months with a stand-in (held
        newest month, or last year's same month), or the line is `held: true` (its connector holds the latest value
        forward). No implied storage is built from one (Reviewer on #434/#435, #493)."""
        return self.held or any(p.fill in ("latest", "seasonal") for p in self.parts)


@dataclass(frozen=True)
class DetailRow:
    """One row of a detail group: a stored series, or a static fact (`value` with its `as_of` and `source`)."""
    label: str
    series: str = ""
    value: float | None = None
    unit: str = ""
    as_of: str = ""
    source: str = ""
    note: str = ""
    quality: str = "measured"


@dataclass
class DetailGroup:
    id: str
    title: str
    rows: list[DetailRow]
    of: str = ""              # the tab's balance line these rows break down ("" = a free-standing table)
    unit: str = ""
    note: str = ""


@dataclass
class Region:
    id: str
    name: str
    members: list[str]
    anchor: tuple[float, float]
    tz: str
    budget_region: str
    coverage: str             # full | standard: how much zoom-in detail we promise
    clips: list[dict]
    gas: dict[str, Line]
    power: dict[str, Line]
    fuels: dict[str, Line]
    oil: dict[str, Line]
    weather: dict
    watch: list[str]
    note: str = ""
    color: str = ""                                      # "#rrggbb": the region's border + tint on the map
    received: list[str] = field(default_factory=list)    # members a neighbour clips to this region (load() fills it)
    lags: dict[str, int] = field(default_factory=dict)   # family -> seconds its slowest part publishes late (`lag:`)
    detail: dict[str, list[DetailGroup]] = field(default_factory=dict)   # tab -> the region page's groups (§5b)
    fuels_note: str = ""                                 # `power: fuels_note:`: the note under the page's fuel-mix group
    whys: dict[str, str] = field(default_factory=dict)   # "<family>.<line>" -> why the region has no such line (`why:`)
    gas_through: str = ""                                # `gas: through: yesterday`: balance only on finished gas days

    @property
    def no_storage(self) -> bool:
        """`gas: storage: {none: true}`: the region has no underground storage, so what the balance leaves over is
        its closure (linepack, timing, measurement), not a storage flow."""
        ln = self.gas.get("storage")
        return ln is not None and ln.none
    box: dict = field(default_factory=dict)              # placement hint for the map box (§5): {dir: E}

    def series_ids(self) -> list[str]:
        """Every input series the region's lines read."""
        lines = [*self.gas.values(), *self.power.values(), *self.fuels.values(), *self.oil.values()]
        return sorted({p.series for ln in lines for p in ln.parts})

    def detail_series(self) -> list[str]:
        """Every series the region page's detail groups show."""
        return sorted({r.series for groups in self.detail.values() for g in groups for r in g.rows if r.series})


# ------------------------------------------------------------------ loading
def _part(raw, where: str) -> Part:
    if isinstance(raw, str):
        neg = raw.startswith("-")
        sid = raw[1:] if neg else raw
        if not SERIES_RE.match(sid) or "." not in sid:
            raise RegionError(f"{where}: {raw!r} is not a series id")
        return Part(sid, -1.0 if neg else 1.0)
    if isinstance(raw, dict):
        unknown = set(raw) - {"series", "sign", "scale", "fill"}
        if unknown:
            raise RegionError(f"{where}: unknown part keys {sorted(unknown)}")
        sid = raw.get("series")
        if not isinstance(sid, str) or not SERIES_RE.match(sid) or "." not in sid:
            raise RegionError(f"{where}: part needs a series id")
        sign = raw.get("sign", 1)
        if sign not in (1, -1):
            raise RegionError(f"{where}: sign must be 1 or -1")
        scale = raw.get("scale", 1.0)
        if not isinstance(scale, (int, float)) or scale == 0:
            raise RegionError(f"{where}: scale must be a non-zero number")
        fill = raw.get("fill", "")
        if fill and fill not in FILLS:
            raise RegionError(f"{where}: fill must be one of {FILLS}")
        return Part(sid, float(sign), float(scale), fill)
    raise RegionError(f"{where}: a part is a series id or a mapping")


def _line(raw, where: str, members: list[str]) -> Line:
    if not isinstance(raw, dict):
        raise RegionError(f"{where}: a line is a mapping with `parts`")
    unknown = set(raw) - LINE_KEYS
    if unknown:
        raise RegionError(f"{where}: unknown keys {sorted(unknown)}")
    if raw.get("none") is True:
        if raw.get("parts"):
            raise RegionError(f"{where}: a `none: true` line has no parts")
        return Line([], [], "measured", str(raw.get("unit", "")), str(raw.get("note", "")), none=True,
                    scope=str(raw.get("scope", "")))
    parts = raw.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RegionError(f"{where}: `parts` must be a non-empty list")
    covers = raw.get("covers", [])
    if not isinstance(covers, list) or any(not MEMBER_RE.match(str(c)) for c in covers):
        raise RegionError(f"{where}: `covers` must list ISO 3166-2 codes (members of the region or clips it receives)")
    quality = raw.get("quality", "measured")
    if quality not in QUALITIES:
        raise RegionError(f"{where}: quality must be one of {QUALITIES}")
    scope = raw.get("scope", "")
    if not isinstance(scope, str) or len(scope) > 16:
        raise RegionError(f"{where}: scope is a short label (<= 16 chars), e.g. CAISO")
    derived = raw.get("derived", False)
    if not isinstance(derived, bool):
        raise RegionError(f"{where}: derived must be true or false")
    lag = parse_lag(raw["lag"], f"{where}.lag") if "lag" in raw else 0
    unproven = raw.get("unproven", False)
    if not isinstance(unproven, bool):
        raise RegionError(f"{where}: unproven must be true or false")
    if unproven and quality != "estimate":
        raise RegionError(f"{where}: an unproven line is a model: it must say `quality: estimate`")
    held = raw.get("held", False)
    if not isinstance(held, bool):
        raise RegionError(f"{where}: held must be true or false")
    if held and quality != "estimate":
        raise RegionError(f"{where}: a held line isn't a measurement of now: it must say `quality: estimate`")
    return Line([_part(p, f"{where}.parts[{i}]") for i, p in enumerate(parts)], list(covers), quality,
                str(raw.get("unit", "")), str(raw.get("note", "")), scope=scope, derived=derived, lag=lag,
                unproven=unproven, held=held)


LAG_RE = re.compile(r"^(\d+)\s*(m|h|d)$")


def parse_lag(v, where: str) -> int:
    """`lag: 5d` / `36h` / `90m` -> seconds: how late the section's slowest part publishes (BC Hydro posts weekdays)."""
    m = LAG_RE.match(str(v).strip())
    if not m:
        raise RegionError(f"{where}: lag is a duration like 90m, 36h or 5d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _detail_row(raw, where: str) -> DetailRow:
    if not isinstance(raw, dict):
        raise RegionError(f"{where}: a row is a mapping with `label` and `series` (or `value`)")
    unknown = set(raw) - ROW_KEYS
    if unknown:
        raise RegionError(f"{where}: unknown keys {sorted(unknown)}")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 60:
        raise RegionError(f"{where}: `label` is required (<= 60 chars)")
    quality = raw.get("quality", "measured")
    if quality not in QUALITIES:
        raise RegionError(f"{where}: quality must be one of {QUALITIES}")
    series, value = raw.get("series"), raw.get("value")
    if (series is None) == (value is None):
        raise RegionError(f"{where}: a row has `series` (a stored series) or `value` (a static fact), one of the two")
    if series is not None:
        if not isinstance(series, str) or not SERIES_RE.match(series) or "." not in series:
            raise RegionError(f"{where}: {series!r} is not a series id")
        if raw.get("as_of") or raw.get("source"):
            raise RegionError(f"{where}: `as_of` / `source` belong to a static `value` row (a series has its own)")
        return DetailRow(label.strip(), series=series, unit=str(raw.get("unit", "")), note=str(raw.get("note", "")),
                         quality=quality)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegionError(f"{where}: `value` must be a number")
    for k in ("unit", "as_of", "source"):       # a number with no date and no source is a rumour
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            raise RegionError(f"{where}: a static `value` row needs `{k}` (a quoted string)")
    return DetailRow(label.strip(), value=float(value), unit=raw["unit"].strip(), as_of=raw["as_of"].strip(),
                     source=raw["source"].strip(), note=str(raw.get("note", "")), quality=quality)


def _detail(raw, where: str) -> dict[str, list[DetailGroup]]:
    """`detail: {gas|power|oil: [{id, title, of?, unit?, note?, rows: [...]}, ...]}` (REGIONS.md §5b)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - set(TABS):
        raise RegionError(f"{where}: must be a mapping with keys from {', '.join(TABS)} (the region page's tabs)")
    out: dict[str, list[DetailGroup]] = {}
    for tab, groups in raw.items():
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise RegionError(f"{where}.{tab}: must be a list of groups")
        seen: set[str] = set()
        out[tab] = []
        for i, g in enumerate(groups):
            w = f"{where}.{tab}[{i}]"
            if not isinstance(g, dict):
                raise RegionError(f"{w}: a group is a mapping with `id`, `title`, `rows`")
            unknown = set(g) - GROUP_KEYS
            if unknown:
                raise RegionError(f"{w}: unknown keys {sorted(unknown)}")
            gid = g.get("id")
            if not isinstance(gid, str) or not GROUP_ID_RE.match(gid):
                raise RegionError(f"{w}: id must be snake_case ({GROUP_ID_RE.pattern}), the same id across regions")
            if gid in seen:
                raise RegionError(f"{w}: duplicate group id {gid!r}")
            seen.add(gid)
            title = g.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 60:
                raise RegionError(f"{w}: `title` is required (<= 60 chars)")
            of = g.get("of", "")
            if of and of not in TAB_LINES[tab]:
                raise RegionError(f"{w}: `of` must be one of the {tab} lines ({', '.join(TAB_LINES[tab])})")
            rows = g.get("rows")
            if not isinstance(rows, list) or not rows:
                raise RegionError(f"{w}: `rows` must be a non-empty list")
            out[tab].append(DetailGroup(gid, title.strip(),
                                        [_detail_row(r, f"{w}.rows[{j}]") for j, r in enumerate(rows)],
                                        of=of, unit=str(g.get("unit", "")), note=str(g.get("note", ""))))
    return out


def _section(raw, allowed: tuple[str, ...], where: str, members: list[str]) -> dict[str, Line]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RegionError(f"{where}: must be a mapping")
    out = {}
    for k, v in raw.items():
        if k in ("fuels", "lag", "fuels_note", "why", "through"):
            continue
        if k not in allowed:
            raise RegionError(f"{where}: unknown line {k!r} (allowed: {', '.join(allowed)})")
        if v is not None:
            ln = _line(v, f"{where}.{k}", members)
            allowed_fills = LINE_FILLS.get(k)
            bad = [p.fill for p in ln.parts if p.fill and allowed_fills is not None and p.fill not in allowed_fills]
            if bad:
                raise RegionError(f"{where}.{k}: fill {bad[0]!r} isn't allowed on a {k} line (allowed: "
                                  f"{', '.join(allowed_fills)}): a held or seasonal stand-in gets a storage flow's sign "
                                  f"wrong (Reviewer on #434)")
            out[k] = ln
    return out


def _whys(raw: dict, where: str) -> dict[str, str]:
    """`why: {generation: "MISO publishes no generation by region"}` in a section: the reason a line the region does
    NOT have is missing, shown instead of "no source yet". Only for lines without a source."""
    out: dict[str, str] = {}
    for fam, allowed in (("gas", GAS_LINES), ("power", POWER_LINES), ("oil", OIL_LINES)):
        sec = raw.get(fam)
        why = sec.get("why") if isinstance(sec, dict) else None
        if why is None:
            continue
        if not isinstance(why, dict):
            raise RegionError(f"{where}.{fam}.why: must map a line to its reason")
        for line, text in why.items():
            if line not in allowed:
                raise RegionError(f"{where}.{fam}.why: unknown line {line!r} (allowed: {', '.join(allowed)})")
            if sec.get(line) is not None:
                raise RegionError(f"{where}.{fam}.why.{line}: the line has a source; `why` is for a line the region lacks")
            if not isinstance(text, str) or not text.strip() or len(text) > 120:
                raise RegionError(f"{where}.{fam}.why.{line}: a short reason (<= 120 chars)")
            out[f"{fam}.{line}"] = " ".join(text.split())
    return out


THROUGH = ("yesterday",)


def _through(raw: dict, where: str) -> str:
    """`gas: through: yesterday`: the region's gas lines are written only for gas days that have ended (REGIONS §3)."""
    sec = raw.get("gas")
    v = sec.get("through") if isinstance(sec, dict) else None
    if v is None:
        return ""
    if v not in THROUGH:
        raise RegionError(f"{where}.gas.through: must be one of {THROUGH} (or leave it out)")
    return v


def parse(raw: dict, where: str) -> Region:
    if not isinstance(raw, dict):
        raise RegionError(f"{where}: must be a mapping")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise RegionError(f"{where}: unknown keys {sorted(unknown)}")
    rid = raw.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise RegionError(f"{where}: id must match {ID_RE.pattern}")
    members = raw.get("members")
    if not isinstance(members, list) or not members or any(not MEMBER_RE.match(str(m)) for m in members):
        raise RegionError(f"{where}: members must be ISO 3166-2 codes like CA-AB / US-TX")
    anchor = raw.get("anchor")
    if (not isinstance(anchor, list) or len(anchor) != 2 or not all(isinstance(x, (int, float)) for x in anchor)
            or not (-180 <= anchor[0] <= 180 and -90 <= anchor[1] <= 90)):
        raise RegionError(f"{where}: anchor must be [lon, lat]")
    for k in ("name", "tz", "budget_region"):
        if not isinstance(raw.get(k), str) or not raw[k]:
            raise RegionError(f"{where}: {k!r} is required")
    coverage = raw.get("coverage", "standard")
    if coverage not in ("full", "standard"):
        raise RegionError(f"{where}: coverage must be full|standard")
    box = raw.get("box") or {}
    if not isinstance(box, dict) or set(box) - {"dir"} or box.get("dir", "E") not in BOX_DIRS:
        raise RegionError(f"{where}: box is {{dir: one of {', '.join(BOX_DIRS)}}}: where the box goes when the region "
                          f"is too small on screen to hold it")
    clips = raw.get("clips") or []
    for i, c in enumerate(clips):
        if not isinstance(c, dict) or c.get("member") not in members or not isinstance(c.get("to"), str):
            raise RegionError(f"{where}.clips[{i}]: needs member (one of this region's), to, polygon")
        poly = c.get("polygon")
        if not isinstance(poly, list) or len(poly) < 4 or poly[0] != poly[-1]:
            raise RegionError(f"{where}.clips[{i}]: polygon must be a closed ring of [lon, lat]")
    power_raw = raw.get("power") or {}
    fuels_raw = power_raw.get("fuels") or {} if isinstance(power_raw, dict) else {}
    bad = set(fuels_raw) - set(FUELS)
    if bad:
        raise RegionError(f"{where}.power.fuels: unknown fuels {sorted(bad)} (allowed: {', '.join(FUELS)})")
    color = raw.get("color", "")
    if color and not re.match(r"^#[0-9a-fA-F]{6}$", str(color)):
        raise RegionError(f"{where}: color must be #rrggbb")
    watch = raw.get("watch") or []
    if not isinstance(watch, list) or any(not isinstance(w, str) for w in watch):
        raise RegionError(f"{where}: watch must be a list of strings")
    return Region(
        id=rid, name=raw["name"], members=list(members), anchor=(float(anchor[0]), float(anchor[1])), tz=raw["tz"],
        budget_region=raw["budget_region"], coverage=coverage, clips=list(clips),
        detail=_detail(raw.get("detail"), f"{where}.detail"), box=dict(box),
        gas=_section(raw.get("gas"), GAS_LINES, f"{where}.gas", members),
        power=_section(power_raw, POWER_LINES, f"{where}.power", members),
        fuels={k: _line(v, f"{where}.power.fuels.{k}", members) for k, v in fuels_raw.items() if v is not None},
        oil=_section(raw.get("oil"), OIL_LINES, f"{where}.oil", members),
        weather=raw.get("weather") or {}, watch=list(watch), note=str(raw.get("note", "")), color=str(color),
        fuels_note=str(power_raw.get("fuels_note", "")) if isinstance(power_raw, dict) else "",
        whys=_whys(raw, where), gas_through=_through(raw, where),
        lags={fam: parse_lag(sec["lag"], f"{where}.{fam}.lag") for fam in ("gas", "power", "oil")
              if isinstance(sec := raw.get(fam), dict) and "lag" in sec})


def load(root: Path = REGIONS_DIR) -> dict[str, Region]:
    """Every region file, validated, by id. Raises RegionError on the first problem."""
    out: dict[str, Region] = {}
    for f in sorted(root.glob("*.yaml")):
        reg = parse(yaml.safe_load(f.read_text(encoding="utf-8")), f.name)
        if reg.id != f.stem:
            raise RegionError(f"{f.name}: id {reg.id!r} must match the file name")
        out[reg.id] = reg
    members: dict[str, str] = {}
    for reg in out.values():
        for m in reg.members:
            if m in members:
                raise RegionError(f"{m} is a member of both {members[m]} and {reg.id} (use a clip to split it)")
            members[m] = reg.id
        for c in reg.clips:
            if c["to"] not in out:
                raise RegionError(f"{reg.id}: clip goes to unknown region {c['to']!r}")
            if c["member"] not in out[c["to"]].received:
                out[c["to"]].received.append(c["member"])
    for reg in out.values():         # covers may name a clip the region receives (pnw's CA-BC), checked once known
        held = set(reg.members) | set(reg.received)
        for sect, lines in (("gas", reg.gas), ("power", reg.power), ("power.fuels", reg.fuels), ("oil", reg.oil)):
            for name, ln in lines.items():
                bad = [c for c in ln.covers if c not in held]
                if bad:
                    raise RegionError(f"{reg.id}.{sect}.{name}: covers {bad} are neither members nor received clips")
    return out


# ------------------------------------------------------------------ standard series (docs/REGIONS.md §4)
GAS_OUT = {"production": "Production", "imports": "Imports", "exports": "Exports", "net_imports": "Net imports",
           "demand": "Demand", "storage_implied": "Storage implied (production + net imports − demand)",
           "storage": "Storage, measured net injection", "residual": "Residual (implied − measured storage)",
           "inventory": "Storage inventory"}
POWER_OUT = {"load": "Load", "generation": "Generation", "interchange": "Net interchange (imports +)",
             "residual": "Residual (generation + interchange − load)"}


def sid(region: str, family: str, line: str) -> str:
    return f"region.{region}.{family}.{line}"


def outputs(reg: Region) -> list[tuple[str, str, str, str]]:
    """(series id, label, unit, freq) of every standard series this region can produce."""
    out = []
    g = reg.gas
    for ln in ("production", "imports", "exports", "demand", "storage"):
        if ln in g and not g[ln].none:
            out.append((sid(reg.id, "gas", ln), f"{reg.name}: gas {GAS_OUT[ln].lower()}", "MMcf/d", "D"))
    if "storage" in g and g["storage"].none and "inventory" in g and not g["inventory"].none:
        raise RegionError(f"{reg.id}.gas: storage is `none: true` (no underground storage) but an inventory is given")
    if "imports" in g and "exports" in g and not (g["imports"].none and g["exports"].none):
        out.append((sid(reg.id, "gas", "net_imports"), f"{reg.name}: gas net imports", "MMcf/d", "D"))
    balance_derived = any(ln.derived for ln in g.values())     # implied = measured by construction: say nothing
    # implied storage from a held or seasonal stand-in is noise, not a balance (Reviewer on #434/#435)
    balance_derived = balance_derived or any(g[k].stand_in for k in ("production", "imports", "exports", "demand")
                                             if k in g)
    # ... and so is one from a model whose daily error hasn't been measured yet (Reviewer on #471: the demand model's
    # band is a monthly p90). `unproven: true` comes off per region once a daily back-test says the error is small
    # next to the storage signal.
    balance_derived = balance_derived or any(g[k].unproven for k in ("production", "imports", "exports", "demand")
                                             if k in g)
    if {"production", "demand"} <= set(g) and ("imports" in g or "exports" in g) and not balance_derived:
        out.append((sid(reg.id, "gas", "storage_implied"), f"{reg.name}: gas storage implied", "MMcf/d", "D"))
        if "storage" in g and not g["storage"].none:       # no storage: the residual would repeat the closure
            out.append((sid(reg.id, "gas", "residual"), f"{reg.name}: gas residual", "MMcf/d", "D"))
    if "inventory" in g and not g["inventory"].none:
        out.append((sid(reg.id, "gas", "inventory"), f"{reg.name}: gas storage inventory", "Bcf", "D"))
    p = reg.power
    for ln in ("load", "generation", "interchange"):
        if ln in p and not p[ln].none:
            out.append((sid(reg.id, "power", ln), f"{reg.name}: {POWER_OUT[ln].lower()}", "MW", "H"))
    for fuel in reg.fuels:
        out.append((f"region.{reg.id}.power.gen.{fuel}", f"{reg.name}: generation, {fuel}", "MW", "H"))
    if {"load", "generation", "interchange"} <= set(p):
        out.append((sid(reg.id, "power", "residual"), f"{reg.name}: power residual", "MW", "H"))
    if "production" in reg.oil:
        # monthly: the engine writes one point per month (EIA / StatCan publish months). Declared "D" it was judged
        # by the daily rule and read stale after 4 days (Frontend on #530: 8 regions).
        out.append((sid(reg.id, "oil", "production"), f"{reg.name}: oil production", "bbl/d", "M"))
    return out


# ------------------------------------------------------------------ the region box (tier 0)
def line_tags(reg: Region, ln: Line | list[Line] | None) -> list[str]:
    """A row's tags: scope, partial covers, "seasonal" / "held", "est.". A derived row (net imports, storage implied)
    passes its input lines: it inherits every scope and partial cover they have, and est. if any of them is
    (Reviewer on #417)."""
    lines = [x for x in (ln if isinstance(ln, list) else [ln]) if x is not None and not x.none]
    tags: list[str] = []
    for x in lines:
        if x.scope and x.scope not in tags:
            tags.append(x.scope)
        # An empty `covers` means the region's own members; a clip it receives (pnw's Lower Mainland, mbnwo's NW Ontario)
        # is territory too, so a line has to list the clipped member in `covers` to count as the whole region.
        cov_set = x.covers or reg.members
        if set(cov_set) != set(reg.members) | set(reg.received):
            cov = ", ".join(c.split("-", 1)[1] for c in cov_set)
            if cov not in tags:
                tags.append(cov)
    fills = {p.fill for x in lines for p in x.parts}
    if "seasonal" in fills:
        tags.append("seasonal")
    if "latest" in fills or any(x.held for x in lines):
        tags.append("held")
    if any(x.estimate for x in lines):
        tags.append("est.")
    return tags


# The box, v2 (Erik, 2026-11-13; REGIONS.md §5): the same six rows in every region. (line, label, display sign): sources
# read +, sinks -, so the first four add up to the fifth.
BOX_GAS = (("production", "Production", 1), ("demand", "Demand", -1), ("imports", "Imports", 1),
           ("exports", "Exports", -1), ("storage_implied", "Storage implied (+ inj.)", 1))
# A region with no underground storage (`storage: {none: true}`): the same series, honestly named.
CLOSURE_LABEL = "Closure (no storage)"
IMPLIED_INPUTS = ("production", "imports", "exports", "demand")
NO_SOURCE = "no source yet"


def implied_why(reg: Region) -> str:
    """Why a region has no storage-implied series ("" when it has one): what's missing, in plain words."""
    g = reg.gas
    if sid(reg.id, "gas", "storage_implied") in {s for s, *_ in outputs(reg)}:
        return ""
    missing = [k for k in ("production", "demand") if k not in g]
    if "imports" not in g and "exports" not in g:
        missing.append("imports or exports")
    if missing:
        return f"needs {', '.join(missing)}"
    if any(ln.derived for ln in g.values()):
        return "a line is derived from the others, so the balance closes by construction"
    fwd = [k for k in IMPLIED_INPUTS if k in g and g[k].held]
    if fwd:
        return f"{', '.join(fwd)} {'is' if len(fwd) == 1 else 'are'} held forward, not a reading of the day"
    held = [k for k in IMPLIED_INPUTS if k in g and g[k].stand_in]
    if held:
        one = len(held) == 1
        return f"{', '.join(held)} {'is a monthly stand-in' if one else 'are monthly stand-ins'}, not daily data"
    model = [k for k in IMPLIED_INPUTS if k in g and g[k].unproven]
    return f"{', '.join(model)} {'is a model' if len(model) == 1 else 'are models'} not yet checked against daily data"


def box_v2(reg: Region) -> list[dict]:
    """The region box's six rows, always all six (a row without data says why and reads "—" on the map)."""
    have = {s for s, *_ in outputs(reg)}
    g = reg.gas
    rows = []
    for line, label, sign in BOX_GAS:
        series = sid(reg.id, "gas", line)
        inputs = [g[k] for k in IMPLIED_INPUTS if k in g] if line == "storage_implied" else g.get(line)
        if line == "storage_implied" and reg.no_storage:
            label = CLOSURE_LABEL
        row = {"line": f"gas.{line}", "series": series, "label": label, "unit": "MMcf/d", "section": "gas",
               "sign": sign, "tags": line_tags(reg, inputs), "have": series in have}
        if line != "storage_implied" and line in g and g[line].none:
            row["zero"] = True                 # `none: true`: known to be ~0 (California exports nothing by pipe)
        elif not row["have"]:
            row["why"] = implied_why(reg) if line == "storage_implied" else reg.whys.get(f"gas.{line}", NO_SOURCE)
        rows.append(row)
    series = sid(reg.id, "power", "generation")
    row = {"line": "power.generation", "series": series, "label": "Generation", "unit": "MW", "section": "power",
           "sign": 1, "tags": line_tags(reg, reg.power.get("generation")), "have": series in have}
    if not row["have"]:
        row["why"] = reg.whys.get("power.generation", NO_SOURCE)
    rows.append(row)
    return rows


def box_features(regions: dict[str, Region]) -> dict:
    """network/region/boxes.geojson: one anchor node per region, its box rows from the region file."""
    feats = []
    for reg in regions.values():
        rows = box_v2(reg)
        props = {"id": f"region.{reg.id}", "kind": "node", "system": "region", "type": "region",
                 "name": reg.name, "region": reg.id,
                 "source": f"generated by montney/regions.py from regions/{reg.id}.yaml (docs/REGIONS.md)",
                 "region_value": True, "coverage": reg.coverage, "members": reg.members, "color": reg.color,
                 "callout": {"label": reg.name, "priority": 0},     # every region has its box: the same six rows
                 "box_rows": rows}
        if reg.box:
            props["box"] = reg.box          # placement hint (Frontend): {dir: E}
        lead = next((r for r in rows if r["have"]), None)     # the feature's own value: its first line with a series
        if lead:
            props["value"], props["unit"] = lead["series"], lead["unit"]
        if reg.watch:
            props["watch"] = reg.watch
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(reg.anchor)},
                      "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def write_boxes(regions: dict[str, Region] | None = None, path: Path = BOXES) -> str:
    fc = box_features(regions if regions is not None else load())
    text = json.dumps(fc, indent=1, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return text


# ------------------------------------------------------------------ point in region
def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(x: float, y: float, poly: list) -> bool:
    return bool(poly) and _in_ring(x, y, poly[0]) and not any(_in_ring(x, y, h) for h in poly[1:])


COAST_TOLERANCE_DEG = 0.3     # the 1:50m coastline, simplified, cuts off Vancouver's and Manhattan's waterfronts


def _seg_dist(x: float, y: float, a: list, b: list) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    return ((x - a[0] - t * dx) ** 2 + (y - a[1] - t * dy) ** 2) ** 0.5


def region_of(lon: float, lat: float, *, country: str, member: str | None = None,
              outlines: dict | None = None) -> str | None:
    """The region id whose outline contains (lon, lat); else the nearest outline within COAST_TOLERANCE_DEG (a
    waterfront plant or city just off the simplified coast); else None (outside every region).

    `country` ("CA" / "US" / "MX") is required: only regions with territory in that country count. The outlines are
    simplified, and Natural Earth's provinces run to the international line in the Great Lakes and border rivers, so a
    US plant on the St. Clair River can sit inside Ontario's outline (Reviewer on #408).
    `member` (ISO 3166-2, e.g. "US-OH"), when the point's state/province is known: only regions holding that member
    (or a clip of it) count, so a Toledo plant can't fall back to Michigan's outline across a land border; Ohio isn't
    in any region, so it's None (Reviewer on #411)."""
    if country not in ("CA", "US", "MX"):
        raise ValueError(f"country must be CA, US or MX, got {country!r}")
    fc = outlines if outlines is not None else json.loads(OUTLINES.read_text(encoding="utf-8"))
    best, best_d = None, COAST_TOLERANCE_DEG
    for f in fc["features"]:
        props = f["properties"]
        if country not in props.get("countries", [country]):
            continue
        if member and member not in props.get("members", [member]):
            continue
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        if any(_in_polygon(lon, lat, p) for p in polys):
            return f["properties"]["region"]
        for p in polys:
            for ring in p[:1]:
                for a, b in zip(ring, ring[1:]):
                    d = _seg_dist(lon, lat, a, b)
                    if d < best_d:
                        best, best_d = f["properties"]["region"], d
    return best


_REGIONS_CACHE: dict | None = None


def cached() -> dict[str, Region]:
    """The region files, loaded once per process ({} when they don't load: callers degrade, they don't crash)."""
    global _REGIONS_CACHE
    if _REGIONS_CACHE is None:
        try:
            _REGIONS_CACHE = load()
        except (RegionError, OSError):
            _REGIONS_CACHE = {}
    return _REGIONS_CACHE


def line_lag(reg: "Region", family: str, line: str) -> int:
    """A line's effective lag: its own `lag:`, else its section's. Derived series use their inputs' largest lag."""
    sect = {"gas": reg.gas, "power": reg.power, "oil": reg.oil}[family]
    default = reg.lags.get(family, 0)
    if family == "power" and line.startswith("gen."):
        ln = reg.fuels.get(line[4:])
        return (ln.lag if ln and ln.lag else default)
    inputs = {"net_imports": ("imports", "exports"),
              "storage_implied": ("production", "imports", "exports", "demand"),
              "residual": ("production", "imports", "exports", "demand", "storage", "load", "generation",
                           "interchange")}.get(line, (line,))
    lags = [(sect[k].lag or default) for k in inputs if k in sect]
    return max(lags) if lags else default


def stale_after(series_id: str) -> int:
    """Seconds after which a `region.<r>.<family>.*` series reads stale: its line's `lag:` (or its section's) plus 3
    hours of run slack, or 0 (the snapshot's normal rule). A composite fed by a weekday feed is late by design; the box
    shows its age instead of calling it stale (Reviewer on #428). Per line, so a monthly storage line's lag doesn't make
    the same section's daily imports look fresh for months (Reviewer on #434)."""
    parts = series_id.split(".")
    if len(parts) < 4 or parts[0] != "region" or parts[2] not in ("gas", "power", "oil"):
        return 0
    reg = cached().get(parts[1])
    if reg is None:
        return 0
    lag = line_lag(reg, parts[2], ".".join(parts[3:]))
    return lag + 3 * 3600 if lag else 0
