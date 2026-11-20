"""Postgres + TimescaleDB storage (PLAN P3).

Design: docs/db/ERD.md (schema, frozen L-033), docs/db/RETENTION.md (tiers, change-only writes, budget),
docs/db/INSTALL.md (install, `montney db init`, migrations).

The app still reads and writes DuckDB (`montney/store.py`) until the P3 port step.
"""

from montney.db.conn import connect, conninfo  # noqa: F401
