"""Quebec hydro water at a glance (L-091, Erik: "once a day kinda thing"): is the north wet or dry vs normal?
Numbers only, no market call.

**No public reservoir storage exists for Quebec** (checked 2026-11-03): Hydro-Quebec's open data carries no storage,
% full or energy-reserve dataset, and its reservoir levels/inflows keep only a rolling 10 days (no normal possible); the
Regie de l'energie's "etat des reserves" (TWh) is a PDF twice a year. WSC has no real-time data in northern Quebec.
So the box uses the best proxy, labelled as such: **natural inflow** from 9 large UNREGULATED rivers ("regime non
influence") of the provincial network (MELCCFP, formerly CEHQ), spread over the James Bay, Ungava and Cote-Nord basins
that feed HQ's reservoirs:
    qc.hydro.inflow.pct_normal          % of normal: sum of the current flows / sum of their normals (>= 7 of 9 rivers)
    qc.hydro.<stn>.discharge            m3/s, the latest reading
    qc.hydro.<stn>.discharge_pct_normal %

Two requests:
1. Daily: the province's public station layer as ONE CSV (geoegl.msp.gouv.qc.ca vigilance WFS, CC BY 4.0), latest
   value per station; `dern_date_prise_valeur_utc` is UTC. A reading older than 48 h is left out.
2. Every 30 days, per river: the daily-mean history file (cehq.gouv.qc.ca historique_donnees, ~1 MB, 1960s ->
   provisional to a few days ago), cached on disk. Normal = the median of the daily means within the date +/-7 d,
   1995 -> 30 days ago (as weather_flow_normal). NO-RESULT under 10 years.
Current = a provisional instantaneous reading; normal = daily means (validated, then provisional for the last year).

Degraded mode (2026-11-13): the station layer has answered every request since 2026-11-11 ~00:30 UTC with an OWS
ExceptionReport ("msPostGISLayerGetItems(): Query error", HTTP 200; every layer of that WFS, from the PC and from kraken's
VPN alike). When it's down, or has under 7 fresh rivers, the values come from each river's latest PROVISIONAL DAILY MEAN
in its CEHQ file instead (regenerated about daily, 1-2 days behind), dated that day, with a warning saying so. The files
are cached raw and re-checked with If-Modified-Since (a 304 costs no body). If neither source has a value within 4 days,
nothing is written and the box goes stale on its own: a warning, never a failed run.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
import time
from datetime import date, datetime, time as dtime, timedelta

from montney import paths
from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.weather_flow_normal import alberta_date
from montney.connectors.weather_percentile import window_keys
from montney.store import Obs, SeriesMeta

VIGILANCE = "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi"
HISTORY = "https://www.cehq.gouv.qc.ca/depot/historique_donnees/fichier/{stn}_Q.txt"
# station: (river, basin). All "regime non influence" (unregulated), records from 1953-1994.
RIVERS: dict[str, tuple[str, str]] = {
    "093801": ("Grande riviere de la Baleine", "Hudson Bay / James Bay"),
    "080809": ("Broadback", "James Bay"),
    "104001": ("Riviere a la Baleine", "Ungava Bay"),
    # Pyrite Falls measures the undiverted residual to Ungava Bay: HQ's Caniapiscau diversion cut its catchment by
    # 39,089 km2 on 1981-10-25 (CEHQ file note); the 1995+ normals are all post-diversion, so the % is like for like.
    "103702": ("Caniapiscau at Pyrite Falls (below the diversion, to Ungava Bay)", "Ungava Bay"),
    "072301": ("Moisie", "Cote-Nord"),
    "074903": ("Natashquan", "Cote-Nord"),
    "075705": ("Petit Mecatina", "Cote-Nord"),
    "061901": ("Ashuapmushuan", "Saguenay - Lac-Saint-Jean"),
    "062102": ("Mistassini", "Saguenay - Lac-Saint-Jean"),
}
MIN_RIVERS = 7
MAX_AGE = timedelta(hours=48)
HIST_START = date(1995, 1, 1)
HIST_MAX_AGE = 30 * 86400                 # the normals' copy of each file: re-checked monthly
FALLBACK_REFRESH_S = 12 * 3600            # while the station layer is down: re-checked twice a day (304 when unchanged)
FALLBACK_MAX_AGE = timedelta(days=4)      # a daily mean older than this isn't "current"
MIN_YEARS = 10
G = "Rivers / Quebec hydro"


def parse_vigilance(text: str) -> dict[str, tuple[datetime, float]]:
    """{station: (naive UTC, m3/s)} for our rivers from the station-layer CSV (blank flow cells skipped)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        s, q, t = r.get("station"), (r.get("dern_valeur_deb") or "").strip(), (r.get("dern_date_prise_valeur_utc") or "")
        if s in RIVERS and q and t.strip():
            out[s] = (datetime.strptime(t.strip(), "%Y/%m/%d %H:%M:%S"), float(q))
    return out


