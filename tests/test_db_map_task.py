"""The "MontneyStack Map" task (L-068): scripts/db/montney-map.ps1 + install-map-task.ps1.

Checked with the real wrapper under PowerShell and a fake `uv` that records its arguments:
- with something already listening on the port, the wrapper exits 0 and never starts a second server (no fight
  with the desktop shortcut's server);
- seeded red: on a free port it does start one, with --host 127.0.0.1, the port and --no-schedule (read-only);
- the scripts never bind 0.0.0.0, are pure ASCII and parse;
- the installer's dry run shows the localhost URL and the every-5-min self-heal, and registers nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "db"
WRAPPER, INSTALLER = SCRIPTS / "montney-map.ps1", SCRIPTS / "install-map-task.ps1"

FAKE_UV = """
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))
"""


@pytest.fixture(scope="module")
def pwsh():
    for name in filter(None, (os.environ.get("MONTNEY_PWSH"), "pwsh", "powershell")):
        found = shutil.which(name)
        if found:
            return found
    if os.environ.get("CI"):
        pytest.fail("no PowerShell on CI: the map-task checks would be a NO-RESULT")
    pytest.skip("no PowerShell here")


def _fake_uv(tmp_path) -> tuple[Path, Path]:
    marker = tmp_path / "served.json"
    script = tmp_path / "fake_uv.py"
    script.write_text(FAKE_UV)
    if sys.platform == "win32":
        exe = tmp_path / "uv.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" "{marker}" %*\r\n')
    else:
        exe = tmp_path / "uv"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{marker}" "$@"\n')
        exe.chmod(0o755)
    return exe, marker


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run_wrapper(pwsh, tmp_path, port):
    exe, marker = _fake_uv(tmp_path)
    p = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER), "-Exe", str(exe),
                        "-Port", str(port), "-LogDir", str(tmp_path / "logs")], capture_output=True, text=True, timeout=60)
    log = (tmp_path / "logs" / "map.log").read_text(encoding="utf-8-sig")
    return p.returncode, marker, log


def test_a_busy_port_means_no_second_server(pwsh, tmp_path):
    port = _free_port()
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen()
    try:
        code, marker, log = _run_wrapper(pwsh, tmp_path, port)
    finally:
        srv.close()
    print(log)
    assert code == 0 and not marker.exists() and "already serving" in log


def test_RED_a_free_port_starts_the_localhost_read_only_server(pwsh, tmp_path):
    port = _free_port()
    code, marker, log = _run_wrapper(pwsh, tmp_path, port)
    args = json.loads(marker.read_text())
    print(code, args)
    assert code == 0 and marker.exists()                                   # it did serve (the check above isn't vacuous)
    i = args.index("serve")
    assert args[i - 1] == "montney" and args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--port") + 1] == str(port) and "--no-schedule" in args and "--no-browser" in args


def test_never_bound_to_all_interfaces_and_scripts_parse(pwsh):
    for path in (WRAPPER, INSTALLER):
        text = path.read_text(encoding="utf-8")
        assert path.read_bytes().isascii(), path.name
        assert "'0.0.0.0'" not in text and "--host 0.0.0.0" not in text, path.name
        cmd = (f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}', "
               "[ref]$null, [ref]$e); if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }")
        out = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stdout + out.stderr


def test_installer_dry_run_registers_nothing(pwsh, tmp_path):
    env = dict(os.environ, MONTNEY_HOME=str(tmp_path))
    out = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER), "-Action", "Install",
                          "-DryRun"], capture_output=True, text=True, timeout=60, env=env)
    print(out.stdout)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "http://127.0.0.1:8050" in out.stdout and "every 5 min" in out.stdout and "montney-map.ps1" in out.stdout
    assert "(dry run: nothing registered)" in out.stdout
