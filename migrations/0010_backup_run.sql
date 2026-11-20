-- 0010_backup_run: the nightly backup and the monthly restore test (PLAN P3, docs/db/BACKUPS.md).
-- One row per attempt, written by montney/db/backup.py (the runner's `backup` task, `montney db backup`,
-- `montney db restore-test`, and the pre-migrate dump in `db init`). The runner decides what is due from it:
-- a dump when there is no ok dump yet this Alberta day, a restore test when there is none this Alberta month.
CREATE TABLE montney.backup_run (
    id        bigint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind      text      NOT NULL CHECK (kind IN ('dump', 'restore_test')),
    reason    text      NOT NULL DEFAULT '' CHECK (reason IN ('', 'nightly', 'manual', 'pre_migrate', 'monthly')),
    started   timestamp NOT NULL,                     -- naive UTC
    finished  timestamp,
    ok        boolean   NOT NULL DEFAULT false,
    path      text      NOT NULL DEFAULT '',          -- the dump written (dump) or restored (restore_test)
    bytes     bigint,
    detail    text      NOT NULL DEFAULT ''           -- the error, or a one-line summary
);
CREATE INDEX backup_run_kind_started ON montney.backup_run (kind, started DESC);
