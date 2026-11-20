# Porting reads and writes to Postgres: the contract (P3)

**Status: AGREED (L-035)** with the orchestrator, who owns `montney/server.py`, `snapshot.py`, `runner.py`'s
callers, `cli.py` and `connectors/base.py`. Database owns `montney/store.py`, `montney/runner.py` and
`montney/db/**`. Design: [`ERD.md`](ERD.md), [`RETENTION.md`](RETENTION.md).

## Goal

The app reads and writes Postgres with the **smallest possible change** to orchestrator-owned code. L-002
splits it into two processes: a runner that writes, and an API that reads.

## 1. The surface today (DuckDB `montney.store.Store`)

| method | used by | returns |
|---|---|---|
| `series(ids, start, end, as_of_max)` | `server.py` `/api/series`, `snapshot.py` | `{id: {"ts": [epoch s], "v": [float], "as_of": iso}}`: the latest vintage per ts (or the latest known by `as_of_max`) |
| `meta(ids)` | `server.py`, `snapshot.py` | `{id: {source, label, unit, freq, group, day}}` |
| `catalog()` | `/api/catalog` | `[{id, source, label, unit, freq, group, n, first, last, fetched}]` |
| `events(kinds, ref, state, at, since, include_withdrawn, limit)` | `/api/events` | `[{id, source, kind, ..., status}]`, with v1's TTL/state rules |
| `last_fetch()` | `/api/status`, the scheduler | `{source: {finished, ok, n_obs, error, last_ok}}` |
| `count()` | `/api/status` | int |
| `close()` | `server.py` lifespan, `cli.py` | — |
| `upsert_meta`, `write(obs, live)`, `upsert_events(events, complete_scopes)`, `log_fetch(...)` | `runner.py` | — |

## 2. Proposal: a drop-in Postgres store with the same interface

