"""US hourly grid: EIA-930 demand, net generation, total interchange and generation by fuel for every balancing authority
(L-106, PLAN 8b, REGIONS §8). The per-BA series are summed into the US regions' ``power:`` sections (regions/<id>.yaml).

Source (keyless): the JSON service behind EIA's Hourly Electric Grid Monitor, https://www.eia.gov/electricity/930-api/:

  region_data/series_data?type[0]=D                     demand                             (~25 KB per group)
  region_data/series_data?type[0]=NG&type[1]=TI          net generation + total interchange (~50 KB per group)
  region_data_by_fuel_type/series_data                   net generation by fuel             (~250 KB per group)

with ``respondent[i]`` (one GROUPS batch), ``start``/``end`` (``MMDDYYYY HH:MM:SS``, UTC), ``frequency=hourly``,
``timezone=utc``. The window is the last WINDOW_H hours, so a late revision and the local-midnight hour (HE24, which
arrives with the NEXT daily batch) are picked up; the store's change-only writes drop the repeats.

**Batches of <= 15 respondents** (research 2026-11-13): a request without a respondent filter (every BA) or with 16+
respondents hangs past 45-240 s (a server-side cliff; it still worked from kraken, not from the PC), while <= 15 return
in 2-4 s. GROUPS holds the 38 BAs the region files and the plant inventory use (the inventory's GRIF, GRIS, NBSO, WACM,
WAUW and WWA return no data any more, 2026-11-13; the south-east BAs no region uses are no longer fetched). A group that
fails is a warning, the others are still written; all of them failing raises.

**The service is undocumented** (found in the Grid Monitor's JS, 2026-11-07), so it can change without notice.
**Documented fallback:** the EIA API v2, https://api.eia.gov/v2/electricity/rto/{region-data, fuel-type-data}/data/
(a free key from https://www.eia.gov/opendata/register.php, stored as ``{secret:EIA_API_KEY}``; 5,000 rows per request,
so ~10 requests per run for a 72 h window). Same data, same lag, same respondent and fuel codes (``respondent``,
``type`` D/NG/TI, ``fueltype`` COL/NG/...; ``period`` is the UTC hour ending, like ``DATES`` here). A change of the
service fails closed here (FormatDrift), and the swap is ``_series()`` + the URLs.

Publication lag (measured 2026-11-07 and 2026-11-13): demand is ~1-2 h behind real time. Net generation, generation by
fuel and total interchange come only in the BA's **daily** file (Form EIA-930), which EIA posts ~15:00-16:00 UTC, through
local 23:00 hour-ending yesterday: 8-12 h old just after, ~33-36 h just before the next one. Hence two specs:
``report: demand`` (sources/power_eia930_demand.yaml, 1 h, 3 requests) and ``report: gen``
(sources/power_eia930_gen.yaml, 1 h so the daily drop lands within the hour, 6 requests). Each gets its own stale
threshold. The hours between (no NG yet) are estimated by power_eia930_nowcast (``eia930.<ba>.*_best``).

Time: EIA labels each value with the **UTC hour ending**; ``ts`` here is the hour's start (the project's convention), so
``DATES`` 11/07/2026 17:00:00 -> ts 2026-11-07 16:00.

Series (MW, freq 1h, ``<ba>`` = the EIA respondent code in lower case, e.g. ciso, erco, nyis, isne, bpat):
  eia930.<ba>.demand        demand
  eia930.<ba>.net_gen       net generation
  eia930.<ba>.interchange   total interchange, EIA's sign: **+ = net export** out of the BA, - = net import
  eia930.<ba>.gen.<fuel>    net generation by fuel, <fuel> a FUELS key (montney/regions.py); several EIA codes can
                            feed one key (SUN + SNB -> solar) and are summed per hour, only when all of them have it
  eia930.<ba>.util.<fuel>   est. utilisation, fraction: max(0, gen) / EIA-860M nameplate of that BA and fuel
                            (network/power/eia930_capacity.json, built with the plant inventory): what inventory plants
                            animate with (wind spins, gas glows; a steady glow only, never smoke). Always est.
EIA's aggregate regions (US48, CAL, NW, TEX...) aren't stored: regions are summed from BAs (REGIONS §1 rule 2).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import json

import httpx

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift
from montney.store import Obs, SeriesMeta

BASE = "https://www.eia.gov/electricity/930-api"
WINDOW_H = 72
AGGREGATES = {"US48", "CAL", "CAR", "CENT", "FLA", "MIDA", "MIDW", "NE", "NW", "NY", "SE", "SW", "TEN", "TEX"}
FUEL = {"COL": "coal", "NG": "gas", "NUC": "nuclear", "OIL": "oil", "WAT": "hydro", "PS": "pumped_storage",
        "SUN": "solar", "SNB": "solar", "WND": "wind", "WNB": "wind", "BAT": "battery", "OES": "battery",
        "UES": "battery", "GEO": "geothermal", "OTH": "other", "UNK": "other"}
TYPES = {"D": "demand", "NG": "net_gen", "TI": "interchange"}
GROUP = "EIA-930 / US balancing authorities (hourly)"
CAPACITY = paths.REPO_ROOT / "network" / "power" / "eia930_capacity.json"   # built by network/power/build_plants.py
MIN_CAP_MW = 1.0
MAX_MW = 200_000
# <= 15 respondents per request (more hang: module docstring). Every BA a region file or plant uses.
GROUPS = (("CISO", "BANC", "LDWP", "IID", "TIDC", "BPAT", "PACW", "PGE", "PSEI", "SCL", "TPWR", "AVA", "CHPD", "DOPD",
           "GCPD"),
          ("IPCO", "AVRN", "GRID", "NWMT", "GWA", "PACE", "PSCO", "SWPW", "AZPS", "SRP", "TEPC", "WALC", "NEVP", "PNM",
           "DEAA"),
          ("ERCO", "EPE", "NYIS", "ISNE", "MISO", "SPA", "SWPP", "PJM"))
MAX_GROUP = 15


def _fmt(t: datetime) -> str:
    return t.strftime("%m%d%Y %H:%M:%S")


def _series(payload) -> list[dict]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict) or "data" not in payload[0]:
        raise FormatDrift("EIA 930-api: not [{data: [...]}] (the undocumented service changed: see the v2 fallback)")
    return payload[0]["data"]


def _points(s: dict) -> list[tuple[datetime, float]]:
    v = s.get("VALUES") or {}
    dates, data = v.get("DATES"), v.get("DATA")
    if dates is None or data is None or len(dates) != len(data):
        raise FormatDrift(f"EIA 930-api: series {s.get('RESPONDENT_ID')} without VALUES.DATES/DATA")
    out = []
    for d, x in zip(dates, data):
        if x is None:
            continue
        if not -MAX_MW <= float(x) <= MAX_MW:
            raise FormatDrift(f"EIA 930-api: {s.get('RESPONDENT_ID')} {d}: {x} MW")
        out.append((datetime.strptime(d, "%m/%d/%Y %H:%M:%S") - timedelta(hours=1), float(x)))   # hour ending -> start
    return out


def parse_region(payload) -> list[Obs]:
    """region_data: D / NG / TI per respondent -> eia930.<ba>.<demand|net_gen|interchange>."""
    out = []
    for s in _series(payload):
        ba, typ = str(s.get("RESPONDENT_ID", "")), s.get("TYPE_ID")
        if ba in AGGREGATES or typ not in TYPES:
            continue
        for ts, v in _points(s):
            out.append(Obs(f"eia930.{ba.lower()}.{TYPES[typ]}", ts, v, ts))
    return out


def parse_fuel(payload) -> list[Obs]:
    """region_data_by_fuel_type -> eia930.<ba>.gen.<fuel>, EIA codes summed into FUELS keys (fail closed per hour)."""
    codes: dict[tuple[str, str], list[dict[datetime, float]]] = defaultdict(list)
    for s in _series(payload):
        ba, code = str(s.get("RESPONDENT_ID", "")), str(s.get("FUEL_TYPE_ID", ""))
        if ba in AGGREGATES:
            continue
        if code not in FUEL:
            raise FormatDrift(f"EIA 930-api: unknown fuel code {code!r} ({s.get('FUEL_TYPE_NAME')}): map it in FUEL")
        codes[(ba, FUEL[code])].append(dict(_points(s)))
    out = []
    for (ba, fuel), parts in codes.items():
        for ts in sorted(set().union(*parts)):
            if all(ts in p for p in parts):                 # SUN + SNB: only when every code has the hour
                out.append(Obs(f"eia930.{ba.lower()}.gen.{fuel}", ts, sum(p[ts] for p in parts), ts))
    return out


def util_est(gen: list[Obs], capacity: dict[str, dict[str, float]]) -> list[Obs]:
    """eia930.<ba>.util.<fuel> = max(0, gen) / nameplate(ba, fuel): the est. utilisation inventory plants animate with
    (REGIONS §8; always labelled est.). Nameplate from EIA-860M via network/power/eia930_capacity.json."""
    out = []
    for o in gen:
        _, ba, kind, fuel = o.series_id.split(".")
        cap = capacity.get(ba, {}).get(fuel, 0.0)
        if kind == "gen" and cap >= MIN_CAP_MW:
            out.append(Obs(f"eia930.{ba}.util.{fuel}", o.ts, round(max(0.0, o.value) / cap, 4), o.as_of))
    return out


class Eia930(Connector):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self.report = spec.get("params", {}).get("report", "demand")
        if self.report not in ("demand", "gen"):
            raise ValueError(f"{self.id}: params.report must be demand or gen, not {self.report!r}")

    def _get(self, ctx: Context, path: str, extra: dict, group: tuple[str, ...]) -> list:
        end = ctx.now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        params = {**extra, **{f"respondent[{i}]": ba for i, ba in enumerate(group)},
                  "start": _fmt(end - timedelta(hours=WINDOW_H + 1)), "end": _fmt(end),
                  "frequency": "hourly", "timezone": "utc"}
        return ctx.get(f"{BASE}/{path}", params=params, timeout=60, retries=2).json()

    def _groups(self, ctx: Context, res: FetchResult, path: str, extra: dict, parse) -> list[Obs]:
        """One request per GROUPS batch. A failed batch (HTTP error, or a body that isn't JSON: Reviewer on #482) is a
        warning, so the other BAs still land; all failing raises. A FormatDrift in a parsed batch still fails closed."""
        out, errors = [], []
        for group in GROUPS:
            try:
                out += parse(self._get(ctx, path, extra, group))
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                errors.append(e)
                res.warnings.append(f"EIA 930-api {path} {group[0]}..{group[-1]}: {type(e).__name__} {e}")
        if len(errors) == len(GROUPS):
            raise errors[-1]
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult()
        if self.report == "demand":
            res.obs = self._groups(ctx, res, "region_data/series_data", {"type[0]": "D"}, parse_region)
        else:
            res.obs = self._groups(ctx, res, "region_data/series_data", {"type[0]": "NG", "type[1]": "TI"},
                                   parse_region)
            fuel = self._groups(ctx, res, "region_data_by_fuel_type/series_data", {}, parse_fuel)
            cap = json.loads(CAPACITY.read_text(encoding="utf-8"))["ba"] if CAPACITY.exists() else {}
            if not cap:
                res.warnings.append("EIA-930: no network/power/eia930_capacity.json: no est. utilisation")
            res.obs += fuel + util_est(fuel, cap)
        if not res.obs:
            raise FormatDrift(f"EIA 930-api ({self.report}): no values (NO-RESULT)")
        ex = {"tier": "hourly", "lag": "~1-2 h" if self.report == "demand" else "published daily, 8-36 h behind"}
        labels = {"demand": "demand", "net_gen": "net generation", "interchange": "total interchange (+ net export)"}
        metas = {}
        for o in res.obs:
            if o.series_id in metas:
                continue
            _, ba, rest = o.series_id.split(".", 2)
            if rest.startswith("util."):
                metas[o.series_id] = SeriesMeta(o.series_id, self.id, f"EIA-930 {ba.upper()} {rest[5:]} utilisation "
                                                f"(est.: generation / EIA-860M nameplate)", "fraction", "1h", GROUP,
                                                extra={**ex, "estimate": True})
                continue
            label = f"{ba.upper()} " + (f"generation, {rest[4:]}" if rest.startswith("gen.") else labels[rest])
            metas[o.series_id] = SeriesMeta(o.series_id, self.id, f"EIA-930 {label}", "MW", "1h", GROUP, extra=ex)
        res.meta = list(metas.values())
        return res
