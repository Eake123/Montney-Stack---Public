"""Numbered plain-SQL migrations (docs/db/INSTALL.md "Migrations").

* Files: migrations/NNNN_<name>.sql, applied in number order, never renumbered.
* Each file runs in its own transaction together with its ledger row: a failing file leaves no trace.
* A file whose first line is `-- montney:no-transaction` runs outside a transaction, one chunk at a
  time, chunks separated by lines reading `-- montney:break` (for statements Postgres refuses inside a
  transaction block, e.g. CREATE INDEX CONCURRENTLY). Such a file MUST be idempotent (IF NOT EXISTS,
  CREATE OR REPLACE, ...): if chunk 3 fails, chunks 1-2 stay applied, no ledger row is written, and the
  next `db init` runs the whole file again.
* The sha256 of every applied file is recorded. An applied file that changed on disk, or an applied
  version missing on disk, is a hard error: fixes are new migrations.
* A session advisory lock serialises `db init` against a second `db init` or the runner.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from montney.paths import REPO_ROOT

MIGRATIONS_DIR = REPO_ROOT / "migrations"
FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
NO_TX = "-- montney:no-transaction"
BREAK = "-- montney:break"
LOCK_KEY = 0x6D6F6E746E6579  # "montney"

LEDGER_SQL = """
CREATE SCHEMA IF NOT EXISTS montney;
CREATE TABLE IF NOT EXISTS montney.schema_migration (
    version    integer   PRIMARY KEY,
    name       text      NOT NULL,
    sha256     text      NOT NULL,
    applied_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    sha256: str

    @property
    def no_transaction(self) -> bool:
        return self.sql.lstrip().startswith(NO_TX)


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    out: list[Migration] = []
    for p in sorted(directory.glob("*.sql")):
        m = FILE_RE.match(p.name)
        if not m:
            raise MigrationError(f"{p.name}: migration files must be named NNNN_<name>.sql")
        raw = p.read_bytes()
        out.append(Migration(int(m[1]), m[2], p, raw.decode("utf-8"),
                             hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()))
    versions = [m.version for m in out]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration numbers in {directory}")
    if not out:
        raise MigrationError(f"no migrations found in {directory}")
    return out


def _applied(conn) -> dict[int, tuple[str, str, object]]:
    conn.execute(LEDGER_SQL)
    rows = conn.execute("SELECT version, name, sha256, applied_at FROM montney.schema_migration").fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def status(conn, directory: Path = MIGRATIONS_DIR) -> list[dict]:
    """One row per migration: version, name, state (applied | pending), applied_at.
    Raises MigrationError on a changed applied file or an applied version missing on disk."""
    files = discover(directory)
    applied = _applied(conn)
    on_disk = {m.version for m in files}
    missing = sorted(set(applied) - on_disk)
    if missing:
        raise MigrationError(f"applied migration(s) missing on disk: {missing}")
    out = []
    for m in files:
        a = applied.get(m.version)
        if a and a[1] != m.sha256:
            raise MigrationError(
                f"{m.path.name} changed after it was applied (sha256 {a[1][:12]} -> {m.sha256[:12]}). "
                "Applied migrations are never edited: write a new one.")
        out.append({"version": m.version, "name": m.name,
                    "state": "applied" if a else "pending", "applied_at": a[2] if a else None})
    return out


def apply(conn, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in order. `conn` must be in autocommit mode. Returns applied file names."""
    if not conn.autocommit:
        raise MigrationError("apply() needs an autocommit connection (each file opens its own transaction)")
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        st = {r["version"]: r["state"] for r in status(conn, directory)}
        done = []
        for m in discover(directory):
            if st[m.version] == "applied":
                continue
            try:
                if m.no_transaction:
                    for chunk in m.sql.split(BREAK):
                        if chunk.strip() and chunk.strip() != NO_TX:
                            conn.execute(chunk)
                    _record(conn, m)
                else:
                    with conn.transaction():
                        conn.execute(m.sql)
                        _record(conn, m)
            except Exception as e:
                raise MigrationError(f"{m.path.name} failed, nothing from it was applied: {e}") from e
            done.append(m.path.name)
        ensure_reader_grants(conn)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _record(conn, m: Migration) -> None:
    conn.execute("INSERT INTO montney.schema_migration (version, name, sha256) VALUES (%s, %s, %s)",
                 (m.version, m.name, m.sha256))


def ensure_reader_grants(conn, reader: str | None = None) -> bool:
    """Give the read-only API role SELECT on everything in the schema (L-035 Q-P2), if the role exists.
    Idempotent; run after every `db init`, so tables added by later migrations are covered. Returns whether
    the role exists."""
    from montney.db.conn import READER_USER
    import os

    reader = reader or os.environ.get("MONTNEY_DB_READER_USER", READER_USER)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (reader,)).fetchone():
        return False
    ident = '"' + reader.replace('"', '""') + '"'
    conn.execute(f"GRANT USAGE ON SCHEMA montney TO {ident}")
    conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA montney TO {ident}")
    conn.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA montney GRANT SELECT ON TABLES TO {ident}")
    # its one write (L-035 a): asking the runner to refresh a source
    if conn.execute("SELECT to_regclass('montney.refresh_request')").fetchone()[0]:
        conn.execute(f"GRANT INSERT ON montney.refresh_request TO {ident}")
    return True
