"""L-081 PR 3a: every upstream request is counted and limited per host, and the runner logs what each run downloaded.

- Context counts every HTTP attempt (retries too) and the bytes on the wire; run_source logs them on success AND on
  failure (a failing source still costs requests).
- At most PER_HOST_LIMIT requests are in flight to one host across threads; other hosts aren't held up.
- `allow=(404,)` returns a 404 rather than raising (the three ex-ctx.client callers depend on it).
"""

from __future__ import annotations

import pathlib
import threading
import time
from datetime import datetime

import httpx
import pytest

from montney import runner
from montney.connectors import base
from montney.connectors.base import Context, FetchResult

NOW = datetime(2026, 11, 2, 18)


def _ctx(handler) -> Context:
    return Context(client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)


def test_requests_and_bytes_are_counted_retries_included(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200, content=b"x" * 1000)

    ctx = _ctx(handler)
    assert ctx.get("https://a.example/data").status_code == 200
    assert ctx.n_requests == 2 and ctx.bytes_in == 2000


def test_allow_returns_the_status_instead_of_raising():
    ctx = _ctx(lambda req: httpx.Response(404))
    assert ctx.get("https://a.example/missing", allow=(404,)).status_code == 404
    with pytest.raises(httpx.HTTPStatusError):
        ctx.get("https://a.example/missing")
    assert ctx.n_requests == 2


def test_at_most_per_host_limit_in_flight_and_other_hosts_are_not_held_up():
    live, peak, lock = {"a.example": 0, "b.example": 0}, {"a.example": 0, "b.example": 0}, threading.Lock()

    def handler(req):
        h = req.url.host
        with lock:
            live[h] += 1
            peak[h] = max(peak[h], live[h])
        time.sleep(0.15)
        with lock:
            live[h] -= 1
        return httpx.Response(200, content=b"ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ts = [threading.Thread(target=lambda: Context(client=client, now=NOW).get("https://a.example/x")) for _ in range(6)]
    ts.append(threading.Thread(target=lambda: Context(client=client, now=NOW).get("https://b.example/x")))
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print(peak)
    assert peak["a.example"] == base.PER_HOST_LIMIT          # seeded red: PER_HOST_LIMIT = 10 -> peak 6
    assert peak["b.example"] == 1


class _Store:
    """Records what run_source logs (only the calls run_source makes)."""

    def __init__(self):
        self.logged = []

    def upsert_meta(self, metas):
        pass

    def write(self, obs, live=False):
        return len(obs)

    def log_fetch(self, source, started, ok, n, error, **kw):
        self.logged.append((ok, kw))


@pytest.mark.parametrize("fail", [False, True])
def test_run_source_logs_requests_and_bytes(monkeypatch, fail):
    class Conn:
        live = False

        def run(self, ctx):
            ctx.get("https://a.example/1")
            ctx.get("https://a.example/2")
            if fail:
                raise ValueError("format drift")
            return FetchResult()

    monkeypatch.setattr(runner.connectors, "build", lambda spec: Conn())
    st = _Store()
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"y" * 300)))
    res = runner.run_source(st, {"id": "test_src"}, client)
    assert res["ok"] is (not fail)
    assert st.logged == [(not fail, {"n_requests": 2, "bytes_in": 600, "trigger": "manual"})]     # the default


# ------------------------------------------------------------------ conditional GET (L-107)
def _etag_server(calls):
    def handler(req):
        calls.append(dict(req.headers))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"file", headers={"ETag": '"v1"', "Last-Modified": "Fri, 13 Nov 2026 14:30:00 GMT"})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("fail", [False, True])
