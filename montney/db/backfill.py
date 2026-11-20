"""DuckDB → Postgres backfill (PLAN P3 "Migration"): move v1's store, including Erik's 2021+ CSV history.

    uv run montney db import-duckdb [--duckdb PATH] [--dry-run] [--csv PATH]

* Read-only on DuckDB (stop `montney serve` first: DuckDB allows one process at a time).
* Each series gets a tier: `forecast` if any value was known before the time it is for (as_of < ts),
  otherwise by freq: sub-hourly `fast`, hourly `hourly`, everything else `daily`. A tier is fixed once a
  series has rows (ERD.md); when specs gain `retention:` (the contract), CI cross-checks the two.
* OLDEST VINTAGE FIRST (reviewer, #57): rows are sorted by (as_of, ts), and consecutive equal vintages of the
  same (series, ts) are dropped here, which is change-only's own rule, so history stays compact. The writer
  (PgStore) then dedupes against whatever Postgres already holds; it never dedupes an older vintage against
  a newer one, so loading under a live runner keeps the as-of history.
* The backfill holds thin_forecasts' advisory lock for its whole run, so thinning (and its chunk
  compression) can't run while old as_of rows land (orchestrator, L-033 follow-up; tests/
  test_db_backfill.py runs it against concurrent compression). The writer retries lost lock fights.
* Excluded by default: Alberta Rivers TAU series (`abrivers.tau_*`), until L-034's DST correction and
  Power's 2023 rescan land (orchestrator).
* After loading, the rollups are refreshed over the backfilled range, clipped to each raw retention window
  (a refresh beyond raw retention would empty buckets: RETENTION.md).
* verify(): the latest vintage per (series, ts) in Postgres equals DuckDB's, for every series, within the
  retention windows. With --csv, every CSV value is also checked as an as-of query (CSV vintage = day + 1).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from montney.db.store import Obs, PgStore

FAST_FREQS = {"1min", "5min", "10min", "15min", "30min"}
HOURLY_FREQS = {"1h", "60min", "h", "H"}
DEFAULT_EXCLUDE = ("abrivers.tau_",)
BATCH = 5000
# Raw retention per tier (migration 0002): verification and rollup refresh stay inside these.
RAW_KEEP = {"fast": timedelta(days=60), "hourly": timedelta(days=730), "daily": None,
            "forecast": timedelta(days=372)}
ROLLUP_SOURCE = {"obs_fast_1h": "fast", "obs_fast_1d": "fast", "obs_hourly_1d": "hourly"}
THIN_LOCK = "montney.thin_forecasts"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Plan:
    series: dict = field(default_factory=dict)        # key -> dict(source, tier, label, unit, freq, day, grp, rows)
    excluded: dict = field(default_factory=dict)      # key -> reason
    events: int = 0
    # L-036 realized outage history derived from DuckDB's vintages: key -> [(ts, value)]
    realized: dict = field(default_factory=dict)
    realized_skipped_existing: int = 0
    realized_late: set = field(default_factory=set)   # (realized_key, ts) taken from a post-day vintage (L-037)
    realized_not_ended: int = 0           # outage days that haven't ended yet (the connector will realize them)

    def summary(self) -> str:
        by_tier = defaultdict(lambda: [0, 0])
        for s in self.series.values():
            by_tier[s["tier"]][0] += 1
            by_tier[s["tier"]][1] += s["rows"]
        lines = [f"{t:<9} {n:>5} series {r:>9,} rows" for t, (n, r) in sorted(by_tier.items())]
        lines.append(f"events    {self.events:>5}")
        meta_only = sorted(k for k, s in self.series.items() if s.get("meta_only"))
        if meta_only:
            lines.append(f"metadata-only series (in DuckDB's catalog, no rows): {len(meta_only)}")
        if self.realized or self.realized_skipped_existing:
            n = sum(len(v) for v in self.realized.values())
            lines.append(f"realized outage history derived (L-036): {n:,} rows for {len(self.realized)} series"
                         f" (+{self.realized_skipped_existing:,} days already realized in DuckDB, kept as is)")
        if self.realized_late or self.realized_not_ended:
            lines.append(f"of those, {len(self.realized_late):,} were first seen only after their period ended: realized = the "
                         f"earliest later vintage, flagged realized_late (L-037); {self.realized_not_ended:,} (series, period) "
                         f"not ended yet (left to the connector)")
        # Evidence for the forecast tier (tiers lock once rows exist): rows known before their ts, per family.
        fam = defaultdict(lambda: [0, 0, 0])
        for k, s in self.series.items():
            if s["tier"] == "forecast":
                f = fam[".".join(k.split(".")[:2])]
                f[0] += 1
                f[1] += s.get("before_ts", 0)
                f[2] += s["rows"]
        # Series pinned non-forecast by their spec although DuckDB has rows known before their time:
        # listed, never reclassified (Power, #70). Also every place spec and data disagree.
        pinned = [(k, s) for k, s in self.series.items()
                  if s.get("declared") and s["declared"] != "forecast" and s.get("before_ts", 0) > 0]
        if pinned:
            lines.append("pinned non-forecast by spec, but with rows known before their ts (kept as pinned):")
            for k, s in sorted(pinned):
                lines.append(f"  {k:<40} {s['declared']:<8} {s['before_ts']:>7,} / {s['rows']:>7,}")
        differ = [(k, s) for k, s in self.series.items() if s.get("declared") and s["declared"] != s["derived"]]
        if differ:
            lines.append(f"spec tier differs from the data-derived tier ({len(differ)} series; spec wins):")
            for k, s in sorted(differ):
                lines.append(f"  {k:<40} spec {s['declared']:<8} data {s['derived']}")
        undeclared = sorted(k for k, s in self.series.items() if not s.get("declared"))
        if undeclared:
            lines.append(f"no retention: declared, tier derived from data ({len(undeclared)} series), e.g. "
                         + ", ".join(undeclared[:5]))
        if fam:
            lines.append("forecast-classed families (series, rows with as_of < ts / rows):")
            for f, (n, b, r) in sorted(fam.items()):
                lines.append(f"  {f:<28} {n:>4} series {b:>8,} / {r:>8,}")
        for k, why in sorted(self.excluded.items()):
            lines.append(f"excluded  {k}: {why}")
        return "\n".join(lines)


@dataclass
class Result:
    plan: Plan
    read: int = 0
    after_vintage_dedupe: int = 0
    written: int = 0
    unchanged: int = 0
    retries: int = 0
    events: int = 0
    realized: int = 0
    refreshed: dict = field(default_factory=dict)
    fetch_runs: int = 0          # DuckDB fetch_log rows imported into fetch_run (parity: last_fetch)
    state_synced: int = 0        # series whose last-seen vintage / fetch time came from DuckDB (parity: as_of)


def derive_tier(freq: str, any_as_of_before_ts: bool) -> str:
    if any_as_of_before_ts:
        return "forecast"
    if freq in FAST_FREQS:
        return "fast"
    if freq in HOURLY_FREQS:
        return "hourly"
    return "daily"


def _duck(path: Path):
    import duckdb

    return duckdb.connect(str(path), read_only=True)


DB_TIERS = {"fast", "hourly", "daily", "forecast", "live"}


class RetentionConflict(ValueError):
    pass


def _source_rule(spec: dict, series_id: str) -> str | None:
    """The source-level `retention:` for a series: a string, or {default: <tier>, "<glob>": <tier>, ...}.
    More than one matching glob is an error (L-036), never a silent pick."""
    import fnmatch

    r = spec.get("retention")
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        hits = [t for pat, t in r.items() if pat != "default" and fnmatch.fnmatchcase(series_id, pat)]
        if len(hits) > 1:
            raise RetentionConflict(f"{spec.get('id')}: {series_id} matches {len(hits)} retention patterns")
        if hits:
            return hits[0]
        if "default" not in r:   # L-036: fail closed, never a silent tier
            raise RetentionConflict(f"{spec.get('id')}: {series_id} matches no retention pattern and there is no default")
        return r["default"]
    return None


def spec_tier(series_id: str, spec: dict | None) -> str | None:
    """The tier a spec declares for a series (RETENTION.md spec contract; the same resolution as Power's
    tier_of() in tests/test_power_retention.py): the extract block that declares the series, else the
    source rule (globs, then default). None when the spec declares nothing."""
    if not spec:
        return None
    for block in spec.get("extract", []) or []:
        ids = {f.get("id") for f in (block.get("fields") or {}).values() if isinstance(f, dict)}
        if series_id in ids:
            return block.get("retention") or _source_rule(spec, series_id)
    return _source_rule(spec, series_id)


# ---------------------------------------------------------------- L-036 realized outage history
OUTAGE_DAILY_RE = r"^aeso\.outage\.[a-z0-9_]+$"     # daily outage series (monthly ones have one more dot)
OUTAGE_MONTHLY_RE = r"^aeso\.outage\.monthly\.[a-z0-9_]+$"
OUTAGE_ANY_RE = r"^aeso\.outage\.(monthly\.)?[a-z0-9_]+$"
REALIZED_SUFFIX = ".realized"
_REALIZED_CMP = "lt"     # seeded red: "le" lets a vintage AT the day end (06:00Z) count as known on the day


def alberta_day_end(day: datetime) -> datetime:
    """End of the Alberta calendar day `day` (naive, 00:00), as naive UTC. America/Edmonton from the bundled
    tzdata (L-029: Python keeps the zone, no fixed offset): 06:00Z the next day for every date since MDT."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Edmonton")
    nxt = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    nxt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)          # wall-clock midnight, DST-safe
    return nxt.astimezone(timezone.utc).replace(tzinfo=None)


