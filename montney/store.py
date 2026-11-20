"""DuckDB-backed observation store.

One long table holds everything:

    observations(series_id, ts, value, as_of, fetched_at)

* ts     - the time the value is FOR. Naive UTC for intraday series; for daily
           series (freq "D", e.g. gas days, weather days) it is the calendar date
           at 00:00 with no timezone meaning.
* as_of  - the vintage: when the value was known. Re-fetching a revised gas day or
           a new weather run adds a row instead of overwriting, so you can compare
           today's forecast with yesterday's. Queries return the latest vintage
           unless you ask for an older one.

Sources in "live" mode go to a TEMP (in-memory) table instead and are replaced on
every fetch - nothing is persisted.

All access goes through one connection guarded by a lock: DuckDB allows a single
writer process, and this app is that process.
"""

from __future__ import annotations

import csv
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    series_id  VARCHAR   NOT NULL,
    ts         TIMESTAMP NOT NULL,
    value      DOUBLE,
    as_of      TIMESTAMP NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (series_id, ts, as_of)
);
CREATE TABLE IF NOT EXISTS series (
    series_id VARCHAR PRIMARY KEY,
    source    VARCHAR,
    label     VARCHAR,
    unit      VARCHAR,
    freq      VARCHAR,
    grp       VARCHAR,
    day       VARCHAR DEFAULT ''   -- daily series: '' = calendar day (Alberta), 'gas' = NGTL gas day
);
ALTER TABLE series ADD COLUMN IF NOT EXISTS day VARCHAR DEFAULT '';
CREATE TABLE IF NOT EXISTS events (
    id         VARCHAR PRIMARY KEY,
    source     VARCHAR,
    kind       VARCHAR,
    title      VARCHAR,
    body       VARCHAR,
    url        VARCHAR,
    starts     TIMESTAMP,
    ends       TIMESTAMP,
    refs       VARCHAR[],
    severity   VARCHAR,
    published  TIMESTAMP,
    extra      VARCHAR,
    first_seen TIMESTAMP,
    updated    TIMESTAMP,
    status     VARCHAR DEFAULT 'active',   -- active | withdrawn
    content    VARCHAR                     -- hash of the user-visible fields
);
CREATE TABLE IF NOT EXISTS fetch_log (
    source   VARCHAR,
    started  TIMESTAMP,
    finished TIMESTAMP,
    ok       BOOLEAN,
    n_obs    INTEGER,
    error    VARCHAR
);
"""


@dataclass(slots=True)
class Obs:
    series_id: str
    ts: datetime
    value: float
    as_of: datetime


@dataclass(slots=True)
class SeriesMeta:
    series_id: str
    source: str
    label: str = ""
    unit: str = ""
    freq: str = ""
    group: str = ""
    extra: dict = field(default_factory=dict)
    day: str = ""          # daily series only: "" = calendar day, "gas" = NGTL gas day (15:00 UTC)


@dataclass(slots=True)
class Event:
    """A discrete item: outage/maintenance, bulletin, notice, grid alert.

    `id` must be stable across fetches ("<source>:<native id>") so re-fetching updates in place.
    Times are naive UTC. `refs` are network feature ids (see docs/ARCHITECTURE.md).
    """
    id: str
    source: str
    kind: str                      # outage | bulletin | notice | alert
    title: str
    body: str = ""
    url: str = ""
    starts: datetime | None = None
    ends: datetime | None = None
    refs: list[str] = field(default_factory=list)
    severity: str = "info"         # info | warn | critical
    published: datetime | None = None
    extra: dict = field(default_factory=dict)


EVENT_KINDS = {"outage", "bulletin", "notice", "alert"}
SEVERITIES = {"info", "warn", "critical"}


def _bumps(fn):
    """L-081: bump the store's data_version AFTER a write (in `finally`: a partial write counts too), so a snapshot
    built while the write runs is keyed to the old version and can't be cached as new with old data (#312 review)."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        finally:
            self._version += 1
    return wrapper


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.con = duckdb.connect(self.path)
        self.lock = threading.Lock()
        self._version = 0       # bumped by every write: the server's cache key (data_version, L-081)
        with self.lock:
            self.con.execute(SCHEMA)
            self.con.execute(
                "CREATE TEMP TABLE IF NOT EXISTS live_obs (series_id VARCHAR NOT NULL, ts TIMESTAMP NOT NULL, "
                "value DOUBLE, as_of TIMESTAMP NOT NULL, fetched_at TIMESTAMP NOT NULL, "
                "PRIMARY KEY (series_id, ts, as_of))"
            )

    def close(self) -> None:
        with self.lock:
            self.con.close()

    # ---------------------------------------------------------------- writes
    @_bumps
    def upsert_meta(self, metas: Iterable[SeriesMeta]) -> None:
        rows = [(m.series_id, m.source, m.label, m.unit, m.freq, m.group, m.day or "") for m in metas]
        if not rows:
            return
        with self.lock:
            self.con.executemany(
                "INSERT OR REPLACE INTO series (series_id, source, label, unit, freq, grp, day) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    @_bumps
    def write(self, obs: list[Obs], live: bool = False) -> int:
        """Insert observations. Same (series, ts, as_of) overwrites; new as_of adds a vintage."""
        if not obs:
            return 0
        now = _utcnow().replace(microsecond=0)
        # Stage through a temp CSV: far faster than executemany for big imports
        # and keeps us free of pandas/pyarrow.
        fd, tmp = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                w = csv.writer(f)
                for o in obs:
                    w.writerow((o.series_id, o.ts.isoformat(sep=" "), repr(float(o.value)),
                                o.as_of.isoformat(sep=" "), now.isoformat(sep=" ")))
            table = "live_obs" if live else "observations"
            with self.lock:
                stage = self.con.read_csv(
                    tmp, header=False,
                    names=["series_id", "ts", "value", "as_of", "fetched_at"],
                    dtype=["VARCHAR", "TIMESTAMP", "DOUBLE", "TIMESTAMP", "TIMESTAMP"],
                )
                self.con.register("_stage", stage)
                try:
                    if live:
                        self.con.execute(
                            "DELETE FROM live_obs WHERE series_id IN (SELECT DISTINCT series_id FROM _stage)"
                        )
                    # DISTINCT ON guards against duplicate keys inside one batch.
                    self.con.execute(
                        f"INSERT OR REPLACE INTO {table} "
                        "SELECT DISTINCT ON (series_id, ts, as_of) * FROM _stage"
                    )
                finally:
                    self.con.unregister("_stage")
        finally:
            os.unlink(tmp)
        return len(obs)

    @_bumps
    def upsert_events(self, events: Iterable[Event],
                      complete_scopes: Iterable[tuple[str, str]] = ()) -> int:
        """Upsert by stable id. `updated` only moves when the content changes (so `?since=` is a
        real change feed). For each (source, kind) in `complete_scopes`, `events` is the full
        current set: active events of that scope that are missing get status 'withdrawn'
        (e.g. a cancelled outage that dropped out of the operator's republish)."""
        import hashlib
        import json as _json
        now = _utcnow().replace(microsecond=0)
        rows, seen = [], {}
        for e in events:
            if e.kind not in EVENT_KINDS:
                raise ValueError(f"event {e.id}: kind must be one of {sorted(EVENT_KINDS)}")
            if e.severity not in SEVERITIES:
                raise ValueError(f"event {e.id}: severity must be one of {sorted(SEVERITIES)}")
            extra = _json.dumps(e.extra, sort_keys=True)
            content = hashlib.sha1(repr((e.kind, e.title, e.body, e.url, e.starts, e.ends,
                                         sorted(e.refs), e.severity, e.published, extra))
                                   .encode()).hexdigest()
            rows.append([e.id, e.source, e.kind, e.title, e.body, e.url, e.starts, e.ends,
                         list(e.refs), e.severity, e.published, extra, now, now, "active", content])
            seen.setdefault((e.source, e.kind), []).append(e.id)
        with self.lock:
            if rows:
                self.con.executemany("""
                    INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                      source = excluded.source, kind = excluded.kind, title = excluded.title,
                      body = excluded.body, url = excluded.url, starts = excluded.starts,
                      ends = excluded.ends, refs = excluded.refs, severity = excluded.severity,
                      published = excluded.published, extra = excluded.extra,
                      updated = CASE WHEN events.content IS DISTINCT FROM excluded.content
                                       OR events.status = 'withdrawn'
                                     THEN excluded.updated ELSE events.updated END,
                      status = 'active', content = excluded.content
                """, rows)
            for source, kind in complete_scopes:
                ids = seen.get((source, kind), [])
                self.con.execute("""
                    UPDATE events SET status = 'withdrawn', updated = ?
                    WHERE source = ? AND kind = ? AND status = 'active'
                      AND NOT list_contains(?, id)
                """, [now, source, kind, ids])
        return len(rows)

    EVENT_STATES = ("ongoing", "upcoming", "current", "past")
    # Events without `ends` stay "ongoing" only this long after they were published/first seen.
    # Outages are expected to carry real start/end times, so they have no default lifetime.
    EVENT_TTL_HOURS = {"alert": 12, "bulletin": 48, "notice": 72}

    def events(self, *, kinds: list[str] | None = None, ref: str | None = None,
               state: str | None = None, at: datetime | None = None,
               since: datetime | None = None, include_withdrawn: bool = False,
               limit: int = 200) -> list[dict]:
        """Events, newest first.

        state (relative to `at`, default now): ongoing = started and not ended;
        upcoming = starts in the future; current = ongoing + upcoming; past = ended.
        An event without `ends` is treated as ending EVENT_TTL_HOURS[kind] after it was
        published (or first seen); an outage without `ends` stays open. `since`: changed after
        (includes withdrawals).
        """
        import json as _json
        at = at or _utcnow()
        where, params = ["TRUE"], []
        if not include_withdrawn and not since:
            where.append("status = 'active'")
        if kinds:
            where.append("kind IN (SELECT unnest(?))")
            params.append(kinds)
        if ref:
            where.append("list_contains(refs, ?)")
            params.append(ref)
        if state:
            if state not in self.EVENT_STATES:
                raise ValueError(f"state must be one of {self.EVENT_STATES}")
            ttl = " ".join(
                f"WHEN '{k}' THEN coalesce(published, first_seen) + INTERVAL {int(h)} HOUR"
                for k, h in self.EVENT_TTL_HOURS.items())
            eff_end = f"coalesce(ends, CASE kind {ttl} END)"   # NULL = open-ended (outages)
            cond = {
                "ongoing": f"(starts IS NULL OR starts <= $at) AND ({eff_end} IS NULL OR {eff_end} >= $at)",
                "upcoming": "starts > $at",
                "current": f"({eff_end} IS NULL OR {eff_end} >= $at)",
                "past": f"{eff_end} < $at",
            }[state]
            n = cond.count("$at")
            where.append(cond.replace("$at", "?"))
            params.extend([at] * n)
        if since:
            where.append("updated > ?")
            params.append(since)
        rows = self._q(f"""
            SELECT id, source, kind, title, body, url, starts, ends, refs, severity, published,
                   extra, first_seen, updated, status
            FROM events WHERE {' AND '.join(where)}
            ORDER BY coalesce(published, first_seen) DESC LIMIT {int(limit)}
        """, params)
        keys = ["id", "source", "kind", "title", "body", "url", "starts", "ends", "refs",
                "severity", "published", "extra", "first_seen", "updated", "status"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            for k in ("starts", "ends", "published", "first_seen", "updated"):
                d[k] = d[k].isoformat() if d[k] else None
            d["extra"] = _json.loads(d["extra"] or "{}")
            out.append(d)
        return out

    @_bumps
    def log_fetch(self, source: str, started: datetime, ok: bool, n: int, error: str | None, *,
                  n_requests: int | None = None, bytes_in: int | None = None, trigger: str | None = None) -> None:
        """n_requests / bytes_in / trigger are recorded on Postgres only (fetch_run, L-081); DuckDB's fetch_log has none."""
        with self.lock:
            self.con.execute(
                "INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?)",
                [source, started, _utcnow(), ok, n, (error or "")[:2000]],
            )

    # ----------------------------------------------------------------- reads
    def _q(self, sql: str, params: list | None = None) -> list[tuple]:
        with self.lock:
            return self.con.execute(sql, params or []).fetchall()

    def series(
        self,
        ids: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
        as_of_max: datetime | None = None,
    ) -> dict[str, dict]:
        """Latest vintage (optionally: latest vintage known by `as_of_max`) per timestamp."""
        if not ids:
            return {}
        where = ["series_id IN (SELECT unnest(?))"]
        params: list = [ids]
        if start:
            where.append("ts >= ?")
            params.append(start)
        if end:
            where.append("ts <= ?")
            params.append(end)
        if as_of_max:
            where.append("as_of <= ?")
            params.append(as_of_max)
        w = " AND ".join(where)
        sql = f"""
            SELECT series_id, ts, arg_max(value, as_of) AS v, max(as_of) AS a
            FROM (SELECT * FROM observations WHERE {w}
                  UNION ALL SELECT * FROM live_obs WHERE {w})
            GROUP BY series_id, ts
            ORDER BY series_id, ts
        """
        rows = self._q(sql, params + params)
        out: dict[str, dict] = {i: {"ts": [], "v": [], "as_of": None} for i in ids}
        for sid, ts, v, a in rows:
            d = out[sid]
            d["ts"].append(ts.timestamp() if ts.tzinfo else _epoch(ts))
            d["v"].append(v)
            if d["as_of"] is None or a > d["as_of"]:
                d["as_of"] = a
        for d in out.values():
            d["as_of"] = d["as_of"].isoformat() if d["as_of"] else None
        return out

    def catalog(self) -> list[dict]:
        rows = self._q("""
            WITH stats AS (
                SELECT series_id, count(DISTINCT ts) n, min(ts) first, max(ts) last, max(fetched_at) fetched
                FROM (SELECT * FROM observations UNION ALL SELECT * FROM live_obs)
                GROUP BY series_id
            )
            SELECT s.series_id, s.source, s.label, s.unit, s.freq, s.grp,
                   coalesce(st.n, 0), st.first, st.last, st.fetched
            FROM series s LEFT JOIN stats st USING (series_id)
            ORDER BY s.source, s.series_id
        """)
        keys = ["id", "source", "label", "unit", "freq", "group", "n", "first", "last", "fetched"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            for k in ("first", "last", "fetched"):
                d[k] = d[k].isoformat() if d[k] else None
            out.append(d)
        return out

    def meta(self, ids: list[str]) -> dict[str, dict]:
        rows = self._q(
            "SELECT series_id, source, label, unit, freq, grp, day FROM series WHERE series_id IN (SELECT unnest(?))",
            [ids],
        )
        return {r[0]: {"source": r[1], "label": r[2], "unit": r[3], "freq": r[4], "group": r[5],
                       "day": r[6] or ""} for r in rows}

    def last_fetch(self) -> dict[str, dict]:
        rows = self._q("""
            SELECT source, arg_max(finished, started), arg_max(ok, started), arg_max(n_obs, started),
                   arg_max(error, started), max(finished) FILTER (WHERE ok)
            FROM fetch_log GROUP BY source
        """)
        return {
            r[0]: {"finished": _iso(r[1]), "ok": r[2], "n_obs": r[3], "error": r[4] or None,
                   "last_ok": _iso(r[5])}
            for r in rows
        }

    def count(self) -> int:
        return self._q("SELECT count(*) FROM observations")[0][0]

    def data_version(self, any_run: bool = False) -> str:
        """Changes whenever anything the API reads may have changed (L-081). DuckDB runs in one process (the server's
        own scheduler writes), so a counter bumped after every write (and every fetch log, ok or not) is exact."""
        return f"d{self._version}"


def _epoch(ts: datetime) -> float:
    # naive timestamps are UTC by convention
    return (ts - datetime(1970, 1, 1)).total_seconds()


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def open_store(role: str = "writer"):
    """The store the app should use (docs/db/PORT.md, L-035).

    MONTNEY_DB=postgres → montney.db.pgstore.PostgresStore on the `writer` or the read-only `reader` role;
    otherwise (the default until Erik's install, import and a parity run on his data) v1's DuckDB Store.
    This is the only call server.py / cli.py need to switch."""
    if os.environ.get("MONTNEY_DB", "duckdb").lower() == "postgres":
        from montney.db import connect
        from montney.db.pgstore import PostgresStore

        return PostgresStore(connect(role=role))
    from montney import paths

    return Store(paths.db_path())
