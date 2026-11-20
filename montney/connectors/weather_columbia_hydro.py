"""Columbia hydro at a glance (L-085, Erik): is the system running at, above or below normal? No gas call in the data.

Two cheap requests a run, both bulk:
1. **Reservoir storage**: USBR Pacific Northwest Hydromet daily storage (pcode AF, acre-feet), ONE CSV for Grand Coulee
   (GCL, Lake Roosevelt), Hungry Horse (HGH) and Dworshak (DWR), 1995 -> today (~0.5 MB). Public domain (US federal).
   The same request carries each project's history, so the normal for the date and the record maximum come with it.
   - aggregate storage (kaf) on the latest date ALL three have a value (Grand Coulee posts a few days late);
   - % full = aggregate / the sum of each project's 1995+ maximum (≈ full pool; a record max, not a design capacity);
   - % of normal = aggregate / the sum of each project's median for the date (+/-7 days, 1995 -> 30 days ago;
     Dworshak from 2013-02-10, when Hydromet's series changed from active to total storage: BASIS_START).
   Not included, stated: Libby (USACE; not in Hydromet, and its CWMS daily history isn't served), and BC's Mica and Arrow
   (only levels are public: weather_wsc 08NB017 / 08NE104 / 08NE102; no public storage curve).
2. **Water-supply forecast**: NWRFC ESP (with 10-day QPF), Jan-Jul runoff volume at The Dalles (TDAO3), from its CSV
   product (`ws_text.cgi?id=TDAO3&per=JAN-JUL`), newest issuance. Public (NOAA). Stored: the 50 % (median), 90 % and 10 %
   exceedance forecasts (kaf), and the median as % of the 1991-2020 average, 103,714 kaf (NWRFC's own table, read
   2026-11-03; it changes only when NOAA re-bases its normals).

Cue for the box (Frontend): at or above 110 % "above normal", at or below 90 % "below normal", else "near normal". The
numbers only; the reader makes the market call.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.weather_percentile import window_keys
from montney.store import Obs, SeriesMeta

HYDROMET = "https://www.usbr.gov/pn-bin/daily.pl"
NWRFC = "https://www.nwrfc.noaa.gov/water_supply/ws_text.cgi"
PROJECTS = {"gcl": "Grand Coulee (Lake Roosevelt)", "hgh": "Hungry Horse", "dwr": "Dworshak"}
# Hydromet switched Dworshak from active to total storage on 2013-02-10 (1,088,700 -> 2,542,430 AF overnight; the yearly
# max went 2.02 -> 3.47 MAF; 1995-98 are zeros). Only the total-storage basis is read, so today and its normal match.
BASIS_START = {"dwr": date(2013, 2, 10)}
JAN_JUL_AVG_KAF = 103714.0         # NWRFC TDAO3 30-year average 1991-2020, Jan-Jul (ws_forecasts.php, 2026-11-03)
MIN_YEARS = 10
G = "Rivers / Columbia hydro"


def parse_hydromet(text: str) -> dict[str, dict[date, float]]:
    """{project: {date: acre-feet}} from the Hydromet CSV (blank cells are missing, never zero)."""
    out: dict[str, dict[date, float]] = {p: {} for p in PROJECTS}
    for row in csv.DictReader(io.StringIO(text.strip())):
        d = date.fromisoformat(row["DateTime"][:10])
        for p in PROJECTS:
            v = (row.get(f"{p}_af") or "").strip()
            if v and d >= BASIS_START.get(p, date.min):
                out[p][d] = float(v)
    return out


def storage_summary(hist: dict[str, dict[date, float]], today: date) -> dict | None:
    """Aggregate storage on the latest common date: kaf, % full, % of normal. None (NO-RESULT) if any part is missing."""
    common = set.intersection(*(set(h) for h in hist.values())) if hist else set()
    recent = [d for d in common if d > today - timedelta(days=14)]
    if not recent:
        return None
    d = max(recent)
    cutoff = today - timedelta(days=30)           # the last 30 days are never part of their own baseline
    keys = window_keys(d)
    total = full = norm = 0.0
    for p, h in hist.items():
        base = [(k.year, v) for k, v in h.items() if k < cutoff and ((2, 28) if (k.month, k.day) == (2, 29)
                                                                     else (k.month, k.day)) in keys]
        ys = {y for y, _ in base}
        if len(ys) < MIN_YEARS:
            return None
        total += h[d]
        full += max(h.values())
        norm += statistics.median(v for _, v in base)
    return {"date": d, "kaf": total / 1000.0, "pct_full": 100.0 * total / full, "pct_normal": 100.0 * total / norm}


def parse_nwrfc(text: str) -> dict | None:
    """Newest Jan-Jul row of the NWRFC CSV: {issued, p90, p50, p10} in kaf, or None."""
    for line in re.sub(r"<[^>]+>", "\n", text).splitlines():
        f = line.strip().split(",")
        if len(f) >= 12 and f[0] == "TDAO3" and f[3] == "JAN" and f[4] == "JUL":
            # ID, Ensemble Date, Issuance Date, Start, End, Min, 90%, 75%, 50%, 25%, 10%, Max (rows newest first)
            return {"issued": date.fromisoformat(f[2]), "p90": float(f[6]), "p50": float(f[8]), "p10": float(f[10])}
    return None


class ColumbiaHydro(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        m = [SeriesMeta("pnw.hydro.storage.total", self.id,
                        "Columbia US storage, Grand Coulee + Hungry Horse + Dworshak (USBR Hydromet)", "kaf", "D", G),
             SeriesMeta("pnw.hydro.storage.pct_full", self.id,
                        "Columbia US storage, % full (vs each project's 1995+ maximum)", "%", "D", G),
             SeriesMeta("pnw.hydro.storage.pct_normal", self.id,
                        "Columbia US storage, % of normal for the date (median +/-7 d; 1995+, Dworshak 2013+)", "%", "D", G)]
        m += [SeriesMeta(f"pnw.hydro.wsf.tdao3.jan_jul.{k}", self.id,
                         f"Jan-Jul runoff forecast at The Dalles, {lbl} (NWRFC ESP)", "kaf", "D", G)
              for k, lbl in (("p50", "median (50 %)"), ("p90", "90 % exceedance (low)"), ("p10", "10 % exceedance (high)"))]
        m.append(SeriesMeta("pnw.hydro.wsf.tdao3.jan_jul.pct_avg", self.id,
                            "Jan-Jul runoff forecast at The Dalles, median as % of the 1991-2020 average", "%", "D", G))
        return m

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        today = ctx.now.date()
        try:
            text = ctx.get(HYDROMET, params={"list": ",".join(f"{p} af" for p in PROJECTS), "start": "1995-01-01",
                                             "end": today.isoformat(), "format": "csv"}, timeout=90).text
            s = storage_summary(parse_hydromet(text), today)
            if s is None:
                res.warnings.append("storage: no recent common date or under 10 years of history: NO-RESULT")
            else:
                ts = datetime(s["date"].year, s["date"].month, s["date"].day)
                res.obs += [Obs("pnw.hydro.storage.total", ts, round(s["kaf"], 1), ts),
                            Obs("pnw.hydro.storage.pct_full", ts, round(s["pct_full"], 1), ts),
                            Obs("pnw.hydro.storage.pct_normal", ts, round(s["pct_normal"], 1), ts)]
        except Exception as e:  # one source failing must not drop the other
            res.warnings.append(f"storage: {type(e).__name__}: {e}")
        try:
            w = parse_nwrfc(ctx.get(NWRFC, params={"id": "TDAO3", "per": "JAN-JUL"}).text)
            if w is None:
                res.warnings.append("water supply: no Jan-Jul row for TDAO3: NO-RESULT")
            else:
                ts = datetime(w["issued"].year, w["issued"].month, w["issued"].day)
                res.obs += [Obs(f"pnw.hydro.wsf.tdao3.jan_jul.{k}", ts, round(w[k], 1), ts) for k in ("p50", "p90", "p10")]
                res.obs.append(Obs("pnw.hydro.wsf.tdao3.jan_jul.pct_avg", ts, round(100.0 * w["p50"] / JAN_JUL_AVG_KAF, 1), ts))
        except Exception as e:
            res.warnings.append(f"water supply: {type(e).__name__}: {e}")
        if not res.obs:
            raise RuntimeError(f"Columbia hydro: nothing from either source: {'; '.join(res.warnings)[:300]}")
        return res
