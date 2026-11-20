"""Local CI on Erik's PC: the merge gate since GitHub Actions is off (L-052). Run through scripts/ci/local-ci.ps1.

    local-ci.ps1 -Pr 154            # the PR's head MERGED INTO current origin/main (what will land); status on the head
    local-ci.ps1 -Sha <sha>         # exactly that SHA

One run:
  1. a lock (one run at a time); a log under %LOCALAPPDATA%\\MontneyStack\\ci\\logs\\<run id>.log;
  2. the SHA in a TEMPORARY git worktree under ...\\ci\\runs\\ (never an agent's worktree or the main checkout), then
     `uv sync --extra dev`;
  3. the THROWAWAY Postgres cluster: initdb once into ...\\ci\\pg with PG 18's own binaries (TimescaleDB preloaded, CI's
     worker pool 80/64, localhost only, its own port, a random password kept in ...\\ci\\pg.password); started per run.
     Guards, each a hard stop: port 5432 is refused, and the server must report the CI cluster's own data_directory
     (never Erik's D:\\montney\\pgdata). The `montney` database there is dropped and recreated per run (as CI's fresh
     container);
  4. the checks of .github/workflows/ci.yml (test, db and windows jobs):
     - repo hygiene (no data files);
     - db init / check / du; db backup / restore-test / backups;
     - the full pytest with MONTNEY_DB_REQUIRED=1;
     - the runner-service tests again under Windows PowerShell 5.1;
     - the gate: 0 failures/errors overall, and 0 skipped tests/test_db* tests;
  5. the commit status `local-ci` (success | failure | error) with the counts and the run id; a run that can't finish
     posts `error`, never success;
  6. cleanup: stop the cluster, remove the worktree, release the lock.

The child processes get a CLEAN environment: every inherited MONTNEY_*, PG* and POSTGRES_* variable is dropped
(Erik's user env has MONTNEY_DB=postgres, L-048), then the throwaway values are set, including POSTGRES_SUPERUSER_PASSWORD,
so Windows Credential Manager (Erik's real passwords) is never read.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_SLUG = "Eake123/Montney-Stack"
CONTEXT = "local-ci"
LIVE_PORT = 5432
CI_PORT = 55432
LIVE_DATA_DIR = r"D:\montney\pgdata"
PG_BIN = Path(os.environ.get("MONTNEY_CI_PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
CI_ROOT = Path(os.environ.get("MONTNEY_CI_ROOT") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MontneyStack" / "ci")
DROP_PREFIXES = ("MONTNEY_", "PG", "POSTGRES_", "VIRTUAL_ENV")
PG_CONF = {
    "listen_addresses": "'localhost'",
    "shared_preload_libraries": "'timescaledb'",
    "timescaledb.telemetry_level": "off",
    "max_worker_processes": "80",                     # CI's pool for the scratch-database churn (#134)
    "timescaledb.max_background_workers": "64",
    "max_connections": "200",
    "fsync": "off", "synchronous_commit": "off", "full_page_writes": "off",   # throwaway: speed over durability
}


class Refused(RuntimeError):
    """A safety guard refused the target: nothing runs."""


class MergeConflict(RuntimeError):
    """-Pr: the head doesn't merge cleanly into current main."""


# ---------------------------------------------------------------- guards (pure, unit-tested)
def check_port(port: int) -> None:
    if int(port) == LIVE_PORT:
        raise Refused(f"port {LIVE_PORT} is Erik's live server: local CI uses only its own cluster (port {CI_PORT})")


def check_data_directory(reported: str, expected: Path) -> None:
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))                  # noqa: E731
    if norm(reported) == norm(LIVE_DATA_DIR):
        raise Refused(f"the server at this port is Erik's LIVE cluster ({reported}): refusing")
    if norm(reported) != norm(expected):
        raise Refused(f"the server reports data_directory {reported!r}, not the CI cluster {str(expected)!r}: refusing")


def child_env(base: dict, *, port: int, password: str, home: Path, backups: Path) -> dict:
    """The child processes' environment: inherited MONTNEY_*/PG*/POSTGRES_* dropped, then only throwaway values."""
    check_port(port)
    env = {k: v for k, v in base.items() if not k.upper().startswith(DROP_PREFIXES)}
    env.update({
        "MONTNEY_DB_HOST": "localhost", "MONTNEY_DB_PORT": str(port), "MONTNEY_DB_USER": "montney",
        "MONTNEY_DB_PASSWORD": password, "MONTNEY_DB_READER_PASSWORD": "ci-reader-throwaway",
        "POSTGRES_SUPERUSER_PASSWORD": password, "MONTNEY_DB_RESTORE_USER": "montney",
        "MONTNEY_DB_REQUIRED": "1", "MONTNEY_DB_TESTS_SERVER_IS_THROWAWAY": "1",
        "MONTNEY_PG_BIN": str(PG_BIN), "MONTNEY_BACKUP_DIR": str(backups), "MONTNEY_HOME": str(home),
        "CI": "true", "PYTHONUTF8": "1",
    })
    return env


