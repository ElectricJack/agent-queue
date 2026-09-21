"""The dashboard server's ASGI app (docs/specs/dashboard-server.md).

One dispatcher decides where a request goes, in this order:

1. ``/__aq/health`` -- the process's identity; the rest of ``/__aq/`` is
   reserved and answers ``404``.
2. ``/api``, ``/health``, ``/ready``, ``/ws`` (on a segment boundary) -- a path
   with a ``.`` or ``..`` segment is ``400``, then the edge gates
   (:mod:`src.dashboard_server.edge`), then the daemon proxy.
3. The daemon's other prefixes (``/mcp``, ``/docs``, ``/redoc``,
   ``/openapi.json``, ``/plans``, ``/dashboard``) -- ``404`` JSON, never
   ``index.html``, so a client pointed at the wrong port gets an error.
4. Everything else -- the verified bundle with SPA fallback.

Every response this process generates itself carries
``X-AQ-Dashboard-Server: <version>``; relayed daemon responses do not, which
is what separates the proxy's ``503`` from the daemon's own degraded one.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import unquote

from src.dashboard_server.bundle import (
    BundleStaticApp,
    DashboardBundle,
    installed_version,
    verify_dashboard_bundle,
)
from src.dashboard_server.edge import EdgeDenial, EdgeGate
from src.dashboard_server.proxy import DASHBOARD_SERVER_HEADER, DaemonProxy
from src.dashboard_server.settings import DashboardServerSettings

SERVICE_NAME = "aq-dashboard-server"
HEALTH_PATH = "/__aq/health"
RESERVED_PREFIX = "/__aq"
#: Forwarded to the daemon (spec §2.1).  ``/ws`` carries the WebSockets.
PROXIED_PREFIXES = ("/api", "/health", "/ready", "/ws")
#: The daemon's other surfaces; never proxied and never an SPA route.
NOT_SERVED_PREFIXES = ("/mcp", "/docs", "/redoc", "/openapi.json", "/plans", "/dashboard")


def has_prefix(path: str, prefix: str) -> bool:
    """``prefix`` matches ``path`` on a segment boundary (``/api``, never ``/apix``)."""
    return path == prefix or path.startswith(prefix + "/")


def classify(path: str) -> str:
    """``identity``, ``reserved``, ``proxy``, ``not_served`` or ``static``."""
    if path == HEALTH_PATH:
        return "identity"
    if has_prefix(path, RESERVED_PREFIX):
        return "reserved"
    if any(has_prefix(path, prefix) for prefix in PROXIED_PREFIXES):
        return "proxy"
    if any(has_prefix(path, prefix) for prefix in NOT_SERVED_PREFIXES):
        return "not_served"
    return "static"


def has_dot_segment(scope: dict[str, Any]) -> bool:
    """A ``.`` or ``..`` segment in the decoded path, however it was encoded."""
    raw = scope.get("raw_path")
    candidates = [scope.get("path", "")]
    if isinstance(raw, bytes | bytearray):
        candidates.append(unquote(bytes(raw).decode("latin-1")))
    for candidate in candidates:
        for segment in candidate.replace("\\", "/").split("/"):
            if segment in {".", ".."}:
                return True
    return False


class DashboardServerApp:
    """The whole process: identity, edge gates, proxy and static bundle."""

    def __init__(
        self,
        settings: DashboardServerSettings,
        *,
        bundle: DashboardBundle,
        proxy: DaemonProxy | None = None,
        version: str | None = None,
    ) -> None:
        self.settings = settings
        self.bundle = bundle
        self.version = version or installed_version()
        self.proxy = proxy or DaemonProxy(settings.api_url, version=self.version)
        self.edge = EdgeGate(settings)
        self._static = BundleStaticApp(bundle)
        self._own_header = (DASHBOARD_SERVER_HEADER.encode("latin-1"), self.version.encode())

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(receive, send)
        elif kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.proxy.start()
                except Exception as error:  # noqa: BLE001 - reported to the server
                    await send({"type": "lifespan.startup.failed", "message": str(error)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.proxy.close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    # -- HTTP ---------------------------------------------------------------

    async def _http(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        route = classify(scope["path"])
        if route == "proxy":
            if has_dot_segment(scope):
                await self._json(send, 400, {
                    "ok": False, "error": "bad_path",
                    "message": "A proxied path may not contain '.' or '..' segments.",
                })
                return
            denial = self.edge.check(scope)
            if denial is not None:
                await self._json(send, denial.status, denial.body())
                return
            await self.proxy.http(scope, receive, send)
        elif route == "identity":
            if scope["method"] not in {"GET", "HEAD"}:
                await self._json(send, 405, {"ok": False, "error": "method_not_allowed"},
                                 extra=[(b"allow", b"GET, HEAD")])
                return
            await self._json(send, 200, await self.identity(), head=scope["method"] == "HEAD")
        elif route in {"reserved", "not_served"}:
            await self._json(send, 404, {
                "ok": False, "error": "not_found",
                "message": "Not served by the dashboard server. The daemon's own surfaces "
                           f"are at {self.settings.api_url}.",
            })
        else:
            await self._static(scope, receive, self._stamped(send))

    async def identity(self) -> dict[str, Any]:
        """The ``/__aq/health`` body: who this is and what it serves (spec §1)."""
        return {
            "service": SERVICE_NAME,
            "version": self.version,
            "pid": os.getpid(),
            "bundle": {
                "version": self.bundle.version,
                "files": len(self.bundle.files),
                "verified": True,
                # Which build this process serves; `aq status` and `aq doctor`
                # compare it with the installed manifest to spot a stale server.
                "manifest_sha256": self.bundle.manifest_sha256,
            },
            "api_url": self.settings.api_url,
            "upstream_ok": await self.proxy.upstream_ok(),
        }

    def _stamped(self, send: Any) -> Any:
        header = self._own_header

        async def stamped(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", ()), header]}
            await send(message)

        return stamped

    async def _json(
        self,
        send: Any,
        status: int,
        payload: dict[str, Any],
        *,
        head: bool = False,
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                self._own_header,
                *(extra or ()),
            ],
        })
        await send({"type": "http.response.body", "body": b"" if head else body})

    # -- WebSocket ----------------------------------------------------------

    async def _websocket(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope["path"]
        if not has_prefix(path, "/ws"):
            await self._deny(scope, send, EdgeDenial(
                404, "not_found", "WebSockets are proxied only under /ws.",
            ))
            return
        if has_dot_segment(scope):
            await self._deny(scope, send, EdgeDenial(
                400, "bad_path", "A proxied path may not contain '.' or '..' segments.",
            ))
            return
        denial = self.edge.check(scope)
        if denial is not None:
            await self._deny(scope, send, denial)
            return
        await self.proxy.websocket(scope, receive, send)

    async def _deny(self, scope: dict[str, Any], send: Any, denial: EdgeDenial) -> None:
        """Refuse a handshake before accepting it -- never accept-then-close."""
        if "websocket.http.response" in scope.get("extensions", {}):
            body = json.dumps(denial.body()).encode()
            await send({
                "type": "websocket.http.response.start",
                "status": denial.status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    self._own_header,
                ],
            })
            await send({"type": "websocket.http.response.body", "body": body})
            return
        await send({"type": "websocket.close", "code": 1008})


def create_app(
    settings: DashboardServerSettings,
    *,
    proxy: DaemonProxy | None = None,
    version: str | None = None,
) -> DashboardServerApp:
    """Verify the bundle (failing closed) and build the app around it.

    Raises :class:`ValueError` when the bundle is missing or does not verify;
    ``__main__`` turns that into the exit status the spec names.
    """
    bundle = verify_dashboard_bundle(settings.bundle_directory)
    return DashboardServerApp(settings, bundle=bundle, proxy=proxy, version=version)