def alberta_month_end(month: datetime) -> datetime:
    """End of the Alberta calendar month that `month` (naive, its 1st at 00:00) starts, as naive UTC:
    Alberta midnight on the 1st of the next month (America/Edmonton, bundled tzdata; L-029)."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Edmonton")
    y, m = (month.year + 1, 1) if month.month == 12 else (month.year, month.month + 1)
    return datetime(y, m, 1, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def period_end(key: str, ts: datetime) -> datetime | None:
    """When the period a realized value describes ends: the Alberta day for daily outage series, the Alberta
    month for monthly ones (L-036/L-037). None for anything else."""
    import re

    if re.match(OUTAGE_MONTHLY_RE, key):
        return alberta_month_end(ts)
    if re.match(OUTAGE_DAILY_RE, key):
        return alberta_day_end(ts)
    return None


_LATE_PICK = "earliest"   # seeded red: "latest" picks the last post-day vintage instead (L-037 wants the first)
REALIZED_LATE_FLAG = "realized_late"


def derive_realized(rows, existing: set, now: datetime, min_end: datetime | None = None):
    """Realized outage history from vintages, for periods (Alberta days, or months for the monthly series)
    that have ended.

    L-036: the newest vintage with as_of BEFORE the end of the period. L-037: a period with no such vintage
    (first seen only after it ended) takes the EARLIEST vintage after the end instead, flagged
    `realized_late`. A period with neither stays empty. Periods that already have a realized row are left to
    it. `min_end` (the live fill's window) skips periods that ended before it.
    `rows`: (series, ts, as_of, value) of outage series (daily and monthly).
    Returns ({realized_key: [(ts, value)]}, skipped_existing, late {(realized_key, ts)}, not_ended)."""
    before: dict[tuple[str, datetime], tuple[datetime, float]] = {}
    after: dict[tuple[str, datetime], tuple[datetime, float]] = {}
    not_ended: set = set()
    for key, ts, as_of, value in rows:
        end = period_end(key, ts)
        if end is None or (min_end is not None and end < min_end):
            continue
        if end > now:
            not_ended.add((key, ts))
            continue
        known = as_of < end if _REALIZED_CMP == "lt" else as_of <= end
        if known:
            b = before.get((key, ts))
            if b is None or as_of > b[0]:
                before[(key, ts)] = (as_of, value)
        else:
            b = after.get((key, ts))
            if b is None or ((as_of < b[0]) if _LATE_PICK == "earliest" else (as_of > b[0])):
                after[(key, ts)] = (as_of, value)
    out: dict[str, list] = defaultdict(list)
    late: set = set()
    skipped = 0
    for k in sorted(set(before) | set(after)):
        key, ts = k
        rk = key + REALIZED_SUFFIX
        if (rk, ts) in existing:
            skipped += 1
            continue
        if k in before:
            out[rk].append((ts, before[k][1]))
        else:
            out[rk].append((ts, after[k][1]))
            late.add((rk, ts))
    return dict(out), skipped, late, len(not_ended)


def plan(duck_path: Path, exclude=DEFAULT_EXCLUDE, specs: dict | None = None, now: datetime | None = None) -> Plan:
    con = _duck(duck_path)
    try:
        rows = con.execute("""
            SELECT o.series_id, coalesce(s.source, ''), coalesce(s.label, ''), coalesce(s.unit, ''),
                   coalesce(s.freq, ''), coalesce(s.day, ''), coalesce(s.grp, ''),
                   count(*) FILTER (WHERE o.as_of < o.ts), count(*)
            FROM observations o LEFT JOIN series s USING (series_id)
            GROUP BY ALL ORDER BY 1""").fetchall()
        p = Plan(events=con.execute("SELECT count(*) FROM events").fetchone()[0])
        outage_rows = con.execute(
            "SELECT series_id, ts, as_of, value FROM observations WHERE regexp_matches(series_id, ?)",
            [OUTAGE_ANY_RE]).fetchall()
        existing = set(con.execute(
            "SELECT DISTINCT series_id, ts FROM observations WHERE series_id LIKE 'aeso.outage.%.realized'").fetchall())
        # series in DuckDB's catalog with no rows yet: v1's catalog() lists them, so Postgres must too (parity)
        meta_only = con.execute("""
            SELECT series_id, coalesce(source, ''), coalesce(label, ''), coalesce(unit, ''), coalesce(freq, ''),
                   coalesce(day, ''), coalesce(grp, ''), 0, 0
            FROM series WHERE series_id NOT IN (SELECT DISTINCT series_id FROM observations) ORDER BY 1""").fetchall()
    finally:
        con.close()
    if specs is None:
        from montney.connectors import load_specs

        specs = load_specs(include_disabled=True)
    for key, src, label, unit, freq, day, grp, fc, n in rows + meta_only:
        if any(key.startswith(x) for x in exclude):
            p.excluded[key] = "excluded until L-034 + Power's 2023 rescan (TAU)"
            continue
        if not src:
            p.excluded[key] = "no series metadata (source unknown) in DuckDB"
            continue
        derived = derive_tier(freq, bool(fc))
        declared = spec_tier(key, specs.get(src))
        if declared is not None and declared not in DB_TIERS:
            p.excluded[key] = f"spec {src} declares retention {declared!r} for an observation series"
            continue
        tier = declared or derived          # the spec decides wherever it declares (L-035 Q-P3, Power #70)
        p.series[key] = dict(source=src, tier=tier, derived=derived, declared=declared, label=label, unit=unit,
                             freq=freq, day=day or "", grp=grp, rows=n, before_ts=fc, meta_only=n == 0)
    # L-036 gate (orchestrator, after #73): realized history for every pre-flip outage day in DuckDB.
    p.realized, p.realized_skipped_existing, p.realized_late, p.realized_not_ended = derive_realized(
        [r for r in outage_rows if r[0] in p.series], existing, now or _utcnow())
    for rk in p.realized:
        base = p.series[rk[:-len(REALIZED_SUFFIX)]]
        if rk not in p.series:
            declared = spec_tier(rk, specs.get(base["source"]))
            p.series[rk] = dict(source=base["source"], tier=declared or "daily", derived="daily", declared=declared,
                                label=(base["label"] + " (realized)").strip(), unit=base["unit"], freq=base["freq"],
                                day=base["day"], grp=base["grp"], rows=0, before_ts=0, realized_only=True)
    return p


def _vintage_dedupe(rows):
    """rows sorted by (series, ts, as_of): drop a vintage equal to the previous one of the same point."""
    prev_key, prev_val = None, object()
    for r in rows:
        k = (r[0], r[1])
        if k == prev_key and r[3] == prev_val:
            continue
        prev_key, prev_val = k, r[3]
        yield r


def run(pg, duck_path: Path, exclude=DEFAULT_EXCLUDE, region: str = "ab_nebc", now: datetime | None = None,
        hold_thin_lock: bool = True, on_start=None, specs: dict | None = None) -> Result:
    """Load DuckDB into Postgres. `pg` is an autocommit connection. Idempotent: a re-run writes nothing.
    Tiers: the spec's `retention:` wherever declared, else derived from the data (plan())."""
    p = plan(duck_path, exclude, specs, now)
    res = Result(p)
    st = PgStore(pg)
    # The backfill is a second writer: take the writer lock, so it fails fast while a runner is writing
    # (its change-only cache would go stale under it). Released when this connection closes.
    st.acquire_writer_lock()
    # Hold thinning off for the whole backfill (old as_of rows land in chunks thinning would compress).
    if hold_thin_lock:                                          # False only for the seeded red
        pg.execute("SELECT pg_advisory_lock(hashtext(%s))", (THIN_LOCK,))
    try:
        if on_start:                                            # test hook: the lock (if any) is held now
            on_start()
        for src in sorted({s["source"] for s in p.series.values()}):
            st.ensure_source(src, region=region)
        for key, s in p.series.items():
            st.upsert_series(key, source=s["source"], tier=s["tier"], label=s["label"], unit=s["unit"],
                             freq=s["freq"], day=s["day"], attrs={"grp": s["grp"]} if s["grp"] else {})
        con = _duck(duck_path)
        try:
            keys = list(p.series)
            raw = con.execute("SELECT series_id, ts, as_of, value FROM observations "
                              "WHERE series_id IN (SELECT unnest(?)) ORDER BY series_id, ts, as_of",
                              [keys]).fetchall()
            events = con.execute("SELECT id, source, kind, title, body, url, starts, ends, refs, severity, "
                                 "published, extra, first_seen, updated, status, content FROM events").fetchall()
        finally:
            con.close()
        res.read = len(raw)
        kept = list(_vintage_dedupe(raw))
        res.after_vintage_dedupe = len(kept)
        kept.sort(key=lambda r: (r[2], r[0], r[1]))            # oldest vintage first
        ranges: dict[str, list] = {}
        for i in range(0, len(kept), BATCH):
            chunk = kept[i:i + BATCH]
            w = st.write([Obs(k, ts, v, a) for k, ts, a, v in chunk], now=now)
            res.written += w.written
            res.unchanged += w.unchanged
            res.retries += w.retries
            for k, ts, _a, _v in chunk:
                tier = p.series[k]["tier"]
                if not w.by_tier.get(tier):                     # nothing written for this tier: no refresh
                    continue
                r = ranges.setdefault(tier, [ts, ts])
                r[0], r[1] = min(r[0], ts), max(r[1], ts)
        realized = [Obs(rk, ts, v, ts) for rk, pts in p.realized.items() for ts, v in pts]   # as_of = ts
        if realized:
            from montney.db.store import Flag

            flags = [Flag(rk, ts, ts, REALIZED_LATE_FLAG, "first seen only after the Alberta day ended (L-037)")
                     for rk, ts in sorted(p.realized_late)]
            w = st.write(realized, flags=flags, now=now)
            res.written += w.written
            res.unchanged += w.unchanged
            res.realized = w.written
        res.events = _events(pg, events, region)
        res.fetch_runs = import_fetch_log(pg, duck_path, region)
        res.state_synced = sync_state(pg, duck_path, [k for k, s in p.series.items() if not s.get("meta_only")])
        res.refreshed = refresh_rollups(pg, ranges, now or _utcnow())
    finally:
        if hold_thin_lock:
            pg.execute("SELECT pg_advisory_unlock(hashtext(%s))", (THIN_LOCK,))
        st.release_writer_lock()
    return res


def import_fetch_log(pg, duck_path: Path, region: str = "ab_nebc") -> int:
    """DuckDB's fetch_log -> fetch_run, so last_fetch() (the UI's source status) answers as v1 did instead of
    "never fetched" until the runner's first fetch. Idempotent on (source, started); last_ok_at only advances."""
    con = _duck(duck_path)
    try:
        rows = con.execute("SELECT source, started, finished, ok, n_obs, error FROM fetch_log "
                           "WHERE source IS NOT NULL AND started IS NOT NULL ORDER BY started").fetchall()
    finally:
        con.close()
    st = PgStore(pg)
    for src in sorted({r[0] for r in rows}):
        st.ensure_source(src, region=region)
    n = 0
    for src, started, finished, ok, n_obs, error in rows:
        cur = pg.execute(
            "INSERT INTO montney.fetch_run (source_id, started, finished, ok, n_written, error) "
            "SELECT src.id, %s, %s, %s, %s, %s FROM montney.source src WHERE src.code = %s "
            "AND NOT EXISTS (SELECT 1 FROM montney.fetch_run f WHERE f.source_id = src.id AND f.started = %s)",
            (started, finished, ok, n_obs or 0, (error or "")[:2000], src, started))
        n += cur.rowcount
    pg.execute("""
        UPDATE montney.source src SET last_ok_at = m.last_ok
        FROM (SELECT source_id, max(finished) last_ok FROM montney.fetch_run WHERE ok GROUP BY 1) m
        WHERE m.source_id = src.id AND (src.last_ok_at IS NULL OR src.last_ok_at < m.last_ok)""")
    return n


def sync_state(pg, duck_path: Path, keys: list[str]) -> int:
    """series_state for imported series, as v1 would answer (parity, L-035 b):
    * last_as_of: the latest vintage SEEN, including the identical re-fetches _vintage_dedupe dropped (the live
      write path records those; the backfill had only kept the last CHANGED vintage);
    * last_seen_at: DuckDB's latest fetched_at (v1's catalog "fetched"), not the time of the import.
    Only for series whose source the runner has NOT fetched natively since (no fetch_run newer than DuckDB's
    last fetch of that source), so a re-run never moves a newer runner value back. Returns rows updated."""
    con = _duck(duck_path)
    try:
        # per series: the newest vintage (max as_of), the ts range of that vintage's rows, the newest fetched_at
        seen = con.execute("""
            WITH m AS (SELECT series_id, max(as_of) a, max(fetched_at) f FROM observations
                       WHERE series_id IN (SELECT unnest(?)) GROUP BY 1)
            SELECT m.series_id, s.source, m.a, m.f, min(o.ts), max(o.ts)
            FROM m JOIN observations o ON o.series_id = m.series_id AND o.as_of = m.a
            JOIN series s ON s.series_id = m.series_id GROUP BY 1, 2, 3, 4""", [keys]).fetchall()
        duck_last = dict(con.execute("SELECT source, max(started) FROM fetch_log GROUP BY 1").fetchall())
    finally:
        con.close()
    pg_last = dict(pg.execute("SELECT src.code, max(f.started) FROM montney.fetch_run f "
                              "JOIN montney.source src ON src.id = f.source_id GROUP BY 1").fetchall())
    native = {src for src, t in pg_last.items() if t is not None and (duck_last.get(src) is None or t > duck_last[src])}
    n = 0
    for key, src, as_of, fetched, lo, hi in seen:
        if src in native:
            continue
        cur = pg.execute(
            "UPDATE montney.series_state st SET last_as_of = %s, last_seen_at = %s, seen_from_ts = %s, seen_to_ts = %s "
            "FROM montney.series s WHERE s.id = st.series_id AND s.key = %s "
            "AND (st.last_as_of IS NULL OR st.last_as_of <= %s) "
            "AND (st.last_as_of, st.last_seen_at, st.seen_from_ts, st.seen_to_ts) IS DISTINCT FROM (%s, %s, %s, %s)",
            (as_of, fetched, lo, hi, key, as_of, as_of, fetched, lo, hi))
        n += cur.rowcount
    return n


def _events(pg, events, region) -> int:
    import hashlib

    st = PgStore(pg)
    n = 0
    with pg.transaction(), pg.cursor() as cur:
        for (eid, src, kind, title, body, url, starts, ends, refs, sev, published, extra, first_seen, updated,
             status, content) in events:
            st.ensure_source(src, region=region)
            cur.execute(
                "INSERT INTO montney.event (id, source_id, kind, title, body, url, starts, ends, severity, published, "
                "extra, first_seen, updated, status, content_sha1) VALUES (%s, (SELECT id FROM montney.source WHERE "
                "code = %s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (eid, src, kind, title, body or "", url or "", starts, ends, sev or "info", published,
                 json.dumps(json.loads(extra or "{}")), first_seen or updated or _utcnow(), updated or _utcnow(),
                 status or "active", bytes.fromhex(content) if content else hashlib.sha1(eid.encode()).digest()))
            n += cur.rowcount
            for ref in refs or []:
                cur.execute("INSERT INTO montney.event_scope (event_id, ref) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (eid, ref))
    return n


CLIP_INWARD = True
REFRESH_MARGIN = timedelta(days=1)


def refresh_rollups(pg, ranges: dict, now: datetime) -> dict:
    """Refresh each rollup over the backfilled range, clipped INSIDE its raw table's retention window (by
    REFRESH_MARGIN), so a refresh never empties buckets whose raw rows are gone (RETENTION.md). `ranges` holds
    only tiers that had rows written; others are skipped. Returns {rollup: (start, end) or None}."""
    out = {}
    for rollup, tier in ROLLUP_SOURCE.items():
        rng = ranges.get(tier)
        if not rng:
            out[rollup] = None
            continue
        if CLIP_INWARD:
            # never closer than REFRESH_MARGIN to the raw retention edge: a bucket whose raw rows are gone
            # would be refreshed to empty (reviewer, #64)
            start = max(rng[0] - timedelta(days=1), now - RAW_KEEP[tier] + REFRESH_MARGIN)
        else:   # the original formula, kept only as the seeded red: it reaches 1 day PAST raw retention
            start = max(rng[0], now - RAW_KEEP[tier]) - timedelta(days=1)
        end = rng[1] + timedelta(days=2)
        if start >= end:
            out[rollup] = None
            continue
        refresh_with_retry(pg, rollup, start, end)
        out[rollup] = (start, end)
    return out


REFRESH_DEADLINE_S = 120.0     # how long to keep retrying a refresh that collides with a policy refresh
_REFRESH_RETRY = True          # seeded red: False lets the first "concurrent refresh" error through


def refresh_with_retry(pg, rollup: str, start: datetime, end: datetime, deadline_s: float | None = None) -> int:
    """CALL refresh_continuous_aggregate, retrying while TimescaleDB's own scheduled refresh policy holds the
    rollup ("could not refresh continuous aggregate ... due to a concurrent refresh", SQLSTATE 55P03). An
    import racing a policy refresh must wait, not fail (reviewer: main's db job, 5976529/c20abbe). Returns the
    number of retries. Raises after `deadline_s`."""
    import random
    import time

    import psycopg

    deadline = time.monotonic() + (REFRESH_DEADLINE_S if deadline_s is None else deadline_s)
    delay, retries = 0.2, 0
    while True:
        try:
            pg.execute(f"CALL refresh_continuous_aggregate('montney.{rollup}', %s::timestamp, %s::timestamp)",
                       (start, end))
            return retries
        except psycopg.errors.LockNotAvailable:
            if not _REFRESH_RETRY or time.monotonic() + delay > deadline:
                raise
            retries += 1
            time.sleep(delay * (1 + random.random()))
            delay = min(delay * 2, 5.0)


def verify(pg, duck_path: Path, exclude=DEFAULT_EXCLUDE, now: datetime | None = None) -> list[str]:
    """Latest vintage per (series, ts) in Postgres == DuckDB's, for every planned series, inside the raw
    retention windows. Fails closed: an empty comparison is a problem, not a pass."""
    now = now or _utcnow()
    p = plan(duck_path, exclude)
    con = _duck(duck_path)
    try:
        duck = con.execute("SELECT series_id, ts, arg_max(value, as_of) FROM observations GROUP BY 1, 2").fetchall()
    finally:
        con.close()
    want = defaultdict(dict)
    for k, ts, v in duck:
        if k in p.series:
            keep = RAW_KEEP[p.series[k]["tier"]]
            if keep is None or ts >= now - keep or p.series[k]["tier"] == "forecast":
                want[k][ts] = v
    got = defaultdict(dict)
    for k, ts, v in pg.execute("""
            SELECT DISTINCT ON (series_id, ts) series_id, ts, value FROM montney.observation
            ORDER BY series_id, ts, as_of DESC""").fetchall():   # the v1-shaped view: series_id is the key
        got[k][ts] = v
    problems, compared = [], 0
    for k, pts in want.items():
        for ts, v in pts.items():
            compared += 1
            g = got.get(k, {}).get(ts, "missing")
            if g != v and not (g is None and v is None):
                problems.append(f"{k} @ {ts}: duckdb {v!r}, postgres {g!r}")
    if compared == 0:
        problems.append("nothing compared (NO-RESULT is not a pass)")
    return problems


def verify_csv(pg, csv_path: Path) -> tuple[int, list[str]]:
    """Every value of Erik's NGTL CSV, as an as-of query at the CSV's vintage (gas day + 1). Read-only."""
    from montney.importers import ngtl_csv

    parsed = ngtl_csv.parse(csv_path)
    obs = ngtl_csv.to_observations(parsed, ngtl_csv.guess_units(parsed, csv_path))
    problems = []
    for o in obs:
        row = pg.execute("SELECT o.value FROM montney.obs_daily o JOIN montney.series s ON s.id = o.series_id "
                         "WHERE s.key = %s AND o.ts = %s AND o.as_of <= %s ORDER BY o.as_of DESC LIMIT 1",
                         (o.series_id, o.ts, o.as_of)).fetchone()
        if row is None or abs(row[0] - o.value) > 1e-9:
            problems.append(f"{o.series_id} @ {o.ts:%Y-%m-%d}: csv {o.value!r}, postgres {row[0] if row else None!r}")
    if not obs:
        problems.append("CSV parsed to 0 values (NO-RESULT is not a pass)")
    return len(obs), problems


# ---------------------------------------------------------------- L-037 live gap-fill (one code path)
FILL_WINDOW_DAYS = 14     # forecast-tier runs are all kept this long (thinning starts after)


def fill_realized(pg, now: datetime | None = None, window_days: int = FILL_WINDOW_DAYS) -> dict:
    """L-037 (3): the connector writes a realized value only for "today" and never revisits a missed day
    (runner downtime), and it has no flags channel. So the runner calls this on a schedule (and the backfill
    logic is the same): for every ended day in the last `window_days` that has no realized row yet, the
    SAME derive_realized() as the backfill decides the value: the newest vintage before the Alberta day end,
    else the earliest one after it, flagged realized_late. Rows that already exist are never touched.
    Covers every forecast-tier daily and monthly outage series with a daily-tier `<key>.realized` sibling;
    the window applies to when the period ENDED. `pg` is an autocommit connection. Returns counts."""
    from montney.db.store import Flag

    now = now or _utcnow()
    since = now - timedelta(days=window_days)
    first = since - timedelta(days=32)          # a month that ended inside the window started up to ~31 d before it
    rows = pg.execute("""
        SELECT b.key, f.ts, f.as_of, f.value
        FROM montney.series b
        JOIN montney.series r ON r.key = b.key || '.realized' AND r.tier = 'daily'
        JOIN montney.obs_forecast f ON f.series_id = b.id
        WHERE b.tier = 'forecast' AND f.ts >= %s""", (first,)).fetchall()
    existing = set(pg.execute("""
        SELECT s.key, d.ts FROM montney.obs_daily d JOIN montney.series s ON s.id = d.series_id
        WHERE s.key LIKE '%%.realized' AND d.ts >= %s""", (first,)).fetchall())
    realized, skipped, late, not_ended = derive_realized(rows, existing, now, min_end=since)
    obs = [Obs(rk, ts, v, ts) for rk, pts in realized.items() for ts, v in pts]       # as_of = ts
    flags = [Flag(rk, ts, ts, REALIZED_LATE_FLAG, "filled after the Alberta day ended (L-037)")
             for rk, ts in sorted(late)]
    w = PgStore(pg).write(obs, flags=flags, now=now) if obs else None
    return {"filled": w.written if w else 0, "late": len(late), "already": skipped, "not_ended": not_ended}