def may_kill(cmdline: str | None, pg_data: Path) -> bool:
    """Only a postgres whose command line names the CI cluster's data directory may be killed, never Erik's."""
    if not cmdline:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(str(p))).replace("\\", "/")     # noqa: E731
    c = os.path.normcase(cmdline).replace("\\", "/")
    return norm(pg_data) in c and norm(LIVE_DATA_DIR) not in c


def merge_into(wt: Path, ref: str) -> tuple[bool, str]:
    """Merge `ref` into the worktree's detached HEAD (what GitHub's PR merge ref did). On a conflict: abort, False."""
    p = subprocess.run(["git", "-C", str(wt), "-c", "user.name=local-ci", "-c", "user.email=local-ci@localhost",
                        "merge", "--no-edit", "--no-ff", ref], capture_output=True, text=True)
    if p.returncode != 0:
        subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True)
        return False, (p.stdout + p.stderr).strip()[-300:]
    return True, ""


def verdict(steps: list[dict], junit: dict | None) -> tuple[str, str]:
    """(state, description). error if any step couldn't run or produced no result; failure if a check failed; success
    only when every step ran and passed, the run's tests passed and NO tests/test_db* test was skipped."""
    for s in steps:
        if s["status"] == "error":
            return "error", f"error at {s['name']}: {s.get('detail', '')}"[:120]
    if junit is None or junit.get("tests", 0) == 0:
        return "error", "no test results (NO-RESULT is not a pass)"
    counts = f"{junit['passed']} passed, {junit['failed']} failed, {junit['errors']} errors, {junit['skipped']} skipped"
    bad = [s["name"] for s in steps if s["status"] == "fail"]
    if junit["db_skipped"]:
        bad.append(f"{junit['db_skipped']} db test(s) skipped")
    if junit["failed"] or junit["errors"]:
        bad.append("pytest")
    if bad:
        return "failure", f"{counts}; failed: {', '.join(bad)}"[:120]
    return "success", counts[:120]


def read_junit(paths: list[Path]) -> dict | None:
    """Totals over JUnit files, plus how many tests/test_db* cases were skipped."""
    tot = {"tests": 0, "failed": 0, "errors": 0, "skipped": 0, "passed": 0, "db_skipped": 0, "db_tests": 0}
    found = False
    for p in paths:
        if not p.exists():
            continue
        found = True
        for case in ET.parse(p).getroot().iter("testcase"):
            tot["tests"] += 1
            cls = case.get("classname", "")
            is_db = cls.startswith("tests.test_db")
            tot["db_tests"] += is_db
            if case.find("failure") is not None:
                tot["failed"] += 1
            elif case.find("error") is not None:
                tot["errors"] += 1
            elif case.find("skipped") is not None:
                tot["skipped"] += 1
                tot["db_skipped"] += is_db
            else:
                tot["passed"] += 1
    return tot if found else None


# ---------------------------------------------------------------- lock
def acquire_lock(path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held = {}
            if held.get("pid") and _pid_alive(int(held["pid"])):
                raise Refused(f"another local CI run holds {path} (run {held.get('run')}, pid {held['pid']})")
            path.unlink(missing_ok=True)                    # stale: its process is gone
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "run": run_id, "started": _now()}, f)
        return
    raise Refused(f"could not take the lock {path}")


def release_lock(path: Path, run_id: str) -> None:
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("run") == run_id:
            path.unlink()
    except (OSError, ValueError):
        pass


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline(pid: int) -> str | None:
    if sys.platform == "win32":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                             capture_output=True, text=True)
        return out.stdout.strip() or None
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_text().replace("\0", " ")
    except OSError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the run
