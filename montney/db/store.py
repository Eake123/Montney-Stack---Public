"""The Postgres write path (docs/db/RETENTION.md "Change-only writes" and "Re-anchoring").

`PgStore.write()` takes the same `Obs` rows as the DuckDB store and writes only what changed:

* One comparator for every tier: the latest stored vintage of the same (series, ts). Equal means no row.
  Forecasts read it from `forecast_latest`; the other tiers from the tier table itself.
* The filtering happens HERE, before anything is sent. A per-process cache holds the last written
  value per (series, ts); misses are loaded with one read-only range query per tier. `ON CONFLICT ... WHERE
  value IS DISTINCT FROM` stays as the backstop for races and stale caches.
* The cache is updated only AFTER the transaction commits. If the commit fails, the cache would otherwise
  claim rows that were never stored, and the next fetch would skip them as "unchanged".
* Re-anchoring: a forecast point still in the feed is written again, unchanged, once its newest vintage
  is older than REANCHOR_DAYS (default 7), so `obs_forecast`'s 1 y + 7 d retention never drops the only
  vintage of a long-horizon point.
* Flags (`obs_flag`, per vintage) are written in the same transaction as their observation, and only
  for rows actually written, so `prune_obs_flags` can never see a flag before its row.
* An OLDER vintage than the latest stored one (a backfill) is never deduped: it is the as-of history
  before the newer vintage. Only same-or-newer vintages are compared.
* Single writer: the cache assumes one writer process. The runner takes `acquire_writer_lock()`, so a
  second runner fails fast (WriterBusy) instead of writing alongside with a stale cache.
* Every write updates `series_state.last_seen_at` for every series in the batch, even when nothing
  changed, so staleness and "last fetched" stay right.

Switches (tests and emergencies only): MONTNEY_DB_DEDUPE=off is v1 behaviour (no pre-filter, the
backstop updates unconditionally); MONTNEY_DB_REANCHOR_DAYS=off disables re-anchoring.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from montney.store import Obs

TIERS = ("fast", "hourly", "daily", "forecast")
TABLE = {"fast": "obs_fast", "hourly": "obs_hourly", "daily": "obs_daily", "forecast": "obs_forecast"}
CACHE_MAX = 500_000


@dataclass
class Flag:
    series_id: str          # series key
    ts: datetime
    as_of: datetime
    flag: str
    note: str = ""


@dataclass
class WriteResult:
    written: int = 0
    unchanged: int = 0
    reanchored: int = 0
    retries: int = 0
    by_tier: dict = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(v):
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) else v


def dedupe_enabled() -> bool:
    return os.environ.get("MONTNEY_DB_DEDUPE", "on").lower() != "off"


def reanchor_days() -> int | None:
    raw = os.environ.get("MONTNEY_DB_REANCHOR_DAYS", "7").lower()
    return None if raw == "off" else int(raw)


def write_retries() -> int:
    """How often a write is retried after losing a lock fight (deadlock_timeout 1 s < the 2 s lock_timeout
    thin_forecasts uses, so the runner can be the deadlock victim). MONTNEY_DB_WRITE_RETRIES=0: none."""
    return int(os.environ.get("MONTNEY_DB_WRITE_RETRIES", "5"))


def _retryable():
    import psycopg

    return (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable,
            psycopg.errors.SerializationFailure)


class WriterBusy(RuntimeError):
    pass


class UnknownSeries(KeyError):
    pass


class PgStore:
    # The ordering rule under test (seeded red): flip to False and the commit-failure test must fail.
    CACHE_AFTER_COMMIT = True
    # Seeded red for the backfill rule (reviewer, #57): flip to True and an older vintage equal to the
    # latest one is skipped, losing the as-of history between it and the next newer vintage.
    DEDUPE_OLDER_VINTAGES = False

    def __init__(self, conn):
        if not conn.autocommit:
            raise ValueError("PgStore needs an autocommit connection (it opens its own transactions)")
        self.conn = conn
        self._ids: dict[str, tuple[int, str]] = {}            # key -> (series id, tier)
        # (series id, ts) -> (as_of, value) of the latest stored vintage; forecasts mirror forecast_latest
        self._last: dict[tuple[int, datetime], tuple[datetime, float | None]] = {}
        self._fc: dict[tuple[int, datetime], tuple[datetime, float | None]] = {}
        self._before_commit = None                              # test hook: called inside the transaction

    # ------------------------------------------------------------ single writer
    WRITER_LOCK = "montney.writer"

    def acquire_writer_lock(self) -> None:
        """The change-only cache assumes ONE writer process (the runner): a second writer would make the
        cache stale, and the ON CONFLICT backstop would then be the only guard. The runner calls this at
        start, so a second runner fails fast instead of writing alongside. Session-level: released when
        the connection closes."""
        if not self.conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (self.WRITER_LOCK,)).fetchone()[0]:
            raise WriterBusy("another montney writer (runner) holds the writer lock; stop it first")

    def release_writer_lock(self) -> None:
        self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.WRITER_LOCK,))

    # ------------------------------------------------------------ catalogue
    def ensure_source(self, code: str, *, name: str = "", region: str = "ab_nebc", retention: str | None = None,
                      interval_s: int | None = None, mode: str = "cached") -> int:
        """Register or update a source. An existing code is a SELECT + an UPDATE only if a field differs: an
        `INSERT ... ON CONFLICT` calls nextval on EVERY call, and montney.source.id is a smallint identity (32767),
        which the runner exhausted in about 6 h on 2026-11-01. Only a genuinely new code takes a value."""
        row = self.conn.execute("SELECT id FROM montney.source WHERE code = %s", (code,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE montney.source s SET name = %s, region_id = r.id, retention = %s, interval_s = %s, mode = %s "
                "FROM (SELECT (SELECT id FROM montney.region WHERE code = %s) id) r WHERE s.id = %s AND "
                "(s.name, s.region_id, s.retention, s.interval_s, s.mode) IS DISTINCT FROM (%s, r.id, %s, %s, %s)",
                (name, retention, interval_s, mode, region, row[0], name, retention, interval_s, mode))
            return row[0]
        return self.conn.execute(
            "INSERT INTO montney.source (code, name, region_id, retention, interval_s, mode) "
            "VALUES (%s, %s, (SELECT id FROM montney.region WHERE code = %s), %s, %s, %s) "
            "ON CONFLICT (code) DO UPDATE SET name = excluded.name, region_id = excluded.region_id, "
            "retention = excluded.retention, interval_s = excluded.interval_s, mode = excluded.mode "
            "RETURNING id", (code, name, region, retention, interval_s, mode)).fetchone()[0]   # new (or a race)

    def upsert_series(self, key: str, *, source: str, tier: str, label: str = "", unit: str = "",
                      freq: str = "", day: str = "", region: str | None = None, attrs: dict | None = None) -> int:
        """Register a series. Its tier is fixed once it has rows (ERD.md): a different tier is an error."""
        import json

        if tier not in TIERS + ("live",):
            raise ValueError(f"{key}: tier must be one of {TIERS + ('live',)}, got {tier!r}")
        row = self.conn.execute("SELECT id, tier FROM montney.series WHERE key = %s", (key,)).fetchone()
        if row and row[1] != tier:
            # Fixed once it HAS ROWS (ERD.md). A series registered under another tier but never written (e.g. seeded
            # with the freq-derived tier before L-080's fix) simply moves.
            old_table = TABLE.get(row[1], "obs_live")
            if self.conn.execute(f"SELECT EXISTS (SELECT 1 FROM montney.{old_table} WHERE series_id = %s)",
                                 (row[0],)).fetchone()[0]:
                raise ValueError(f"{key}: tier is fixed at {row[1]!r}; moving it to {tier!r} needs a migration")
            self.conn.execute("UPDATE montney.series SET tier = %s WHERE id = %s", (tier, row[0]))
            self._ids.pop(key, None)
            row = (row[0], tier)
        if row:                     # existing key: no INSERT, so no nextval (see ensure_source)
            self.conn.execute(
                "UPDATE montney.series SET label = %s, unit = %s, freq = %s, day = %s, attrs = %s::jsonb WHERE id = %s AND "
                "(label, unit, freq, day, attrs) IS DISTINCT FROM (%s, %s, %s, %s, %s::jsonb)",
                (label, unit, freq, day or "", json.dumps(attrs or {}), row[0],
                 label, unit, freq, day or "", json.dumps(attrs or {})))
            self._ids[key] = (row[0], tier)
            return row[0]
        sid = self.conn.execute(
            "INSERT INTO montney.series (key, source_id, region_id, label, unit, freq, day, tier, attrs) "
            "VALUES (%s, (SELECT id FROM montney.source WHERE code = %s), "
            "        coalesce((SELECT id FROM montney.region WHERE code = %s), "
            "                 (SELECT region_id FROM montney.source WHERE code = %s)), "
            "        %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET label = excluded.label, unit = excluded.unit, "
            "freq = excluded.freq, day = excluded.day, attrs = excluded.attrs RETURNING id",
            (key, source, region, source, label, unit, freq, day or "", tier, json.dumps(attrs or {}))).fetchone()[0]
        self._ids[key] = (sid, tier)
        return sid

    def _resolve(self, keys: Iterable[str]) -> None:
        missing = [k for k in set(keys) if k not in self._ids]
        if missing:
            for sid, key, tier in self.conn.execute(
                    "SELECT id, key, tier FROM montney.series WHERE key = ANY(%s)", (missing,)).fetchall():
                self._ids[key] = (sid, tier)
        unknown = [k for k in missing if k not in self._ids]
        if unknown:
            raise UnknownSeries(f"series not registered (upsert_series first): {sorted(unknown)[:5]}")

    # ------------------------------------------------------------ writes
    def write(self, obs: list[Obs], *, flags: Iterable[Flag] = (), now: datetime | None = None) -> WriteResult:
        """Write the changed rows of `obs` (and their flags) in one transaction. Returns what happened."""
        now = (now or _utcnow()).replace(microsecond=0)
        res = WriteResult()
        if not obs:
            return res
        self._resolve(o.series_id for o in obs)
        dedupe, anchor = dedupe_enabled(), reanchor_days()

        # Collapse duplicates inside the batch: the last occurrence of a (series, ts, as_of) wins.
        rows: dict[tuple[int, datetime, datetime], tuple[str, float | None]] = {}
        for o in obs:
            sid, tier = self._ids[o.series_id]
            if tier == "live":
                continue
            rows[(sid, o.ts, o.as_of)] = (tier, _norm(o.value))
        if dedupe:
            self._load_missing(rows)

        send: dict[str, list[tuple]] = {t: [] for t in TIERS}
        new_last: dict[tuple[int, datetime], tuple[datetime, float | None]] = {}
        new_fc: dict[tuple[int, datetime], tuple[datetime, float | None]] = {}
        for (sid, ts, as_of), (tier, value) in rows.items():
            if tier == "forecast":
                prev = self._fc.get((sid, ts))
                # An OLDER vintage (backfill) is never deduped against a newer one: it fills the as-of
                # history before that newer vintage, and the PK backstop still stops true duplicates.
                same = prev is not None and prev[1] == value and (self.DEDUPE_OLDER_VINTAGES or as_of >= prev[0])
                stale = anchor is not None and prev is not None and as_of - prev[0] > timedelta(days=anchor)
                if dedupe and same and not stale:
                    res.unchanged += 1
                    continue
                if same and stale:
                    res.reanchored += 1
                if prev is None or as_of >= prev[0]:
                    new_fc[(sid, ts)] = (as_of, value)
            else:
                key = (sid, ts)
                prev = self._last.get(key)
                older = prev is not None and as_of < prev[0] and not self.DEDUPE_OLDER_VINTAGES
                if dedupe and prev is not None and prev[1] == value and not older:
                    res.unchanged += 1
                    continue
                if prev is None or as_of >= prev[0]:            # an older (backfilled) vintage isn't "latest"
                    new_last[key] = (as_of, value)
            send[tier].append((sid, ts, as_of, value, now))

        written_keys = {(r[0], r[1], r[2]) for t in TIERS for r in send[t]}
        flag_rows = []
        for f in flags:
            self._resolve([f.series_id])
            sid = self._ids[f.series_id][0]
            if (sid, f.ts, f.as_of) in written_keys:          # only flags of rows written now
                flag_rows.append((sid, f.ts, f.as_of, f.flag, f.note))

        RETRYABLE = _retryable()
        retries = write_retries()
        for attempt in range(retries + 1):
            res.written, res.by_tier = 0, {}
            try:
                self._send(send, new_fc, flag_rows, rows, now, dedupe, res, new_last)
                break
            except RETRYABLE:
                if attempt == retries:
                    raise
                res.retries += 1
                # Lost a lock fight (e.g. against thin_forecasts compressing a chunk): the transaction was
                # rolled back and the cache untouched, so the whole batch is simply sent again.
                time.sleep(min(2.0, 0.1 * 2 ** attempt) * (1 + random.random()))
        if self.CACHE_AFTER_COMMIT:
            self._remember(new_last, new_fc)
        return res

    def _send(self, send, new_fc, flag_rows, rows, now, dedupe, res, new_last) -> None:
        """One transaction: the changed rows, forecast_latest, flags and series_state. All or nothing."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for tier in TIERS:
                    if not send[tier]:
                        continue
                    where = "WHERE t.value IS DISTINCT FROM excluded.value" if dedupe else ""
                    conflict = "(series_id, as_of, ts)" if tier == "forecast" else "(series_id, ts, as_of)"
                    cur.executemany(
                        f"INSERT INTO montney.{TABLE[tier]} AS t (series_id, ts, as_of, value, fetched_at) "
                        f"VALUES (%s, %s, %s, %s, %s) ON CONFLICT {conflict} DO UPDATE "
                        f"SET value = excluded.value, fetched_at = excluded.fetched_at {where}",
                        send[tier])
                    # rows actually inserted or changed (psycopg sums rowcount over executemany): a re-sent row
                    # the backstop finds identical counts 0, so a re-run reports 0 written
                    res.by_tier[tier] = cur.rowcount
                    res.written += cur.rowcount
                if new_fc:
                    cur.executemany(
                        "INSERT INTO montney.forecast_latest (series_id, ts, as_of, value) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (series_id, ts) DO UPDATE SET as_of = excluded.as_of, value = excluded.value "
                        "WHERE excluded.as_of >= forecast_latest.as_of",
                        [(sid, ts, a, v) for (sid, ts), (a, v) in new_fc.items()])
                if flag_rows:
                    cur.executemany(
                        "INSERT INTO montney.obs_flag (series_id, ts, as_of, flag, note) VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING", flag_rows)
                self._touch_state(cur, rows, now)
            if not self.CACHE_AFTER_COMMIT:
                self._remember(new_last, new_fc)                # the bug the seeded red looks for
            if self._before_commit:
                self._before_commit()

    def _touch_state(self, cur, rows, now) -> None:
        """series_state for every series in the batch (changed rows AND identical ones change-only skipped):
        last_ts/last_value = the newest point; last_as_of = the newest vintage seen (monotonic);
        seen_from_ts/seen_to_ts = the ts range that vintage covered in this batch (migration 0011)."""
        latest: dict[int, tuple[datetime, datetime, float | None]] = {}
        seen: dict[int, list] = {}                      # sid -> [newest as_of, min ts, max ts] of that vintage
        for (sid, ts, as_of), (_tier, value) in rows.items():
            cur_best = latest.get(sid)
            if cur_best is None or (ts, as_of) > (cur_best[0], cur_best[1]):
                latest[sid] = (ts, as_of, value)
            s = seen.get(sid)
            if s is None or as_of > s[0]:
                seen[sid] = [as_of, ts, ts]
            elif as_of == s[0]:
                s[1], s[2] = min(s[1], ts), max(s[2], ts)
        cur.executemany(
            "INSERT INTO montney.series_state AS s (series_id, last_ts, last_as_of, last_value, last_seen_at, "
            "seen_from_ts, seen_to_ts) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (series_id) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, "
            "last_ts = CASE WHEN s.last_ts IS NULL OR excluded.last_ts >= s.last_ts THEN excluded.last_ts ELSE s.last_ts END, "
            "last_value = CASE WHEN s.last_ts IS NULL OR excluded.last_ts >= s.last_ts THEN excluded.last_value ELSE s.last_value END, "
            "seen_from_ts = CASE WHEN s.last_as_of IS NULL OR excluded.last_as_of >= s.last_as_of "
            "THEN excluded.seen_from_ts ELSE s.seen_from_ts END, "
            "seen_to_ts = CASE WHEN s.last_as_of IS NULL OR excluded.last_as_of >= s.last_as_of "
            "THEN excluded.seen_to_ts ELSE s.seen_to_ts END, "
            "last_as_of = GREATEST(s.last_as_of, excluded.last_as_of)",
            [(sid, ts, seen[sid][0], v, now, seen[sid][1], seen[sid][2]) for sid, (ts, _a, v) in latest.items()])

    def _remember(self, new_last, new_fc) -> None:
        if len(self._last) + len(self._fc) > CACHE_MAX:
            self._last.clear()
            self._fc.clear()
        self._last.update(new_last)
        self._fc.update(new_fc)

    def _load_missing(self, rows) -> None:
        """Fill the cache for every (series, ts) in the batch it doesn't hold. Read-only."""
        want: dict[str, dict[int, list[datetime]]] = {}
        for (sid, ts, _a), (tier, _v) in rows.items():
            cache = self._fc if tier == "forecast" else self._last
            if (sid, ts) not in cache:
                want.setdefault(tier, {}).setdefault(sid, []).append(ts)
        for tier, by_sid in want.items():
            sids = list(by_sid)
            lo = min(min(v) for v in by_sid.values())
            hi = max(max(v) for v in by_sid.values())
            if tier == "forecast":
                for sid, ts, a, v in self.conn.execute(
                        "SELECT series_id, ts, as_of, value FROM montney.forecast_latest "
                        "WHERE series_id = ANY(%s) AND ts BETWEEN %s AND %s", (sids, lo, hi)).fetchall():
                    self._fc[(sid, ts)] = (a, v)
            else:
                for sid, ts, a, v in self.conn.execute(
                        f"SELECT DISTINCT ON (series_id, ts) series_id, ts, as_of, value FROM montney.{TABLE[tier]} "
                        "WHERE series_id = ANY(%s) AND ts BETWEEN %s AND %s "
                        "ORDER BY series_id, ts, as_of DESC", (sids, lo, hi)).fetchall():
                    self._last[(sid, ts)] = (a, v)

    def forget(self) -> None:
        """Drop the in-memory cache (the next write reloads it from the database)."""
        self._last.clear()
        self._fc.clear()

    # ------------------------------------------------------------ fetch log
    def log_fetch(self, source: str, started: datetime, ok: bool, res: WriteResult | None = None,
                  error: str | None = None, bytes_in: int | None = None) -> None:
        res = res or WriteResult()
        self.conn.execute(
            "INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written, n_unchanged, bytes_in, error) "
            "VALUES ((SELECT id FROM montney.source WHERE code = %s), %s, %s, %s, %s, %s, %s, %s)",
            (source, started, _utcnow(), ok, res.written, res.unchanged, bytes_in, (error or "")[:2000] or None))
        if ok:
            self.conn.execute("UPDATE montney.source SET last_ok_at = %s WHERE code = %s", (_utcnow(), source))
