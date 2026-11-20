"""Fetch execution and the background scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
from datetime import datetime, timedelta

import httpx

from montney import connectors, grids, secrets
from montney.connectors.base import Context, utcnow
from montney.store import Store

log = logging.getLogger("montney")

USER_AGENT = "MontneyStack/0.1 (+https://github.com/Eake123/Montney-Stack)"


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def seed_catalog(store: Store, specs: dict[str, dict]) -> None:
    """Register every declared series so the UI can list them before the first fetch. On Postgres, with the
    spec's `retention:` tier, as resolve_tiers applies it at write time: seeding with the freq-derived tier made
    a new forecast series `daily` at start-up, and the first write then clashed (L-080, the whole `weather`
    source failed)."""
    pg = _is_postgres(store)
    for spec in specs.values():
        try:
            if pg and isinstance(spec.get("region"), bool):     # YAML 1.1: a bare on/off/yes/no is a boolean (L-091)
                log.warning("seed %s: region: %r is a YAML boolean, not a region code (Ontario is `ont`; quote it "
                            "otherwise); left where it is", spec["id"], spec["region"])
            elif pg and spec.get("region") and not store.set_source_region(spec["id"], str(spec["region"])):
                log.warning("seed %s: region %r is not in the region table (a migration adds it); left where it is",
                            spec["id"], spec["region"])
            metas = connectors.build(spec).declared_series()
            store.upsert_meta(_with_spec_tiers(spec, metas) if pg else metas)
        except Exception as e:  # a broken spec shouldn't stop the app
            log.warning("seed %s: %s", spec.get("id"), e)


def _with_spec_tiers(spec: dict, metas: list) -> list:
    """Declared metas with the spec's retention tier in extra["tier"] (events/grid series aren't observations:
    dropped); a spec without `retention:` leaves them to the freq-derived tier."""
    from montney.db.backfill import spec_tier

    out = []
    for m in metas:
        tier = spec_tier(m.series_id, spec)
        if tier in ("events", "grid"):
            continue
        if tier:
            m.extra = {**(m.extra or {}), "tier": tier}
        out.append(m)
    return out


def _is_postgres(store) -> bool:
    return type(store).__name__ == "PostgresStore"


def resolve_tiers(spec: dict, res) -> None:
    """Postgres only (L-036): resolve every written series' tier from the spec's `retention:` at WRITE time,
    since python connectors emit ids at run time. Fails closed (RetentionConflict): a series the spec's
    retention doesn't cover, or one with no retention, no declared freq and no meta, is an error, never a
    silent tier. A spec that declares no `retention:` yet keeps the freq-derived tier for declared series."""
    from montney.db.backfill import RetentionConflict, spec_tier
    from montney.store import SeriesMeta

    metas = {m.series_id: m for m in res.meta}
    written = {o.series_id for o in res.obs}
    drop: set = set()
    for o in res.obs:
        metas.setdefault(o.series_id, SeriesMeta(o.series_id, spec["id"]))
    for sid, m in metas.items():
        tier = spec_tier(sid, spec)                 # raises on overlapping globs / no match without default
        if tier in ("events", "grid"):
            if sid in written:
                raise RetentionConflict(f"{spec['id']}: {sid}: retention {tier!r} is not an observation tier, "
                                        f"but the connector emitted observations for it")
            drop.add(sid)                           # a declared-only series of an events/grid source: not stored
            continue
        if tier is None:
            if not m.freq:
                raise RetentionConflict(f"{spec['id']}: {sid}: no retention: declared and no freq to derive a tier")
            continue                                # undeclared spec: PostgresStore derives it from freq
        m.extra = {**(m.extra or {}), "tier": tier}
    res.meta = [m for sid, m in metas.items() if sid not in drop]


TRIGGERS = ("schedule", "refresh", "manual", "import")


def run_source(store: Store, spec: dict, client: httpx.Client | None = None, *,
               blocked: set | None = None, db_lock=None, trigger: str = "manual") -> dict:
    """Fetch one source and write it. `db_lock` serialises every store call (the Postgres runner shares one
    connection across fetch threads; the network part runs outside the lock). `blocked`: sources over budget
    (du): they are still fetched, so status stays live, but nothing is written (PLAN P3)."""
    started = utcnow()
    sid = spec["id"]
    lk = db_lock or contextlib.nullcontext()
    missing = sorted(s for s in connectors.required_secrets(spec) if not secrets.has(s))
    if missing:
        err = f"missing secret(s): {', '.join(missing)} - run: uv run montney secrets set {missing[0]}"
        with lk:
            store.log_fetch(sid, started, False, 0, err, trigger=trigger)
        return {"source": sid, "ok": False, "error": err, "n": 0}
    own = client is None
    client = client or make_client()
    ctx = Context(client=client, now=started, source=sid)
    req = lambda: {"n_requests": ctx.n_requests, "bytes_in": ctx.bytes_in, "trigger": trigger}   # logged per run (L-081)
    try:
        conn = connectors.build(spec)
        res = conn.run(ctx)
        if _is_postgres(store):
            resolve_tiers(spec, res)
        if blocked and sid in blocked:
            err = f"budget: source {sid} is blocked (region or 100 GB total full; see montney du); writes stopped"
            with lk:
                store.log_fetch(sid, started, False, 0, err, **req())
            log.warning("%s: %s", sid, err)
            return {"source": sid, "ok": False, "error": err, "n": 0, "blocked": True}
        with lk:
            store.upsert_meta(res.meta)
            n = store.write(res.obs, live=conn.live)
            if res.events or res.event_scopes:
                store.upsert_events(res.events, complete_scopes=res.event_scopes)
                n += len(res.events)
        for payload in res.grids.values():
            grids.save(payload)
            n += 1
        warn = "; ".join(res.warnings) or None
        with lk:
            store.log_fetch(sid, started, True, n, warn, **req())
        ctx.commit_validators()      # only now: a 304 next time means "what's stored is current" (ctx.get_if_changed)
        log.info("%s: %d obs%s", sid, n, f" (warnings: {warn})" if warn else "")
        return {"source": sid, "ok": True, "n": n, "warnings": res.warnings}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        with lk:
            store.log_fetch(sid, started, False, 0, err, **req())
        log.warning("%s failed: %s", sid, err)
        return {"source": sid, "ok": False, "error": err, "n": 0}
    finally:
        if own:
            client.close()


class Scheduler:
    """Runs each enabled source on its `interval`, catching up on start if stale."""

    RETRY_AFTER_FAIL = 300.0   # first retry after a failure, seconds
    MAX_BACKOFF = 6 * 3600.0   # cap for repeated failures
    STARTUP_JITTER_S = 60.0    # L-081: a source's first run waits a random 0-60 s more, so a restart isn't one burst
    INTERVAL_JITTER = 0.05     # and each wait is +-5 %, so sources on the same interval drift apart

    def __init__(self, store: Store):
        self.store = store
        self.specs = connectors.load_specs()
        self.client = make_client()
        self._tasks: list[asyncio.Task] = []
        self._locks: dict[str, threading.Lock] = {k: threading.Lock() for k in self.specs}

    def reload(self) -> None:
        self.specs = connectors.load_specs()
        for k in self.specs:
            self._locks.setdefault(k, threading.Lock())

    def run_now(self, source_id: str, trigger: str = "refresh") -> dict:
        """`trigger`: schedule from the loop; refresh from the API / refresh_request (the default)."""
        spec = self.specs.get(source_id)
        if not spec:
            return {"source": source_id, "ok": False, "error": "unknown or disabled source"}
        lock = self._locks[source_id]
        if not lock.acquire(blocking=False):
            return {"source": source_id, "ok": False, "error": "already running"}
        try:
            return run_source(self.store, spec, self.client, trigger=trigger)
        finally:
            lock.release()

    def _last_fetch(self) -> dict:
        return self.store.last_fetch()

    async def _loop(self, source_id: str) -> None:
        spec = self.specs[source_id]
        interval = connectors.parse_interval(spec.get("interval", "1h"))
        last = (await asyncio.to_thread(self._last_fetch)).get(source_id, {})
        wait = 0.0
        if last.get("last_ok"):
            age = (utcnow() - datetime.fromisoformat(last["last_ok"])).total_seconds()
            wait = max(0.0, interval - age)
        await asyncio.sleep(wait + random.uniform(0.0, self.STARTUP_JITTER_S))
        failures = 0
        while True:
            res = await asyncio.to_thread(self.run_now, source_id, "schedule")
            failures = 0 if res.get("ok") else failures + 1
            await asyncio.sleep(self.next_wait(interval, failures)
                                * random.uniform(1 - self.INTERVAL_JITTER, 1 + self.INTERVAL_JITTER))

    @classmethod
    def next_wait(cls, interval: float, failures: int) -> float:
        """Seconds until the next attempt. After failures, back off exponentially
        (5 min, 10, 20, ...), never retrying sooner than 5 min and never waiting more than
        max(interval, 6 h), so a broken upstream isn't hammered from the user's IP."""
        if failures == 0:
            return interval
        backoff = cls.RETRY_AFTER_FAIL * (2 ** (min(failures, 16) - 1))  # bounded exponent
        return min(max(backoff, cls.RETRY_AFTER_FAIL), max(interval, cls.MAX_BACKOFF))

    def start(self) -> None:
        seed_catalog(self.store, self.specs)
        for sid in self.specs:
            self._tasks.append(asyncio.create_task(self._loop(sid), name=f"fetch:{sid}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.client.close()


def next_due(store: Store, specs: dict[str, dict]) -> dict[str, str | None]:
    out = {}
    lf = store.last_fetch()
    for sid, spec in specs.items():
        last = lf.get(sid, {}).get("last_ok")
        if not last:
            out[sid] = None
            continue
        out[sid] = (datetime.fromisoformat(last)
                    + timedelta(seconds=connectors.parse_interval(spec.get("interval", "1h")))).isoformat()
    return out


# ---------------------------------------------------------------- the Postgres runner process (L-002, PORT step 4)
class PgRunner(Scheduler):
    """`montney run`: the one process that writes Postgres. On top of the per-source schedule it runs:
      refresh_requests  every 10 s: requests the API queued (POST /api/refresh -> montney.refresh_request);
      budget_states     every 5 min: re-read which sources du blocked;
      du                every 6 h: storage per source/region + the 100 GB total, then the budget states;
      fill_realized     hourly: L-037 realized outage history for missed days/months (backfill.fill_realized);
      backup            every 15 min, acts when due: the nightly pg_dump (from 02:00 Alberta) and the monthly
                        restore test (montney/db/backup.py, docs/db/BACKUPS.md). MONTNEY_BACKUP=off disables it.
    It holds the writer lock for its whole life, so a second runner (or an import) fails fast."""

    PERIODIC = {"heartbeat": 10.0, "refresh_requests": 10.0, "budget_states": 300.0, "du": 6 * 3600.0,
                "fill_realized": 3600.0, "backup": 900.0}

    def __init__(self, store, specs: dict | None = None, home=None):
        self.store = store
        self.specs = specs if specs is not None else connectors.load_specs()
        self.client = make_client()
        self._tasks: list[asyncio.Task] = []
        self._locks = {k: threading.Lock() for k in self.specs}
        self.db_lock = threading.RLock()
        self.blocked: set[str] = set()
        self.home = home
        self.last_periodic: dict[str, dict] = {}
        self.started_at = utcnow()

    # ------------------------------------------------------------ sources
    def run_now(self, source_id: str, trigger: str = "refresh") -> dict:
        spec = self.specs.get(source_id)
        if not spec:
            return {"source": source_id, "ok": False, "error": "unknown or disabled source"}
        lock = self._locks.setdefault(source_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"source": source_id, "ok": False, "error": "already running"}
        try:
            return run_source(self.store, spec, self.client, blocked=self.blocked, db_lock=self.db_lock,
                              trigger=trigger)
        finally:
            lock.release()

    def _last_fetch(self) -> dict:
        with self.db_lock:                         # the shared connection: never outside the lock
            return self.store.last_fetch()

    # ------------------------------------------------------------ periodic tasks
    def task_refresh_requests(self) -> dict:
        conn = self.store.conn
        with self.db_lock:
            rows = conn.execute("SELECT source, max(requested_at) FROM montney.refresh_request GROUP BY 1").fetchall()
        ran, unknown = [], []
        for source, upto in rows:
            if source in self.specs:
                self.run_now(source)
                ran.append(source)
            else:
                unknown.append(source)
                log.warning("refresh request for unknown source %r ignored", source)
            with self.db_lock:
                conn.execute("DELETE FROM montney.refresh_request WHERE source = %s AND requested_at <= %s",
                             (source, upto))
        return {"ran": ran, "unknown": unknown}

    def task_heartbeat(self) -> dict:
        """One row in montney.runner_state, read by /api/status (orchestrator, #88): the UI flags "runner not
        running" when it's older than 60 s. Times from the database clock, so age_s is consistent."""
        import os
        import socket

        from montney import __version__

        # Its own small connection, never db_lock: a long write or du holding the shared connection must not
        # stall the beat and make the UI say "runner not running" (reviewer, #90).
        if getattr(self, "_beat_conn", None) is None or self._beat_conn.closed:
            from montney.db import connect

            self._beat_conn = connect(dbname=self.store.conn.info.dbname)
        self._beat_conn.execute("""
            INSERT INTO montney.runner_state (id, last_beat, started_at, pid, version, host)
            VALUES (1, now() AT TIME ZONE 'UTC', %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET last_beat = excluded.last_beat, started_at = excluded.started_at,
                pid = excluded.pid, version = excluded.version, host = excluded.host""",
            (self.started_at, os.getpid(), __version__, socket.gethostname()))
        return {"pid": os.getpid()}

    def task_budget_states(self) -> dict:
        from montney.db import du

        with self.db_lock:
            self.blocked = du.blocked_sources(self.store.conn)
        return {"blocked": sorted(self.blocked)}

    def task_du(self) -> dict:
        from montney import paths
        from montney.db import du

        from montney.db import connect

        # Its own short-lived connection, so the (possibly slow) size scan never holds the shared connection's
        # lock and fetches keep writing meanwhile (reviewer, #85). Only self.blocked needs the lock.
        conn = connect(dbname=self.store.conn.info.dbname)
        try:
            rep = du.compute(conn, home=self.home or paths.home())
            du.save(conn, rep)
            states = du.apply_states(conn, rep)
            blocked = du.blocked_sources(conn)
        finally:
            conn.close()
        with self.db_lock:
            self.blocked = blocked
        for w in rep.warnings:
            log.warning("du: %s", w)
        return {"total_pct": round(rep.total_pct, 2), "states": states}

    def task_fill_realized(self) -> dict:
        from montney.db.backfill import fill_realized

        with self.db_lock:
            return fill_realized(self.store.conn)

    def task_backup(self, now=None) -> dict:
        """Whatever is due (backup.due): a dump, then a restore test of the newest backup. pg_dump/pg_restore
        are separate processes with their own connections, so this never takes db_lock; it only blocks its
        own periodic loop while it runs."""
        import os

        from montney.db import backup, connect

        if os.environ.get("MONTNEY_BACKUP", "on").lower() == "off":
            return {"skipped": "MONTNEY_BACKUP=off"}
        dbname = self.store.conn.info.dbname
        conn = connect(dbname=dbname)
        try:
            todo = backup.due(now or utcnow(), backup.run_state(conn))
        finally:
            conn.close()
        out: dict = {"due": todo}
        if "dump" in todo:
            try:
                r = backup.dump(dbname=dbname, reason="nightly")
                out["dump"] = {"path": str(r.path), "bytes": r.bytes, "pruned": r.pruned}
            except backup.BackupError as e:
                log.error("backup: nightly dump FAILED: %s", e)
                out["dump"] = {"error": str(e)}
                return out                      # no restore test without a fresh dump
        if "restore_test" in todo:
            res = backup.restore_test(dbname=dbname, reason="monthly")
            (log.info if res.ok else log.error)("backup: monthly restore test %s: %s", "PASS" if res.ok else "FAIL",
                                                [c for c in res.checks if not c[1]] or res.path)
            out["restore_test"] = {"ok": res.ok, "path": str(res.path), "failed": [c for c in res.checks if not c[1]]}
        return out

    def run_periodic(self, name: str) -> dict:
        try:
            out = getattr(self, f"task_{name}")()
            self.last_periodic[name] = {"ok": True, "at": utcnow().isoformat(), "result": out}
            return out
        except Exception as e:  # a failing task must not stop the runner
            log.warning("periodic %s failed: %s: %s", name, type(e).__name__, e)
            self.last_periodic[name] = {"ok": False, "at": utcnow().isoformat(), "error": f"{type(e).__name__}: {e}"}
            return {"error": str(e)}

    async def _periodic(self, name: str, every: float) -> None:
        while True:
            await asyncio.to_thread(self.run_periodic, name)
            await asyncio.sleep(every)

    # ------------------------------------------------------------ lifecycle
    async def stop(self) -> None:
        await super().stop()
        if getattr(self, "_beat_conn", None) is not None:
            self._beat_conn.close()

    def start(self) -> None:
        self.store.w.acquire_writer_lock()        # WriterBusy if another runner (or an import) is writing
        self.started_at = utcnow()
        self.task_heartbeat()                      # beat at once, before the first fetch
        with self.db_lock:
            seed_catalog(self.store, self.specs)
        self.task_budget_states()
        for sid in self.specs:
            self._tasks.append(asyncio.create_task(self._loop(sid), name=f"fetch:{sid}"))
        for name, every in self.PERIODIC.items():
            self._tasks.append(asyncio.create_task(self._periodic(name, every), name=f"periodic:{name}"))


def setup_runner_logging(home) -> None:
    """A size-capped log in <home>/logs/runner.log (5 x 5 MB), plus the console."""
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    d = Path(home) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(d / "runner.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(h)


def quiet_console() -> None:
    """Drop the console handlers (logging.basicConfig's stderr), keeping the file ones. Under the service
    supervisor stderr goes to a file that is only replaced at the next start, so it must not grow with every
    log line of a months-long run; a crash's traceback still reaches stderr (and runner.log)."""
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, (logging.FileHandler, RotatingFileHandler)):
            root.removeHandler(h)


# The runner's own "configuration error, don't restart me" exit code (sysexits EX_CONFIG). Not 2: uv and
# argparse exit 2 too (uv when it can't spawn the command, e.g. mid-sync), and those are worth a retry
# (reviewer, #99). The service supervisor (scripts/db/montney-runner.ps1) stops only on 0 and this.
EXIT_CONFIG = 78


def run_forever() -> int:
    """`montney run`: needs MONTNEY_DB=postgres. Returns an exit code."""
    import os

    from montney import paths
    from montney.store import open_store

    if os.environ.get("MONTNEY_DB", "duckdb").lower() != "postgres":
        print("montney run is the Postgres runner: set MONTNEY_DB=postgres (DuckDB keeps the in-process scheduler)")
        return EXIT_CONFIG
    setup_runner_logging(paths.home())
    if os.environ.get("MONTNEY_LOG_CONSOLE", "1") == "0":
        quiet_console()          # the service supervisor: runner.log (size-capped) is the log
    try:
        store = open_store("writer")
        runner = PgRunner(store)
    except Exception:
        log.exception("runner: could not start")
        raise

    async def _main():
        runner.start()
        log.info("runner: %d sources, periodic %s", len(runner.specs), runner.PERIODIC)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.stop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("runner: crashed")       # into runner.log too, not only the console
        raise
    finally:
        store.close()
    return 0
