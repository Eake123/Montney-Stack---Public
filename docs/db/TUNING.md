# Postgres memory and I/O settings (L-040 gap b)

`timescaledb-tune` never ran during the install, so memory sat at Postgres's defaults on a 64 GB machine. This
is the proposal, how it gets applied (ALTER SYSTEM, never a `postgresql.conf` edit), and what it measured on
Erik's data. Code: `montney/db/tune.py`. Launcher: `scripts/db/apply-tuning.ps1`.

## The machine

| | |
|---|---|
| RAM | 64 GB |
| CPU | 24 logical processors |
| Data disk | `D:` on a Samsung 970 EVO Plus 1 TB, an **NVMe SSD** (`D:\montney\pgdata`) |
| Role | a Windows desktop, shared with the dashboard and a browser; data budget 100 GB |

## The proposal: sized to the data, not the box (orchestrator, L-049)

The database is about 20 MB, and its AB/NE BC budget is 5 GB. The same 64 GB machine also runs T3 and the
agents: T3 alone spiked to 9 GB and crashed on 2026-11-01 (L-047). `shared_buffers` is reserved up front
whether it's used or not, and the BEFORE numbers below show no gain from memory at this size. So the values
are modest.

| setting | now | proposed | restart | why |
|---|---|---|---|---|
| `shared_buffers` | 128MB | **2GB** | yes | room for the 5 GB budget's hot set; reserved up front |
| `effective_cache_size` | 4GB | **16GB** | no | a planner hint: about a quarter of RAM as OS file cache, the rest is T3/agents |
| `work_mem` | 4MB | **32MB** | no | per sort/hash node; 100 connections × a few nodes stays far below RAM |
| `maintenance_work_mem` | 64MB | **512MB** | no | index builds, restores, compression |
| `wal_buffers` | 4MB | **16MB** | yes | the usual cap |
| `max_wal_size` | 1GB | **4GB** | no | fewer forced checkpoints under bulk writes (an import, a restore) |
| `random_page_cost` | 4 | **1.1** | no | NVMe: random reads cost about what sequential ones do |
| `effective_io_concurrency` | 16 | **200** | no | NVMe serves many concurrent reads |
| `max_parallel_workers_per_gather` | 2 | **4** | no | 24 threads; up to 4 workers per query |

Unchanged: `max_worker_processes` 24 and `timescaledb.max_background_workers` 16 (INSTALL.md §3, #134),
`max_parallel_workers` 8, `max_connections` 100.

**Revisit when the database passes about 1.5 GB** (`montney du`, the ab_nebc row). By then the working set
outgrows 2 GB of `shared_buffers`, and the restore and maintenance workloads start to feel
`maintenance_work_mem`. Re-run `tune bench` before and after any change.

**Guard rails (L-049):**
- `tune apply`/`revert` uses the superuser (the `restore` role's credentials) on the `postgres` database only;
- it may ALTER only the settings above, an allowlist checked in code before any statement runs (a red covers
  a setting outside it);
- it runs only from Erik's click or by hand, never from the runner, and a test checks that nothing in
  `montney/` imports it.

## Applying it (Erik's one click)

`shared_buffers` and `wal_buffers` take effect only after a service restart, which needs administrator
rights. So the launcher asks Windows for them once, then:
1. stops the runner;
2. `python -m montney.db.tune apply` (ALTER SYSTEM, then reload);
3. restarts `postgresql-x64-18`;
4. `tune verify`: every value must be live, else FAIL;
5. starts the runner again, even if a step failed.

The file holds no password. The superuser password comes from Credential Manager
(`POSTGRES_SUPERUSER_PASSWORD`, L-040), which montney reads itself.

```powershell
.\scripts\db\apply-tuning.ps1 -CreateShortcut   # puts "Montney - apply DB tuning" on the desktop
.\scripts\db\apply-tuning.ps1                   # apply (asks for admin)
.\scripts\db\apply-tuning.ps1 -Revert           # back to the previous values (also asks for admin)
uv run python -m montney.db.tune show           # live vs proposed
```

## Measured on Erik's data (`python -m montney.db.tune bench`, n=5)

Nothing is written to the store. The restore goes into a scratch `montney_restore_*` database that is dropped
afterwards, and no `backup_run` row is recorded, so the runner's monthly restore schedule is untouched. The
runner kept fetching meanwhile, as it will in real use.

| workload | before: median (min–max) s | after: median (min–max) s |
|---|---|---|
| `restore`: the newest backup into a scratch db (COPY, index builds, pre/post_restore) | 1.182 (1.149–1.301) | *after Erik's click* |
| `history`: `series()` over every series, all history | 0.111 (0.110–0.153) | |
| `daily_agg`: 1-day `time_bucket` aggregate over every observation | 0.077 (0.075–0.079) | |
| `catalog`: `catalog()` | 0.041 (0.039–0.045) | |

"Before" was taken 2026-11-01 17:20Z on about 20 MB of data. **At this size everything already fits in the
128 MB of shared buffers.** Expect little difference now. The settings are for the growth to come (a
100 GB budget, compression, imports and restores), and the "after" column will say what changed.
