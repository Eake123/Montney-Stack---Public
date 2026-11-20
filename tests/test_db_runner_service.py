"""The Windows runner supervisor (scripts/db/montney-runner.ps1) and its scheduled-task installer (PLAN P3).

The supervisor is run for real under PowerShell (pwsh on Linux CI, pwsh/powershell on Windows) with a fake
runner (a Python script exiting with scripted codes) in place of `uv run montney run`. Checked: a crash is
restarted with doubling, capped backoff; a long healthy run resets the backoff; exit 0 and exit 78 (the runner's
config error) are not restarted, while exit 2 (uv's own failures) is; the crash's stderr reaches supervisor.log; the child's PID file is written and removed.
Seeded reds: a tampered copy that treats exit 78 like a crash, and one that never resets the backoff, must fail
the same checks. No PowerShell on CI is a failure, not a skip (NO-RESULT is not a pass).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "db"
SUPERVISOR = SCRIPTS / "montney-runner.ps1"
INSTALLER = SCRIPTS / "install-runner-task.ps1"

FAKE = r'''
import json, sys, time
from pathlib import Path
plan, count = Path(sys.argv[1]), Path(sys.argv[2])
steps = json.loads(plan.read_text())
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n + 1))
import os
(plan.parent / "env.json").write_text(json.dumps({k: os.environ.get(k) for k in ("MONTNEY_DB", "MONTNEY_LOG_CONSOLE")}))
code, sleep = steps[min(n, len(steps) - 1)]
time.sleep(sleep)
if code not in (0, 78):
    print(f"Traceback (most recent call last): fake crash {n}", file=sys.stderr)
sys.exit(code)
'''


def _pwsh() -> str | None:
    """MONTNEY_PWSH picks one (e.g. powershell for Windows PowerShell 5.1, which the scheduled task uses)."""
    for name in filter(None, (os.environ.get("MONTNEY_PWSH"), "pwsh", "powershell")):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def pwsh():
    exe = _pwsh()
    if exe is None:
        if os.environ.get("CI"):
            pytest.fail("no PowerShell on CI: the supervisor checks would be a NO-RESULT")
        pytest.skip("no PowerShell here")
    return exe


def _run(pwsh, tmp_path, steps, script=SUPERVISOR, healthy=60.0, max_restarts=8, extra=()):
    """Run the supervisor over a fake runner that exits with `steps` [(code, seconds), ...] in turn."""
    plan, count, logs = tmp_path / "plan.json", tmp_path / "count.txt", tmp_path / "logs"
    plan.write_text(json.dumps(steps))
    fake = tmp_path / "fake_runner.py"
    fake.write_text(FAKE)
    exe_args = f'"{fake}" "{plan}" "{count}"'
    proc = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                           "-Repo", str(tmp_path), "-Exe", sys.executable, "-ExeArgs", exe_args,
                           "-LogDir", str(logs), "-MinBackoff", "0.1", "-MaxBackoff", "0.4",
                           "-HealthySeconds", str(healthy), "-MaxRestarts", str(max_restarts), *extra],
                          capture_output=True, text=True, timeout=120,
                          env={k: v for k, v in os.environ.items() if k not in ("MONTNEY_DB", "MONTNEY_LOG_CONSOLE")})
    log = (logs / "supervisor.log").read_text(encoding="utf-8-sig") if (logs / "supervisor.log").exists() else ""
    runs = int(count.read_text()) if count.exists() else 0
    waits = [float(w) for w in re.findall(r"restarting in ([\d.]+) s", log)]
    return {"code": proc.returncode, "runs": runs, "waits": waits, "log": log, "logs": logs,
            "stderr": proc.stderr}


def test_a_crash_is_restarted_with_doubling_capped_backoff(pwsh, tmp_path):
    r = _run(pwsh, tmp_path, [(1, 0)] * 4 + [(0, 0)])
    print(r["code"], r["runs"], r["waits"])
    assert r["code"] == 0 and r["runs"] == 5, (r["log"], r["stderr"])
    assert r["waits"] == [0.1, 0.2, 0.4, 0.4]                          # doubles, capped at MaxBackoff
    assert "stderr| Traceback (most recent call last): fake crash 0" in r["log"]
    assert not (r["logs"] / "runner.pid").exists()                     # removed after each exit


@pytest.mark.parametrize("code", [0, 78])
def test_a_clean_stop_or_config_error_is_not_restarted(pwsh, tmp_path, code):
    r = _run(pwsh, tmp_path, [(code, 0), (0, 0)])
    print(code, r["code"], r["runs"], r["waits"])
    assert (r["code"], r["runs"], r["waits"]) == (code, 1, [])
    if code == 78:
        assert "configuration error: not restarting" in r["log"]


def test_exit_2_from_uv_is_restarted(pwsh, tmp_path):
    """Reviewer (#99): uv exits 2 when it can't spawn the command (a venv mid-sync), argparse too. That is
    transient: restart it, don't stop the service until the next logon."""
    r = _run(pwsh, tmp_path, [(2, 0), (0, 0)])
    print(r["code"], r["runs"], r["waits"])
    assert (r["code"], r["runs"], r["waits"]) == (0, 2, [0.1])


def test_the_supervisor_and_the_runner_agree_on_the_config_code():
    from montney import runner

    assert f"$NoRestartCodes = @(0, {runner.EXIT_CONFIG})" in SUPERVISOR.read_text(encoding="utf-8")


def test_a_healthy_run_resets_the_backoff(pwsh, tmp_path):
    # two quick crashes (0.1, 0.2), then a run longer than HealthySeconds crashes: back to 0.1, not 0.4
    r = _run(pwsh, tmp_path, [(1, 0), (1, 0), (1, 1.5), (0, 0)], healthy=1.0)
    print(r["waits"])
    assert r["code"] == 0 and r["waits"] == [0.1, 0.2, 0.1]


def test_the_restart_cap_ends_a_crash_loop(pwsh, tmp_path):
    r = _run(pwsh, tmp_path, [(1, 0)], max_restarts=2)
    assert (r["code"], r["runs"]) == (3, 3) and "reached the cap" in r["log"]


def test_the_pid_file_names_the_running_child(pwsh, tmp_path):
    """While the runner runs, runner.pid holds its PID (what `-Action Stop` kills)."""
    import time

    plan, count, logs = tmp_path / "plan.json", tmp_path / "count.txt", tmp_path / "logs"
    plan.write_text(json.dumps([[0, 4]]))
    fake = tmp_path / "fake_runner.py"
    fake.write_text(FAKE)
    p = subprocess.Popen([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SUPERVISOR),
                          "-Repo", str(tmp_path), "-Exe", sys.executable,
                          "-ExeArgs", f'"{fake}" "{plan}" "{count}"', "-LogDir", str(logs)])
    try:
        pidfile, seen = logs / "runner.pid", None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and seen is None:
            if pidfile.exists() and pidfile.read_text().strip():
                seen = int(pidfile.read_text().split()[0])          # "<pid> <start time>"
            time.sleep(0.1)
        assert seen is not None, "runner.pid never appeared"
        log = (logs / "supervisor.log").read_text(encoding="utf-8-sig")
        assert f"runner started: pid {seen}" in log
        assert p.wait(timeout=60) == 0
        assert not pidfile.exists()
    finally:
        if p.poll() is None:
            p.kill()


