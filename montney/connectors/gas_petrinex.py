"""Petrinex Alberta: monthly sales gas per gas plant / gathering system (PLAN 5a, "Petrinex facility list + volumetrics").

Public data API (no auth; docs/research/v2/gas-petrinex.md §1). Each response is a zip that contains a zip that contains a CSV:
  GET https://www.petrinex.gov.ab.ca/publicdata/API/Files/AB/Vol/{YYYY-MM}/CSV          conventional volumetrics, ~7.7 MB
  GET https://www.petrinex.gov.ab.ca/publicdata/API/Files/AB/Infra/Facility%20Infrastructure/CSV   facility list, ~7.8 MB
An unpublished month answers 404 {"Message":"Requested resource doesn't exist."}, and is skipped.

**What we keep (aggregated on ingest; the raw files are streamed and never saved, per PLAN P3):** for each reporting
facility and month, the **masked sales-gas disposition**:
  ActivityID = DISP, ProductID = GAS, FromToID = "***", ReportingFacilitySubType != 406
- Petrinex hides the target of every DISP to a pipeline meter station ("***"), so this row is the facility's total gas
  sent to pipelines.
- Straddle plants (subtype 406) re-extract mainline gas and send it back, so they're excluded. Otherwise the gas is
  counted twice.
- In Aug 2026: 655 facilities, 12,170.1 MMcf/d. P2 counted 658, but 3 of those only have a masked volume.

Series (freq M, ts = the first of the month, MMcf/d = 10³m³ × 0.0353147 ÷ days):
  petrinex.<facility id, lower>.sales_gas     e.g. petrinex.abgp0001892.sales_gas (AEC Saddle Hills)
SeriesMeta.extra carries {facility_id, name, subtype, operator, location (LSD), status} from the facility list. The
same names/operators/locations go to <home>/facilities/ab.json for /api/top-producers (montney/top_producers.py).
  petrinex.ab.sales_to.<group>                that month's sales gas by the pipeline system of each facility's nearest
                                              meter station (L-080 item 4; montney/connectors/petrinex_destination.py)
  production.ab.monthly                       AB production: the month's total over every facility **except gas
                                              storage** (STORAGE below), MMcf/d (PLAN 4b item 4)

**Storage is not production.** AB storage plants report their winter withdrawals as the same masked sales DISP, which
put ~2 Bcf/d of storage into "sales gas" every winter (Jan 2026: 14,315 with storage, 12,569 without). STORAGE is the
12 facilities whose Jun-Aug mean is < 10 % of their Dec-Feb mean with a winter mean ≥ 15 MMcf/d, over Jun 2023-Sep 2026
(AECO Suffield and Countess, Carbon, CrossAlta, Big Eddy x2, Edson/Iberdrola, Hussar x2, Warwick, Dimsdale, Bassano).
Every one of them is a known storage site or a facility named for one. A producing plant's own downtime never looks like
this (the next on the list, AAV Glacier, is 366 vs 278).

Revisions: Petrinex reissues whole months (the trailing ~4 are re-cut every month), so each fetch re-reads the last
`months_back` published months. as_of = the fetch day: a report vintage, and change-only writes keep only values that moved.

Fails loudly (FormatDrift: nothing stored) if:
- a CSV lacks the expected columns;
- no month is published in the window;
- a month's total sales gas is outside 6,000-20,000 MMcf/d (a unit or filter break; Aug 2026 was 12,170).

Params:
  months_back  trailing published months to read (default 4)
  from_month   history load: read every month from this one, e.g. "2022-01" (montney/importers/gas_petrinex.py)
Terms: Petrinex/AER non-commercial use with Crown copyright acknowledged; personal use (L-006). Never redistributed.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

from montney.connectors.base import Connector, Context, FetchResult, local_today
from montney.connectors import petrinex_destination as dest_
from montney.connectors.gas_common import E3M3_TO_MMCF, FormatDrift, read_stored
from montney.store import Obs, SeriesMeta
from montney.top_producers import save_registry

API = "https://www.petrinex.gov.ab.ca/publicdata/API/Files/AB"
GROUP = "Petrinex AB / plant sales gas"
STRADDLE = "406"
MASKED_KEY = "_masked_rows"
TOTAL_MMCFD = (6000.0, 20000.0)
VOL_COLS = {"ProductionMonth", "ReportingFacilityID", "ReportingFacilitySubType", "ActivityID", "ProductID", "FromToID",
            "Volume"}
INFRA_COLS = {"FacilityID", "FacilityName", "FacilitySubTypeDesc", "FacilityLocation", "OperatorName", "OperatorBAID",
              "FacilityOperationalStatus"}
# Last-month context bands (DECISIONS P5a: production by plant/operator is never "live"). Petrinex runs ~2 months behind
# "now", so the band is the P90 of |month M+2 / month M − 1|, measured on Jan-Sep 2026. A plant that stops reporting
# at M+2 counts as −100 % (Reviewer on #174: leaving those out had given 33 %, n 862):
# - plants ≥ 20 MMcf/d: P90 31 % (n 849). It was 37 % (n 877; #174) with the STORAGE sites in, whose summer zeros
#   looked like vanished plants (Reviewer on #207). Plants on the map alone: 28 % (n 255);
# - operators ≥ 50 MMcf/d: P90 23 % (n 208), with STORAGE left out of the operator sums too (31 %, n 220, with it).
# These are reported sums, not link-inferred, so the Q7-gated "assumed ±11 %" operator band (which is for production
# attributed through station links) doesn't apply. This band is the measured staleness.
PLANT_BAND, OPERATOR_BAND = 0.31, 0.23
AB_TOTAL = "production.ab.monthly"
STORAGE = frozenset({"ABGS0003410", "ABGS0077521", "ABGP0001014", "ABGP0119754", "ABGS0095087", "ABGP0086640",
                     "ABGP0131712", "ABIF0009315", "ABGS0004873", "ABGS0113648", "ABGS0004155", "ABGP0001177"})


def series_id(facility: str) -> str:
    return f"petrinex.{facility.lower()}.sales_gas"


# Facility series exist only once fetched; the map's network test accepts refs matching this (tests/test_gas_network.py).
SERIES_PATTERN = re.compile(r"^petrinex\.(ab[a-z]{2}\d{7}|operator\.[a-z0-9]{4})\.sales_gas$")


def operator_series_id(baid: str) -> str:
    return f"petrinex.operator.{baid.lower()}.sales_gas"


def inner_csv(payload: bytes):
    """The CSV inside Petrinex's zip-in-zip, as a text stream (never written to disk)."""
    outer = zipfile.ZipFile(io.BytesIO(payload))
    inner_name = next(n for n in outer.namelist() if n.lower().endswith(".zip"))
    inner = zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))
    csv_name = next(n for n in inner.namelist() if n.lower().endswith(".csv"))
    return io.TextIOWrapper(inner.open(csv_name), encoding="utf-8", errors="replace", newline="")