class Run:
    def __init__(self, sha: str, repo: Path, post: bool = True, merge_main: bool = False):
        self.sha, self.post, self.repo, self.merge_main = sha, post, repo, merge_main
        self.base = ""                                   # "on main@<short>" once merged (-Pr)
        self.id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{sha[:10]}"
        self.dir = CI_ROOT / "runs" / self.id
        self.log_path = CI_ROOT / "logs" / f"{self.id}.log"
        self.steps: list[dict] = []
        self.pg_data = CI_ROOT / "pg"
        self.pw_file = CI_ROOT / "pg.password"
        self.wt = self.dir / "src"
        self._log = None

    # -- logging
    def log(self, msg: str) -> None:
        line = f"{_now()} {msg}"
        print(line, flush=True)
        self._log.write(line + "\n")
        self._log.flush()

    def sh(self, name: str, cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
           timeout: int = 3600, ok_codes=(0,), required: bool = True, via_file: bool = False) -> int:
        """Run one step; its output goes to the log. A non-zero exit is a FAIL (or ERROR for setup steps).
        via_file: send output to a FILE, not a pipe. `pg_ctl start` leaves the server running with pg_ctl's handles,
        and a pipe would then never reach EOF: the first real run hung there (Windows)."""
        t0 = time.monotonic()
        self.log(f"== {name}: {' '.join(cmd)}")
        try:
            if via_file:
                out = CI_ROOT / "step.out"
                with open(out, "w", encoding="utf-8") as fh:
                    p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                       timeout=timeout)
                self._log.write(out.read_text(encoding="utf-8", errors="replace"))
            else:
                p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                                   encoding="utf-8", errors="replace")
                self._log.write(p.stdout)
                self._log.write(p.stderr)
            code = p.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"   {type(e).__name__}: {e}")
            code = -1
        secs = time.monotonic() - t0
        status = "pass" if code in ok_codes else ("fail" if required == "check" else "error")
        self.steps.append({"name": name, "status": status, "seconds": round(secs, 1), "code": code,
                           "detail": f"exit {code}"})
        self.log(f"   -> {status} (exit {code}, {secs:.1f} s)")
        return code

    def check(self, name, cmd, **kw) -> int:
        return self.sh(name, cmd, required="check", **kw)

    # -- the throwaway cluster
    def cluster_password(self) -> str:
        if not self.pw_file.exists():
            self.pw_file.write_text(pysecrets.token_urlsafe(24), encoding="utf-8")
        return self.pw_file.read_text(encoding="utf-8").strip()

    def recover(self) -> None:
        """After an aborted run (killed script or cluster, as the orchestrator had to on 2026-11-01): stop a CI
        server left running (pg_ctl, else kill it, but only if its command line names the CI data directory),
        remove a stale postmaster.pid, and remove leftover run worktrees. Runs under the lock."""
        pidf = self.pg_data / "postmaster.pid"
        if pidf.exists():
            try:
                pid = int(pidf.read_text(encoding="utf-8", errors="replace").splitlines()[0])
            except (OSError, ValueError, IndexError):
                pid = None
            if pid and _pid_alive(pid):
                self.log(f"   recover: a CI server is still running (pid {pid}); stopping it")
                self.pg_ctl("stop")
                if _pid_alive(pid):
                    cmd = _cmdline(pid)
                    if may_kill(cmd, self.pg_data):
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"] if sys.platform == "win32"
                                       else ["kill", "-9", str(pid)], capture_output=True)
                        self.log(f"   recover: killed pid {pid} ({cmd[:120]!r})")
                    else:
                        raise Refused(f"pid {pid} from {pidf} is not the CI cluster ({(cmd or '?')[:120]!r}): not touched")
            if not (pid and _pid_alive(pid)):
                pidf.unlink(missing_ok=True)
                self.log("   recover: removed a stale postmaster.pid")
        runs = CI_ROOT / "runs"
        for d in (runs.iterdir() if runs.exists() else []):
            if d != self.dir:
                subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(d / "src")],
                               capture_output=True)
                shutil.rmtree(d, ignore_errors=True)
                self.log(f"   recover: removed a leftover run dir {d.name}")
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], capture_output=True)

    def ensure_cluster(self) -> None:
        check_port(CI_PORT)
        if (self.pg_data / "PG_VERSION").exists():
            return
        pw = self.cluster_password()
        pwf = CI_ROOT / "pg.pwfile.tmp"
        pwf.write_text(pw, encoding="utf-8")
        try:
            if self.sh("initdb", [str(PG_BIN / "initdb.exe" if sys.platform == "win32" else PG_BIN / "initdb"),
                                  "-D", str(self.pg_data), "-U", "montney", "-E", "UTF8", "--auth=scram-sha-256",
                                  f"--pwfile={pwf}"]) != 0:
                raise RuntimeError("initdb failed")
        finally:
            pwf.unlink(missing_ok=True)
        conf = self.pg_data / "postgresql.conf"
        with open(conf, "a", encoding="utf-8") as f:
            f.write("\n# montney local CI (scripts/ci/local_ci.py): a THROWAWAY cluster\n")
            f.write(f"port = {CI_PORT}\n")
            for k, v in PG_CONF.items():
                f.write(f"{k} = {v}\n")

    def pg_ctl(self, action: str) -> int:
        exe = str(PG_BIN / ("pg_ctl.exe" if sys.platform == "win32" else "pg_ctl"))
        args = [exe, "-D", str(self.pg_data), "-w", "-t", "60"]
        if action == "start":
            args += ["-l", str(CI_ROOT / "pg.log"), "start"]
        elif action == "stop":
            args += ["-m", "fast", "stop"]
        return self.sh(f"pg_ctl {action}", args, ok_codes=(0,) if action == "start" else (0, 1, 3), via_file=True,
                       timeout=120)

    def prepare_databases(self, password: str) -> None:
        """Identity guard, then a fresh `montney` database with timescaledb; leftovers of earlier runs dropped."""
        import psycopg

        kw = dict(host="localhost", port=CI_PORT, user="montney", password=password, autocommit=True, connect_timeout=10)
        with psycopg.connect(dbname="postgres", **kw) as c:
            check_data_directory(c.execute("SHOW data_directory").fetchone()[0], self.pg_data)
        # As CI's image (and Erik's install, where `postgres` created it): timescaledb lives in template1's `public`, so
        # every new database inherits it there. Without it, migration 0001's CREATE EXTENSION lands in the first
        # search_path schema (`montney`), and restores of those dumps fail (`montney.time_bucket` ...).
        with psycopg.connect(dbname="template1", **kw) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb SCHEMA public")
        with psycopg.connect(dbname="postgres", **kw) as c:
            for (d,) in c.execute("SELECT datname FROM pg_database WHERE datname = 'montney' OR datname LIKE "
                                  "'montney\\_t\\_%' OR datname LIKE 'montney\\_restore\\_%'").fetchall():
                c.execute(f'DROP DATABASE "{d}" WITH (FORCE)')
            c.execute("CREATE DATABASE montney OWNER montney")
        with psycopg.connect(dbname="montney", **kw) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        self.log(f"   CI cluster ok: data_directory = {self.pg_data}, port {CI_PORT}; fresh `montney` database")

    # -- status
    def post_status(self, state: str, description: str) -> None:
        if not self.post:
            self.log(f"(not posting) {CONTEXT}: {state}: {description}")
            return
        r = subprocess.run(["gh", "api", f"repos/{REPO_SLUG}/statuses/{self.sha}", "-f", f"state={state}",
                            "-f", f"context={CONTEXT}", "-f", f"description={description} [{self.id}]"[:140]],
                           capture_output=True, text=True)
        self.log(f"posted {CONTEXT}: {state}: {description} (gh exit {r.returncode}) {r.stderr.strip()[:200]}")

    # -- main
    def run(self) -> str:
        CI_ROOT.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "w", encoding="utf-8")
        lock = CI_ROOT / "ci.lock"
        t_all = time.monotonic()
        state, desc, junit = "error", "did not finish", None
        started = False
        try:
            acquire_lock(lock, self.id)
            self.log(f"local CI {self.id}: sha {self.sha}, log {self.log_path}")
            self.post_status("pending", "running")
            # 1. the SHA in a temporary worktree
            self.recover()
            fetch = ["git", "-C", str(self.repo), "fetch", "--quiet", "origin", self.sha]
            if self.merge_main:
                fetch.append("+refs/heads/main:refs/remotes/origin/main")
            if self.sh("git fetch", fetch) != 0:
                raise RuntimeError("git fetch failed")
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.sh("git worktree add", ["git", "-C", str(self.repo), "worktree", "add", "--detach", str(self.wt),
                                            self.sha]) != 0:
                raise RuntimeError("git worktree add failed")
            if self.merge_main:                             # -Pr: test what will land (GitHub tested the merge ref)
                main_sha = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "origin/main"],
                                          capture_output=True, text=True).stdout.strip()
                self.base = f"on main@{main_sha[:7]}"
                ok, detail = merge_into(self.wt, "origin/main")
                self.log(f"== merge origin/main ({main_sha[:10]}) into {self.sha[:10]}: {'ok' if ok else 'CONFLICT'} {detail}")
                if not ok:
                    raise MergeConflict(f"conflicts with main@{main_sha[:7]}")
            if self.sh("uv sync", ["uv", "sync", "--extra", "dev", "--quiet"], cwd=self.wt, timeout=1200) != 0:
                raise RuntimeError("uv sync failed")
            # 2. the throwaway cluster
            self.ensure_cluster()
            pw = self.cluster_password()
            if self.pg_ctl("start") != 0:
                raise RuntimeError("the CI cluster did not start")
            started = True
            self.prepare_databases(pw)
            home, backups = self.dir / "home", self.dir / "backups"
            home.mkdir(exist_ok=True)
            env = child_env(os.environ, port=CI_PORT, password=pw, home=home, backups=backups)
            uv = ["uv", "run", "--no-sync"]
            # 3. the checks (ci.yml: test, db, windows jobs)
            self.check("repo hygiene", uv + ["python", "-c", HYGIENE], cwd=self.wt, env=env)
            for args in (["db", "init"], ["db", "check"], ["du"], ["db", "backup"], ["db", "restore-test"],
                         ["db", "backups"]):
                self.check("montney " + " ".join(args), uv + ["montney", *args], cwd=self.wt, env=env, timeout=600)
            j_all, j_ps51 = self.dir / "pytest.xml", self.dir / "pytest-ps51.xml"
            self.check("pytest (all, DB required)", uv + ["pytest", "-q", "-p", "no:cacheprovider", "-rfEs",
                                                          f"--junitxml={j_all}"], cwd=self.wt, env=env, timeout=5400)
            self.check("pytest runner service under Windows PowerShell 5.1",
                       uv + ["pytest", "-q", "-p", "no:cacheprovider", "tests/test_db_runner_service.py",
                             f"--junitxml={j_ps51}"], cwd=self.wt, env={**env, "MONTNEY_PWSH": "powershell"},
                       timeout=1800)
            junit = read_junit([j_all, j_ps51])
            for j in (j_all, j_ps51):                      # kept beside the log (the run dir is removed)
                if j.exists():
                    shutil.copy2(j, self.log_path.with_name(f"{self.id}_{j.name}"))
            state, desc = verdict(self.steps, junit)
        except MergeConflict as e:
            self.log(f"CONFLICT: {e}")
            state, desc = "error", str(e)
        except Refused as e:
            self.log(f"REFUSED: {e}")
            state, desc = "error", f"refused: {e}"[:120]
            self.steps.append({"name": "guard", "status": "error", "detail": str(e)})
        except Exception as e:                               # noqa: BLE001 - anything unfinished is an error
            self.log(f"ERROR: {type(e).__name__}: {e}")
            state, desc = "error", f"error: {e}"[:120]
        finally:
            if started:
                self.pg_ctl("stop")
            if self.wt.exists():
                subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.wt)],
                               capture_output=True)
                subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], capture_output=True)
                shutil.rmtree(self.dir, ignore_errors=True)
            mins = (time.monotonic() - t_all) / 60
            self.log("steps: " + "; ".join(f"{s['name']} {s['status']} {s.get('seconds', '-')}s" for s in self.steps))
            self.log(f"RESULT {state}: {desc} ({mins:.1f} min)")
            if not any(s["name"] == "guard" and "another local CI run" in s.get("detail", "") for s in self.steps):
                self.post_status(state, (f"{self.base}: " if self.base else "") + f"{desc} ({mins:.0f} min)")
            release_lock(lock, self.id)
            self._log.close()
        return state