def test_the_runner_gets_postgres_and_a_quiet_console(pwsh, tmp_path):
    """The supervisor sets MONTNEY_DB=postgres and MONTNEY_LOG_CONSOLE=0 when unset, so runner-stderr.log holds
    crashes only and runner.log (rotated by the runner) is the log."""
    r = _run(pwsh, tmp_path, [(0, 0)])
    env = json.loads((tmp_path / "env.json").read_text())
    assert r["code"] == 0 and env == {"MONTNEY_DB": "postgres", "MONTNEY_LOG_CONSOLE": "0"}, env


def _rotation(pwsh, tmp_path, script=SUPERVISOR):
    r = _run(pwsh, tmp_path, [(1, 0)] * 6 + [(0, 0)], script=script, extra=("-LogMaxBytes", "300"))
    files = sorted(p.name for p in r["logs"].glob("supervisor.log*"))
    sizes = {p.name: p.stat().st_size for p in r["logs"].glob("supervisor.log*")}
    print(files, sizes)
    return r, files, sizes


def test_supervisor_log_is_size_capped(pwsh, tmp_path):
    r, files, sizes = _rotation(pwsh, tmp_path)
    assert r["code"] == 0 and files == ["supervisor.log", "supervisor.log.1"]     # never a third file
    assert all(n < 300 + 400 for n in sizes.values())                          # cap + one write's worth


