"""Frontier AI data-centre IT power by region, monthly, from Epoch AI (PLAN 9, Erik: "see what the total data center
production is"). **A floor, not a total**: Epoch tracks only the largest AI campuses (96 sites worldwide in Nov 2026).

No grid operator publishes a real-time (or daily) data-centre consumption series (docs research, 2026-11-13). Epoch AI's
"Frontier Data Centers" hub is the one public, per-site, regularly refreshed source: https://epoch.ai/data/ai-data-centers,
licence **CC BY 4.0, credit Epoch AI**. We read its ZIP (one request, ~120 KB, strong ETag, so ``ctx.get_if_changed``
skips it when unchanged; Epoch rebuilds it about weekly):

* ``data_centers.csv``: one row per site. ``Current power (MW)`` is the site's **IT power** now (it equals the
  timeline's ``IT power (MW)`` at the build date: Stargate Abilene 824, Colossus 2 946), plus ``Country`` and a
  free-text ``Address``. There are no coordinates.
* ``data_center_timelines.csv``: dated satellite/permit observations per site (``Data center, Date, IT power (MW),
  Power (MW), ...``), a step function. Rows dated after today are Epoch's projections and are never read.

Placing a site: US/Canada only. The state/province comes from the address (``..., TX 79601``, ``Kuna ID 83634``,
``Ridgeland, Mississippi``); a few sites with a blank or partial address are named in ``PLACES``. The member
("US-TX") maps to its region through the region files' ``members`` (``montney.regions``). A US/Canadian site we can't
place, or a placed site outside every region (Ohio, Virginia...), is left out; an unplaced one with power gets a
warning.

Series (MW, freq M, ts = the month's first day; the month's value is the IT power in operation at its END, the current
month's is Epoch's ``Current power (MW)``):

  dc.epoch.<region>.it_mw        sum of the region's sites (0 = no tracked frontier campus, not "no data centres")
  dc.epoch.<region>.sites        how many of them have IT power > 0
  dc.epoch.site.<slug>.it_mw     each site in one of our regions, from its first timeline row

IT power excludes cooling and losses; Epoch's facility ``Power (MW)`` runs ~1.2-1.4x higher.
Fail closed (FormatDrift): a missing file or column, a date or MW that doesn't parse, or MW outside 0-10,000.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import zipfile
from datetime import date, datetime

from montney import regions
from montney.connectors.base import Connector, Context, FetchResult, slug
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

URL = "https://epoch.ai/data/data_centers/data_centers.zip"
GROUP = "Data centres / Epoch AI frontier sites (IT MW, a floor)"
MAX_MW = 10_000
SITES_COLS = ("Name", "Current power (MW)", "Country", "Address")
TL_COLS = ("Data center", "Date", "IT power (MW)")
COUNTRY = {"United States": "US", "United States of America": "US", "USA": "US", "Canada": "CA"}
US = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
      "OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
CA = "AB BC MB NB NL NS NT NU ON PE QC SK YT".split()
NAMES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
         "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
         "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
         "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
         "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
         "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
         "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
         "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
         "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
         "Wisconsin": "WI", "Wyoming": "WY", "Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB",
         "Ontario": "ON", "Quebec": "QC", "Québec": "QC", "Saskatchewan": "SK"}
# Sites whose address is blank or has no state (Nov 2026), from Epoch's own cited sources for each.
PLACES = {"Google Mesa": "US-AZ", "Google Storey County": "US-NV", "Google Kansas City East": "US-MO",
          "AWS New Albany": "US-OH", "Stream Phoenix": "US-AZ", "Anthropic Barber Lake": "US-TX",
          "OpenAI Stargate Milam": "US-TX", "OpenAI Stargate Michigan": "US-MI",
          "OpenAI Stargate Wisconsin": "US-WI", "OpenAI Stargate New Mexico": "US-NM"}


def member_of(name: str, address: str, country: str) -> str | None:
    """ISO 3166-2 member ("US-TX") of a site, or None when the address doesn't say."""
    if name in PLACES:
        return PLACES[name]
    codes = US if country == "US" else CA
    a = address or ""
    m = (re.search(r"\b([A-Z]{2})\s*,?\s*\d{5}(?:-\d{4})?\b", a) if country == "US"
         else re.search(r"\b([A-Z]{2})\s*,?\s*[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", a))
    if m and m.group(1) in codes:
        return f"{country}-{m.group(1)}"
    for m in re.finditer(r",\s*([A-Z]{2})\b", a):
        if m.group(1) in codes:
            return f"{country}-{m.group(1)}"
    for full, code in NAMES.items():
        if code in codes and re.search(rf"\b{full}\b", a, re.I):
            return f"{country}-{code}"
    return None


