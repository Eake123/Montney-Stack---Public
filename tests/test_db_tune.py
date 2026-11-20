"""Memory/IO tuning (montney/db/tune.py, scripts/db/apply-tuning.ps1, docs/db/TUNING.md; L-040 gap b).

No database: the proposal is well-formed (SHOW-formatted values, a reason each, only the restart-only settings
flagged), verify catches a value that isn't live (seeded red), the launcher parses, is ASCII and holds no
password, and TUNING.md lists every value. Database (CI): every name is a real server setting (a typo would be
an ALTER SYSTEM error on Erik's machine, or a silently unknown custom setting).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from montney.db import tune

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "db" / "apply-tuning.ps1"
DOC = ROOT / "docs" / "db" / "TUNING.md"


def test_the_proposal_is_well_formed():
    assert {n for n, (_v, r, _w) in tune.PROPOSED.items() if r} == {"shared_buffers", "wal_buffers"}
    for n, (v, _r, why) in tune.PROPOSED.items():
        assert re.fullmatch(r"\d+(\.\d+)?(kB|MB|GB)?", v), (n, v)          # SHOW's own format
        assert len(why) > 20, n
    assert not set(tune.PROPOSED) & set(tune.UNCHANGED)


def _live(**over):
    cur = {n: {"value": v, "pending_restart": False} for n, (v, _r, _w) in tune.PROPOSED.items()}
    for n, v in over.items():
        cur[n] = {"value": v, "pending_restart": n == "shared_buffers"}
    return cur


def test_verify_passes_when_every_value_is_live():
    assert tune.mismatches(_live()) == []


@pytest.mark.parametrize("name, live", [("shared_buffers", "128MB"), ("work_mem", "4MB"), ("random_page_cost", "4")])
def test_RED_verify_catches_a_value_that_is_not_live(name, live):
    bad = tune.mismatches(_live(**{name: live}))
    print(bad)
    assert len(bad) == 1 and bad[0].startswith(f"{name}: live {live!r}")
    if name == "shared_buffers":
        assert "restart pending" in bad[0]


class _RecordingConn:
    def __init__(self):
        self.ran = []

    def execute(self, sql, params=None):
        self.ran.append(sql)


def test_apply_alters_exactly_the_allowlist():
    assert set(tune.PROPOSED) == tune.ALLOWED_SETTINGS
    c = _RecordingConn()
    tune.apply(c)
    altered = {s.split()[3] for s in c.ran if s.startswith("ALTER SYSTEM SET")}
    assert altered == tune.ALLOWED_SETTINGS and c.ran[-1] == "SELECT pg_reload_conf()"


@pytest.mark.parametrize("op", ["apply", "revert"])
def test_RED_a_setting_outside_the_allowlist_is_refused_and_nothing_runs(op, monkeypatch):
    """L-049: the superuser may change anything, so `tune` limits itself. A setting slipped into PROPOSED that
    isn't on the allowlist (here fsync) makes apply/revert refuse BEFORE any ALTER SYSTEM runs."""
    monkeypatch.setitem(tune.PROPOSED, "fsync", ("off", False, "never: a red, not a proposal"))
    c = _RecordingConn()
    with pytest.raises(tune.NotAllowed, match="fsync"):
        getattr(tune, op)(c)
    assert c.ran == []


def test_the_runner_never_tunes():
    """L-049: tune runs from Erik's click or by hand only. The runner (and the rest of the app) never imports it."""
    for path in (ROOT / "montney").rglob("*.py"):
        if path.name == "tune.py":
            continue
        assert "montney.db.tune" not in path.read_text(encoding="utf-8") and "db import tune" not in \
            path.read_text(encoding="utf-8"), path


def test_the_docs_list_every_proposed_value():
    doc = DOC.read_text(encoding="utf-8")
    for n, (v, _r, _w) in tune.PROPOSED.items():
        assert f"`{n}`" in doc and v in doc, n


def test_the_launcher_is_ascii_has_no_password_and_parses():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert LAUNCHER.read_bytes().isascii()
    assert not re.search(r"PGPASSWORD|-Password|password\s*=", text, re.I)    # read from Credential Manager by montney
    shells = [s for s in (shutil.which("pwsh"), shutil.which("powershell")) if s]
    if not shells:
        pytest.skip("no PowerShell here")
    for sh in shells:
        cmd = (f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{LAUNCHER}', "
               "[ref]$null, [ref]$e); if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }")
        out = subprocess.run([sh, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (sh, out.stdout + out.stderr)


def test_every_setting_name_exists_on_the_server():
    from montney.db.testing import admin_connection

    admin = admin_connection()
    try:
        cur = tune.current(admin)
    finally:
        admin.close()
    print({n: c["value"] for n, c in cur.items()})
    assert set(cur) == set(tune.PROPOSED) | set(tune.UNCHANGED)