# ------------------------------------------------------------------ seeded reds
def test_RED_no_rotation_is_caught(pwsh, tmp_path):
    bad = _tampered(tmp_path, "(Get-Item $Log).Length -gt $LogMaxBytes", "$false")
    r, files, sizes = _rotation(pwsh, tmp_path, script=bad)
    assert files == ["supervisor.log"] and sizes["supervisor.log"] > 700         # the cap check would go red

def _tampered(tmp_path, old: str, new: str) -> Path:
    text = SUPERVISOR.read_text(encoding="utf-8")
    assert old in text, f"tamper target {old!r} not found: the red would test nothing"
    bad = tmp_path / "tampered" / "montney-runner.ps1"
    bad.parent.mkdir()
    bad.write_text(text.replace(old, new, 1), encoding="utf-8")
    return bad


def test_RED_exit_78_restarted_like_a_crash_is_caught(pwsh, tmp_path):
    bad = _tampered(tmp_path, "$NoRestartCodes = @(0, 78)", "$NoRestartCodes = @(0)")
    r = _run(pwsh, tmp_path, [(78, 0), (0, 0)], script=bad)
    print(r["code"], r["runs"], r["waits"])
    assert (r["code"], r["runs"], r["waits"]) != (78, 1, [])           # the check above would go red
    assert r["runs"] == 2


def test_RED_exit_2_treated_as_config_is_caught(pwsh, tmp_path):
    """The old policy (#99 at 7ddb96b): stopping on 2 would leave a uv spawn failure down until logon."""
    bad = _tampered(tmp_path, "$NoRestartCodes = @(0, 78)", "$NoRestartCodes = @(0, 2)")
    r = _run(pwsh, tmp_path, [(2, 0), (0, 0)], script=bad)
    assert (r["code"], r["runs"]) == (2, 1)                            # != (0, 2): the uv check goes red


def test_RED_no_backoff_reset_is_caught(pwsh, tmp_path):
    bad = _tampered(tmp_path, "if ($ran -ge $HealthySeconds) { $backoff = $MinBackoff }", "")
    r = _run(pwsh, tmp_path, [(1, 0), (1, 0), (1, 1.5), (0, 0)], script=bad, healthy=1.0)
    print(r["waits"])
    assert r["waits"] == [0.1, 0.2, 0.4]                               # != [0.1, 0.2, 0.1]


# ------------------------------------------------------------------ the installer
def test_scripts_parse_and_are_ascii(pwsh):
    """Both scripts parse (PowerShell's own parser) and are pure ASCII, so Windows PowerShell 5.1 reads them
    right without a BOM."""
    for path in (SUPERVISOR, INSTALLER):
        assert path.read_bytes().isascii(), f"{path.name} has non-ASCII bytes"
        cmd = (f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}', "
               "[ref]$null, [ref]$e); if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }")
        out = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stdout + out.stderr


def test_installer_dry_run_registers_nothing_and_names_the_supervisor(pwsh, tmp_path):
    env = dict(os.environ, MONTNEY_HOME=str(tmp_path))
    out = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                          "-Action", "Install", "-DryRun"], capture_output=True, text=True, timeout=60, env=env)
    print(out.stdout)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "montney-runner.ps1" in out.stdout and "at logon" in out.stdout
    assert "every 5 min" in out.stdout                     # self-healing trigger (runner killed 2026-11-02 04:36Z)
    assert "(dry run: nothing registered)" in out.stdout


# ------------------------------------------------------------------ the runner side (Python)
@pytest.fixture
def root_logging():
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    yield root
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
            h.close()
    for h in saved:
        if h not in root.handlers:
            root.addHandler(h)


