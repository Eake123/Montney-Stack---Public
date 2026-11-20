"""Command line entry point:  uv run montney <command>"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import threading
import webbrowser
from collections import defaultdict
from pathlib import Path

import yaml


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="montney", description="Montney Stack dashboard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the dashboard (default http://127.0.0.1:8050)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8050)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--no-schedule", action="store_true", help="don't fetch in the background")

    sec = sub.add_parser("secrets", help="manage API keys in the OS credential store")
    ssub = sec.add_subparsers(dest="action", required=True)
    sset = ssub.add_parser("set", help="store a secret (prompts, input hidden)")
    sset.add_argument("name")
    ssub.add_parser("list", help="list stored secret names (never values)")
    schk = ssub.add_parser("check", help="confirm a secret is stored (shows length + last 4 chars)")
    schk.add_argument("name")
    sdel = ssub.add_parser("delete")
    sdel.add_argument("name")

    f = sub.add_parser("fetch", help="fetch sources once now (all if none given)")
    f.add_argument("sources", nargs="*")

    sub.add_parser("sources", help="list sources and their last fetch")

    t = sub.add_parser("try", help="run a source spec WITHOUT saving and print a preview")
    t.add_argument("spec", help="path to a .yaml spec, or a source id")
    t.add_argument("-n", type=int, default=3, help="sample points per series")

    imp = sub.add_parser("import-ngtl", help="import NGTL Gas Day Summary history from a CSV")
    imp.add_argument("csv")
    imp.add_argument("--inspect", action="store_true", help="show what was detected; import nothing")
    imp.add_argument("--units", choices=["MMcf", "e3m3", "e6m3"], help="override detected file units")

    db = sub.add_parser("db", help="Postgres + TimescaleDB: init, status, check (docs/db/INSTALL.md)")
    db.add_argument("action", choices=["init", "status", "check", "import-duckdb", "backup", "restore-test",
                                       "backups"],
                    help="init: apply pending migrations; status: list migrations; check: install checks; "
                         "backup / restore-test / backups: docs/db/BACKUPS.md")
    db.add_argument("--duckdb", help="import-duckdb: the v1 DuckDB file (default: the data dir's montney.duckdb)")
    db.add_argument("--dry-run", action="store_true", help="import-duckdb: show the plan; write nothing")
    db.add_argument("--csv", help="import-duckdb: also verify against this NGTL CSV (read-only)")
    db.add_argument("--no-backup", action="store_true", help="init: skip the pre-migrate backup")
    db.add_argument("--dump", help="restore-test: this dump (default: the newest complete backup)")

    sub.add_parser("run", help="the Postgres runner: fetch + write every source (MONTNEY_DB=postgres; PLAN P3)")

    du = sub.add_parser("du", help="storage per source and region vs the budget (docs/db/RETENTION.md)")
    du.add_argument("--no-save", action="store_true", help="don't record a usage snapshot or update states")

    rep = sub.add_parser("report", help="report-only checks, read-only (Power: peace-energy, L-051)")
    rep.add_argument("name", choices=["peace-energy", "burn-check"],
                     help="peace-energy: Peace dams annual-energy bound vs BC Hydro; "
                          "burn-check: monthly gas-burn est vs NGTL delivered, 6 validated plants")
    rep.add_argument("--days", type=int, default=365, help="peace-energy: window length in days (default 365)")
    rep.add_argument("--months", type=int, default=12, help="burn-check: completed months to check (default 12)")
    rep.add_argument("--json", action="store_true", help="print JSON instead of a table")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    {"serve": _serve, "secrets": _secrets, "fetch": _fetch, "sources": _sources,
     "try": _try, "import-ngtl": _import_ngtl, "db": _db, "du": _du, "run": _run, "report": _report}[args.cmd](args)


def _report(args) -> None:
    """Report-only (no alarms, no writes): opens the store on the read-only role, so it runs beside the runner."""
    import json

    from montney import power_report
    from montney.connectors.base import utcnow
    from montney.store import open_store

    store = open_store("reader")
    if args.name == "burn-check":
        rep = power_report.burn_check(store, utcnow(), months=args.months)
        print(json.dumps(rep, indent=1) if args.json else power_report.format_burn_check(rep))
        return
    rep = power_report.peace_energy(store, utcnow(), days=args.days)
    print(json.dumps(rep, indent=1) if args.json else power_report.format_peace_energy(rep))


def _run(args) -> None:
    from montney.runner import run_forever

    sys.exit(run_forever())


def _du(args) -> None:
    from montney.db.cli import du_main

    sys.exit(du_main(save=not args.no_save))


def _db(args) -> None:
    from montney.db.cli import main as db_main

    sys.exit(db_main(args.action, args))


def _serve(args) -> None:
    import uvicorn

    from montney import paths
    from montney.server import create_app

    url = f"http://{args.host}:{args.port}"
    print(f"Montney Stack  ->  {url}\ndata: {paths.home()}")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(schedule=not args.no_schedule), host=args.host, port=args.port,
                log_level="warning")


def _secrets(args) -> None:
    from montney import secrets

    if args.action == "set":
        if sys.stdin.isatty():
            value = getpass.getpass(f"{args.name} (input hidden): ").strip()
        else:  # piped:  echo $KEY | montney secrets set NAME
            value = sys.stdin.readline().strip()
        value = secrets.clean(value)
        if not value:
            sys.exit("nothing entered; secret not changed")
        secrets.set(args.name, value)
        print(f"saved {args.name} to the OS credential store ({secrets.masked(value)})")
    elif args.action == "list":
        names = secrets.list_names()
        print("\n".join(names) if names else "(no secrets stored)")
    elif args.action == "check":
        try:
            print(f"{args.name}: set ({secrets.masked(secrets.get(args.name))})")
        except secrets.MissingSecret as e:
            sys.exit(str(e))
    elif args.action == "delete":
        print("deleted" if secrets.delete(args.name) else "not found")


def _store():
    import duckdb

    from montney.store import open_store

    busy = ("The dashboard is running and holds the database (DuckDB allows one process).\n"
            "Stop it (Ctrl+C in its window), run this command, then start it again.\n"
            "To refresh a source while it runs, click its status dot in the UI instead.")
    busy_pg = ("The runner (`montney run`) is running and is the only writer (one writer at a time).\n"
               "Stop it first to write from the command line, or click a source's status dot in the UI\n"
               "to queue a refresh for the runner instead.")
    try:
        store = open_store("writer")   # MONTNEY_DB=postgres → Postgres (PORT step 3, L-035); else v1's DuckDB
    except duckdb.IOException as e:
        if "lock" in str(e).lower():
            sys.exit(busy)
        raise
    if hasattr(store, "w"):            # Postgres: take the single-writer lock (#57), as DuckDB's file lock did
        from montney.db.store import WriterBusy

        try:
            store.w.acquire_writer_lock()
        except WriterBusy:
            store.close()
            sys.exit(busy_pg)
    return store


def _fetch(args) -> None:
    from montney import connectors
    from montney.runner import run_source, seed_catalog

    specs = connectors.load_specs()
    ids = args.sources or list(specs)
    store = _store()
    seed_catalog(store, specs)
    for sid in ids:
        if sid not in specs:
            print(f"{sid}: unknown or disabled")
            continue
        r = run_source(store, specs[sid])
        msg = f"{r['n']} obs" if r["ok"] else f"FAILED - {r['error']}"
        print(f"{sid:14s} {msg}")
        for w in r.get("warnings") or []:
            print(f"  warning: {w}")


def _sources(args) -> None:
    from montney import connectors, secrets

    specs = connectors.load_specs(include_disabled=True)
    lf = _store().last_fetch()
    for sid, spec in specs.items():
        last = lf.get(sid, {})
        miss = [n for n in connectors.required_secrets(spec) if not secrets.has(n)]
        flag = "off " if not spec.get("enabled", True) else ("key?" if miss else "ok  " if last.get("ok") else "--  ")
        print(f"[{flag}] {sid:14s} every {spec.get('interval', '1h'):5s} last ok: {last.get('last_ok') or 'never'}"
              + (f"   error: {last['error'][:80]}" if last and not last.get("ok") and last.get("error") else ""))


def _try(args) -> None:
    from montney import connectors
    from montney.connectors.base import Context, utcnow
    from montney.runner import make_client

    p = Path(args.spec)
    if p.exists():
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    else:
        spec = connectors.load_specs(include_disabled=True).get(args.spec)
        if not spec:
            sys.exit(f"no spec file or source id {args.spec!r}")
    with make_client() as client:
        res = connectors.build(spec).run(Context(client=client, now=utcnow()))
    by: dict[str, list] = defaultdict(list)
    for o in res.obs:
        by[o.series_id].append(o)
    meta = {m.series_id: m for m in res.meta}
    print(f"{spec['id']}: {len(res.obs)} observations across {len(by)} series")
    for sid in sorted(by):
        pts = sorted(by[sid], key=lambda o: o.ts)
        m = meta.get(sid)
        sample = ", ".join(f"{o.ts:%Y-%m-%d %H:%M}={o.value:,.2f}" for o in pts[-args.n:])
        print(f"  {sid:42s} [{m.unit if m else '?':7s}] n={len(pts):<5d} {sample}")
    for var, g in sorted(res.grids.items()):
        times = g.get("times") or []
        span = f"{times[0]} .. {times[-1]}" if times else "static"
        print(f"  grid {var:6s} [{g.get('unit', '?'):5s}] {g.get('model')} run {g.get('run')}  "
              f"{g.get('nx')}x{g.get('ny')} x {len(times) or 1} steps  {span}")
    for e in res.events[: args.n * 3]:
        print(f"  event {e.kind:8s} {e.severity:8s} {e.title[:70]}")
    if len(res.events) > args.n * 3:
        print(f"  ... {len(res.events)} events in total")
    for w in res.warnings:
        print(f"  warning: {w}")


def _import_ngtl(args) -> None:
    from montney import connectors
    from montney.importers import ngtl_csv
    from montney.runner import seed_catalog

    path = Path(args.csv)
    if not path.exists():
        sys.exit(f"file not found: {path}")
    parsed = ngtl_csv.parse(path)
    file_units = args.units or ngtl_csv.guess_units(parsed, path)
    print(ngtl_csv.describe(parsed, file_units))
    if args.inspect:
        print("\n(--inspect: nothing imported)")
        return
    if not parsed.values:
        sys.exit("nothing to import - run with --inspect and check the layout")
    obs = ngtl_csv.to_observations(parsed, file_units)
    store = _store()
    seed_catalog(store, {k: v for k, v in connectors.load_specs().items() if k == "ngtl_gdsr"})
    n = store.write(obs)
    print(f"\nimported {n:,} observations into {getattr(store, 'path', 'Postgres')}")


if __name__ == "__main__":
    main()