def sales_by_facility(stream) -> dict[str, float]:
    """{facility id: masked sales-gas DISP in 10³m³} for one month's volumetrics CSV."""
    rdr = csv.DictReader(stream)
    if not VOL_COLS <= set(rdr.fieldnames or []):
        raise FormatDrift(f"Petrinex volumetrics: expected columns {sorted(VOL_COLS)}")
    out: dict[str, float] = defaultdict(float)
    masked = 0
    for r in rdr:
        if (r["ActivityID"] == "DISP" and r["ProductID"] == "GAS" and r["FromToID"] == "***"
                and r["ReportingFacilitySubType"] != STRADDLE and r["Volume"]):
            try:
                v = float(r["Volume"])   # parse first: a masked volume must not leave a 0 entry behind
            except ValueError:          # the volume itself is masked ("***", confidential): not counted
                masked += 1
                continue
            out[r["ReportingFacilityID"]] += v
    out = dict(out)
    out[MASKED_KEY] = masked
    return out


def facility_list(stream, wanted: set[str]) -> dict[str, dict]:
    rdr = csv.DictReader(stream)
    if not INFRA_COLS <= set(rdr.fieldnames or []):
        raise FormatDrift(f"Petrinex facility infrastructure: expected columns {sorted(INFRA_COLS)}")
    return {r["FacilityID"]: {"facility_id": r["FacilityID"], "name": r["FacilityName"].strip(),
                              "subtype": r["FacilitySubTypeDesc"].strip(), "operator": r["OperatorName"].strip(),
                              "operator_baid": r["OperatorBAID"].strip(),
                              "location": r["FacilityLocation"].strip(), "status": r["FacilityOperationalStatus"].strip()}
            for r in rdr if r["FacilityID"] in wanted}


