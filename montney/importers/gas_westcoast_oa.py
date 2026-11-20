"""One-off back-fill of Westcoast scheduled volumes (PLAN step 6, 6A): every gas day from --from to today.

    uv run python -m montney.importers.gas_westcoast_oa --from 2025-11-01 --dry-run   # fetch, write nothing
    uv run python -m montney.importers.gas_westcoast_oa --from 2025-11-01             # write (runner must be stopped)

It runs the live connector (`sources/gas_westcoast_oa.yaml`) with `from_date` set. That reads each gas day's latest cycle
through the page's date picker (5 requests a day; the page serves at least a year back). It writes through
`runner.run_source` (tiers, change-only writes, the budget guard, the fetch log) under the single-writer lock. The posting
has no working bulk "Historical Download" (its `/Infopost/rtbaDownload.asp` link is 404), hence the day-by-day read.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from montney import connectors
from montney.connectors.base import Context, utcnow

SOURCE = "gas_westcoast_oa"


def history_spec(from_date: str) -> dict:
    spec = connectors.load_specs(include_disabled=True)[SOURCE]
    return {**spec, "params": {**spec.get("params", {}), "from_date": from_date}}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m montney.importers.gas_westcoast_oa", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="from_date", default="2025-11-01", help="first gas day, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="fetch only; write nothing")
    args = ap.parse_args(argv)
    spec = history_spec(args.from_date)
    if args.dry_run:
        from montney.runner import make_client

        with make_client() as client:
            res = connectors.build(spec).run(Context(client=client, now=utcnow()))
        days = Counter(o.ts.date() for o in res.obs)
        print(f"{len(res.obs):,} values over {len(days)} gas days ({min(days)}..{max(days)})")
        for w in res.warnings:
            print(f"warning: {w}")
        print("(--dry-run: nothing written)")
        return
    from montney.cli import _store
    from montney.runner import run_source, seed_catalog

    store = _store()
    try:
        seed_catalog(store, {SOURCE: spec})
        r = run_source(store, spec, trigger="import")
    finally:
        store.close()
    if not r["ok"]:
        sys.exit(f"{SOURCE}: FAILED - {r['error']}")
    print(f"{SOURCE}: wrote {r['n']:,} observations (unchanged values are skipped)")


if __name__ == "__main__":
    main()
