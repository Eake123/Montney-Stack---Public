"""Connecting to Postgres.

Defaults: host localhost, port 5432, database montney. Override with MONTNEY_DB_HOST, MONTNEY_DB_PORT,
MONTNEY_DB_NAME.

Two roles (L-035 Q-P2):
* writer (the runner, `db init`, imports): user MONTNEY_DB_USER (default `montney`), password secret
  MONTNEY_DB_PASSWORD;
* reader (the API, SELECT only): user MONTNEY_DB_READER_USER (default `montney_reader`), password secret
  MONTNEY_DB_READER_PASSWORD.

Secrets: `uv run montney secrets set <NAME>`; an environment variable of that name wins, which is how CI
passes its throwaway passwords. Never read from a spec, a config file or the repo.
"""

from __future__ import annotations

import os

from montney import secrets

PASSWORD_SECRET = "MONTNEY_DB_PASSWORD"
READER_PASSWORD_SECRET = "MONTNEY_DB_READER_PASSWORD"
READER_USER = "montney_reader"


# Under pytest only these databases may be opened: the maintenance database (to create/drop scratch ones) and
# the scratch databases themselves (montney/db/testing.py). A test that forgets dbname= must fail here, not
# reach the live store (reviewer, after L-040 put a real server on Erik's machine).
TEST_DB_PREFIXES = ("montney_t_", "montney_restore_")
TEST_MAINTENANCE_DB = "postgres"


class LiveDatabaseRefused(RuntimeError):
    pass


def under_pytest() -> bool:
    """PYTEST_CURRENT_TEST is set only while a test runs; a connect at import or collection time would slip
    past it, so an imported pytest counts too (reviewer, #101). The app never imports pytest."""
    import sys

    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _pytest_guard(dbname: str) -> None:
    if not under_pytest():
        return
    if dbname == TEST_MAINTENANCE_DB or dbname.startswith(TEST_DB_PREFIXES):
        return
    raise LiveDatabaseRefused(
        f"refusing to connect to database {dbname!r} under pytest: tests may open only {TEST_MAINTENANCE_DB!r} "
        f"and scratch databases ({', '.join(p + '*' for p in TEST_DB_PREFIXES)}); pass dbname= explicitly")


RESTORE_PASSWORD_SECRET = "POSTGRES_SUPERUSER_PASSWORD"   # stored at install (L-040)
RESTORE_DB_PREFIX = "montney_restore_"


def _user_and_secret(role: str) -> tuple[str, str]:
    if role == "reader":
        return os.environ.get("MONTNEY_DB_READER_USER", READER_USER), READER_PASSWORD_SECRET
    if role == "restore":
        return os.environ.get("MONTNEY_DB_RESTORE_USER", "postgres"), RESTORE_PASSWORD_SECRET
    return os.environ.get("MONTNEY_DB_USER", "montney"), PASSWORD_SECRET


def conninfo(dbname: str | None = None, role: str = "writer") -> dict:
    """Roles: writer (the runner, init, imports), reader (the API), and restore: the superuser, ONLY for
    the backup restore test's scratch databases (TimescaleDB's timescaledb_pre_restore() stops background
    workers, which no grant allows a non-superuser; CI #104). The restore role is refused any database but
    `postgres` and montney_restore_*."""
    if role not in ("writer", "reader", "restore"):
        raise ValueError(f"role must be 'writer', 'reader' or 'restore', got {role!r}")
    dbname = dbname or os.environ.get("MONTNEY_DB_NAME", "montney")
    _pytest_guard(dbname)                         # before the secret is read: a refused test learns nothing
    if role == "restore" and not (dbname == TEST_MAINTENANCE_DB or dbname.startswith(RESTORE_DB_PREFIX)):
        raise LiveDatabaseRefused(f"the restore role may open only {TEST_MAINTENANCE_DB!r} and "
                                  f"{RESTORE_DB_PREFIX}* databases, not {dbname!r}")
    if role == "restore" and under_pytest() and not (os.environ.get("MONTNEY_DB_REQUIRED") == "1"
                                                     or os.environ.get("MONTNEY_DB_TESTS") == "1"):
        # the superuser never connects from a test that hasn't opted into DB tests (a laptop's server is live)
        raise LiveDatabaseRefused("the restore role (superuser) is off in tests unless DB tests are enabled "
                                  "(MONTNEY_DB_REQUIRED=1 or MONTNEY_DB_TESTS=1)")
    user, secret = _user_and_secret(role)
    return {
        "host": os.environ.get("MONTNEY_DB_HOST", "localhost"),
        "port": int(os.environ.get("MONTNEY_DB_PORT", "5432")),
        "dbname": dbname,
        "user": user,
        "password": secrets.get(secret),
        "connect_timeout": 5,
        "application_name": f"montney-{role}",
    }


def connect(dbname: str | None = None, autocommit: bool = True, role: str = "writer"):
    """A psycopg connection. Autocommit by default: callers open explicit transactions.

    Auto-prepare is OFF (prepare_threshold=None), for every role (L-102). psycopg prepares a query after its 5th execution,
    and Postgres then soon switches a prepared statement to a GENERIC plan. For PgStore.series' DISTINCT ON / UNION ALL
    read (the snapshot's main query, run with a different id list and window every time) the generic plan spills to disk:
    /api/snapshot?z=3 builds went 1 s -> 37 s from the 3rd build on (BufFileRead), and the live map took 35-38 s per
    snapshot. Unprepared, every execution gets a custom plan for its real parameters: ~1 s, every time. The cost is a
    re-plan per query (milliseconds), which the writer's batch paths can afford too, and it rules the same trap out there.
    """
    import psycopg

    conn = psycopg.connect(**conninfo(dbname, role), autocommit=autocommit)
    conn.prepare_threshold = None
    return conn