def _mw(raw: str, where: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise FormatDrift(f"Epoch {where}: MW {raw!r} isn't a number") from None
    if not 0 <= v <= MAX_MW:
        raise FormatDrift(f"Epoch {where}: {v} MW is outside 0-{MAX_MW}")
    return v


def _csv(z: zipfile.ZipFile, name: str, cols: tuple[str, ...]) -> list[dict]:
    try:
        text = z.read(name).decode("utf-8-sig")
    except KeyError:
        raise FormatDrift(f"Epoch ZIP: no {name}") from None
    rows = list(csv.DictReader(io.StringIO(text)))
    head = rows[0].keys() if rows else []
    missing = [c for c in cols if c not in head]
    if missing:
        raise FormatDrift(f"Epoch {name}: missing columns {missing}")
    return rows


def parse(content: bytes) -> tuple[dict[str, dict], dict[str, list[tuple[date, float]]]]:
    """ZIP -> ({site: {country, address, current}}, {site: [(date, IT MW)] sorted})."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise FormatDrift(f"Epoch: not a ZIP ({e})") from e
    sites = {}
    for r in _csv(z, "data_centers.csv", SITES_COLS):
        name = r["Name"].strip()
        if name:
            sites[name] = {"country": COUNTRY.get(r["Country"].strip(), ""), "address": r["Address"].strip(),
                           "current": _mw(r["Current power (MW)"], f"{name} current power")}
    if not sites:
        raise FormatDrift("Epoch data_centers.csv: no sites")
    tl: dict[str, list[tuple[date, float]]] = {}
    for r in _csv(z, "data_center_timelines.csv", TL_COLS):
        name = r["Data center"].strip()
        try:
            d = datetime.strptime(r["Date"].strip(), "%Y-%m-%d").date()
        except ValueError:
            raise FormatDrift(f"Epoch timeline {name}: date {r['Date']!r}") from None
        v = _mw(r["IT power (MW)"], f"{name} {d}")
        if v is not None:
            tl.setdefault(name, []).append((d, v))
    for rows in tl.values():
        rows.sort()
    return sites, tl


def at(rows: list[tuple[date, float]], day: date) -> float | None:
    """The step function's value on `day` (None before the first row)."""
    v = None
    for d, x in rows:
        if d > day:
            break
        v = x
    return v


def month_end(m: date) -> date:
    return m.replace(day=calendar.monthrange(m.year, m.month)[1])


def months(first: date, last: date) -> list[date]:
    out, m = [], first
    while m <= last:
        out.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def member_regions() -> dict[str, str]:
    return {m: r.id for r in regions.cached().values() for m in r.members}


class EpochDataCentres(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        ex = {"licence": "CC BY 4.0, credit Epoch AI", "url": "https://epoch.ai/data/ai-data-centers",
              "scope": "frontier AI campuses only: a floor, not a total"}
        out = []
        for rid, reg in sorted(regions.cached().items()):
            out.append(SeriesMeta(f"dc.epoch.{rid}.it_mw", self.id,
                                  f"{reg.name}: frontier AI data centres, IT power (Epoch AI; a floor)", "MW", "M",
                                  GROUP, extra={**ex, "region": rid}))
            out.append(SeriesMeta(f"dc.epoch.{rid}.sites", self.id,
                                  f"{reg.name}: frontier AI data centres operating (Epoch AI)", "sites", "M", GROUP,
                                  extra={**ex, "region": rid}))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        res = FetchResult(meta=self.declared_series())
        r = ctx.get_if_changed(p.get("url", URL), timeout=60)
        if r is None:
            return res                                  # unchanged since the last write: what it says is stored
        sites, tl = parse(r.content)
        res.obs, metas, res.warnings = build(sites, tl, ctx.now.date(), p.get("from", "2022-01"), member_regions(),
                                             self.id)
        res.meta.extend(metas)
        return res


def build(sites: dict, tl: dict, today: date, first: str, by_member: dict[str, str],
          source: str = "power_dc_epoch") -> tuple[list[Obs], list[SeriesMeta], list[str]]:
    warnings = [f"Epoch timeline: {n} isn't in data_centers.csv" for n in sorted(set(tl) - set(sites))]
    placed: dict[str, str] = {}                         # site -> region
    for name, s in sites.items():
        if not s["country"]:
            continue
        mem = member_of(name, s["address"], s["country"])
        if mem is None:
            if s["current"]:
                warnings.append(f"Epoch: can't place {name} ({s['current']:g} MW; address {s['address']!r})")
            continue
        if mem in by_member:
            placed[name] = by_member[mem]
    cur = today.replace(day=1)
    y, mo = (int(x) for x in first.split("-"))
    obs, metas = [], []
    rids = sorted(set(by_member.values()))
    for m in months(date(y, mo, 1), cur):
        tot = {rid: 0.0 for rid in rids}
        n = {rid: 0 for rid in rids}
        for name, rid in placed.items():
            rows = tl.get(name, [])
            if m == cur:
                v = sites[name]["current"]
                v = at(rows, today) if v is None else v
            else:
                v = at(rows, month_end(m))
            if v is None:
                continue                                # not tracked yet this month
            sid = f"dc.epoch.site.{slug(name)}.it_mw"
            obs.append(Obs(sid, datetime(m.year, m.month, 1), v, datetime(m.year, m.month, 1)))
            tot[rid] += v
            n[rid] += v > 0
        ts = datetime(m.year, m.month, 1)
        for rid in rids:
            obs.append(Obs(f"dc.epoch.{rid}.it_mw", ts, round(tot[rid], 3), ts))
            obs.append(Obs(f"dc.epoch.{rid}.sites", ts, float(n[rid]), ts))
    for name, rid in sorted(placed.items()):
        metas.append(SeriesMeta(f"dc.epoch.site.{slug(name)}.it_mw", source, f"{name}: IT power (Epoch AI)", "MW",
                                "M", GROUP, extra={"region": rid, "licence": "CC BY 4.0, credit Epoch AI"}))
    return obs, metas, warnings
