"""A/B parity on REAL data: v1's DuckDB `Store` vs `PostgresStore`, every read the app makes (PLAN P3 step 5;
docs/db/PORT.md; tests/test_db_parity.py does the same on a synthetic scenario in CI).

    MONTNEY_DB=postgres uv run python -m montney.db.parity [--duckdb PATH]

* DuckDB is never touched: v1's Store opens its file read-write and runs DDL, so it opens a temporary COPY.
* Postgres is read through the API's role (reader), as the app will.
* One frozen clock for both stores and for snapshot.build.
* Compared: series() for every DuckDB series over a grid of windows and as_of_max points; meta(); catalog();
  last_fetch(); count(); events() over kinds x refs x states x times x withdrawn; events(since); and
  snapshot.build() over the real map network (network/*.geojson), i.e. what /api/snapshot serves.
* Differences are allowed ONLY in the documented forms (ALLOWED below, the approved PORT.md entries, plus
  L-036's derived realized series); each allowed hit is counted and printed. Anything else is a problem.
  Comparing nothing is a failure (NO-RESULT is not a pass). Exit 0 = parity.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALLOWED = {
    "count": "postgres <= duckdb: change-only stores fewer rows (DuckDB stored every repeated vintage)",
    "series.as_of@as_of_max": "with as_of_max set, postgres as_of <= duckdb (ts and v equal)",
    "events.refs.duplicates": "a ref repeated within one event appears once in postgres (order kept)",
    "catalog.realized": "postgres also lists the L-036 derived `<key>.realized` series DuckDB never had",
    # Approved by the orchestrator 2026-11-01 (import parity): v1 orders events by coalesce(published,
    # first_seen) with no tie-breaker, so equal keys come out in an arbitrary order. Postgres breaks ties by id,
    # and that is the contract from now on (docs/db/PORT.md "Event order").
    "events.tie_order": "same id multiset, identical content by id, identical sort-key sequence; order free "
                        "only among equal (published, first_seen) keys",
}


def _event_key(e: dict):
    return e.get("published") or e.get("first_seen")


def events_equal(a: list, b: list, hits: dict) -> bool:
    """DuckDB's event list `a` vs Postgres's `b`: equal, or different only in an ALLOWED form, each counted
    in `hits`:
    * events.tie_order: same id multiset, identical content by id, identical sort-key sequence, so the order
      differs only among events with an equal sort key;
    * events.refs.duplicates: Postgres's refs are DuckDB's with repeats removed (first kept, order kept).
    Anything else (an event missing or changed, or two events with DIFFERENT keys swapped) is not equal."""
    if a == b:
        return True
    if len(a) != len(b) or [_event_key(x) for x in a] != [_event_key(y) for y in b]:
        return False
    by_id = {y["id"]: y for y in b}
    if len(by_id) != len(b) or {x["id"] for x in a} != set(by_id):
        return False

    def dedup(x):
        return {**x, "refs": list(dict.fromkeys(x["refs"]))}

    for x in a:
        y = by_id[x["id"]]
        if y != x and y != dedup(x):
            return False
    if [x["id"] for x in a] != [y["id"] for y in b]:
        hits["events.tie_order"] = hits.get("events.tie_order", 0) + 1
    if any(by_id[x["id"]] != x for x in a):
        hits["events.refs.duplicates"] = hits.get("events.refs.duplicates", 0) + 1
    return True


def _parse(a):
    return datetime.fromisoformat(a) if a else None


def compare(v1, pg, ids: list[str], now: datetime, as_of_points: list[datetime], refs: list[str],
            kinds: list[str], hits: dict, skip: tuple = ()) -> tuple[int, list[str]]:
    n, problems = 0, []

    def hit(k):
        hits[k] = hits.get(k, 0) + 1

    def events_same(a, b) -> bool:
        return events_equal(a, b, hits)

    def same(name, a, b):
        nonlocal n
        n += 1
        if a != b:
            problems.append(f"{name}:\n  duckdb   {a!r:.400}\n  postgres {b!r:.400}")

    grid = [(None, None, None), (now - timedelta(days=1), now + timedelta(days=1), None),
            (now - timedelta(days=7), now, None), (now - timedelta(days=60), now - timedelta(days=30), None),
            (datetime(2021, 1, 1), datetime(2022, 1, 1), None)] + [(None, None, t) for t in as_of_points]
    for start, end, amax in grid:
        a, b = v1.series(ids, start, end, amax), pg.series(ids, start, end, amax)
        for sid in ids:
            sa, sb = a.get(sid, {}), b.get(sid, {})
            same(f"series[{sid}] ts {start, end, amax}", sa.get("ts"), sb.get("ts"))
            same(f"series[{sid}] v {start, end, amax}", sa.get("v"), sb.get("v"))
            pa, pb = _parse(sa.get("as_of")), _parse(sb.get("as_of"))
            if amax is None:
                same(f"series[{sid}] as_of {start, end}", pa, pb)
            else:
                n += 1
                if (pa is None) != (pb is None) or (pa and pb > pa):
                    problems.append(f"series[{sid}] as_of {start, end, amax}: duckdb {pa}, postgres {pb} (must be <=)")
                elif pa != pb:
                    hit("series.as_of@as_of_max")
    same("meta", v1.meta(ids), pg.meta(ids))
    def keep(c):
        k = c["id"] if isinstance(c, dict) else c
        return not any(k.startswith(x) for x in skip)

    ca, cb = [c for c in v1.catalog() if keep(c)], [c for c in pg.catalog() if keep(c)]
    ka = {c["id"] if isinstance(c, dict) else c for c in ca}
    kb = {c["id"] if isinstance(c, dict) else c for c in cb}
    extra = kb - ka
    n += 1
    if ka - kb:
        problems.append(f"catalog: missing in postgres: {sorted(ka - kb)[:20]}")
    if any(not k.endswith(".realized") for k in extra):
        problems.append(f"catalog: unexpected extra in postgres: {sorted(k for k in extra if not k.endswith('.realized'))[:20]}")
    elif extra:
        hits["catalog.realized"] = len(extra)
    same("catalog (DuckDB's series)", [c for c in ca], [c for c in cb if (c["id"] if isinstance(c, dict) else c) in ka])
    same("last_fetch", v1.last_fetch(), pg.last_fetch())
    n += 1
    if not pg.count() <= v1.count():
        problems.append(f"count: postgres {pg.count()} > duckdb {v1.count()}")
    elif pg.count() < v1.count():
        hit("count")
    for kind in [None, *kinds]:
        for ref in [None, *refs]:
            for state in (None, "ongoing", "upcoming", "current", "past"):
                for at in (now, now + timedelta(days=1), now - timedelta(days=7)):
                    for wd in (False, True):
                        k = [kind] if kind else None
                        n += 1
                        a = v1.events(kinds=k, ref=ref, state=state, at=at, include_withdrawn=wd)
                        b = pg.events(kinds=k, ref=ref, state=state, at=at, include_withdrawn=wd)
                        if not events_same(a, b):
                            problems.append(f"events {kind, ref, state, at, wd}: duckdb {len(a)}, postgres {len(b)}")
    for since in as_of_points:
        n += 1
        if not events_same(v1.events(since=since, at=now), pg.events(since=since, at=now)):
            problems.append(f"events since {since}")
    return n, problems


def snapshot_parity(v1, pg) -> tuple[int, list[str]]:
    """snapshot.build() over the real network, exactly as /api/snapshot calls it."""
    from montney import network, snapshot

    fc = network.load(strict=False)
    refs = network.series_refs(fc)
    compares = {f["id"]: f["properties"]["compare"] for f in fc["features"] if "compare" in f["properties"]}
    units = {f["id"]: f["properties"]["unit"] for f in fc["features"] if "unit" in f["properties"]}
    a = snapshot.build(v1, refs, compares, [], units)
    b = snapshot.build(pg, refs, compares, [], units)
    problems = []
    for k in sorted(set(a["values"]) | set(b["values"])):
        if a["values"].get(k) != b["values"].get(k):
            problems.append(f"snapshot[{k}]:\n  duckdb   {a['values'].get(k)!r:.300}\n  postgres {b['values'].get(k)!r:.300}")
    rest_a = {k: v for k, v in a.items() if k != "values"}
    rest_b = {k: v for k, v in b.items() if k != "values"}
    if rest_a != rest_b:
        problems.append(f"snapshot (other keys): duckdb {rest_a!r:.300} / postgres {rest_b!r:.300}")
    return len(a["values"]), problems


def run(duck: Path, pg_conn, now: datetime | None = None, verbose: bool = True, min_comparisons: int = 1000,
        exclude: tuple | None = None) -> dict:
    """`exclude`: id prefixes the import leaves out on purpose (default: backfill.DEFAULT_EXCLUDE, i.e. TAU
    until L-034). They are listed in the result, never compared, and never silently counted as equal."""
    import duckdb

    from montney import snapshot
    from montney import store as v1mod
    from montney.db import pgstore as pgmod
    from montney.db import store as dbstore
    from montney.db.pgstore import PostgresStore

    now = now or datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    clock = lambda: now                                                    # noqa: E731
    saved = [(m, getattr(m, "_utcnow")) for m in (v1mod, dbstore, pgmod)] + [(snapshot, snapshot.utcnow)]
    tmp = Path(tempfile.mkdtemp(prefix="montney-parity-"))
    try:
        copy = tmp / "copy.duckdb"
        shutil.copy2(duck, copy)                                           # the original is never opened
        ro = duckdb.connect(str(copy), read_only=True)
        if exclude is None:
            from montney.db.backfill import DEFAULT_EXCLUDE as exclude
        all_ids = [r[0] for r in ro.execute("SELECT DISTINCT series_id FROM observations ORDER BY 1").fetchall()]
        excluded = [i for i in all_ids if any(i.startswith(x) for x in exclude)]
        ids = [i for i in all_ids if i not in excluded]
        pts = [r[0] for r in ro.execute(
            "SELECT DISTINCT fetched_at FROM observations ORDER BY 1").fetchall()]
        refs = sorted({r[0] for r in ro.execute("SELECT DISTINCT unnest(refs) FROM events").fetchall()})[:6] \
            if _has_col(ro, "events", "refs") else []
        kinds = [r[0] for r in ro.execute("SELECT DISTINCT kind FROM events ORDER BY 1").fetchall()]
        ro.close()
        as_of_points = [pts[i] for i in sorted({0, len(pts) // 4, len(pts) // 2, 3 * len(pts) // 4, len(pts) - 1})] if pts else []
        for m, _ in saved[:3]:
            setattr(m, "_utcnow", clock)
        snapshot.utcnow = clock
        v1 = v1mod.Store(copy)
        pg = PostgresStore(pg_conn)
        hits: dict = {}
        n, problems = compare(v1, pg, ids, now, as_of_points, refs, kinds, hits, skip=tuple(exclude))
        sn, sp = snapshot_parity(v1, pg)
        v1.close()
    finally:
        for m, f in saved[:3]:
            setattr(m, "_utcnow", f)
        snapshot.utcnow = saved[3][1]
        shutil.rmtree(tmp, ignore_errors=True)
    out = {"now": now, "series": len(ids), "excluded": excluded, "comparisons": n, "problems": problems, "snapshot_values": sn,
           "snapshot_problems": sp, "allowed_hits": hits, "refs": refs, "kinds": kinds,
           "as_of_points": as_of_points}
    out["min_comparisons"] = min_comparisons
    out["ok"] = n > min_comparisons and sn > 0 and not problems and not sp
    return out


def _has_col(con, table, col) -> bool:
    return bool(con.execute("SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                            [table, col]).fetchone())


def main(argv=None) -> int:
    from montney import paths
    from montney.db import connect

    ap = argparse.ArgumentParser(prog="python -m montney.db.parity")
    ap.add_argument("--duckdb", help="the v1 DuckDB file (default: the data dir's montney.duckdb); only a copy is opened")
    args = ap.parse_args(argv)
    duck = Path(args.duckdb or paths.db_path())
    conn = connect(role="reader")
    try:
        r = run(duck, conn)
    finally:
        conn.close()
    print(f"parity at {r['now']:%Y-%m-%d %H:%M}Z: {r['series']} DuckDB series, {r['comparisons']:,} comparisons, "
          f"{len(r['problems'])} problems; snapshot.build over the map: {r['snapshot_values']} values, "
          f"{len(r['snapshot_problems'])} problems")
    print(f"grid: as_of points {[f'{t:%Y-%m-%d %H:%M}' for t in r['as_of_points']]}; event kinds {r['kinds']}; refs {r['refs']}")
    print(f"allowed differences hit: {r['allowed_hits']}")
    print(f"excluded by the import on purpose (not compared): {len(r['excluded'])} {r['excluded'][:5]}")
    for p in (r["problems"] + r["snapshot_problems"])[:30]:
        print(p)
    if r["comparisons"] <= 1000 or not r["snapshot_values"]:
        print("NO-RESULT: the grid compared too little")
    print("parity:", "PASS" if r["ok"] else "FAIL")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