def test_an_unchanged_file_is_not_downloaded_again_but_only_after_a_run_that_was_written(monkeypatch, fail):
    base._validators.clear()
    calls, got = [], []

    class Conn:
        live = False

        def run(self, ctx):
            got.append(ctx.get_if_changed("https://a.example/daily.csv"))
            if fail and len(got) == 1:
                raise ValueError("format drift")          # the first run downloads the file, then fails to parse it
            return FetchResult()

    monkeypatch.setattr(runner.connectors, "build", lambda spec: Conn())
    client = _etag_server(calls)
    for _ in range(3):
        runner.run_source(_Store(), {"id": "test_src"}, client)
    # a failed run keeps nothing: the next run downloads the file again (it was never stored); then 304s
    want = [200, 200, None] if fail else [200, None, None]
    assert [r.status_code if r is not None else None for r in got] == want
    assert ["if-none-match" in c for c in calls] == [w is None for w in want]     # seeded red: commit in get_if_changed
    assert calls[-1]["if-modified-since"] == "Fri, 13 Nov 2026 14:30:00 GMT"
    # Reviewer on #461: another source reading the same URL never stored it, so it must get the file, not a 304
    n = len(got)
    runner.run_source(_Store(), {"id": "other_src"}, client)
    assert got[n].status_code == 200 and "if-none-match" not in calls[-1]        # seeded red: key by URL only
    # ... and a context with no source (montney try, importers) never sends a validator
    assert Context(client=client, now=NOW).get_if_changed("https://a.example/daily.csv").status_code == 200
    assert "if-none-match" not in calls[-1]
    base._validators.clear()


def test_a_server_without_validators_is_downloaded_every_time_and_other_statuses_pass_through():
    base._validators.clear()
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(404 if req.url.path == "/missing" else 200, content=b"x")))
    ctx = Context(client=client, now=NOW, source="s")
    assert ctx.get_if_changed("https://a.example/f").status_code == 200
    assert ctx.commit_validators() == 0 and not base._validators
    assert ctx.get_if_changed("https://a.example/f").status_code == 200
    assert ctx.get_if_changed("https://a.example/missing", allow=(404,)).status_code == 404   # "not published yet"
    with pytest.raises(httpx.HTTPStatusError):
        ctx.get_if_changed("https://a.example/missing")


# ------------------------------------------------------------------ the database (CI / local CI)
def test_fetch_run_records_requests_and_bytes_and_survives_a_pre_0012_database():
    from montney.db.pgstore import PostgresStore
    from montney.db.testing import scratch_db

    with scratch_db() as conn:
        st = PostgresStore(conn)
        st.log_fetch("test_src", NOW, True, 3, None, n_requests=4, bytes_in=12345)
        assert conn.execute("SELECT n_requests, bytes_in FROM montney.fetch_run").fetchall() == [(4, 12345)]
    with scratch_db() as conn:
        PostgresStore(conn).log_fetch("test_src", NOW, True, 3, None, n_requests=1, trigger="schedule")
        assert conn.execute("SELECT trigger FROM montney.fetch_run").fetchall() == [("schedule",)]
    with scratch_db() as conn:                       # new code, database not yet migrated to 0012 / 0013
        conn.execute("ALTER TABLE montney.fetch_run DROP COLUMN n_requests")
        conn.execute("ALTER TABLE montney.fetch_run DROP COLUMN trigger")
        st = PostgresStore(conn)
        st.log_fetch("test_src", NOW, True, 3, None, n_requests=4, bytes_in=12345, trigger="schedule")
        assert conn.execute("SELECT n_written, bytes_in FROM montney.fetch_run").fetchall() == [(3, 12345)]


def test_the_trigger_says_what_started_the_run(monkeypatch):
    """L-081 / 0013: the loop logs schedule, the API and refresh_request log refresh, importers log import."""
    class Conn:
        live = False

        def run(self, ctx):
            return FetchResult()

    monkeypatch.setattr(runner.connectors, "build", lambda spec: Conn())
    st = _Store()
    runner.run_source(st, {"id": "s"}, httpx.Client(), trigger="import")
    sched = runner.Scheduler.__new__(runner.Scheduler)        # just run_now's plumbing
    sched.store, sched.client, sched.specs = st, httpx.Client(), {"s": {"id": "s"}}
    sched._locks = {"s": threading.Lock()}
    sched.run_now("s")
    sched.run_now("s", "schedule")
    assert [kw["trigger"] for _, kw in st.logged] == ["import", "refresh", "schedule"]
    import inspect
    assert '"schedule"' in inspect.getsource(runner.Scheduler._loop)     # the loop is the only "schedule" caller
    for imp in ("gas_balance", "gas_petrinex", "gas_westcoast_oa", "ngtl_station_util", "tc_mainline"):
        src = (pathlib.Path(runner.__file__).parent / "importers" / f"{imp}.py").read_text(encoding="utf-8")
        assert 'trigger="import"' in src, imp
