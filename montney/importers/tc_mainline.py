"""One-off back-fill of the TC Mainline areas and capability (L-077): gas days from --from to today.

    uv run python -m montney.importers.tc_mainline --from 2024-11-01 --dry-run   # fetch, write nothing
    uv run python -m montney.importers.tc_mainline --from 2024-11-01             # write (runner must be stopped)

Flows: the live connector (`sources/tc_mainline.yaml`) with `from_date` set. GCTS serves the whole range in one request
(2 years ≈ 3.7 MB). Capability: `sources/tc_mainline_capability.yaml` reading every maintenance bulletin in the newest
--pages × 100 Customer Express bulletins (13 pages reach back to May 2025). Both write through `runner.run_source`
(tiers, change-only writes, the budget guard, the fetch log) under the single-writer lock.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from montney import connectors
from montney.connectors.base import Context, utcnow

FLOWS, CAPABILITY = "tc_mainline", "tc_mainline_capability"


def history_specs(from_date: str, pages: int) -> dict[str, dict]:
    specs = connectors.load_specs(include_disabled=True)
    f, c = specs[FLOWS], specs[CAPABILITY]
    return {FLOWS: {**f, "params": {**f.get("params", {}), "from_date": from_date}},
            CAPABILITY: {**c, "params": {**c.get("params", {}), "pages": pages, "max_bulletins": 1000}}}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m montney.importers.tc_mainline", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="from_date", default="2024-11-01", help="first gas day, YYYY-MM-DD")
    ap.add_argument("--pages", type=int, default=13, help="bulletin pages of 100 to search for capability tables")
    ap.add_argument("--dry-run", action="store_true", help="fetch only; write nothing")
    args = ap.parse_args(argv)
    specs = history_specs(args.from_date, args.pages)
    if args.dry_run:
        from montney.runner import make_client

        with make_client() as client:
            for sid, spec in specs.items():
                res = connectors.build(spec).run(Context(client=client, now=utcnow()))
                days = Counter(o.ts.date() for o in res.obs)
                print(f"{sid}: {len(res.obs):,} values over {len(days)} gas days ({min(days)}..{max(days)})")
                for w in res.warnings:
                    print(f"  warning: {w}")
        print("(--dry-run: nothing written)")
        return
    from montney.cli import _store
    from montney.runner import run_source, seed_catalog

    store = _store()
    try:
        seed_catalog(store, specs)
        for sid, spec in specs.items():
            r = run_source(store, spec, trigger="import")
            if not r["ok"]:
                sys.exit(f"{sid}: FAILED - {r['error']}")
            print(f"{sid}: wrote {r['n']:,} observations (unchanged values are skipped)")
    finally:
        store.close()


if __name__ == "__main__":
    main()