@pytest.mark.parametrize("quiet", ["0", "1"], ids=["service-quiet", "RED-console-kept"])
def test_runner_console_is_quiet_under_the_service_and_a_crash_reaches_runner_log(
        quiet, root_logging, tmp_path, monkeypatch, capfd):
    """MONTNEY_LOG_CONSOLE=0 (set by the supervisor) leaves runner.log as the only log; a failed start is
    logged there with its traceback either way. Seeded red: with "1" the console copy is still written."""
    import logging

    from montney import runner, store

    root_logging.addHandler(logging.StreamHandler())               # what `montney`'s basicConfig installs
    root_logging.setLevel(logging.INFO)
    monkeypatch.setenv("MONTNEY_DB", "postgres")
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setenv("MONTNEY_LOG_CONSOLE", quiet)

    def boom(role):
        raise RuntimeError("database is down")

    monkeypatch.setattr(store, "open_store", boom)
    with pytest.raises(RuntimeError):
        runner.run_forever()
    for h in root_logging.handlers:
        h.flush()
    text = (tmp_path / "logs" / "runner.log").read_text(encoding="utf-8")
    err = capfd.readouterr().err
    print(f"quiet={quiet}: console {len(err)} chars")
    assert "runner: could not start" in text and "database is down" in text
    if quiet == "0":
        assert "could not start" not in err
    else:
        assert "could not start" in err


# ------------------------------------------------------------------ runner.pid is validated (reviewer, #99)
def _runner_action(pwsh, home, script=INSTALLER):
    env = dict(os.environ, MONTNEY_HOME=str(home))
    out = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", "Runner"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def _sleeper(code="import time; time.sleep(60)"):
    return subprocess.Popen([sys.executable, "-c", code])


def _write_pid(home, text):
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "logs" / "runner.pid").write_text(text, encoding="ascii")
    return home / "logs" / "runner.pid"


def test_a_live_runner_pid_is_accepted(pwsh, tmp_path):
    """The supervisor's own runner.pid (pid + start time) names its live child, and -Action Runner accepts it."""
    import time

    plan, count, logs = tmp_path / "plan.json", tmp_path / "count.txt", tmp_path / "logs"
    plan.write_text(json.dumps([[0, 8]]))
    fake = tmp_path / "fake_runner.py"
    fake.write_text(FAKE)
    p = subprocess.Popen([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SUPERVISOR),
                          "-Repo", str(tmp_path), "-Exe", sys.executable,
                          "-ExeArgs", f'"{fake}" "{plan}" "{count}"', "-LogDir", str(logs)])
    try:
        pidfile = logs / "runner.pid"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
            time.sleep(0.1)
        child = int(pidfile.read_text().split()[0])
        out = _runner_action(pwsh, tmp_path)
        print(pidfile.read_text(), out)
        assert f"runner: pid {child} (python" in out and "stale" not in out
        assert pidfile.exists()                                         # a valid file is kept
    finally:
        p.wait(timeout=60)


@pytest.mark.parametrize("case", ["reused-pid-wrong-start", "not-python", "not-running", "old-format"])
def test_a_stale_runner_pid_is_never_trusted(pwsh, tmp_path, case):
    """After a power loss runner.pid can name a PID that now belongs to something else. It must be reported
    stale and removed, and that process left alone (Stop would otherwise taskkill /T /F it)."""
    victim = None
    try:
        if case == "reused-pid-wrong-start":                    # a python process, but not the one recorded
            victim = _sleeper()
            text = f"{victim.pid} 2001-01-01T00:00:00.0000000Z"
        elif case == "not-python":                              # a live process that isn't uv/python
            victim = subprocess.Popen([pwsh, "-NoProfile", "-Command", "Start-Sleep 60"])
            text = f"{victim.pid} 2001-01-01T00:00:00.0000000Z"
        elif case == "not-running":
            gone = _sleeper("pass")
            gone.wait()
            text = f"{gone.pid} 2001-01-01T00:00:00.0000000Z"
        else:                                                   # the format before #99: just a PID
            victim = _sleeper()
            text = f"{victim.pid}"
        pidfile = _write_pid(tmp_path, text)
        out = _runner_action(pwsh, tmp_path)
        print(case, out)
        assert "stale runner.pid" in out and not pidfile.exists()
        if victim is not None:
            assert victim.poll() is None, "the stale PID's process was touched"
    finally:
        if victim is not None and victim.poll() is None:
            victim.kill()


