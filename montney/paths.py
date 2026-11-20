"""Where things live.

Repo (shareable, on GitHub):   sources/  network/  web/
User home (private, never committed): DuckDB file, weather grids, caches, user-added sources.

The home dir defaults to %LOCALAPPDATA%\\MontneyStack on Windows and
~/.local/share/montney elsewhere. Override with the MONTNEY_HOME env var.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_SOURCES = REPO_ROOT / "sources"
WEB_DIR = REPO_ROOT / "web"


def home() -> Path:
    env = os.environ.get("MONTNEY_HOME")
    if env:
        p = Path(env)
    elif sys.platform == "win32":
        p = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "MontneyStack"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "montney"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "montney.duckdb"


def user_sources() -> Path:
    p = home() / "sources"
    p.mkdir(exist_ok=True)
    return p
