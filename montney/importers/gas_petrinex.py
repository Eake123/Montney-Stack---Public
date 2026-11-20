"""One-off history load for Petrinex AB plant sales gas (PLAN 5a): every published month from --from (default 2022-01).

    uv run python -m montney.importers.gas_petrinex --from 2022-01 --dry-run   # fetch + aggregate, write nothing
    uv run python -m montney.importers.gas_petrinex --from 2022-01             # write (runner must be stopped)

It runs the live connector (`sources/gas_petrinex.yaml`) with `from_month` set, and writes through `runner.run_source`
(tiers, change-only writes, the budget guard, the fetch log) under the single-writer lock. The free public window starts at 2022-01.
Earlier months are AER-only and paid (Q8: not bought). ~58 months × ~7.7 MB are downloaded, streamed, and never saved.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from montney import connectors
from montney.connectors.base import Context, utcnow

SOURCE = "gas_petrinex"


def history_spec(from_month: str) -> dict:
    spec = connectors.load_specs(include_disabled=True)[SOURCE]
    return {**spec, "params": {**spec.get("params", {}), "from_month": from_month}}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m montney.importers.gas_petrinex", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="from_month", default="2022-01", help="first month, YYYY-MM (default 2022-01)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and aggregate only; write nothing")
    args = ap.parse_args(argv)
    spec = history_spec(args.from_month)
    if args.dry_run:
        from montney.runner import make_client

        with make_client() as client:
            res = connectors.build(spec).run(Context(client=client, now=utcnow()))
        months = Counter(o.ts.strftime("%Y-%m") for o in res.obs)
        print(f"{len(res.obs):,} monthly values across {len({o.series_id for o in res.obs}):,} facilities, "
              f"{len(months)} months ({min(months)}..{max(months)})")
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
