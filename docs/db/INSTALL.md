# Installing Postgres + TimescaleDB on Windows (for Erik)

**Status: DRAFT guide.** Don't start until the orchestrator says P3's design is approved. Nothing here has
been run on your machine yet. An agent wrote it and checked it against the vendors' pages. Expect about
20 minutes, with two downloads (~350 MB and ~10 MB).

## Versions (checked 2026-10-31)

| component | version | why |
|---|---|---|
| **PostgreSQL** | **18.6** (Windows x86-64, EDB installer). If 18.7 is out by then (expected 2026-11-12), take that: TimescaleDB needs the same *major* (18), not a particular minor | newest stable major supported by current TimescaleDB |
| **TimescaleDB** | **2.31.0** (2026-10-20), file `timescaledb-postgresql-18-windows-amd64.zip` from the GitHub release | newest release. 2.29-2.31 support PostgreSQL 16, 17 and 18. 2.28 was the last one with 15 |
| Visual C++ Redistributable | 2015-2022, x64 | TimescaleDB lists it as a prerequisite. The EDB installer usually brings it already |

Sources: [TimescaleDB supported platforms](https://www.tigerdata.com/docs/get-started/timescaledb-supported-platforms)
(2.31.x / 2.30.x / 2.29.x → PG 16, 17, 18; Windows 10/11 and Server 2019-2025),
[TimescaleDB Windows install](https://www.tigerdata.com/docs/self-hosted/latest/install/installation-windows),
[TimescaleDB 2.31.0 release](https://github.com/timescale/timescaledb/releases/tag/2.31.0) (assets for PG 16, 17
and 18 on Windows), [PostgreSQL Windows downloads](https://www.postgresql.org/download/windows/) (EDB installer),
and [PostgreSQL 18.6 release](https://www.postgresql.org/about/news/postgresql-186-1711-1615-1519-1424-and-19-beta-3-released-3365/).

**Don't install PostgreSQL 19.** It's at RC stage, and no TimescaleDB release supports it yet. Also note EDB
tests PostgreSQL 18 on Windows Server 2022/2025. Windows 11 isn't in EDB's table, but it's on TimescaleDB's
list, and EDB says comparable versions generally work. The verification step below proves it on your machine.

## Before you start

- **Disk:** the data lives in its own folder. Recommended: `D:\montney\pgdata`. D: has ~554 GB free, and the
  budget is 100 GB. Don't put it inside the repo or a worktree, and not on G: (that's Google Drive; it's only
  for backups).
- **Passwords:** you'll choose two.
  1. The `postgres` superuser password. You type it into the installer. Keep it in your own password manager.
     Montney never stores it.
  2. The app password for the `montney` role. It goes into Windows Credential Manager with
     `montney secrets set` (step 6). It never goes in a file.

## 1. Install PostgreSQL 18

1. From <https://www.postgresql.org/download/windows/>, follow "Download the installer" to EDB and pick
   **18.6, Windows x86-64**.
2. Run it. In the wizard:
   - **Components:** PostgreSQL Server and Command Line Tools. pgAdmin 4 is optional. Untick Stack Builder.
   - **Data Directory:** `D:\montney\pgdata`.
   - **Password:** the `postgres` superuser password (see above).
   - **Port:** 5432. **Locale:** default.
3. It installs the Windows service `postgresql-x64-18`, which starts automatically at boot.
4. Put the tools on your PATH (TimescaleDB's setup needs `pg_config`). In PowerShell:
   ```powershell
   $p = [Environment]::GetEnvironmentVariable("Path", "User")
   [Environment]::SetEnvironmentVariable("Path", "$p;C:\Program Files\PostgreSQL\18\bin", "User")
   ```
   Open a **new** PowerShell and check: `pg_config --version` → `PostgreSQL 18.6`.

## 2. Install TimescaleDB 2.31.0

1. Download `timescaledb-postgresql-18-windows-amd64.zip` from
   <https://github.com/timescale/timescaledb/releases/tag/2.31.0> and unzip it anywhere, e.g. `Downloads`.
2. In the unzipped `timescaledb` folder, right-click `setup.exe` → **Run as administrator**.
3. It finds PostgreSQL through `pg_config`. If it asks for `postgresql.conf`, the path is
   `D:\montney\pgdata\postgresql.conf`.
4. When it offers to run **timescaledb-tune**, accept. That adds `timescaledb` to
   `shared_preload_libraries` and sizes memory settings for your RAM.

## 3. Settings to check in `D:\montney\pgdata\postgresql.conf`

Open it in a text editor and make sure these lines exist (add or edit them; remove a leading `#`):

```ini
shared_preload_libraries = 'timescaledb'   # tune sets this
listen_addresses = 'localhost'             # EDB's default is '*'; only this PC needs the DB
timescaledb.telemetry_level = off          # no usage telemetry to Tiger Data
timescaledb.max_background_workers = 16    # retention, compression and rollup jobs
max_worker_processes = 24                  # 16 TimescaleDB workers + 8 parallel query workers; the default 8 runs out
max_wal_size = 1GB
```

`max_worker_processes` has to cover TimescaleDB's background workers (16, plus its launcher and one
scheduler per database) **and** Postgres's own parallel query workers (`max_parallel_workers`, 8). With the
default 8, jobs fail to start: the log fills with `WARNING: failed to launch job ... out of background
workers`, and retention, compression and rollup refreshes run late. CI's container gets the same values.

Then restart the service (PowerShell as administrator):

```powershell
Restart-Service postgresql-x64-18
```

## 4. Verify: this must pass before anything else

```powershell
psql -U postgres -h localhost -c "SHOW server_version;" -c "SHOW shared_preload_libraries;"
```

Expect `18.6` (or 18.x) and `timescaledb`. Then create the app's role and database, and the extension. You'll
be asked for the `postgres` password each time. `\password` prompts for the app password with hidden input,
so it never lands in your shell history. Use the same one you'll store in step 6.

```powershell
psql -U postgres -h localhost -c "CREATE ROLE montney LOGIN;" -c "CREATE DATABASE montney OWNER montney;"
psql -U postgres -h localhost -c "\password montney"
psql -U postgres -h localhost -d montney -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';" -c "SHOW timescaledb.license;"
```

| check | must be | if not |
|---|---|---|
| `extversion` | `2.31.0` | wrong zip: re-run `setup.exe` from the 2.31.0 PG 18 zip |
| `timescaledb.license` | `timescale` (Community edition) | `apache` means compression, rollups and policies are unavailable: **stop and tell the orchestrator** |
| `CREATE EXTENSION` | no error | "could not load library": install the VC++ 2015-2022 x64 redistributable, restart, retry |

`montney db check` will later run these checks plus a smoke test: a scratch hypertable that gets compressed,
rolled up and dropped. It fails on any mismatch, and it fails closed: no connection means FAIL, never "skipped".

## 5. The Python side

Nothing to install by hand. The P3 build adds the `psycopg` driver to `pyproject.toml`, and `uv sync` picks
it up.

## 6. Store the app password

```powershell
uv run montney secrets set MONTNEY_DB_PASSWORD
```

It prompts with hidden input and stores the password in Windows Credential Manager (service `montney-stack`),
like the API keys. Check it with `uv run montney secrets check MONTNEY_DB_PASSWORD`. The connection otherwise
uses defaults: host `localhost`, port `5432`, database `montney`, user `montney`. The P3 build documents
overrides (`MONTNEY_DB_HOST`, `MONTNEY_DB_PORT`, `MONTNEY_DB_NAME`, `MONTNEY_DB_USER`). The password is never
read from a spec, a config file or the repo.

### 6b. The read-only role for the map's API (L-035)

The API only reads, so it logs in as `montney_reader`, which can SELECT and nothing else. Create it once as the
`postgres` superuser (roles belong to the whole server, so a migration doesn't create it), then store its
password:

```powershell
psql -U postgres -h localhost -c "CREATE ROLE montney_reader LOGIN;"
psql -U postgres -h localhost -c "\password montney_reader"
uv run montney secrets set MONTNEY_DB_READER_PASSWORD
```

`montney db init` (step 7) grants it SELECT on everything in the `montney` schema, every time it runs. If you
create the role after `db init`, just run `db init` again.

## 7. Create the tables: `montney db init`

```powershell
uv run montney db init      # applies all pending migrations
uv run montney db status    # lists applied and pending migrations
uv run montney db check     # the step 4 checks + smoke test
```

### Migrations

- Plain SQL files in `migrations/`, numbered and never renumbered: `0001_core.sql`,
  `0002_observation_tiers.sql`, `0003_rollups.sql`, ...
- `montney db init` connects as `montney`, takes a Postgres advisory lock (so the runner and a second `init`
  can't race), creates `montney.schema_migration` if needed, and applies every pending file in number order.
  **Each file runs in its own transaction.** If one fails, it rolls back and stops, and nothing after it runs.
- Each applied file's sha256 is recorded. **An applied migration is never edited.** A changed checksum is a
  hard error. Fixes are new migrations.
- No down-migrations. Before applying anything to a database that already has migrations, `db init` makes a
  `pg_dump` backup first (`montney-<time>-pre_migrate.dump`, the same mechanism as the nightly backup). If
  that backup fails, nothing is applied (`--no-backup` overrides). Rolling back means restoring it
  ([BACKUPS.md](BACKUPS.md)).
- A file whose first line is `-- montney:no-transaction` runs outside a transaction, for statements Postgres
  refuses inside one (`CREATE INDEX CONCURRENTLY`). Rollups are created `WITH NO DATA`, so they don't need it.
  **Such a file must be idempotent** (`IF NOT EXISTS`, `CREATE OR REPLACE`). If it fails partway, the
  statements before the failure stay applied, no ledger row is written, and the next `db init` reruns the
  whole file.
- The DB tests run only in CI's Linux `db` job. The Windows job has no service containers, so they skip there.
- CI runs every migration from empty against the service container **`timescale/timescaledb:2.31.0-pg18`**.
  Not the `-oss` tag: that's the Apache build, with no columnstore, rollups or policies. The
  `timescaledb.license = timescale` check in `db check` fails on it, so a wrong tag turns CI red rather than
  passing quietly. It also has a seeded red: a deliberately broken migration fixture must make `db init` fail
  and leave the ledger unchanged.
- **Database tests fail closed in CI.** `tests/test_db*` may skip on a laptop without Postgres. CI sets
  `MONTNEY_DB_REQUIRED=1`, which turns "no database" into a failure, not a skip (Wellsite §4.2, §4.9). The
  build PR's proof includes the CI log lines showing those tests actually ran.

## 8. Keep the runner running

Once `db init` and `db check` pass, register the runner as a logon task that restarts it after a crash:
**[SERVICE.md](SERVICE.md)**. It needs no install and no admin rights.

## Later: upgrades and backups

- **TimescaleDB minor upgrade:** install the new zip's `setup.exe`, restart the service, then run
  `psql -X -U montney -h localhost -d montney -c "ALTER EXTENSION timescaledb UPDATE;"` (`-X`, so nothing else
  runs first in that session). `montney db check` then shows the new version.
- **PostgreSQL minor upgrade** (18.6 → 18.7): run the new EDB installer over the old one. The data stays.
- **Backups:** nightly by the runner, to `G:\My Drive\MontneyStack\backups`, with 7 daily, 4 weekly and 12
  monthly copies and a monthly restore test (which uses the `postgres` superuser, confined to scratch
  databases). Everything is in
  **[BACKUPS.md](BACKUPS.md)**.

## Undo

Uninstall "PostgreSQL 18" from Windows Settings → Apps. That removes the service and binaries, and the
TimescaleDB DLLs inside them. The data folder `D:\montney\pgdata` stays until you delete it yourself. Remove
the PATH entry from step 1.4, and run `uv run montney secrets delete MONTNEY_DB_PASSWORD`.
