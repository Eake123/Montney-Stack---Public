"""Midwest gas demand, computed (L-101 part 2, Erik: "you should be able to calculate gas demand").

Pipeline citygate deliveries aren't demand in the Midwest: on NGPL, Nicor + Peoples + North Shore took ~1.0-1.2 Bcf/d all
of Aug-Oct 2026, when Chicago heats nothing (Nicor's aquifer storage fills behind the gate), and Michigan's storage is
larger still. So the headline is a **weather model** fitted to EIA's monthly state deliveries, and the citygate number
is a row labelled as such.

1. **Weather demand** (`gas_midwest_weather`, ONE Open-Meteo forecast request a run for Chicago, Milwaukee and Detroit,
   every 6 h): per state, daily demand = base + h × HDD + c × CDD, with HDD / CDD in °C-days about 18.3 °C (65 °F) from
   the city's daily mean temperature (yesterday and today, local days). The cooling term carries summer power burn.
   FIT holds the coefficients from a one-off least-squares fit of EIA's monthly "delivered to consumers" (N3060IL2,
   N3060WI2, N3060MI2; MMcf/month = base × days + h × HDD_month + c × CDD_month) against Open-Meteo's archive (ERA5)
   temperatures, Sep 2021 - Aug 2026 (60 months; 59 for WI, whose June 2026 is NA). No history is stored or re-fetched.
   Back-test (monthly, the fit window): 90th-percentile |miss| IL 14 %, WI 12 %, MI 14 %, the band. It is seasonal:
   Nov-Mar IL 6 %, WI 6 %, MI 9 %; May-Sep IL 18 %, WI 14 %, MI 22 % (power burn and industrial load swing more than
   cooling degree days explain). Daily values swing more than monthly ones (weekends, wind, the lag of cold snaps).
   One city stands in for each state: Detroit for Michigan, Milwaukee for Wisconsin, Chicago for Illinois.
2. **Chicago citygate** (`gas_ngpl_chicago`, TWO requests a run, every 6 h): Kinder Morgan's public Operationally
   Available by point page for NGPL (no login, no captcha): the page, then its Excel download (~40 KB) for delivery
   points, today's gas day, the latest posted cycle. `ngpl.chicago.citygate_deliveries` = the NICORGAS + PGLC (Peoples)
   + NO SHORE (North Shore) "CDP" aggregates' scheduled quantity. NGPL only (ANR, Northern Border, Alliance, Horizon …
   also deliver to them), and it includes their storage injection. Dth at 1,037 Btu/cf.

Units MMcf/d. The weather days are local calendar days (the cities' own time zones via Open-Meteo `timezone=auto`).
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from datetime import datetime

from montney.connectors.base import Connector, Context, FetchResult
from montney.connectors.gas_common import FormatDrift, local_to_utc
from montney.connectors.pnw_gas import BTU_PER_CF, CENTRAL, num
from montney.store import Obs, SeriesMeta

GROUP = "Midwest: demand (est. from weather)"
FORECAST = "https://api.open-meteo.com/v1/forecast"
BASE_C = 18.3                       # 65 °F
# state -> (city, lat, lon, base MMcf/d, MMcf/d per HDD °C-day, MMcf/d per CDD °C-day, band, Nov-Mar, May-Sep)
FIT = {"il": ("Chicago", 41.837045, -87.684939, 1042.7, 183.66, 190.68, 0.14, 0.064, 0.18),
       "wi": ("Milwaukee", 43.0642, -87.9673, 718.2, 74.45, 114.34, 0.12, 0.059, 0.14),
       "mi": ("Detroit", 42.3834, -83.1024, 1358.9, 140.99, 147.29, 0.14, 0.086, 0.22)}
STATE = {"il": "Illinois", "wi": "Wisconsin", "mi": "Michigan"}
NGPL_URL = "https://pipeline2.kindermorgan.com/Capacity/OpAvailPoint.aspx?code=NGPL"
NGPL_LDC = ("NICORGAS/NGPL CDP", "PGLC/NGPL CDP", "NO SHORE/NGPL CDP")
CITYGATE = "ngpl.chicago.citygate_deliveries"


def sid(state: str) -> str:
    return f"demand.{state}.weather_est"


def weather_demand(state: str, t_mean: float) -> float:
    _, _, _, base, h, c, *_ = FIT[state]
    return base + h * max(0.0, BASE_C - t_mean) + c * max(0.0, t_mean - BASE_C)


class GasMidwestWeather(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        out = []
        for st, (city, *_, band, win, summ) in FIT.items():
            out.append(SeriesMeta(sid(st), self.id, f"{STATE[st]} gas demand, est. from weather ({city} temperature; "
                                  f"±{band:.0%}: ±{win:.0%} Nov-Mar, ±{summ:.0%} May-Sep, monthly back-test against "
                                  "EIA)", "MMcf/d", "D", GROUP, extra={"band": band}))
        return out

    def fetch(self, ctx: Context) -> FetchResult:
        r = ctx.get(self.spec.get("params", {}).get("url", FORECAST), params={
            "latitude": ",".join(str(v[1]) for v in FIT.values()),
            "longitude": ",".join(str(v[2]) for v in FIT.values()),
            "daily": "temperature_2m_mean", "past_days": 1, "forecast_days": 1, "timezone": "auto"})
        try:
            j = r.json()
            locs = j if isinstance(j, list) else [j]
            if len(locs) != len(FIT):
                raise ValueError(f"{len(locs)} locations, expected {len(FIT)}")
        except ValueError as e:
            raise FormatDrift(f"Open-Meteo forecast: not the expected JSON ({e})") from e
        res = FetchResult(meta=self.declared_series())
        for st, loc in zip(FIT, locs):
            try:
                days, temps = loc["daily"]["time"], loc["daily"]["temperature_2m_mean"]
            except (KeyError, TypeError) as e:
                raise FormatDrift(f"Open-Meteo forecast: no daily temperature_2m_mean for {FIT[st][0]} ({e})") from e
            for d, t in zip(days, temps):
                if t is None:
                    res.warnings.append(f"{FIT[st][0]} {d}: no mean temperature")
                    continue
                ts = datetime.strptime(d, "%Y-%m-%d")
                # as_of = the day: today's value is re-stamped each run until the day is over (a daily actual)
                res.obs.append(Obs(sid(st), ts, weather_demand(st, float(t)), ts))
        return res


# ------------------------------------------------------------------ NGPL (Kinder Morgan) Chicago citygate
def xlsx_rows(content: bytes) -> list[dict[str, str]]:
    """First sheet of an .xlsx as [{column letter: text}], shared strings resolved (stdlib only)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        ss = [html.unescape(re.sub(r"<[^>]+>", "", si)) for si in
              re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8"), re.S)]
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        raise FormatDrift(f"NGPL download: not the expected .xlsx ({e})") from e
    out = []
    for row in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        cells = {}
        for ref, attrs, body in re.findall(r'<c r="([A-Z]+)\d+"([^>]*)>(.*?)</c>', row, re.S):
            v = re.search(r"<v>(.*?)</v>", body)
            if v is not None:
                cells[ref] = (ss[int(v.group(1))] if 't="s"' in attrs else html.unescape(v.group(1))).strip()
        out.append(cells)
    return out


def ngpl_form(page: str) -> dict[str, str]:
    """The OA page's postback fields (checked radios only: Delivery points is the default) + the Download button."""
    data: dict[str, str] = {}
    for m in re.finditer(r"<input[^>]+>", page):
        tag = m.group(0)
        n, v = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if not n or 'type="image"' in tag or 'type="submit"' in tag or ('type="radio"' in tag and "checked" not in tag):
            continue
        data[html.unescape(n.group(1))] = html.unescape(v.group(1)) if v else ""
    for m in re.finditer(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        sel = re.search(r'<option selected="selected" value="([^"]*)"', m.group(2))
        data[html.unescape(m.group(1))] = sel.group(1) if sel else ""
    btn = next((html.unescape(b) for b in re.findall(r'name="([^"]*btnDownload)"', page)), None)
    if btn is None or not any(v == "rbDelivery" for v in data.values()):
        raise FormatDrift("NGPL OA page: no Download button or no Delivery-points option")
    data[btn + ".x"], data[btn + ".y"] = "10", "10"
    data["ctl00$hdnIsDownload"] = "true"
    return data


def ngpl_citygate(rows: list[dict[str, str]]) -> tuple[datetime, datetime, str, float]:
    """(gas day, posted UTC, cycle, Nicor + Peoples + North Shore scheduled Dth) from the download's rows."""
    if len(rows) < 4 or rows[2].get("B") != "Loc Name" or rows[2].get("G") != "Total Scheduled Quantity":
        raise FormatDrift(f"NGPL download: header moved ({rows[2] if len(rows) > 2 else rows})")
    head = rows[1]
    if "Delivery" not in head.get("E", ""):
        raise FormatDrift(f"NGPL download: {head.get('E')!r}, not delivery points")
    try:
        day = datetime.strptime(head["C"].split()[0], "%m/%d/%Y")
        posted = local_to_utc(datetime.strptime(head["G"], "%m/%d/%Y %I:%M %p"), CENTRAL)
    except (KeyError, ValueError) as e:
        raise FormatDrift(f"NGPL download: bad gas day / post time ({e})") from e
    q = {r.get("B", ""): num(r.get("G", "0")) for r in rows[3:]}
    missing = [n for n in NGPL_LDC if n not in q]
    if missing:
        raise FormatDrift(f"NGPL download: no {', '.join(missing)}")
    return day, posted, head.get("D", ""), sum(q[n] for n in NGPL_LDC)


class GasNgplChicago(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(CITYGATE, self.id, "Chicago LDC citygate deliveries off NGPL: Nicor + Peoples + North Shore "
                           "(scheduled; includes their storage injection, so not demand)", "MMcf/d", "D", GROUP,
                           day="gas")]

    def fetch(self, ctx: Context) -> FetchResult:
        url = self.spec.get("params", {}).get("url", NGPL_URL)
        page = ctx.get(url, timeout=120).text
        r = ctx.post(url, data=ngpl_form(page), timeout=180)
        day, posted, cycle, dth = ngpl_citygate(xlsx_rows(r.content))
        res = FetchResult(meta=self.declared_series())
        res.obs.append(Obs(CITYGATE, day, dth / BTU_PER_CF, posted))
        res.warnings += [] if cycle else ["NGPL download: no cycle in the header"]
        return res
