"""Shared, versioned response bodies for the map's polled endpoints (L-081, docs/ARCHITECTURE-REQUESTS.md §2).

A polled body (snapshot, status, current events) is built ONCE per key, where the key is

    (endpoint, its query, the store's data_version(), the 60 s clock bucket, the network files' ETag)

and every client polling with that key shares the build. It's kept gzipped (level 6) in a small LRU. A request whose
If-None-Match carries the key's ETag gets a 304 without anything being built. The ETag is derived from the KEY, not
the bytes, so the server can answer a revalidation with one cheap version query. It's weak (W/), because the same
key is served gzipped or not.

The 60 s bucket bounds how late every time-derived field can be: stale flags, `next` becoming current at a cutoff,
the @1h/@24h window means, the gas or calendar day. It also bounds writes another process makes outside a fetch_run.
Concurrent requests for a key being built wait on that one build (single-flight), so N clients cost one build.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

BUCKET_S = 60
GZIP_LEVEL = 6
MAX_ENTRIES = 32


def encode(obj) -> bytes:
    """The bytes FastAPI's default JSONResponse would send for `obj` (same encoder, same separators)."""
    return json.dumps(jsonable_encoder(obj), ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


def etag_for(key) -> str:
    return 'W/"' + hashlib.sha1(repr(key).encode()).hexdigest()[:20] + '"'


@dataclass(slots=True)
class Body:
    gz: bytes
    etag: str
    meta: dict = field(default_factory=dict)     # e.g. the snapshot's next_update epoch, for the 304 headers

    @property
    def raw(self) -> bytes:
        return gzip.decompress(self.gz)


class BodyCache:
    """An LRU of gzipped JSON bodies with single-flight builds. Thread-safe (FastAPI runs sync routes in a pool)."""

    def __init__(self, maxsize: int = MAX_ENTRIES):
        self.maxsize = maxsize
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._building: dict = {}
        self.builds = 0                          # for tests and measurement

    def peek(self, key) -> Body | None:
        with self._lock:
            return self._d.get(key)

    def get(self, key, build: Callable[[], tuple[object, dict]], etag: str | None = None) -> Body:
        """The cached body for `key`, or build it: `build()` returns (obj, meta). One build per key at a time."""
        with self._lock:
            hit = self._d.get(key)
            if hit is not None:
                self._d.move_to_end(key)
                return hit
            kl = self._building.setdefault(key, threading.Lock())
        with kl:
            with self._lock:
                hit = self._d.get(key)
            if hit is not None:
                return hit
            try:
                obj, meta = build()
                body = Body(gzip.compress(encode(obj), GZIP_LEVEL), etag or etag_for(key), meta)
                with self._lock:
                    self.builds += 1
                    self._d[key] = body
                    self._d.move_to_end(key)
                    while len(self._d) > self.maxsize:
                        self._d.popitem(last=False)
            finally:
                with self._lock:
                    self._building.pop(key, None)
        return body


def matches(if_none_match: str | None, tag: str) -> bool:
    """RFC 9110 weak comparison of an If-None-Match header with our ETag."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    want = tag.removeprefix("W/")
    return any(t.strip().removeprefix("W/") == want for t in if_none_match.split(","))


def respond(accept_encoding: str | None, body: Body, headers: dict | None = None) -> Response:
    h = {"ETag": body.etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding", **(headers or {})}
    if "gzip" in (accept_encoding or ""):
        return Response(body.gz, media_type="application/json", headers={**h, "Content-Encoding": "gzip"})
    return Response(body.raw, media_type="application/json", headers=h)


def not_modified(tag: str, headers: dict | None = None) -> Response:
    return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding",
                                              **(headers or {})})
