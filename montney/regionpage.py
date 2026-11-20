"""The region page (docs/REGIONS.md §5b): everything one region's Gas / Power / Oil tabs show, in one call
(`GET /api/region/<id>`). Built from the region file alone: its balance lines, its `detail:` groups and the latest
stored values. Reads only; no region is special-cased here or in web/.

A row's history is `/api/series?ids=<series>`: every number on the page is a stored series (or a static fact with its
date and source), so a later API can serve the same thing to agents.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from montney import regions as R
from montney import snapshot

# The tabs' balance lines, in page order: (line, label).
PAGE_LINES = {
    "gas": (("production", "Production"), ("demand", "Demand"), ("imports", "Imports"), ("exports", "Exports"),
            ("net_imports", "Net imports"), ("storage_implied", "Storage implied (+ injection)"),
            ("storage", "Storage measured (+ injection)"), ("residual", "Unexplained (implied − measured)"),
            ("inventory", "Storage inventory")),
    "power": (("generation", "Generation"), ("load", "Load"), ("interchange", "Net interchange (+ imports)"),
              ("residual", "Unexplained (generation + interchange − load)")),
    "oil": (("production", "Production"),),
}
# A computed line's inputs: it inherits their tags (scope, partial covers, est.).
INPUTS = {"net_imports": ("imports", "exports"), "storage_implied": R.IMPLIED_INPUTS,
          "residual": (*R.IMPLIED_INPUTS, "storage", "load", "generation", "interchange")}
VALUE_KEYS = ("v", "ts", "prev", "prev_ts", "stale", "freq")
FUEL_GROUP = "generation_by_fuel"


def _sections(reg: R.Region) -> dict[str, dict]:
    return {"gas": reg.gas, "power": reg.power, "oil": reg.oil}


def _weather_series(reg: R.Region) -> list[str]:
    out: list[str] = []
    for k, v in (reg.weather or {}).items():
        if k == "source":
            continue
        for s in (v if isinstance(v, list) else [v]):
            if isinstance(s, str) and "." in s and R.SERIES_RE.match(s):
                out.append(s)
    return out


def series_ids(reg: R.Region) -> list[str]:
    """Every series the page reads: the standard region series, the detail rows and the weather strip."""
    ids = {s for s, *_ in R.outputs(reg)} | set(reg.detail_series()) | set(_weather_series(reg))
    return sorted(ids)


def _value(latest: dict, series: str) -> dict:
    e = latest.get(series)
    if e is None:                       # not in the store (yet): no value, and it says so
        return {"v": None, "ts": None, "prev": None, "prev_ts": None, "stale": True, "freq": None, "missing": True}
    return {k: e.get(k) for k in VALUE_KEYS}


# A known zero (`none: true`: California exports nothing by pipe) has no series: it IS 0, and it is never stale or
# missing (Frontend on #530: six box rows flagged a zero stale).
ZERO = {"v": 0.0, "ts": None, "prev": None, "prev_ts": None, "stale": False, "freq": None}


def _unit_of(latest: dict, series: str, default: str) -> str:
    return (latest.get(series) or {}).get("unit") or default


def _line(reg: R.Region, tab: str, line: str, label: str, have: set, latest: dict) -> dict:
    sect = _sections(reg)[tab]
    series = R.sid(reg.id, tab, line)
    src = sect.get(line)
    inputs = [sect[k] for k in INPUTS[line] if k in sect] if line in INPUTS else src
    unit = "Bcf" if line == "inventory" else R.TAB_UNIT[tab]
    if tab == "gas" and line == "storage_implied" and reg.no_storage:
        label = "Closure (no underground storage): what the four lines leave over"
    out = {"line": line, "label": label, "series": series, "unit": unit, "tags": R.line_tags(reg, inputs),
           "have": series in have, **(ZERO if src is not None and src.none else _value(latest, series))}
    if src is not None:
        out["quality"] = "estimate" if src.estimate else src.quality
        if src.note:
            out["note"] = " ".join(src.note.split())
        if src.none:
            out["zero"] = True          # `none: true`: known to be ~0, not a missing source
        out["parts"] = [p.series for p in src.parts]
        _as_its_month(out, src)
    if not out["have"] and not out.get("zero"):
        out["why"] = R.implied_why(reg) if line == "storage_implied" else (
            "needs storage implied and measured storage" if line == "residual" and tab == "gas"
            else reg.whys.get(f"{tab}.{line}", R.NO_SOURCE))
    return out


def _as_its_month(out: dict, src: R.Line) -> None:
    """A line made only of `fill: month` parts is one monthly value, written across the days of its month. Its newest
    day is that month's last day, months ago: August's storage injection on a November page, the wrong sign for the
    season if it reads as a daily number (Reviewer on #531). So it is presented as the monthly value it is: freq "M",
    stamped at the month's start, `period` "YYYY-MM" (the page names the month), and no day-on-day change. `stale`
    stays the line's own judgement (its declared `lag`, counted from the month's end)."""
    if out.get("ts") is None or not src.parts or any(p.fill != "month" for p in src.parts):
        return
    day = datetime(1970, 1, 1) + timedelta(seconds=out["ts"])
    start = datetime(day.year, day.month, 1)
    out.update(ts=(start - datetime(1970, 1, 1)).total_seconds(), freq="M", period=start.strftime("%Y-%m"),
               prev=None, prev_ts=None)


def _row(row: R.DetailRow, group_unit: str, latest: dict) -> dict:
    if not row.series:
        out = {"label": row.label, "value": row.value, "unit": row.unit, "as_of": row.as_of, "source": row.source}
    else:
        out = {"label": row.label, "series": row.series,
               "unit": row.unit or _unit_of(latest, row.series, group_unit), **_value(latest, row.series)}
    if row.quality != "measured":
        out["quality"] = row.quality
    if row.note:
        out["note"] = " ".join(row.note.split())
    return out


def _rest(line: dict | None, rows: list[dict]) -> float | None:
    """What the rows leave unexplained of the line they break down: line − Σ rows. Only when the line and every series
    row have a fresh value in the line's unit FOR THE SAME READING (same ts): a row that is late but not yet stale (a
    fuel 3 h behind, a monthly component under a daily line) must not be subtracted from a newer line value and shown
    as "unexplained" (Reviewer on #460). A missing row never counts as zero; static facts aren't summed."""
    live = [r for r in rows if "series" in r]
    if line is None or line.get("v") is None or line.get("stale") or not live:
        return None
    if any(r.get("v") is None or r.get("stale") or r.get("unit") != line["unit"] or r.get("ts") != line.get("ts")
           for r in live):
        return None
    return round(line["v"] - sum(r["v"] for r in live), 3)


def _groups(reg: R.Region, tab: str, lines: dict[str, dict], latest: dict) -> list[dict]:
    groups = list(reg.detail.get(tab, []))
    if tab == "power" and reg.fuels and FUEL_GROUP not in {g.id for g in groups}:
        # the fuel mix is every region's first power group, from `power.fuels` (no need to repeat it in `detail:`)
        fuel_rows = [R.DetailRow(f.replace("_", " ").capitalize(), series=f"region.{reg.id}.power.gen.{f}", unit="MW",
                                 quality="estimate" if reg.fuels[f].estimate else reg.fuels[f].quality)
                     for f in R.FUELS if f in reg.fuels]
        groups.insert(0, R.DetailGroup(FUEL_GROUP, "Generation by fuel", fuel_rows, of="generation", unit="MW",
                                       note=reg.fuels_note))
    out = []
    for g in groups:
        unit = g.unit or R.TAB_UNIT[tab]
        rows = [_row(r, unit, latest) for r in g.rows]
        e = {"id": g.id, "title": g.title, "unit": unit, "rows": rows}
        if g.of:
            e["of"] = g.of
            e["rest"] = _rest(lines.get(g.of), rows)
        if g.note:
            e["note"] = " ".join(g.note.split())
        if g.id == FUEL_GROUP:
            # the rows may be shares of their own sum (gross generation) when a battery charges; the page names that
            # basis in the card's header. No other group opts in: interchange by tie is never gross (Reviewer on #532)
            e["gross"] = "fuels' total"
        out.append(e)
    return out


def build(store, reg: R.Region) -> dict:
    ids = series_ids(reg)
    known = sorted(set(store.meta(ids))) if ids else []
    snap = snapshot.build(store, {}, {}, known, gauges=False) if known else {"as_of": None, "series": {}}
    latest = snap["series"]
    # a line whose newest point is older than the snapshot's window (a monthly storage flow, held to its month's end)
    latest.update(snapshot.latest_far(store, [s for s in known if (latest.get(s) or {}).get("v") is None]))
    have = {s for s, *_ in R.outputs(reg)}
    tabs = {}
    for tab in R.TABS:
        lines = [_line(reg, tab, line, label, have, latest) for line, label in PAGE_LINES[tab]]
        tabs[tab] = {"unit": R.TAB_UNIT[tab], "lines": lines,
                     "groups": _groups(reg, tab, {x["line"]: x for x in lines}, latest)}
        lag = reg.lags.get(tab)
        if lag:
            tabs[tab]["lag_s"] = lag
        if tab == "gas" and reg.gas_through:
            tabs[tab]["through"] = reg.gas_through      # the balance lines stop at the last finished gas day
    box = [{**r, **(ZERO if r.get("zero") else _value(latest, r["series"]))} for r in R.box_v2(reg)]
    wx = dict(reg.weather or {})
    if wx:
        wx["values"] = {s: {**_value(latest, s), "unit": _unit_of(latest, s, "")} for s in _weather_series(reg)}
    return {"id": reg.id, "name": reg.name, "color": reg.color, "members": reg.members, "received": reg.received,
            "tz": reg.tz, "coverage": reg.coverage, "anchor": list(reg.anchor),
            "as_of": snap["as_of"] or (snapshot.utcnow().isoformat() + "Z"),
            "box": box, "tabs": tabs, "weather": wx, "watch": reg.watch, "note": reg.note}


def index(regs: dict[str, R.Region]) -> list[dict]:
    """`GET /api/regions`: the regions, lightly (the page of each is `/api/region/<id>`)."""
    return [{"id": r.id, "name": r.name, "color": r.color, "members": r.members, "anchor": list(r.anchor), "tz": r.tz,
             "tabs": {t: len(r.detail.get(t, [])) for t in R.TABS}} for r in regs.values()]
