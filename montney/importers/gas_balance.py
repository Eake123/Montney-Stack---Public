"""One-off back-fill of the AB + BC tie-out history (L-079): the monthly series behind the daily gas_balance check.

    uv run python -m montney.importers.gas_balance --dry-run              # fetch and compute, write nothing
    uv run python -m montney.importers.gas_balance                        # write (runner must be stopped)
    uv run python -m montney.importers.gas_balance --skip-petrinex        # AB months already loaded

The daily check can't go back: its inputs (production.total.daily_est, export.pipeline.total_est and Aitken's daily
estimate) only start in Oct-Nov 2026. History is monthly, from regulator and measured data. Steps, in order:
1. gas_petrinex from --from: production.ab.monthly for every month. It re-reads ~7.7 MB per month from Petrinex.
   **Guard (L-081):** closed months already stored (`production.ab.monthly`, older than the trailing window Petrinex
   still revises) are not read again. The read starts at the first missing closed month, or is just the connector's
   normal trailing window if none are missing. `--force-petrinex` reads everything from --from. Use it when a new
   Petrinex series needs its history (the guard only checks production.ab.monthly), and about once a year to pick up
   late amendments older than the trailing window.
2. gas_bcer_production with --months of BC production and Aitken net: one ~9 MB BCER CSV that holds every month.
3. gas_balance_monthly: the tie-out per month (balance.ab_bc.*_monthly).
Each step writes through `runner.run_source` (tiers, change-only writes, the budget guard, the fetch log) under the
single-writer lock.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, date, datetime

from montney import connectors
from montney.connectors.base import Context, utcnow


def petrinex_from(from_month: str, today: date, stored: set[date], months_back: int = 4) -> str | None:
    """Where the Petrinex history read has to start: the first closed month (older than the connector's trailing,
    still-revised window) from `from_month` that isn't stored, or None when all are (then the normal trailing read)."""
    from montney.connectors.gas_petrinex import months_to_read

    trailing_start = months_to_read(today, months_back, None)[0]
    closed = [m for m in months_to_read(today, months_back, from_month) if m < trailing_start]
    missing = [m for m in closed if m not in stored]
    return f"{min(missing):%Y-%m}" if missing else None


def stored_ab_months(from_month: str) -> set[date]:
    """The months production.ab.monthly already has (read-only)."""
    from montney.connectors.gas_common import read_stored

    start = datetime.strptime(from_month, "%Y-%m")
    got = read_stored(["production.ab.monthly"], start, datetime.now(UTC).replace(tzinfo=None))
    return {t.date().replace(day=1) for t in (got.get("production.ab.monthly") or {})}


def specs(from_month: str, months: int, skip_petrinex: bool, force_petrinex: bool = False,
          stored: set[date] | None = None, today: date | None = None) -> dict[str, dict]:
    from montney.connectors.gas_petrinex import local_today
    from montney.importers.gas_petrinex import history_spec

    allspecs = connectors.load_specs(include_disabled=True)
    out = {}
    if not skip_petrinex:
        start = from_month if force_petrinex else petrinex_from(
            from_month, today or local_today(), stored_ab_months(from_month) if stored is None else stored,
            int(allspecs["gas_petrinex"].get("params", {}).get("months_back", 4)))
        out["gas_petrinex"] = history_spec(start) if start else allspecs["gas_petrinex"]
    b = allspecs["gas_bcer_production"]
    out["gas_bcer_production"] = {**b, "params": {**b.get("params", {}), "months_back": months, "aitken_months": months}}
    g = allspecs["gas_balance_monthly"]
    out["gas_balance_monthly"] = {**g, "params": {**g.get("params", {}), "from_month": from_month}}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m montney.importers.gas_balance", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="from_month", default="2024-01", help="first month, YYYY-MM (default 2024-01)")
    ap.add_argument("--months", type=int, default=36, help="BCER months to (re)write (default 36)")
    ap.add_argument("--skip-petrinex", action="store_true", help="AB months already loaded: skip step 1")
    ap.add_argument("--force-petrinex", action="store_true",
                    help="re-read every Petrinex month from --from, even closed months already stored")
    ap.add_argument("--dry-run", action="store_true", help="fetch and compute only; write nothing")
    args = ap.parse_args(argv)
    todo = specs(args.from_month, args.months, args.skip_petrinex, args.force_petrinex)
    if "gas_petrinex" in todo:
        fm = todo["gas_petrinex"].get("params", {}).get("from_month")
        print(f"gas_petrinex: reading from {fm}" if fm else "gas_petrinex: closed months all stored; normal trailing read")
    if args.dry_run:
        from montney.runner import make_client

        with make_client() as client:
            for sid, spec in todo.items():
                if sid == "gas_balance_monthly":
                    print(f"{sid}: reads the store, so its dry run only sees what is already written")
                res = connectors.build(spec).run(Context(client=client, now=utcnow()))
                months = Counter(o.ts.strftime("%Y-%m") for o in res.obs)
                print(f"{sid}: {len(res.obs):,} values over {len(months)} months"
                      + (f" ({min(months)}..{max(months)})" if months else ""))
                for w in res.warnings[:5]:
                    print(f"  warning: {w}")
        print("(--dry-run: nothing written)")
        return
    from montney.cli import _store
    from montney.runner import run_source, seed_catalog

    store = _store()
    try:
        seed_catalog(store, todo)
        for sid, spec in todo.items():
            r = run_source(store, spec, trigger="import")
            if not r["ok"]:
                sys.exit(f"{sid}: FAILED - {r['error']}")
            print(f"{sid}: wrote {r['n']:,} observations (unchanged values are skipped)")
    finally:
        store.close()


if __name__ == "__main__":
    main()
