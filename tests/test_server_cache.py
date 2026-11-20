"""L-081 PR 1: the polled endpoints are built once per (data version, 60 s bucket), served gzipped, and revalidate.

What must hold, each with a seeded red:
- a revalidation with the current ETag is a 304 and builds nothing;
- a write (any write the store makes) moves the ETag and the next GET has the new value, never a stale cached body;
- the clock bucket moves it too (stale flags, window means and the gas day can't freeze for more than 60 s);
- N concurrent requests for one key cost one build;
- big bodies go out gzipped, and the snapshot says when to poll next.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from montney import httpcache, snapshot
from montney.store import Obs, SeriesMeta, Store

T0 = datetime(2026, 11, 2, 18, 0, 30)
SID = "ngtl.csr.empress"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTNEY_HOME", str(tmp_path))
    monkeypatch.setattr(snapshot, "utcnow", lambda: T0)
    from montney.server import create_app
    st = Store(tmp_path / "t.duckdb")
    st.upsert_meta([SeriesMeta(SID, "ngtl_csr", "Empress", "MMcf/d", "5min")])
    st.write([Obs(SID, T0 - timedelta(minutes=5), 4576.0, T0)])
    a = create_app(store=st, schedule=False)
    with TestClient(a) as c:
        yield c, st, a


def _snap(c, **h):
    return c.get("/api/snapshot", params={"extra": SID}, headers=h)


def test_revalidation_is_a_304_that_builds_nothing(app):
    c, st, a = app
    r = _snap(c)
    assert r.status_code == 200 and r.headers["etag"].startswith('W/"')
    assert r.json()["series"][SID]["v"] == 4576.0
    builds = _cache(c).builds
    r2 = _snap(c, **{"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304 and r2.headers["etag"] == r.headers["etag"]
    assert _cache(c).builds == builds                        # a 304 never builds
    r3 = _snap(c)                                            # a plain GET in the same version + bucket: the cache
    assert r3.status_code == 200 and r3.headers["etag"] == r.headers["etag"] and _cache(c).builds == builds


def test_a_write_moves_the_etag_and_the_value(app):
    c, st, _ = app
    r = _snap(c)
    st.write([Obs(SID, T0, 4600.0, T0)])
    r2 = _snap(c, **{"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 200, "a write must invalidate the cached snapshot"       # seeded red: no _version bump
    assert r2.headers["etag"] != r.headers["etag"]
    assert r2.json()["series"][SID]["v"] == 4600.0


def test_the_clock_bucket_moves_the_etag(app, monkeypatch):
    c, st, _ = app
    r = _snap(c)
    monkeypatch.setattr(snapshot, "utcnow", lambda: T0 + timedelta(seconds=httpcache.BUCKET_S))
    r2 = _snap(c, **{"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 200 and r2.headers["etag"] != r.headers["etag"]
    assert r2.json()["as_of"].startswith((T0 + timedelta(seconds=httpcache.BUCKET_S)).isoformat())


def test_status_and_events_revalidate_too(app):
    c, st, _ = app
    for path in ("/api/status", "/api/events?state=current"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert c.get(path, headers={"If-None-Match": r.headers["etag"]}).status_code == 304, path
    r = c.get("/api/status")
    st.log_fetch("ngtl_csr", T0, False, 0, "boom")           # a failed run moves status (its `last` changed)
    assert c.get("/api/status", headers={"If-None-Match": r.headers["etag"]}).status_code == 200


def test_network_keeps_its_etag_and_is_served_gzipped(app):
    c, _, _ = app
    r = c.get("/api/network", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.json()["type"] == "FeatureCollection"
    assert c.get("/api/network", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_gzip_and_identity_bodies_are_the_same_json(app):
    c, _, _ = app
    gz = _snap(c, **{"Accept-Encoding": "gzip"})
    plain = _snap(c, **{"Accept-Encoding": "identity"})
    assert gz.headers["content-encoding"] == "gzip" and "content-encoding" not in plain.headers
    assert gz.json() == plain.json()


def test_next_update_header_and_body(app):
    c, _, _ = app
    r = _snap(c)
    secs = int(r.headers["x-next-update"])
    assert 15 <= secs <= 300
    assert "next_update" in r.json()


def test_concurrent_requests_for_one_key_cost_one_build():
    cache = httpcache.BodyCache()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return {"x": 1}, {}

    ts = [threading.Thread(target=cache.get, args=(("k",), slow)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and cache.builds == 1


def test_a_failed_build_is_not_cached():
    cache = httpcache.BodyCache()

    def bad():
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        cache.get(("k",), bad)
    assert cache.get(("k",), lambda: ({"ok": True}, {})).raw == b'{"ok":true}'


def test_if_none_match_weak_comparison():
    tag = 'W/"abc"'
    assert httpcache.matches('W/"abc"', tag) and httpcache.matches('"abc"', tag)
    assert httpcache.matches('"x", W/"abc"', tag) and httpcache.matches("*", tag)
    assert not httpcache.matches('W/"abd"', tag) and not httpcache.matches(None, tag)


def _cache(c) -> httpcache.BodyCache:
    return c.app.state.body_cache


def test_the_version_moves_after_the_write_not_before():
    """#312 review: bumped before the write, a snapshot built during it was cached as new with old data."""
    from montney.store import _bumps

    class S:
        _version = 0

        @_bumps
        def write(self):
            seen.append(self._version)        # what a concurrent snapshot build would key on, mid-write

    seen = []
    s = S()
    s.write()
    assert seen == [0] and s._version == 1