def test_RED_no_start_time_check_trusts_a_reused_pid(pwsh, tmp_path):
    """Seeded red: without the start-time comparison, a python process that merely reuses the PID is taken for
    the runner (and Stop would kill it)."""
    text = INSTALLER.read_text(encoding="utf-8")
    needle = ".TotalSeconds) -gt 2)"
    assert needle in text
    bad = tmp_path / "tampered" / "install-runner-task.ps1"
    bad.parent.mkdir()
    bad.write_text(text.replace(needle, ".TotalSeconds) -gt 1e12)", 1), encoding="utf-8")
    victim = _sleeper()
    try:
        _write_pid(tmp_path, f"{victim.pid} 2001-01-01T00:00:00.0000000Z")
        out = _runner_action(pwsh, tmp_path, script=bad)
        print(out)
        assert f"runner: pid {victim.pid}" in out and "stale" not in out    # the check above would go red
    finally:
        victim.kill()


# ------------------------------------------------------------------ the task's own command line, Windows PowerShell 5.1
# Erik's install (2026-11-01) failed at once: the task runs `powershell.exe ... -File montney-runner.ps1 -Exe uv`
# (no -Repo), and Windows PowerShell 5.1 leaves $PSScriptRoot EMPTY in param() defaults under -File, so the old
# `$Repo = (Split-Path ... $PSScriptRoot)` default threw. Every test above passes -Repo, so that default was
# never evaluated, and CI's Linux pwsh 7 fills $PSScriptRoot anyway. These run the task's real command line
# under 5.1 (the Windows CI job, and locally on Windows).
REPO = SCRIPTS.parent.parent
OLD_DEFAULT = "    [string]$Repo = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),   # scripts/db -> the repo"


@pytest.fixture(scope="module")
def winps():
    if sys.platform != "win32":
        pytest.skip("Windows PowerShell 5.1 exists only on Windows (the windows CI job runs these)")
    exe = shutil.which("powershell")
    if exe is None:
        pytest.fail("no Windows PowerShell on Windows: the 5.1 checks would be a NO-RESULT")
    return exe


def _task_cmd(ps: str, script: Path, *extra: str) -> list[str]:
    """Exactly what install-runner-task.ps1 registers (powershell.exe -NoProfile -ExecutionPolicy Bypass
    -WindowStyle Hidden -File <supervisor> -Exe <uv>), plus test-only extras."""
    return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script), *extra]


def test_the_task_command_line_runs_under_windows_powershell_5_1(winps, tmp_path):
    plan, count = tmp_path / "plan.json", tmp_path / "count.txt"
    plan.write_text(json.dumps([[0, 0]]))
    fake = tmp_path / "fake_runner.py"
    fake.write_text(FAKE)
    env = dict(os.environ, MONTNEY_HOME=str(tmp_path))                  # the default LogDir, under tmp
    p = subprocess.run(_task_cmd(winps, SUPERVISOR, "-Exe", sys.executable, "-ExeArgs", f'"{fake}" "{plan}" "{count}"'),
                       capture_output=True, text=True, timeout=120, env=env)
    log = (tmp_path / "logs" / "supervisor.log").read_text(encoding="utf-8-sig") if (tmp_path / "logs" / "supervisor.log").exists() else ""
    print(p.returncode, p.stderr[-500:], log[-500:])
    assert p.returncode == 0, p.stderr
    assert f"in {REPO}" in log                                          # $Repo resolved to this repo, no -Repo given
    assert int(count.read_text()) == 1


