"""One-off history load for NGTL Station % Utilization receipts (PLAN 5a): every year from --from (default 2010).

    uv run python -m montney.importers.ngtl_station_util --from 2010 --dry-run   # fetch + parse, write nothing
    uv run python -m montney.importers.ngtl_station_util --from 2010             # write (runner must be stopped)

It runs the live connector (`sources/ngtl_station_util.yaml`) with `from_year` set. It then writes through the
runner's own path (`montney.runner.run_source`), so tiering, change-only writes, the budget guard and the fetch log
are the same as live. The write takes the single-writer lock like `montney fetch`, and exits with a message
if the runner is running.

Expected size (measured with --dry-run; see the 5a PR): ~17 files, ~7 MB downloaded. The raw xlsx/xls are
parsed in memory and never saved. About 120k monthly rows over ~1,000 station series. At ~10 B/row compressed
and ~100 B/row uncompressed, that's ~1-12 MB of the 5 GB ab_nebc budget.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from montney import connectors
from montney.connectors.base import Context, utcnow

SOURCE = "ngtl_station_util"


def history_spec(from_year: int) -> dict:
    spec = connectors.load_specs(include_disabled=True)[SOURCE]
    return {**spec, "params": {**spec.get("params", {}), "from_year": from_year}}


def summarize(res) -> str:
    years = Counter(o.ts.year for o in res.obs)
    series = {o.series_id for o in res.obs}
    lines = [f"{len(res.obs):,} monthly values across {len(series):,} station series"]
    lines += [f"  {y}: {n:,}" for y, n in sorted(years.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m montney.importers.ngtl_station_util", description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="from_year", type=int, default=2010, help="first year to load (default 2010)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and parse only; write nothing")
    args = ap.parse_args(argv)
    spec = history_spec(args.from_year)
    if args.dry_run:
        from montney.runner import make_client

        with make_client() as client:
            res = connectors.build(spec).run(Context(client=client, now=utcnow()))
        print(summarize(res))
        print("(--dry-run: nothing written)")
        return
    from montney.cli import _store
    from montney.runner import run_source, seed_catalog

    store = _store()                      # takes the single-writer lock; exits if the runner holds it
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
