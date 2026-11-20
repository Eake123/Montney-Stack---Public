"""The read-only MCP at /mcp/read (Krasnodar D29): three tools over montney/readapi.py, nothing else.

Isolation from the map (Supervisor's condition): the route `/mcp/read` is a small gate registered at app creation.
The MCP server itself (the `mcp` SDK, pinned in pyproject/uv.lock) is imported and started only inside the app's
lifespan, inside try/except. If the import, the build or the session manager fails, the map, the API and
/api/status serve exactly as before, and /mcp/read answers 503. Stateless Streamable HTTP with JSON responses:
no sessions, no background tasks beyond the SDK's request handling.

Tailnet only: the LAN view (Krasnodar's LAN proxy) refuses every non-GET, and MCP is POST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

log = logging.getLogger("montney.readmcp")
PATH = "/mcp/read"


def build(get_store: Callable):
    """The MCP server (MCPServer) with the three read-only tools, bound to the app's store."""
    import anyio
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    from montney import readapi

    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    server = MCPServer(name="montney-read", instructions=readapi.HOW_TO_USE)

    async def list_series() -> list[dict]:
        """Montney's headline daily metrics: description (incl. how each day is aggregated), unit, first_date,
        last_date, freqs."""
        return await anyio.to_thread.run_sync(readapi.list_series, get_store())

    async def series(metric: str, start: str, end: str, freq: str = "day") -> dict:
        """One metric as sorted [["YYYY-MM-DD", number], ...] for America/Edmonton days, start..end inclusive
        (ISO dates, at most 5 years). Days without a value are left out."""
        return await anyio.to_thread.run_sync(readapi.series, get_store(), metric, start, end, freq)

    async def how_to_use() -> str:
        """How to read Montney's series: units, day boundaries, revisions, what isn't here."""
        return readapi.HOW_TO_USE

    for fn in (list_series, series, how_to_use):
        server.add_tool(fn, annotations=ro)
    return server


class Gate:
    """The ASGI endpoint behind `/mcp/read`: forwards to the MCP handler once the lifespan has started it, else 503.
    A class instance, so Starlette routes every method to it unchanged."""

    def __init__(self) -> None:
        self.handler = None

    async def __call__(self, scope, receive, send) -> None:
        if self.handler is None:
            body = json.dumps({"error": "read MCP unavailable; the HTTP API /api/read/* still serves"}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.handler(scope, receive, send)


class Runner:
    """Starts and stops the MCP from the app's lifespan. The SDK's session manager (its task group) runs in its OWN
    asyncio task, not in the lifespan task, so nothing it does can cancel or fail the map's lifespan: if it ever
    dies, the gate goes back to 503 and the map serves on."""

    def __init__(self, gate: Gate, get_store: Callable) -> None:
        self.gate, self.get_store = gate, get_store
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self, timeout: float = 10.0) -> bool:
        """Never raises. True when /mcp/read is serving."""
        ready = asyncio.Event()
        try:
            from mcp.server.transport_security import TransportSecuritySettings

            server = build(self.get_store)
            # DNS-rebinding protection off: the endpoint is read-only and serves what GET /api/series already
            # does, which has no such check; the tailnet (Tailscale Serve) is the boundary. The SDK would otherwise
            # accept only Host: localhost.
            sub = server.streamable_http_app(
                streamable_http_path=PATH, stateless_http=True, json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
            handler = next(r.endpoint for r in sub.routes if getattr(r, "path", None) == PATH)
        except Exception:        # noqa: BLE001 - the map must keep serving whatever the MCP SDK does
            log.exception("read MCP failed to build; %s answers 503, the map and /api/read/* are unaffected", PATH)
            return False

        async def run() -> None:
            try:
                async with server.session_manager.run():
                    self.gate.handler = handler
                    ready.set()
                    log.info("read MCP up at %s", PATH)
                    await self._stop.wait()
            except Exception:    # noqa: BLE001
                log.exception("read MCP stopped; %s answers 503, the map is unaffected", PATH)
            finally:
                self.gate.handler = None
                ready.set()

        self._task = asyncio.create_task(run(), name="montney-read-mcp")
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except Exception:        # noqa: BLE001
            log.exception("read MCP did not start in %.0f s; %s answers 503", timeout, PATH)
        return self.gate.handler is not None

    async def stop(self, timeout: float = 5.0) -> None:
        """Never raises."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout)
            except Exception:    # noqa: BLE001
                self._task.cancel()
        self.gate.handler = None