def test_the_installer_dry_run_under_windows_powershell_5_1(winps, tmp_path):
    env = dict(os.environ, MONTNEY_HOME=str(tmp_path))
    p = subprocess.run(_task_cmd(winps, INSTALLER, "-Action", "Install", "-DryRun"),
                       capture_output=True, text=True, timeout=60, env=env)
    print(p.stdout, p.stderr)
    assert p.returncode == 0, p.stderr
    assert str(SUPERVISOR) in p.stdout and f"in:         {REPO}" in p.stdout


def test_RED_the_old_param_default_fails_under_windows_powershell_5_1(winps, tmp_path):
    """Seeded red: the pre-fix default, put back in a copy, must reproduce Erik's failure under 5.1 -File."""
    text = SUPERVISOR.read_text(encoding="utf-8")
    new_default = "    [string]$Repo = '',"
    assert new_default in text
    bad_dir = tmp_path / "scripts" / "db"
    bad_dir.mkdir(parents=True)
    bad = bad_dir / "montney-runner.ps1"
    line = next(ln for ln in text.splitlines() if ln.startswith(new_default))
    bad.write_text(text.replace(line, OLD_DEFAULT, 1), encoding="utf-8")
    p = subprocess.run(_task_cmd(winps, bad, "-Exe", sys.executable, "-LogDir", str(tmp_path / "logs")),
                       capture_output=True, text=True, timeout=60)
    print(p.returncode, p.stderr[-400:])
    assert p.returncode != 0 and "Cannot bind argument to parameter 'Path'" in p.stderr


# The 5.1 tests exist only on Windows: on Linux they would be SKIPPED, and the db job's gate (none skipped)
# rightly counts a skip as a NO-RESULT. Not collected there instead; on Windows (the windows CI job, Erik's
# machine) they are collected and must pass (no Windows PowerShell there = FAIL, see the fixture).
if sys.platform != "win32":
    del test_the_task_command_line_runs_under_windows_powershell_5_1
    del test_the_installer_dry_run_under_windows_powershell_5_1
    del test_RED_the_old_param_default_fails_under_windows_powershell_5_1


def test_stop_survives_the_pid_file_race_and_finishes(pwsh, tmp_path):
    """`-Action Stop` kills the runner tree while the supervisor removes the same runner.pid: the removal must never
    abort Stop (2026-11-03: it threw at the Remove-Item twice, leaving the task disabled). Run for real against a
    fake supervisor + runner under a throwaway MONTNEY_HOME and a task name that doesn't exist (Erik's task and
    runner are never touched), several times, since it's a race."""
    import time
    import uuid

    for i in range(3):
        home = tmp_path / f"h{i}"
        plan, count, logs = home / "plan.json", home / "count.txt", home / "logs"
        home.mkdir()
        plan.write_text(json.dumps([[0, 30]]))
        fake = home / "fake_runner.py"
        fake.write_text(FAKE)
        sup = subprocess.Popen([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SUPERVISOR),
                                "-Repo", str(home), "-Exe", sys.executable,
                                "-ExeArgs", f'"{fake}" "{plan}" "{count}"', "-LogDir", str(logs), "-MaxRestarts", "1", "-MinBackoff", "30"])  # no relaunch during the check
        try:
            pidfile = logs / "runner.pid"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
                time.sleep(0.1)
            assert pidfile.exists(), "runner.pid never appeared"
            env = dict(os.environ, MONTNEY_HOME=str(home))
            out = subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                                  "-Action", "Stop", "-TaskName", f"MontneyStack TEST {uuid.uuid4()}"],
                                 capture_output=True, text=True, timeout=60, env=env)
            print(out.stdout, out.stderr)
            assert out.returncode == 0, out.stdout + out.stderr
            assert "stopped and disabled" in out.stdout and "cannot find the file" not in out.stderr
            assert not pidfile.exists()
        finally:
            if sup.poll() is None:
                sup.kill()


def test_no_bare_pid_file_removal_is_left():
    """Every runner.pid removal goes through the non-fatal form (the seeded red is the pre-fix script)."""
    for path in (INSTALLER, SUPERVISOR):
        text = path.read_text(encoding="utf-8")
        assert "Remove-Item -Force -ErrorAction SilentlyContinue $PidFile" not in text, path.name
