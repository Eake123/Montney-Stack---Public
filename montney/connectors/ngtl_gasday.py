"""NGTL gas day boundaries, and the clocks TC and Enbridge publish in.

NGTL gas day D starts at 08:00 Mountain STANDARD Time on day D, which is a fixed 15:00 UTC
year-round, and ends 24 h later. Source: TC Energy bulletin "REMINDER: Alberta Time Change
Effective November 1, 2026" (NGTL, id 3374725064, published 2026-10-28 20:55 UTC; original id
3374724959, 2026-09-22), https://my.tccustomerexpress.com/#/Bulletin/3374725064:

    "There are no planned changes to the NGTL Tariff and therefore the gas day will continue to
     begin and end at 08:00 Mountain Standard Time"
    "Supply Demand balancing deadline remains at 10:30 MST (11:30 ABT)"

Alberta itself moves to permanent UTC-6 ("Alberta Time", ABT) on 2026-11-01. From then on the
NGTL gas day starts at 09:00 Alberta local time. It is still 15:00 UTC, unchanged.

The same bulletin says Foothills and the TC Mainline gas day stays 09:00 Central Clock Time.
NAESB deadlines stay in CCT, which observes US daylight saving: "Alberta Time = CCT during the
winter months (Nov to Mar); Alberta Time = CCT minus 1 hour during the summer months". CCT is
therefore America/Chicago, not a fixed offset. Enbridge's Westcoast postings are also stamped in
CCT: on 2026-10-31 the newest posted cycle was 14:30 at about 20:10 UTC, which only fits UTC-5.

Clocks of the feeds we read:
  TC CSR lastUpdated, GDSR asofDay   Alberta wall clock (America/Edmonton). Verified 2026-10-31:
                                     a row stamped 14:29:44 appeared at 20:29 UTC.
  TC DOP / bulletins *Utc fields     UTC
  Enbridge infopost Posted/Eff times CCT (America/Chicago)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MST = timezone(timedelta(hours=-7), "MST")
GAS_DAY_START_MST_HOUR = 8
FOOTHILLS_GAS_DAY_START_CCT_HOUR = 9  # Foothills (and TC Mainline): 09:00 Central Clock Time
ALBERTA = ZoneInfo("America/Edmonton")
CCT = ZoneInfo("America/Chicago")
BULLETIN_URL = "https://my.tccustomerexpress.com/#/Bulletin/3374725064"


def gas_day_start_utc(d: date) -> datetime:
    """Start of NGTL gas day `d` as naive UTC: 08:00 MST = 15:00 UTC, every day of the year."""
    return datetime(d.year, d.month, d.day, GAS_DAY_START_MST_HOUR, tzinfo=MST) \
        .astimezone(timezone.utc).replace(tzinfo=None)


def foothills_gas_day_start_utc(d: date) -> datetime:
    """Start of Foothills gas day `d`: 09:00 CCT (America/Chicago, observes US DST), so 14:00 UTC
    in summer and 15:00 UTC in winter. Same bulletin as above."""
    return datetime(d.year, d.month, d.day, FOOTHILLS_GAS_DAY_START_CCT_HOUR, tzinfo=CCT) \
        .astimezone(timezone.utc).replace(tzinfo=None)


def gas_day_of(ts_utc: datetime) -> date:
    """The NGTL gas day containing a naive-UTC instant."""
    return (ts_utc - timedelta(hours=15)).date()
