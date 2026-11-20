"""Local CI's safety guards and verdict (scripts/ci/local_ci.py, L-052). No database: pure functions.

Seeded reds: port 5432 is refused; a server reporting Erik's live data directory (or any other than the CI
cluster's) is refused; inherited MONTNEY_DB=postgres / MONTNEY_DB_NAME / PG* / POSTGRES_* never reach the child
processes; a skipped DB test, a failed test or an unfinished step can never yield `success`; a second run while one
holds the lock is refused.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("local_ci", ROOT / "scripts" / "ci" / "local_ci.py")
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


def test_port_5432_is_refused():
    with pytest.raises(ci.Refused, match="5432"):
        ci.check_port(5432)
    ci.check_port(ci.CI_PORT)


@pytest.mark.parametrize("reported", [r"D:\montney\pgdata", r"d:/MONTNEY/pgdata", r"C:\other\cluster"])
def test_a_server_that_is_not_the_ci_cluster_is_refused(reported, tmp_path):
    with pytest.raises(ci.Refused):
        ci.check_data_directory(reported, tmp_path / "pg")


def test_the_ci_cluster_itself_is_accepted(tmp_path):
    ci.check_data_directory(str(tmp_path / "pg"), tmp_path / "pg")


def test_the_child_environment_drops_every_inherited_db_variable(tmp_path):
    base = {"PATH": "x", "MONTNEY_DB": "postgres", "MONTNEY_DB_NAME": "montney", "PGPASSWORD": "real",
            "POSTGRES_SUPERUSER_PASSWORD": "real-superuser", "MONTNEY_DB_PASSWORD": "real", "PGDATA": r"D:\montney\pgdata",
            "MONTNEY_HOME": r"C:\Users\erikj\AppData\Local\MontneyStack"}
    env = ci.child_env(base, port=ci.CI_PORT, password="throwaway", home=tmp_path / "h", backups=tmp_path / "b")
    assert env["PATH"] == "x"
    for k in ("MONTNEY_DB", "MONTNEY_DB_NAME", "PGPASSWORD", "PGDATA"):
        assert k not in env, k
    assert env["MONTNEY_DB_PASSWORD"] == env["POSTGRES_SUPERUSER_PASSWORD"] == "throwaway"   # Credential Manager never read
    assert env["MONTNEY_DB_PORT"] == str(ci.CI_PORT) and env["MONTNEY_DB_REQUIRED"] == "1"
    assert env["MONTNEY_DB_TESTS_SERVER_IS_THROWAWAY"] == "1" and env["MONTNEY_HOME"] == str(tmp_path / "h")
    with pytest.raises(ci.Refused):
        ci.child_env(base, port=5432, password="x", home=tmp_path, backups=tmp_path)


def _junit(**kw):
    j = {"tests": 100, "failed": 0, "errors": 0, "skipped": 0, "passed": 100, "db_skipped": 0, "db_tests": 60}
    j.update(kw)
    return j


OK = [{"name": "pytest", "status": "pass"}]


def test_success_only_when_everything_ran_and_passed():
    assert ci.verdict(OK, _junit())[0] == "success"
    assert "100 passed" in ci.verdict(OK, _junit())[1]


@pytest.mark.parametrize("steps, junit, want", [
    (OK, _junit(skipped=1, passed=99, db_skipped=1), "failure"),           # a skipped DB test is never a pass
    (OK, _junit(failed=2, passed=98), "failure"),
    ([{"name": "montney db init", "status": "fail"}], _junit(), "failure"),
    ([{"name": "uv sync", "status": "error", "detail": "exit 1"}], _junit(), "error"),
    (OK, None, "error"),                                                   # no results = NO-RESULT
    (OK, _junit(tests=0, passed=0), "error"),
], ids=["RED-db-skip", "RED-test-failed", "RED-step-failed", "RED-unfinished", "RED-no-junit", "RED-zero-tests"])
def test_RED_nothing_but_a_clean_run_is_success(steps, junit, want):
    assert ci.verdict(steps, junit)[0] == want


def test_read_junit_counts_db_skips(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text('<testsuites><testsuite>'
                 '<testcase classname="tests.test_db_backup" name="a"/>'
                 '<testcase classname="tests.test_db_backup" name="b"><skipped message="x"/></testcase>'
                 '<testcase classname="tests.test_server" name="c"><skipped message="y"/></testcase>'
                 '<testcase classname="tests.test_server" name="d"><failure message="z"/></testcase>'
                 '</testsuite></testsuites>')
    j = ci.read_junit([p, tmp_path / "missing.xml"])
    assert (j["tests"], j["passed"], j["skipped"], j["failed"], j["db_skipped"], j["db_tests"]) == (4, 1, 2, 1, 1, 2)
    assert ci.read_junit([tmp_path / "none.xml"]) is None


def test_RED_a_second_run_while_one_holds_the_lock_is_refused(tmp_path):
    lock = tmp_path / "ci.lock"
    ci.acquire_lock(lock, "run-1")                                 # held by this (live) process
    with pytest.raises(ci.Refused, match="another local CI run"):
        ci.acquire_lock(lock, "run-2")
    ci.release_lock(lock, "run-2")                                 # not the holder: nothing happens
    assert lock.exists()
    ci.release_lock(lock, "run-1")
    assert not lock.exists()


def test_a_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "ci.lock"
    lock.write_text('{"pid": 999999, "run": "dead"}')               # no such process
    ci.acquire_lock(lock, "run-3")
    assert '"run-3"' in lock.read_text()


# ------------------------------------------------------------------ recovery after an aborted run; -Pr merges main
def test_recovery_kills_only_the_ci_cluster(tmp_path):
    pg = tmp_path / "ci" / "pg"
    ours = f'"C:\Program Files\PostgreSQL\18\bin\postgres.exe" -D "{pg}"'
    assert ci.may_kill(ours, pg)


@pytest.mark.parametrize("cmd", [r'"C:\Program Files\PostgreSQL\18\bin\postgres.exe" -D "D:\montney\pgdata"',
                                 "notepad.exe", "", None],
                         ids=["RED-eriks-live-cluster", "RED-another-process", "RED-empty", "RED-unknown"])
def test_RED_recovery_never_kills_anything_else(cmd, tmp_path):
    assert not ci.may_kill(cmd, tmp_path / "ci" / "pg")


def _git(cwd, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("1\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "base")
    return r


def test_pr_runs_the_head_merged_into_main(tmp_path):
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "-b", "pr")
    (r / "b.txt").write_text("pr\n")
    _git(r, "add", "b.txt")
    _git(r, "commit", "-q", "-m", "pr change")
    head = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    (r / "c.txt").write_text("main moved on\n")
    _git(r, "add", "c.txt")
    _git(r, "commit", "-q", "-m", "main change")
    _git(r, "checkout", "-q", "--detach", head)
    ok, detail = ci.merge_into(r, "main")
    assert ok, detail
    assert (r / "b.txt").exists() and (r / "c.txt").exists()               # both: what lands


def test_RED_a_conflict_with_main_is_reported_and_aborted(tmp_path):
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "-b", "pr")
    (r / "a.txt").write_text("pr\n")
    _git(r, "commit", "-q", "-am", "pr edits a")
    head = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    (r / "a.txt").write_text("main\n")
    _git(r, "commit", "-q", "-am", "main edits a")
    _git(r, "checkout", "-q", "--detach", head)
    ok, detail = ci.merge_into(r, "main")
    print(detail)
    assert not ok and "CONFLICT" in detail.upper()
    assert _git(r, "status", "--porcelain") == "" and (r / "a.txt").read_text() == "pr\n"   # aborted cleanly
