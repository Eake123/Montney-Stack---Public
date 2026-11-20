"""`PostgresStore`: a drop-in for v1's DuckDB `montney.store.Store` (docs/db/PORT.md, L-035).

Same methods, same signatures, same return shapes, so `snapshot.py` and `server.py` don't change how they call
the store. Writes go through `PgStore` (change-only, re-anchoring, retries); reads are the queries in PORT.md.
`tests/test_db_parity.py` feeds identical calls to both stores and compares every read, plus whole
`snapshot.build()` outputs.

Deliberate differences, all stated in PORT.md and pinned by the parity suite:
* `count()` is rows STORED. Change-only skips repeated vintages that v1 stored, so it is <= v1's.
* `series(...)["as_of"]`: the newest stored vintage among the returned rows, raised to
  `series_state.last_as_of` (the latest vintage seen) when within `as_of_max`. Equal to v1 except with an
  `as_of_max` between two identical vintages (allowlisted: <= v1). Values and ts are always identical.
* `write()` refuses series that were never registered (`upsert_meta` first), unless `source=` is given, in
  which case they're registered under it with a freq-derived tier. v1 accepted anything.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable

from montney.db.store import PgStore
from montney.store import EVENT_KINDS, SEVERITIES, Event, Obs, SeriesMeta

FAST = {"1min", "5min", "10min", "15min", "30min"}
HOURLY = {"1h", "60min", "h", "H"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch(ts: datetime) -> float:
    return (ts - datetime(1970, 1, 1)).total_seconds()


def _iso(d):
    return d.isoformat() if d else None


def tier_for(meta: SeriesMeta) -> str | None:
    """Explicit tier (SeriesMeta.extra['tier'], later the spec's `retention:`), else None."""
    t = (meta.extra or {}).get("tier")
    return t or None


def derived_tier(freq: str) -> str:
    if freq in FAST:
        return "fast"
    if freq in HOURLY:
        return "hourly"
    return "daily"


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


class PostgresStore:
    EVENT_STATES = ("ongoing", "upcoming", "current", "past")
    EVENT_TTL_HOURS = {"alert": 12, "bulletin": 48, "notice": 72}
    # Seeded reds for the parity suite (PORT.md): each switch reproduces a classic port bug.
    _VINTAGE_ORDER = "DESC"      # "ASC": the earliest vintage per ts
    _ASOF_OP = "<="              # "<": the as_of_max boundary off by one
    _INCLUDE_LIVE = True         # False: obs_live left out of series()
    _SINCE_OP = ">"              # ">=": `since` off by one

    def __init__(self, conn):
        self.conn = conn
        self.w = PgStore(conn)
        self._version = 0       # writes made through THIS process (tests, `montney try --write`): data_version
        self._fetch_cols: set | None = None         # optional fetch_run columns present (0012 n_requests, 0013 trigger)
        info = conn.info
        self.path = f"postgres://{info.user}@{info.host}:{info.port}/{info.dbname}"

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- writes (v1 signatures)
    def _source(self, code: str) -> None:
        # SELECT first: INSERT ... ON CONFLICT calls nextval every time (smallint identity; see PgStore.ensure_source)
        if self.conn.execute("SELECT 1 FROM montney.source WHERE code = %s", (code,)).fetchone() is None:
            self.conn.execute("INSERT INTO montney.source (code) VALUES (%s) ON CONFLICT (code) DO NOTHING", (code,))

    @_bumps
    def upsert_meta(self, metas: Iterable[SeriesMeta]) -> None:
        """Register series (v1 signature). In key order, and retried like write() on a lost lock fight, since
        du's budget update runs on another connection (reviewer, #91)."""
        import random
        import time

        from montney.db.store import _retryable, write_retries

        metas = sorted(metas, key=lambda m: m.series_id)
        retries = write_retries()
        for attempt in range(retries + 1):
            try:
                self._upsert_meta(metas)
                return
            except _retryable():
                if attempt == retries:
                    raise
                time.sleep(min(2.0, 0.1 * 2 ** attempt) * (1 + random.random()))

    def _upsert_meta(self, metas: list[SeriesMeta]) -> None:
        for m in metas:
            self._source(m.source)
            row = self.conn.execute("SELECT tier FROM montney.series WHERE key = %s", (m.series_id,)).fetchone()
            explicit = tier_for(m)
            # An existing series keeps its tier unless the spec states one (then a conflict is an error).
            tier = explicit or (row[0] if row else derived_tier(m.freq))
            self.w.upsert_series(m.series_id, source=m.source, tier=tier, label=m.label, unit=m.unit,
                                 freq=m.freq, day=m.day or "", attrs={"grp": m.group} if m.group else {})

    @_bumps
    def write(self, obs: list[Obs], live: bool = False, source: str | None = None) -> int:
        """v1 contract: returns len(obs) (rows received). Change-only decides what is stored."""
        if not obs:
            return 0
        if source:
            known = {k for (k,) in self.conn.execute("SELECT key FROM montney.series WHERE key = ANY(%s)",
                                                     (list({o.series_id for o in obs}),)).fetchall()}
            self._source(source)
            for k in {o.series_id for o in obs} - known:
                self.w.upsert_series(k, source=source, tier="daily")
        if live:
            self._write_live(obs)
        else:
            self.w.write(obs)
        return len(obs)

    def _write_live(self, obs: list[Obs]) -> None:
        self.w._resolve(o.series_id for o in obs)
        now = _utcnow().replace(microsecond=0)
        rows = {(self.w._ids[o.series_id][0], o.ts, o.as_of): o.value for o in obs}
        sids = sorted({k[0] for k in rows})
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM montney.obs_live WHERE series_id = ANY(%s)", (sids,))
            cur.executemany("INSERT INTO montney.obs_live (series_id, ts, as_of, value, fetched_at) "
                            "VALUES (%s, %s, %s, %s, %s)", [(s, t, a, v, now) for (s, t, a), v in rows.items()])
            self.w._touch_state(cur, {(s, t, a): ("live", v) for (s, t, a), v in rows.items()}, now)

    @_bumps
    def upsert_events(self, events: Iterable[Event], complete_scopes: Iterable[tuple[str, str]] = ()) -> int:
        now = _utcnow().replace(microsecond=0)
        rows, seen = [], {}
        for e in events:
            if e.kind not in EVENT_KINDS:
                raise ValueError(f"event {e.id}: kind must be one of {sorted(EVENT_KINDS)}")
            if e.severity not in SEVERITIES:
                raise ValueError(f"event {e.id}: severity must be one of {sorted(SEVERITIES)}")
            extra = json.dumps(e.extra, sort_keys=True)
            # the same content hash as v1, so `updated` moves on the same changes
            content = hashlib.sha1(repr((e.kind, e.title, e.body, e.url, e.starts, e.ends, sorted(e.refs),
                                         e.severity, e.published, extra)).encode()).digest()
            rows.append((e, extra, content))
            seen.setdefault((e.source, e.kind), []).append(e.id)
        with self.conn.transaction(), self.conn.cursor() as cur:
            for e, extra, content in rows:
                self._source(e.source)
                cur.execute("""
                    INSERT INTO montney.event AS t (id, source_id, kind, title, body, url, starts, ends, severity,
                        published, extra, first_seen, updated, status, content_sha1)
                    VALUES (%s, (SELECT id FROM montney.source WHERE code = %s), %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb, %s, %s, 'active', %s)
                    ON CONFLICT (id) DO UPDATE SET
                      source_id = excluded.source_id, kind = excluded.kind, title = excluded.title,
                      body = excluded.body, url = excluded.url, starts = excluded.starts, ends = excluded.ends,
                      severity = excluded.severity, published = excluded.published, extra = excluded.extra,
                      updated = CASE WHEN t.content_sha1 IS DISTINCT FROM excluded.content_sha1
                                       OR t.status = 'withdrawn' THEN excluded.updated ELSE t.updated END,
                      status = 'active', content_sha1 = excluded.content_sha1""",
                            (e.id, e.source, e.kind, e.title, e.body, e.url, e.starts, e.ends, e.severity,
                             e.published, extra, now, now, content))
                cur.execute("DELETE FROM montney.event_scope WHERE event_id = %s", (e.id,))
                for i, ref in enumerate(dict.fromkeys(e.refs)):
                    cur.execute("INSERT INTO montney.event_scope (event_id, ref, ord) VALUES (%s, %s, %s)",
                                (e.id, ref, i))
            for source, kind in complete_scopes:
                cur.execute("""
                    UPDATE montney.event SET status = 'withdrawn', updated = %s
                    WHERE source_id = (SELECT id FROM montney.source WHERE code = %s) AND kind = %s
                      AND status = 'active' AND NOT (id = ANY(%s))""",
                            (now, source, kind, seen.get((source, kind), [])))
        return len(rows)

    def set_source_region(self, source: str, region: str) -> bool:
        """A spec's `region:` (L-085): put the source, and every series it already has, in that region (storage and
        request budgets, `montney du`). New series inherit the source's region on insert. Returns False (and changes
        nothing) for a region code the `region` table doesn't have."""
        self._source(source)
        rid = self.conn.execute("SELECT id FROM montney.region WHERE code = %s", (region,)).fetchone()
        if rid is None:
            return False
        self.conn.execute("UPDATE montney.source SET region_id = %s WHERE code = %s AND region_id <> %s",
                          (rid[0], source, rid[0]))
        self.conn.execute("UPDATE montney.series SET region_id = %s WHERE region_id <> %s AND source_id = "
                          "(SELECT id FROM montney.source WHERE code = %s)", (rid[0], rid[0], source))
        return True

    @_bumps
    def log_fetch(self, source: str, started: datetime, ok: bool, n: int, error: str | None, *,
                  n_requests: int | None = None, bytes_in: int | None = None, trigger: str | None = None) -> None:
        """n_requests / bytes_in: what the run downloaded (L-081, migration 0012), None when not measured. trigger: what
        started it, schedule | refresh | manual | import (0013). On a database without those migrations yet (new code,
        old database) the missing columns are left out rather than failing every run."""
        self._source(source)
        if self._fetch_cols is None:
            self._fetch_cols = {c for (c,) in self.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'montney' "
                "AND table_name = 'fetch_run' AND column_name IN ('n_requests', 'trigger')").fetchall()}
        opt = [(c, v) for c, v in (("n_requests", n_requests), ("trigger", trigger)) if c in self._fetch_cols]
        cols, vals = "".join(f", {c}" for c, _ in opt), tuple(v for _, v in opt)
        self.conn.execute(
            f"INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written, error, bytes_in{cols}) "
            f"VALUES ((SELECT id FROM montney.source WHERE code = %s), %s, %s, %s, %s, %s, %s{', %s' * len(vals)})",
            (source, started, _utcnow(), ok, n, (error or "")[:2000], bytes_in, *vals))
        if ok:
            self.conn.execute("UPDATE montney.source SET last_ok_at = %s WHERE code = %s", (_utcnow(), source))

    # ---------------------------------------------------------------- reads (v1 shapes)
    def series(self, ids: list[str], start: datetime | None = None, end: datetime | None = None,
               as_of_max: datetime | None = None) -> dict[str, dict]:
        if not ids:
            return {}
        where, params = ["s.key = ANY(%s)"], [ids]
        if start:
            where.append("o.ts >= %s")
            params.append(start)
        if end:
            where.append("o.ts <= %s")
            params.append(end)
        if as_of_max:
            where.append(f"o.as_of {self._ASOF_OP} %s")
            params.append(as_of_max)
        w = " AND ".join(where)
        live = "UNION ALL SELECT series_id, ts, value, as_of FROM montney.obs_live" if self._INCLUDE_LIVE else ""
        rows = self.conn.execute(f"""
            SELECT DISTINCT ON (s.key, o.ts) s.key, o.ts, o.value, o.as_of
            FROM (SELECT series_id, ts, value, as_of FROM montney.obs_fast
                  UNION ALL SELECT series_id, ts, value, as_of FROM montney.obs_hourly
                  UNION ALL SELECT series_id, ts, value, as_of FROM montney.obs_daily
                  UNION ALL SELECT series_id, ts, value, as_of FROM montney.obs_forecast
                  {live}) o
            JOIN montney.series s ON s.id = o.series_id
            WHERE {w}
            ORDER BY s.key, o.ts, o.as_of {self._VINTAGE_ORDER}""", params).fetchall()
        out: dict[str, dict] = {i: {"ts": [], "v": [], "as_of": None} for i in ids}
        for key, ts, v, a in rows:
            d = out[key]
            d["ts"].append(_epoch(ts))
            d["v"].append(v)
            if d["as_of"] is None or a > d["as_of"]:
                d["as_of"] = a
        # L-035 (b): as_of also counts the latest vintage SEEN (series_state.last_as_of), including identical
        # ones change-only didn't store, so a re-fetch shows up as in DuckDB. Only for series that returned
        # rows, and only within as_of_max.
        # last_as_of is the newest vintage SEEN; seen_from_ts..seen_to_ts the points it covered (0011). It only
        # speaks for a window overlapping them: a history window (e.g. 2021) keeps the max as_of of its own rows,
        # as v1 does (real-data parity, 2026-11-01). Rows from before 0011: [last_ts, last_ts].
        for key, last, lo, hi in self.conn.execute(
                "SELECT s.key, st.last_as_of, coalesce(st.seen_from_ts, st.last_ts), "
                "coalesce(st.seen_to_ts, st.last_ts) FROM montney.series s JOIN montney.series_state st "
                "ON st.series_id = s.id WHERE s.key = ANY(%s)", (ids,)).fetchall():
            d = out.get(key)
            in_window = lo is not None and (end is None or lo <= end) and (start is None or hi >= start)
            if d and d["ts"] and last and in_window and (as_of_max is None or last <= as_of_max) \
                    and (d["as_of"] is None or last > d["as_of"]):
                d["as_of"] = last
        for d in out.values():
            d["as_of"] = d["as_of"].isoformat() if d["as_of"] else None
        return out

    # ---------------------------------------------------------------- reads for montney/readapi.py (D29)
    _ROLLUP_1D = {"fast": "obs_fast_1d", "hourly": "obs_hourly_1d"}

    def day_rollup(self, key: str, tier: str, lo: datetime, hi: datetime) -> tuple[list, str | None]:
        """One series' daily rollup rows [(bucket_ts, sum, n)] with lo <= bucket <= hi (buckets start 06:00 UTC,
        L-029), and the newest vintage seen for the series. The rollups are real-time continuous aggregates, so
        the latest days are included; they outlive the raw 5-min tier's 60-day retention."""
        table = self._ROLLUP_1D[tier]                       # KeyError on anything else: never a free table name
        rows = self.conn.execute(
            f"SELECT h.ts, h.sum, h.n FROM montney.{table} h JOIN montney.series s ON s.id = h.series_id "
            "WHERE s.key = %s AND h.ts >= %s AND h.ts <= %s ORDER BY h.ts", (key, lo, hi)).fetchall()
        st = self.conn.execute(
            "SELECT st.last_as_of FROM montney.series s JOIN montney.series_state st ON st.series_id = s.id "
            "WHERE s.key = %s", (key,)).fetchone()
        return rows, (st[0].isoformat() if st and st[0] else None)

    def daily_points(self, key: str, lo: datetime, hi: datetime) -> tuple[list, str | None]:
        """A daily-tier series' latest vintage per ts in [lo, hi] from obs_daily alone: [(ts, value)], and the
        newest as_of among them. series() unions every tier (~180 ms to plan on kraken); this reads one (~2 ms)."""
        rows = self.conn.execute(
            "SELECT DISTINCT ON (o.ts) o.ts, o.value, o.as_of FROM montney.obs_daily o "
            "JOIN montney.series s ON s.id = o.series_id WHERE s.key = %s AND o.ts >= %s AND o.ts <= %s "
            "ORDER BY o.ts, o.as_of DESC", (key, lo, hi)).fetchall()
        as_of = max((a for _, _, a in rows), default=None)
        return [(t, v) for t, v, _ in rows], (as_of.isoformat() if as_of else None)

    def series_bounds(self, key: str) -> tuple:
        """(first, last) date of a daily-tier series (obs_daily), or (None, None)."""
        r = self.conn.execute(
            "SELECT min(o.ts), max(o.ts) FROM montney.obs_daily o JOIN montney.series s ON s.id = o.series_id "
            "WHERE s.key = %s", (key,)).fetchone()
        return (r[0].date() if r and r[0] else None, r[1].date() if r and r[1] else None)

    def meta(self, ids: list[str]) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT s.key, src.code, s.label, s.unit, s.freq, coalesce(s.attrs ->> 'grp', ''), s.day "
            "FROM montney.series s JOIN montney.source src ON src.id = s.source_id WHERE s.key = ANY(%s)",
            (ids,)).fetchall()
        return {r[0]: {"source": r[1], "label": r[2], "unit": r[3], "freq": r[4], "group": r[5],
                       "day": r[6] or ""} for r in rows}

    def catalog(self) -> list[dict]:
        rows = self.conn.execute("""
            WITH o AS (SELECT series_id, ts FROM montney.obs_fast UNION ALL SELECT series_id, ts FROM montney.obs_hourly
                       UNION ALL SELECT series_id, ts FROM montney.obs_daily
                       UNION ALL SELECT series_id, ts FROM montney.obs_forecast
                       UNION ALL SELECT series_id, ts FROM montney.obs_live),
                 stats AS (SELECT series_id, count(DISTINCT ts) n, min(ts) first, max(ts) last FROM o GROUP BY 1)
            SELECT s.key, src.code, s.label, s.unit, s.freq, coalesce(s.attrs ->> 'grp', ''),
                   coalesce(st.n, 0), st.first, st.last, ss.last_seen_at
            FROM montney.series s JOIN montney.source src ON src.id = s.source_id
            LEFT JOIN stats st ON st.series_id = s.id
            LEFT JOIN montney.series_state ss ON ss.series_id = s.id
            ORDER BY src.code, s.key""").fetchall()
        keys = ["id", "source", "label", "unit", "freq", "group", "n", "first", "last", "fetched"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            for k in ("first", "last", "fetched"):
                d[k] = d[k].isoformat() if d[k] else None
            out.append(d)
        return out

    def events(self, *, kinds: list[str] | None = None, ref: str | None = None, state: str | None = None,
               at: datetime | None = None, since: datetime | None = None, include_withdrawn: bool = False,
               limit: int = 200) -> list[dict]:
        at = at or _utcnow()
        where, params = ["TRUE"], []
        if not include_withdrawn and not since:
            where.append("e.status = 'active'")
        if kinds:
            where.append("e.kind = ANY(%s)")
            params.append(kinds)
        if ref:
            where.append("EXISTS (SELECT 1 FROM montney.event_scope x WHERE x.event_id = e.id AND x.ref = %s)")
            params.append(ref)
        if state:
            if state not in self.EVENT_STATES:
                raise ValueError(f"state must be one of {self.EVENT_STATES}")
            ttl = " ".join(f"WHEN '{k}' THEN coalesce(e.published, e.first_seen) + INTERVAL '{int(h)} hours'"
                           for k, h in self.EVENT_TTL_HOURS.items())
            eff = f"coalesce(e.ends, CASE e.kind {ttl} END)"
            cond = {
                "ongoing": f"(e.starts IS NULL OR e.starts <= $at) AND ({eff} IS NULL OR {eff} >= $at)",
                "upcoming": "e.starts > $at",
                "current": f"({eff} IS NULL OR {eff} >= $at)",
                "past": f"{eff} < $at",
            }[state]
            n = cond.count("$at")
            where.append(cond.replace("$at", "%s"))
            params.extend([at] * n)
        if since:
            where.append(f"e.updated {self._SINCE_OP} %s")
            params.append(since)
        rows = self.conn.execute(f"""
            SELECT e.id, src.code, e.kind, e.title, e.body, e.url, e.starts, e.ends,
                   coalesce((SELECT array_agg(x.ref ORDER BY x.ord) FROM montney.event_scope x
                             WHERE x.event_id = e.id), '{{}}'),
                   e.severity, e.published, e.extra, e.first_seen, e.updated, e.status
            FROM montney.event e JOIN montney.source src ON src.id = e.source_id
            WHERE {' AND '.join(where)}
            ORDER BY coalesce(e.published, e.first_seen) DESC, e.id LIMIT {int(limit)}""", params).fetchall()
        keys = ["id", "source", "kind", "title", "body", "url", "starts", "ends", "refs", "severity", "published",
                "extra", "first_seen", "updated", "status"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            for k in ("starts", "ends", "published", "first_seen", "updated"):
                d[k] = d[k].isoformat() if d[k] else None
            d["refs"] = list(d["refs"])
            d["extra"] = d["extra"] or {}
            out.append(d)
        return out

    def last_fetch(self) -> dict[str, dict]:
        rows = self.conn.execute("""
            SELECT src.code, l.finished, l.ok, l.n_written, l.error, m.last_ok
            FROM montney.source src
            JOIN LATERAL (SELECT finished, ok, n_written, error FROM montney.fetch_run f
                          WHERE f.source_id = src.id ORDER BY started DESC LIMIT 1) l ON TRUE
            LEFT JOIN LATERAL (SELECT max(finished) last_ok FROM montney.fetch_run f
                               WHERE f.source_id = src.id AND f.ok) m ON TRUE""").fetchall()
        return {r[0]: {"finished": _iso(r[1]), "ok": r[2], "n_obs": r[3], "error": r[4] or None,
                       "last_ok": _iso(r[5])} for r in rows}

    def data_version(self, any_run: bool = False) -> str:
        """Changes whenever the runner (another process) wrote, or this process did (L-081: the server's cache key).
        The runner logs a fetch_run AFTER its writes, so the newest run with n_written > 0 moves only once the data is
        in. One backward scan of the primary key. Writes outside a fetch_run in another process (imports, backfills)
        don't move it; the server's 60 s clock bucket bounds how late they show."""
        # any_run: /api/status shows every run's outcome (a failed manual refresh too), so any fetch_run moves it.
        row = self.conn.execute("SELECT max(id) FROM montney.fetch_run"
                                + ("" if any_run else " WHERE n_written > 0")).fetchone()
        return f"p{row[0] or 0}.{self._version}"

    def count(self) -> int:
        """Rows stored (change-only stores fewer than v1 did: see the module docstring)."""
        return self.conn.execute(
            "SELECT (SELECT count(*) FROM montney.obs_fast) + (SELECT count(*) FROM montney.obs_hourly) + "
            "(SELECT count(*) FROM montney.obs_daily) + (SELECT count(*) FROM montney.obs_forecast)").fetchone()[0]

    # ---------------------------------------------------------------- runner <-> API (PORT.md step 4)
    def request_refresh(self, source: str) -> None:
        """POST /api/refresh/{source}: queue a refresh for the runner (L-035 Q-P1). The API's reader role may
        INSERT here and nowhere else; the runner validates the source against its specs and runs it."""
        self.conn.execute("INSERT INTO montney.refresh_request (source) VALUES (%s)", (source,))

    def budget_states(self) -> dict[str, str]:
        """/api/status: each source's budget state (ok | warn | blocked), set by the runner's du task."""
        return dict(self.conn.execute("SELECT code, budget_state FROM montney.source ORDER BY code").fetchall())

    def runner_status(self) -> dict | None:
        """/api/status: the runner's heartbeat (montney.runner_state), or None if no runner has ever beaten.
        `age_s` is measured on the database clock; the UI flags "runner not running" above 60 s."""
        row = self.conn.execute(
            "SELECT last_beat, extract(epoch FROM (now() AT TIME ZONE 'UTC') - last_beat), pid, started_at, version "
            "FROM montney.runner_state WHERE id = 1").fetchone()
        if row is None:
            return None
        return {"last_beat": row[0].isoformat(), "age_s": round(float(row[1]), 1), "pid": row[2],
                "started_at": row[3].isoformat(), "version": row[4]}