def parse_history(text: str, stn: str) -> dict[date, float]:
    """{date: m3/s} from a CEHQ daily-mean file (fixed-width: station, YYYY/MM/DD, flow, remark; blank flow = missing)."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] == stn and len(f[1]) == 10 and f[1][4] == "/":
            try:
                out[date(int(f[1][:4]), int(f[1][5:7]), int(f[1][8:10]))] = float(f[2])
            except ValueError:
                continue                        # a remark where the value would be: missing
    return out


def median_for(hist: dict[date, float], day: date) -> float | None:
    keys = window_keys(day)
    vals = [(d.year, v) for d, v in hist.items()
            if ((2, 28) if (d.month, d.day) == (2, 29) else (d.month, d.day)) in keys]
    if len({y for y, _ in vals}) < MIN_YEARS:
        return None
    return statistics.median(v for _, v in vals)


class QuebecHydro(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        m = [SeriesMeta("qc.hydro.inflow.pct_normal", self.id,
                        "Quebec natural inflow index (proxy, no public storage): 9 unregulated northern rivers, "
                        "% of normal for the date (median +/-7 d, 1995+)", "%", "D", G)]
        for s, (river, _) in RIVERS.items():
            m += [SeriesMeta(f"qc.hydro.{s}.discharge", self.id,
                             f"{river} discharge, latest reading (or the latest provisional daily mean while the station "
                             f"layer is down) (MELCCFP {s})", "m3/s", "D", G),
                  SeriesMeta(f"qc.hydro.{s}.discharge_pct_normal", self.id,
                             f"{river} discharge, % of normal for the date (median +/-7 d, 1995+)", "%", "D", G)]
        return m

    def history_text(self, ctx: Context, stn: str, refresh_s: float) -> str:
        """The river's CEHQ daily-mean file, from a raw disk cache. Re-checked once the copy is older than `refresh_s`,
        with If-Modified-Since: the server answers 304 (no body) when the file hasn't been regenerated (about daily)."""
        d = paths.home() / "cache"
        raw, lm = d / f"cehq_{stn}_q.txt", d / f"cehq_{stn}_q.lastmod"
        if raw.exists() and time.time() - raw.stat().st_mtime < refresh_s:
            return raw.read_text(encoding="latin-1")
        h = {"If-Modified-Since": lm.read_text().strip()} if raw.exists() and lm.exists() else {}
        r = ctx.get(HISTORY.format(stn=stn), headers=h, timeout=90, allow=(304,))
        if r.status_code == 304 and raw.exists():
            raw.touch()
            return raw.read_text(encoding="latin-1")
        d.mkdir(parents=True, exist_ok=True)
        text = r.content.decode("latin-1")
        raw.write_text(text, encoding="latin-1")
        if r.headers.get("last-modified"):
            lm.write_text(r.headers["last-modified"])
        return text

    def station_layer(self, ctx: Context) -> tuple[dict[str, tuple[datetime, float]], str | None]:
        """(latest readings, None), or ({}, why the layer gave none). The WFS reports its own failures as HTTP 200 with an
        OWS ExceptionReport (2026-11-11 on: "msPostGISLayerGetItems(): Query error"), so the body is checked too."""
        try:
            text = ctx.get(VIGILANCE, params={"service": "wfs", "version": "1.1.0", "request": "getfeature",
                                              "typename": "stations_igo2_public", "outputformat": "CSV"},
                           timeout=60).content.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - reported, then the fallback runs
            return {}, f"{type(e).__name__}: {e}"
        if "ExceptionReport" in text[:600]:
            m = re.search(r"<ows:ExceptionText>(.*?)</ows:ExceptionText>", text, re.S)
            return {}, f"the WFS answered an error: {(m.group(1) if m else text[:200]).strip()}"
        if not text.lstrip().startswith("station"):
            return {}, f"not the station CSV: {text[:120]!r}"
        return parse_vigilance(text), None

    def fetch(self, ctx: Context) -> FetchResult:
        res = FetchResult(meta=self.declared_series())
        now = ctx.now.replace(tzinfo=None)
        today = alberta_date(now)
        cur, why = self.station_layer(ctx)
        fresh = {s: v for s, v in cur.items() if now - v[0] <= MAX_AGE}
        hists: dict[str, dict[date, float]] = {}
        if len(fresh) >= MIN_RIVERS:
            if len(fresh) < len(RIVERS):
                res.warnings.append(f"no reading in {MAX_AGE.total_seconds() / 3600:g} h for "
                                    f"{sorted(set(RIVERS) - set(fresh))}")
            # freq D: dated at 00:00 of the Alberta calendar day of the latest reading, as every daily series (the
            # snapshot cuts daily series at 00:00 of today's Alberta date, so a reading-hour stamp would read as future).
            day = alberta_date(max(t for t, _ in fresh.values()))
            values = {s: q for s, (_, q) in fresh.items()}
            for s in values:
                try:
                    hists[s] = parse_history(self.history_text(ctx, s, HIST_MAX_AGE), s)
                except Exception as e:  # noqa: BLE001 - one history file failing drops only that river's %
                    res.warnings.append(f"{s} history: {type(e).__name__}: {e}")
        else:
            # Fallback: the station layer is down or thin, so use each river's latest provisional daily mean from its
            # CEHQ file (regenerated about daily, 1-2 days behind): the newest day that MIN_RIVERS+ rivers share.
            res.warnings.append(f"station layer: {why or f'only {len(fresh)} rivers fresh'}; using the CEHQ provisional "
                                f"daily means instead")
            for s in RIVERS:
                try:
                    hists[s] = parse_history(self.history_text(ctx, s, FALLBACK_REFRESH_S), s)
                except Exception as e:  # noqa: BLE001
                    res.warnings.append(f"{s} history: {type(e).__name__}: {e}")
            days = sorted({d for h in hists.values() for d in h if d >= today - FALLBACK_MAX_AGE}, reverse=True)
            day = next((d for d in days if sum(d in h for h in hists.values()) >= MIN_RIVERS), None)
            if day is None:
                res.warnings.append(f"no daily mean in the last {FALLBACK_MAX_AGE.days} days for {MIN_RIVERS}+ rivers "
                                    f"either: nothing written (the box goes stale)")
                return res
            values = {s: h[day] for s, h in hists.items() if day in h}
            res.warnings.append(f"values are the provisional daily means of {day} for {len(values)} rivers")
        ts = datetime.combine(day, dtime())
        cutoff = today - timedelta(days=30)              # the last 30 days are never part of their own baseline
        used_q = used_n = 0.0
        used = []
        for s, q in values.items():
            res.obs.append(Obs(f"qc.hydro.{s}.discharge", ts, round(q, 1), ts))
            if s not in hists:
                continue
            nm = median_for({d: v for d, v in hists[s].items() if HIST_START <= d < cutoff}, day)
            if not nm:
                res.warnings.append(f"{s}: under {MIN_YEARS} years of history: NO-RESULT")
                continue
            res.obs.append(Obs(f"qc.hydro.{s}.discharge_pct_normal", ts, round(100.0 * q / nm, 1), ts))
            used_q, used_n = used_q + q, used_n + nm
            used.append(s)
        if len(used) >= MIN_RIVERS:
            res.obs.append(Obs("qc.hydro.inflow.pct_normal", ts, round(100.0 * used_q / used_n, 1), ts))
            if len(used) < len(RIVERS):
                res.warnings.append(f"inflow index from {len(used)} of {len(RIVERS)} rivers: {used}")
        else:
            res.warnings.append(f"inflow index: only {len(used)} rivers with a normal (< {MIN_RIVERS}): NO-RESULT")
        return res
