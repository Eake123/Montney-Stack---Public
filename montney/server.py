"""HTTP API + static frontend. Binds to 127.0.0.1 by default - it's a local tool."""

from __future__ import annotations

import mimetypes
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from montney import (__version__, connectors, grids, httpcache, network, paths, readapi, readmcp, regionpage, regions,
                     scope, secrets, snapshot, top_producers, wells)
from montney.connectors.base import utcnow
from montney.runner import Scheduler, next_due, seed_catalog
from montney.store import Store, open_store


# MapLibre v6 is ESM-only; some Windows registries map .js to text/plain, which browsers refuse.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


def create_app(store: Store | None = None, schedule: bool = True) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # PORT steps 3 + 4b (docs/db/PORT.md, L-035). The API reads through open_store("reader").
        # - DuckDB (the default): one process, one store; the in-process scheduler fetches and writes, as in v1.
        # - Postgres: `montney run` (the runner) is the ONLY writer and holds the writer lock (#57/#85). `montney serve`
        #   is read-only: the montney_reader role, no scheduler, no writer store, no writer lock. Refreshes are
        #   queued in refresh_request for the runner.
        st = store if store is not None else open_store("reader")
        postgres = not isinstance(st, Store)
        state["store"], state["specs"] = st, connectors.load_specs()
        sched = None
        if not postgres:
            sched = Scheduler(st)
            state["specs"] = sched.specs
            if schedule:
                sched.start()
            else:
                seed_catalog(st, sched.specs)
        state["sched"] = sched
        # The read-only MCP (Krasnodar D29) runs in its own task and never raises here: if it can't start, /mcp/read
        # answers 503 and everything else serves as before (montney/readmcp.py).
        mcp_runner = readmcp.Runner(mcp_gate, S)
        await mcp_runner.start()
        yield
        await mcp_runner.stop()
        if sched is not None:
            await sched.stop()
        if store is None:
            st.close()

    app = FastAPI(title="Montney Stack", version=__version__, lifespan=lifespan)
    # L-081: every JSON response over 1 KB is gzipped (snapshot 238 -> 27 KB, network 786 -> 69 KB). Bodies the cache
    # already gzipped (and the pre-gzipped grid files) carry Content-Encoding, so the middleware passes them through.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=httpcache.GZIP_LEVEL)
    cache = app.state.body_cache = httpcache.BodyCache()

    def S() -> Store:
        return state["store"]

    def _cached(request: Request, key: tuple, build, headers=None) -> Response:
        """A polled endpoint's body, shared by every client per (key, data version, 60 s bucket, network) (L-081).
        `build()` returns (obj, meta); `headers(meta)` adds per-response headers (meta is {} on a 304 after eviction)."""
        st = S()
        if not hasattr(st, "data_version"):       # a store that can't say when it changed: build every time
            obj, meta = build()
            return JSONResponse(obj, headers=headers(meta) if headers else None)
        now = snapshot.utcnow()                    # the snapshot's clock (tests freeze it), so the bucket follows it
        bucket = int((now - datetime(1970, 1, 1)).total_seconds()) // httpcache.BUCKET_S
        _net()
        # The runner's band (running/stale/down) is in the key too, so its banner flips at once, not up to 60 s late.
        band = _runner()["state"] if hasattr(st, "runner_status") else None
        full = (key, st.data_version(any_run=key[0] == "status"), bucket, state["net"][2], _cfg()[0], band)
        tag = httpcache.etag_for(full)
        if httpcache.matches(request.headers.get("if-none-match"), tag):
            hit = cache.peek(full)
            return httpcache.not_modified(tag, headers(hit.meta if hit else {}) if headers else None)
        body = cache.get(full, build, tag)
        return httpcache.respond(request.headers.get("accept-encoding"), body, headers(body.meta) if headers else None)

    # ------------------------------------------------------------- data
    @app.get("/api/catalog")
    def catalog():
        return S().catalog()

    @app.get("/api/series")
    def series(
        ids: str = Query(..., description="comma-separated series ids"),
        start: str | None = Query(None, description="ISO date/time or relative like -7d, -48h"),
        end: str | None = None,
        as_of_lag: str | None = Query(None, description="return the vintage known this long ago, e.g. 24h"),
    ):
        id_list = [i for i in ids.split(",") if i][:50]
        as_of_max = utcnow() - _rel(as_of_lag) if as_of_lag else None
        data = S().series(id_list, _when(start), _when(end), as_of_max)
        meta = S().meta(id_list)
        for sid, d in data.items():
            d["meta"] = meta.get(sid, {})
        return data

    # ------------------------------------------------- read-only series (D29)
    # Other apps and agents: a curated allowlist of daily metrics (montney/readapi.py, docs/READ_API.md). Reads only;
    # the same answers as the MCP tools at /mcp/read.
    @app.get("/api/read/list_series")
    def read_list_series():
        return JSONResponse(readapi.list_series(S()), headers={"Cache-Control": "no-store"})

    @app.get("/api/read/series")
    def read_series(metric: str, start: str, end: str, freq: str = "day"):
        try:
            out = readapi.series(S(), metric, start, end, freq)
        except readapi.ReadError as e:
            raise HTTPException(404 if metric not in readapi.METRICS else 400, str(e)) from None
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    mcp_gate = readmcp.Gate()
    # An ASGI endpoint (a class instance): Starlette passes every method through; the gate answers 503 until the
    # lifespan has started the MCP.
    app.router.routes.append(Route(readmcp.PATH, endpoint=mcp_gate))

    # ---------------------------------------------------------- map
    def _net() -> dict:
        cached = state.get("net")
        files = tuple((str(f), f.stat().st_mtime) for f in network.files())
        if not cached or cached[0] != files:
            fc = network.load(strict=False)
            state["net"] = (files, fc, network.etag(fc))
        return state["net"][1]

    @app.get("/api/network")
    def get_network(request: Request):
        fc = _net()
        tag = '"' + state["net"][2] + '"'
        if httpcache.matches(request.headers.get("if-none-match"), tag):
            return httpcache.not_modified(tag)
        body = cache.get(("network", tag), lambda: (fc, {}), tag)     # serialised + gzipped once per network change
        return httpcache.respond(request.headers.get("accept-encoding"), body)

    def _cfg() -> tuple:
        """(mtime, web/map.json): the tiers and overrides the zoom scope reads (scope.py), reloaded when it changes."""
        f = paths.WEB_DIR / "map.json"
        mt = f.stat().st_mtime if f.exists() else None
        if state.get("cfg", (object(),))[0] != mt:
            import json
            state["cfg"] = (mt, json.loads(f.read_text(encoding="utf-8")) if mt is not None else {})
        return state["cfg"]

    @app.get("/api/snapshot")
    def get_snapshot(request: Request, extra: str | None = Query(None, description="comma-separated series ids"),
                     z: int | None = Query(None, ge=0, le=9, description="zoom tier (web/map.json tiers): only "
                                           "what can show at it; unboxed features as lite [v, stale, cap]"),
                     features: str | None = Query(None, description="comma-separated feature ids: their full "
                                                  "entries only (click detail)")):
        """No z / features: every feature in full (as before L-081). z=<tier>: the features that can show at that tier,
        boxed ones in full (`values`), drawn-only ones in `lite`. features=<ids>: those features in full, no gauges."""
        extra_ids = list(dict.fromkeys(i for i in (extra or "").split(",") if i))[:400]       # "<series>" or "<series>@1h|@24h" (#150)
        fids = list(dict.fromkeys(i for i in (features or "").split(",") if i))[:50]

        def build():
            fc = _net()
            refs = network.series_refs(fc)
            lite: set = set()
            extra_feats = []
            if fids:
                refs = {k: v for k, v in refs.items() if k in set(fids)}
                if set(fids) - set(refs):         # L-106: a facility from /api/wells (Gas, montney/wells.py)
                    extra_feats = [f for f in wells.collection(S(), fc)["features"] if f["id"] in set(fids)]
                    refs.update(network.series_refs({"features": extra_feats}))
            elif z is not None:
                hot, lite = scope.split(fc, _cfg()[1], refs, z)
                refs = {k: v for k, v in refs.items() if k in hot | lite}
            compares = {f["id"]: f["properties"]["compare"] for f in fc["features"]
                        if "compare" in f["properties"]}
            units = {f["id"]: f["properties"]["unit"] for f in fc["features"] + extra_feats
                     if "unit" in f["properties"]}
            aggs = {f["id"]: f["properties"]["agg"] for f in fc["features"] if "agg" in f["properties"]}
            want = [] if fids else extra_ids
            if z is not None and not fids:     # L-106: the nodes' shared `util_est` series, once each (scope.shared_series)
                want = list(dict.fromkeys(want + scope.shared_series(fc, _cfg()[1], z)))
            out = snapshot.build(S(), refs, compares, want, units, aggs, gauges=not fids)
            if z is not None and not fids:
                kinds = {f["id"]: f["properties"].get("value_kind") for f in fc["features"]}
                out["lite"] = {fid: scope.lite_entry(out["values"].pop(fid), kinds.get(fid)) for fid in sorted(lite)
                               if fid in out["values"]}
                out["scope"] = {"z": z, "tiers": scope.tiers(_cfg()[1])}
            if fids:
                return out, {}
            hot_refs = {k: v for k, v in refs.items() if k not in lite}       # the cadence follows what has a box
            ids = {sid for r in hot_refs.values() for sid in r.values()} | {t.split("@", 1)[0] for t in extra_ids}
            nxt = _next_update({m.get("source") for m in S().meta(sorted(ids)).values()} if ids else set())
            out["next_update"] = (datetime(1970, 1, 1) + timedelta(seconds=nxt)).isoformat() + "Z" if nxt else None
            if hasattr(S(), "runner_status"):     # the "runner not running" banner without polling /api/status
                out["runner"] = _runner()
            return out, {"next_update": nxt}

        key = ("snapshot-features", tuple(fids)) if fids else ("snapshot", z, tuple(extra_ids))
        return _cached(request, key, build, None if fids else _next_update_header)

    def _next_update(sources: set) -> float | None:
        """Epoch s when the next of these sources is due (last ok + interval). A source overdue by more than a minute
        is failing or backing off: it's left out, or one broken feed would have every client polling at the floor."""
        now = (utcnow() - datetime(1970, 1, 1)).total_seconds()
        best = None
        for sid, due in next_due(S(), {s: state["specs"][s] for s in sources if s in state["specs"]}).items():
            if not due:
                continue
            t = (datetime.fromisoformat(due) - datetime(1970, 1, 1)).total_seconds()
            if t >= now - 60 and (best is None or t < best):
                best = t
        return best

    def _next_update_header(meta: dict) -> dict:
        """X-Next-Update: seconds until the client should poll again (the next hot source due + 10 s for its run),
        clamped to 15-300 s; 60 s when unknown."""
        nxt = meta.get("next_update")
        if not nxt:
            return {"X-Next-Update": "60"}
        secs = nxt + 10 - (utcnow() - datetime(1970, 1, 1)).total_seconds()
        return {"X-Next-Update": str(int(min(max(secs, NEXT_UPDATE_MIN_S), NEXT_UPDATE_MAX_S)))}

    def _runner() -> dict:
        try:
            return runner_state(S().runner_status())
        except Exception as e:              # fail closed: an unreadable heartbeat reads as down, never breaks the call
            return {"state": "down", "reason": f"unreadable: {type(e).__name__}: {e}"}

    # ---------------------------------------------------------- regions (REGIONS.md 5b)
    @app.get("/api/regions")
    def get_regions():
        return regionpage.index(regions.cached())

    @app.get("/api/region/{rid}")
    def get_region(request: Request, rid: str):
        """One region's page: its balance lines and detail groups per tab (gas / power / oil), with the latest values.
        A row's history is /api/series?ids=<series>."""
        reg = regions.cached().get(rid)
        if reg is None:
            raise HTTPException(404, f"unknown region {rid!r}; /api/regions lists them")
        return _cached(request, ("region", rid), lambda: (regionpage.build(S(), reg), {}))

    @app.get("/api/events")
    def get_events(request: Request, kind: str | None = None, ref: str | None = None,
                   state: str | None = Query(None, description="ongoing|upcoming|current|past"),
                   since: str | None = None, limit: int = Query(200, le=1000)):
        if state and state not in Store.EVENT_STATES:
            raise HTTPException(400, f"state must be one of {Store.EVENT_STATES}")
        return _cached(request, ("events", kind, ref, state, since, limit),
                       lambda: (S().events(kinds=[k for k in (kind or "").split(",") if k] or None, ref=ref,
                                           state=state, since=_when(since), limit=limit), {}))

    @app.get("/api/wells")
    def get_wells(request: Request):
        body = wells.body(S(), _net())      # L-106: every AB + BC gas facility as map nodes (Gas, montney/wells.py)
        if httpcache.matches(request.headers.get("if-none-match"), body.etag):
            return httpcache.not_modified(body.etag)
        return httpcache.respond(request.headers.get("accept-encoding"), body)

    @app.get("/api/top-producers")
    def get_top_producers(province: str = Query(..., pattern="^(ab|bc)$")):
        return top_producers.build(S(), province)      # L-075: the logic is Gas's (montney/top_producers.py)

    @app.get("/api/grids")
    def list_grids():
        return grids.catalog()

    @app.get("/api/grid/{var}")
    def get_grid(var: str, prev: bool = False):
        f = grids.path_for(var, prev)
        if not f:
            raise HTTPException(404, f"no grid {var!r}{' (prev)' if prev else ''} yet")
        return FileResponse(f, media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Cache-Control": "no-cache"})

    # ---------------------------------------------------------- sources
    @app.get("/api/status")
    def status(request: Request):
        return _cached(request, ("status",), lambda: (_status(), {}))

    def _status() -> dict:
        all_specs = connectors.load_specs(include_disabled=True)
        lf = S().last_fetch()
        due = next_due(S(), state["specs"])
        out = []
        for sid, spec in all_specs.items():
            need = sorted(connectors.required_secrets(spec))
            out.append({
                "id": sid, "name": spec.get("name", sid), "type": spec.get("type", "http"),
                "mode": spec.get("mode", "cached"), "interval": spec.get("interval", "1h"),
                "enabled": spec.get("enabled", True),
                "secrets": {n: secrets.has(n) for n in need},
                "last": lf.get(sid), "next_due": due.get(sid),
            })
        res = {"version": __version__, "now": utcnow().isoformat(), "observations": S().count(),
               "tz": _tz_info(), "sources": out}
        if hasattr(S(), "budget_states"):   # Postgres (PORT §4b): per-source budget state for the status dots
            res["budget"] = S().budget_states()
            # Change-only storage: identical re-fetches aren't stored, so this counts stored rows (L-035 ALLOWED).
            res["observations_label"] = "stored observations"
        if hasattr(S(), "runner_status"):   # Postgres: is the runner (the only writer) alive? (#90)
            res["runner"] = _runner()
        return res

    @app.get("/api/system")
    def system():
        """The ☰ System tab (L-097): this machine's cost of running Montney. Local reads only, never cached
        (CPU % is since the previous call)."""
        from montney import sysinfo

        runner = S().runner_status() if hasattr(S(), "runner_status") else None
        out = {"now": utcnow().isoformat(), "machine": sysinfo.machine(),
               "processes": sysinfo.processes(runner.get("pid") if runner else None),
               "runner": _runner() if runner is not None else None}
        if hasattr(S(), "conn"):          # Postgres: storage and request budgets (DuckDB has neither)
            out["storage"] = sysinfo.storage(S().conn)
            out["requests"] = sysinfo.requests(S().conn)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post("/api/refresh/{source_id}")
    async def refresh(source_id: str):
        import asyncio
        if state["sched"] is None:   # Postgres (PORT §4b): queue it for the runner, which runs it within ~10 s
            if source_id not in state["specs"]:
                raise HTTPException(404, f"unknown source {source_id!r}")
            await asyncio.to_thread(S().request_refresh, source_id)
            return JSONResponse({"queued": True, "source": source_id}, status_code=202)
        return await asyncio.to_thread(state["sched"].run_now, source_id)

    # ----------------------------------------------------------- static
    # no-cache: the browser keeps its copy but revalidates it (a 304 via ETag), so it never runs stale JS after an update.
    # Without it, ES modules (and their imports) could be reused heuristically from Last-Modified (L-067: a gauge panel
    # missing #267's "% of normal").
    class _Revalidated(StaticFiles):
        async def get_response(self, path, scope):
            res = await super().get_response(path, scope)
            res.headers["Cache-Control"] = "no-cache"
            return res

    app.mount("/static", _Revalidated(directory=paths.WEB_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(paths.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


# Runner heartbeat → a UI state (#90). The beat can lag during a long `du` (it shares the runner's db lock), so the
# 60 s-5 min band is "stale" (busy, likely still alive), not "down".
RUNNER_STALE_S, RUNNER_DOWN_S = 60, 300
NEXT_UPDATE_MIN_S, NEXT_UPDATE_MAX_S = 15, 300     # the snapshot's X-Next-Update clamp (L-081)


def runner_state(status: dict | None) -> dict:
    if status is None:
        return {"state": "down", "reason": "no runner has ever run"}
    age = status.get("age_s")
    if age is not None and age <= RUNNER_STALE_S:
        state = "running"
    elif age is not None and age <= RUNNER_DOWN_S:
        state = "stale"
    else:
        state = "down"
    return {**status, "state": state}


def _tz_info() -> dict:
    """Alberta's current UTC offset from the bundled tzdata. Browsers may carry a stale database,
    so the frontend formats Alberta times with this offset instead of Intl's America/Edmonton."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    import tzdata

    now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Edmonton"))
    return {"name": "America/Edmonton", "abbr": now.tzname(),
            "offset_minutes": int(now.utcoffset().total_seconds() // 60),
            "tzdata": tzdata.IANA_VERSION}


_REL = re.compile(r"^[+-]?(\d+(?:\.\d+)?)([mhdw])$")


def _rel(s: str) -> timedelta:
    m = _REL.match(s.strip())
    if not m:
        raise HTTPException(400, f"bad relative time {s!r}")
    n, u = float(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n),
            "w": timedelta(weeks=n)}[u]


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    if _REL.match(s.strip()):
        sign = -1 if s.strip().startswith("-") else 1
        return utcnow() + sign * _rel(s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"bad time {s!r}")