- **`montney.db.pgstore.PgStore`** (it extends today's write-side `montney.db.store.PgStore`) implements
  **every method above with identical signatures and return shapes.** snapshot.py and server.py don't change
  how they call the store.
- **Proof is A/B parity in one run** (Wellsite §4.6): `tests/test_db_parity.py` feeds the same calls to the
  DuckDB `Store` and to `PostgresStore` under one frozen clock, then compares every read method over a grid
  of arguments, plus the shipped consumer, `snapshot.build()`.
  - **The inputs include identical re-fetches** (as_of: ts points re-sent unchanged, a GDSR day re-published
    with the same values, an identical forecast run), because those are exactly where change-only makes
    Postgres store less than DuckDB.
  - **The grid stays inside retention:** no ts or as_of older than the tier's raw retention, so retention
    can't masquerade as a parity failure.
  - **It fails closed:** a grid that compared fewer than its expected number of cells is NO-RESULT.
  - **Seeded reds**, each of which must break parity:
    1. the earliest vintage instead of the latest (`ORDER BY as_of ASC`);
    2. the `as_of_max` boundary as `<` instead of `<=`;
    3. the live union (`obs_live`) left out of `series()`;
    4. a TTL changed in the events state CASE (e.g. alert 12 h → 13 h);
    5. `since` off by one (`>=` instead of `>`).
- **Where Postgres takes the change-only-sensitive fields from** (L-035 b), so re-fetches that wrote nothing
  still show up exactly as in DuckDB:
  - `series()[id]["as_of"]`: the newest stored vintage among the returned rows, raised to
    `series_state.last_as_of` (the latest vintage SEEN, including identical ones change-only skipped)
    whenever the series returned rows and that vintage is `<= as_of_max` (or no `as_of_max` was given);
  - `catalog()[i]["fetched"]`: `series_state.last_seen_at`, which moves on every write, including one that
    changed nothing (DuckDB's re-written rows carry the latest `fetched_at`);
  - `last_fetch()[s]["n_obs"]`: the `n` the runner passes to `log_fetch` (`fetch_run.n_written`), the same
    number in both.
- **Event refs keep their order:** `event_scope` gets an ordinal (`ord`), and `refs` is
  `array_agg(ref ORDER BY ord)`, so the order is DuckDB's insertion order. The parity inputs use unsorted
  refs to prove it.
- **The allowlist lives inside the parity test** (`ALLOWED` in `tests/test_db_parity.py`), one entry per
  difference, each asserted in its exact form and never skipped. **The orchestrator approves each entry**
  (L-035 c). Proposed:

  | entry | rule | why |
  |---|---|---|
  | `count` | Postgres `<=` DuckDB. If `count()` ever becomes an estimate (`pg_class.reltuples` / `usage_chunk`): within 5 % of the exact count on the same scenario, as a separate entry then | change-only stores fewer rows than DuckDB, which stored every repeated vintage |
  | `series.as_of@as_of_max` | with `as_of_max` set **between** two identical vintages, Postgres `<=` DuckDB (`ts`, `v` equal) | the vintage DuckDB would report wasn't stored; `series_state` only knows the latest one |
  | `events.refs.duplicates` | a ref repeated within one event's `refs` appears once in Postgres | `event_scope`'s primary key is (event, ref). DuckDB kept duplicates; no source sends them today |
  | `events.tie_order` (approved 2026-11-01, import parity) | same id multiset, identical content by id, identical sort-key sequence; the order is free **only among events with an equal `coalesce(published, first_seen)`** | v1 has no tie-breaker after that key, so DuckDB returns equal keys in an arbitrary order. Erik's data has 84 such adjacent ties |
  | `catalog.realized` (L-036) | Postgres's catalog also lists the derived `<key>.realized` series | DuckDB never had them; the backfill derives them |

  The real-data run of the same comparisons is `montney/db/parity.py` (`python -m montney.db.parity`). It
  opens only a temp copy of DuckDB and also compares `snapshot.build()` over the real map.

  **Event order, the contract from now on:** `ORDER BY coalesce(published, first_seen) DESC, id`. Postgres
  breaks ties by `id`, so the API's event order is deterministic. Consumers may rely on that order, and on
  nothing finer than the sort key for v1-era data.

  Everything else is asserted **equal**: values, ts, `as_of` without `as_of_max`, meta, catalog (incl.
  `fetched`), `last_fetch` (incl. `n_obs`), events (incl. refs order), and the whole `snapshot.build()`
  output.
- **Opening a store:** `montney.store.open_store(role="reader" | "writer")` returns PgStore when
  `MONTNEY_DB=postgres` (and later by default), otherwise the DuckDB Store. This is the **only** line that
  changes in `server.py` (`Store(paths.db_path())` → `open_store("reader")`) and in `cli.py` `_store()`.

### Query shapes (Postgres side, all through the v1-column `observation` view or the tier tables)

| method | shape |
|---|---|
| `series` | `SELECT DISTINCT ON (series_id, ts) series_id, ts, value, as_of FROM (tier tables UNION ALL obs_live) WHERE series_id = ANY($ids) AND ts BETWEEN $start AND $end [AND as_of <= $as_of_max] ORDER BY series_id, ts, as_of DESC`. **No rollup fallback in step 1:** past raw retention a range returns only what raw holds, as v1 would have, had it ever dropped anything. A later fallback to `*_1h`/`*_1d` (by `sum/n`) would add a per-series `res: "raw" \| "1h" \| "1d"` key: a **frontend contract**, agreed with "MS Local - Frontend" before it ships (L-035 d) |
| `meta` / `catalog` | `series` joined to `source`; `n`/`first`/`last` from `series_state` + a count over the tier table (catalog only) |
| `events` | the v1 SQL ported as-is (same TTL CASE), with `refs` = `array_agg(ref ORDER BY ord)` from `event_scope`; `?ref=` is an `EXISTS` on `event_scope.ref`; ties in the order are broken by `id` |
| `last_fetch` | `fetch_run` latest per source + `source.last_ok_at` |
| `count` | step 1: an exact sum of `count(*)` over the tier tables. If `/api/status` gets slow at scale: an estimate, allowlisted with a 5 % tolerance (above) |

## 3. Writes (the runner)

- `run_source` keeps its shape. `store.write(res.obs, live=...)` → `PgStore.write`, which is change-only,
  re-anchoring and retrying (merged in #57). `upsert_events` / `log_fetch` keep their signatures.
- **A series' tier comes from the spec's `retention:` key** (RETENTION.md "Spec contract"). This needs:
  `connectors/base.py` to accept `retention:` at source and block level and put it on `SeriesMeta`; every
  spec to declare it; and `tests/test_db_retention.py` to fail when a spec lacks it or contradicts its
  freq/as_of. The backfill (#64) classified today's 479 series, so the PR that adds `retention:` must match
  those classes or carry a migration.
- **Live sources:** an `UNLOGGED` table `obs_live`, replaced on every fetch, never backed up. The API reads it
  like v1's `live_obs`.

## 4. Two processes (L-002)

- **The runner** (`montney run`, later a Windows service): it takes `PgStore.acquire_writer_lock()`, so a
  second runner fails fast. It honours `du.blocked_sources()`, runs `du` every 6 h, and guards the 100 GB
  total (the reviewer's #61 notes).
- **The API** (`montney serve`): read-only, with **no scheduler.** `create_app(schedule=False)` becomes the
  only mode once Postgres is the default.
- **`POST /api/refresh/{source}`** can't run a fetch inside the API any more. **Ruled (L-035 Q-P1):** the API inserts into
  a small `refresh_request(source, requested_at)` table, and the runner polls it (or `LISTEN/NOTIFY`) and
  runs the source. `NOTIFY` may be added later, only as a wake-up hint. The endpoint then returns `{"queued": true}`.
  **`montney_reader` gets `INSERT` on `refresh_request` and nothing else besides SELECT** (#69). It's the one
  write the API makes. The table holds only `(source, requested_at)`, and the runner validates the source
  against the specs before acting.
- **Status dots:** `/api/status` adds `source.budget_state` (`ok`/`warn`/`blocked`) per source, so a
  blocked source shows (PLAN P3).

## 4b. Runner ↔ API contract (step 4: the runner is #85)

The runner (`montney run`, `montney/runner.py` `PgRunner`) is the only writer. It holds the writer lock and
resolves each series' tier from the spec at write time, failing closed (L-036). Budget-blocked sources are
fetched but not written. Periodic tasks: `refresh_requests` every 10 s, `budget_states` every 5 min, `du`
every 6 h (with the 100 GB total guard), and `fill_realized` hourly (L-037).

**The two `server.py` hunks (orchestrator-owned).** Both are one call on the store; `PostgresStore` provides
them, and the DuckDB `Store` doesn't, so guard with `hasattr`:

| endpoint | Postgres behaviour | response |
|---|---|---|
| `POST /api/refresh/{source}` | if `source` is a known spec: `store.request_refresh(source)` (an INSERT into `montney.refresh_request`, the reader role's one write). The runner picks it up within ~10 s, runs the source, and deletes the request. An unknown source → 404, as today | `202 {"queued": true, "source": source}` |
| `GET /api/status` | add `"budget": store.budget_states()` → `{source code: "ok" \| "warn" \| "blocked"}`, so the status dots can show a blocked source | the existing fields + `budget` |

Under DuckDB (the default until Erik's install), both endpoints keep their current behaviour: the in-process
scheduler runs the refresh, and there's no `budget` key.

## 5. Orchestrator-owned changes, in total

| file | change |
|---|---|
| `server.py` | `open_store("reader")`; the refresh endpoint → `refresh_request`; `/api/status` adds `budget_state` |
| `cli.py` | `_store()` → `open_store(...)`; a new `montney run` (the runner process) |
| `connectors/base.py` | the `retention:` key → `SeriesMeta.tier` |
| `sources/*.yaml` (all owners) | add `retention:` |
| `snapshot.py` | **nothing**, if parity holds |

## 6. Order (one PR each)

1. Database: `PgStore` read methods + `open_store` + the parity suite (DuckDB vs Postgres A/B, seeded reds).
   No orchestrator files.
2. Orchestrator + owners: `retention:` in `base.py` and every spec, with Database's cross-check test.
3. Orchestrator: `server.py` / `cli.py` switch (behind `MONTNEY_DB=postgres` first), `refresh_request`,
   `budget_state` in status.
4. Database: the runner process/service, du schedule, the total guard; then backups.
5. Erik: install (Q3) → `db init` → `import-duckdb` → flip the default.

## Rulings (L-035, PR #69)

- **Q-P1:** refresh goes through a `refresh_request` table polled by the runner; `NOTIFY` later, only as a
  wake-up hint. `montney_reader` gets SELECT plus INSERT on `montney.refresh_request` ONLY (L-035 a).
- **Q-P2:** yes, a read-only `montney_reader` role (SELECT only, plus INSERT on `refresh_request`), with its
  password a separate secret (`MONTNEY_DB_READER_PASSWORD`).
- **Q-P3:** yes, `retention:` is a source default + a per-block override. The validation in
  `base.py`/`test_specs` is the orchestrator's; each owner adds `retention:` to their own specs.
- If parity can't be met for some method, it comes back as a contract, **never a `snapshot.py` edit.** The
  default flips only after Erik's install + import + a parity run on his data.
- **L-035 (b)-(e), from the Reviewer's #68 points:** PG sources as above; the allowlist lives in the test, with
  each entry approved by the orchestrator; the rollup fallback (and its `res` key) stays OUT of step 1, and
  if it comes, it's a frontend contract to agree with "MS Local - Frontend" first; and the seeded reds above.