def months_to_read(today: date, months_back: int, from_month: str | None) -> list[date]:
    """Candidate months, oldest first, up to this month. Unpublished ones 404 and are skipped. The trailing window
    starts months_back + 1 months back, because the newest published month is usually last month or the one before."""
    last = today.year * 12 + today.month - 1
    if from_month:
        f = datetime.strptime(from_month, "%Y-%m")
        first = f.year * 12 + f.month - 1
    else:
        first = last - months_back - 1
    return [date(i // 12, i % 12 + 1, 1) for i in range(first, last + 1)]


def mmcfd(e3m3: float, m: date) -> float:
    return e3m3 * E3M3_TO_MMCF / calendar.monthrange(m.year, m.month)[1]


class GasPetrinex(Connector):
    def declared_series(self) -> list[SeriesMeta]:
        return [SeriesMeta(AB_TOTAL, self.id, "Alberta gas production, monthly (Petrinex sales gas, storage excluded)",
                           "MMcf/d", "M", "Gas production / totals")]

    def _destinations(self, ctx, res, infra: bytes, facs: dict, seen: set, as_of: datetime) -> None:
        """L-080 item 4: each month's sales gas by the pipeline system of each facility's nearest meter station
        (petrinex.ab.sales_to.<group>, MMcf/d; montney/connectors/petrinex_destination.py). Storage sites are left out.
        ATCO Pipelines stations that TC counts in NGTL's receipts (stored ngtl.station.<n>.receipt) are atco_in_ngtl."""
        try:
            ms = dest_.meter_stations(inner_csv(infra))
            near = dest_.nearest({f: facs[f].get("location", "") for f in seen if f in facs and f not in STORAGE}, ms)
            atco = {dest_.station_number(m) for m, pipe, _, _ in ms if "ATCO PIPELINE" in pipe.upper()} - {None}
            tc: set[int] = set()
            if atco:
                try:
                    st = self.stored([f"ngtl.station.{n}.receipt" for n in sorted(atco)],
                                     ctx.now - timedelta(days=200), ctx.now)
                    tc = {n for n in atco if st.get(f"ngtl.station.{n}.receipt")}
                except Exception as e:  # noqa: BLE001 - attribution still useful; ATCO then counts as off-system
                    res.warnings.append(f"TC station receipts unreadable ({e}): ATCO stations all counted off-NGTL")
        except Exception as e:  # noqa: BLE001
            res.warnings.append(f"destination attribution skipped: {e}")
            return
        sums: dict[tuple[str, datetime], float] = defaultdict(float)
        months = set()
        for o in res.obs:
            if not o.series_id.endswith(".sales_gas") or o.series_id.startswith("petrinex.operator."):
                continue
            fid = o.series_id.split(".")[1].upper()
            if fid in STORAGE:
                continue
            n = near.get(fid)
            sums[(dest_.group_of(n[1], n[0], tc) if n else "unattributed", o.ts)] += o.value
            months.add(o.ts)
        res.obs += [Obs(f"petrinex.ab.sales_to.{g}", m, sums.get((g, m), 0.0), as_of)
                    for m in sorted(months) for g in dest_.ALL_GROUPS]
        res.meta += [SeriesMeta(f"petrinex.ab.sales_to.{g}", self.id, f"AB sales gas to {dest_.GROUP_LABEL[g]} (Petrinex, "
                                "by each facility's nearest meter station)", "MMcf/d", "M", GROUP)
                     for g in dest_.ALL_GROUPS]
        res.warnings.append(f"destinations: {len(ms)} meter stations, {len(near)} facilities placed, "
                            f"{len(tc)} ATCO stations in TC's NGTL receipts")

    stored = staticmethod(read_stored)          # tests inject

    def fetch(self, ctx: Context) -> FetchResult:
        p = self.spec.get("params", {})
        base = p.get("api", API)
        as_of = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
        res = FetchResult()
        seen: set[str] = set()
        read = 0
        for m in months_to_read(local_today(), int(p.get("months_back", 4)), p.get("from_month")):
            r = ctx.get(f"{base}/Vol/{m:%Y-%m}/CSV", timeout=300, retries=0, allow=(404,))
            if r.status_code == 404:
                continue
            r.raise_for_status()
            sales = sales_by_facility(inner_csv(r.content))
            n_masked = int(sales.pop(MASKED_KEY))
            if n_masked:
                res.warnings.append(f"{m:%Y-%m}: {n_masked} sales-gas rows have a masked volume ('***'), not counted")
            total = sum(mmcfd(v, m) for v in sales.values())
            if not TOTAL_MMCFD[0] <= total <= TOTAL_MMCFD[1]:
                raise FormatDrift(f"{m:%Y-%m}: AB masked sales gas {total:,.0f} MMcf/d outside {TOTAL_MMCFD}")
            ts = datetime(m.year, m.month, 1)
            res.obs.extend(Obs(series_id(f), ts, mmcfd(v, m), as_of) for f, v in sales.items())
            res.obs.append(Obs(AB_TOTAL, ts, sum(mmcfd(v, m) for f, v in sales.items() if f not in STORAGE), as_of))
            seen |= set(sales)
            read += 1
        if not read:
            raise FormatDrift("no published Petrinex month in the window (all 404)")
        infra = ctx.get(f"{base}/Infra/Facility%20Infrastructure/CSV", timeout=300, retries=0, allow=(404,))
        infra.raise_for_status()
        facs = facility_list(inner_csv(infra.content), seen)
        res.meta = self.declared_series() + [SeriesMeta(series_id(f), self.id,
                               f"{facs.get(f, {}).get('name') or f} "
                               + ("storage withdrawals (Petrinex; not production)" if f in STORAGE
                                  else "sales gas (Petrinex, last-month context)"), "MMcf/d",
                               "M", GROUP, extra={**facs.get(f, {"facility_id": f}), "band": PLANT_BAND})
                    for f in sorted(seen)]
        self._destinations(ctx, res, infra.content, facs, seen, as_of)
        # Operator totals: the sum of the non-storage facilities each BA *operates*, per month (reported, not link-inferred).
        # The facility operator isn't the gas owner: midstream operators (Pembina, Keyera) process other producers' gas.
        ops: dict[tuple[str, datetime], float] = defaultdict(float)
        op_name: dict[str, str] = {}
        for o in res.obs:
            fid = o.series_id.split(".")[1].upper()
            info = facs.get(fid)
            if info and info.get("operator_baid") and fid not in STORAGE:      # storage withdrawals aren't production
                ops[(info["operator_baid"], o.ts)] += o.value
                op_name[info["operator_baid"]] = info["operator"]
        res.obs.extend(Obs(operator_series_id(b), ts, v, as_of) for (b, ts), v in sorted(ops.items()))
        res.meta += [SeriesMeta(operator_series_id(b), self.id, f"Sales gas through {op_name[b]}-operated AB facilities "
                                f"(Petrinex, last-month context)", "MMcf/d", "M", GROUP,
                                extra={"operator_baid": b, "operator": op_name[b], "band": OPERATOR_BAND})
                     for b in sorted(op_name)]
        # L-075: the top-producers popup's facility registry (names, operators, locations; the store doesn't keep
        # SeriesMeta.extra). Written to the user's data dir, never the repo.
        try:
            save_registry("ab", {f: {k: facs[f].get(k, "") for k in ("name", "operator", "subtype", "location")}
                                 for f in seen if f in facs and f not in STORAGE})
        except OSError as e:
            res.warnings.append(f"AB facility registry not written: {e}")
        missing = len(seen - set(facs))
        if missing:
            res.warnings.append(f"{missing} facilities with sales gas are not in the facility list (no name/operator)")
        return res