HYGIENE = ("import re, subprocess, sys; "
           "bad = [f for f in subprocess.run(['git', 'ls-files'], capture_output=True, text=True).stdout.split() "
           "if re.search(r'\\.(csv|duckdb|xlsx|parquet)$', f, re.I) and not f.startswith('tests/fixtures/')]; "
           "print('data files committed:', bad) if bad else print('no data files committed'); sys.exit(1 if bad else 0)")


def resolve_pr(pr: int) -> str:
    out = subprocess.run(["gh", "pr", "view", str(pr), "--repo", REPO_SLUG, "--json", "headRefOid", "-q", ".headRefOid"],
                         capture_output=True, text=True)
    sha = out.stdout.strip()
    if out.returncode != 0 or len(sha) != 40:
        raise SystemExit(f"could not resolve PR #{pr}: {out.stderr.strip()}")
    return sha


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="local-ci")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sha")
    g.add_argument("--pr", type=int)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]),
                    help="any checkout of the repo to host the temporary worktree (its own files are not touched)")
    a = ap.parse_args(argv)
    sha = a.sha or resolve_pr(a.pr)
    state = Run(sha, repo=Path(a.repo), merge_main=a.pr is not None).run()
    return {"success": 0, "failure": 1}.get(state, 2)


if __name__ == "__main__":
    sys.exit(main())
