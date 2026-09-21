"""Shared harness for the dashboard server tests (docs/specs/dashboard-server.md §7).

* :func:`serve_asgi` runs any ASGI app under a real ``uvicorn.Server`` on an
  ephemeral loopback port, in the test's own event loop.
* :class:`FakeDaemon` is a small Starlette app standing in for the daemon's API:
  it records what reached it (headers, raw path, query, bodies, WebSocket
  handshakes and closes) and exposes the ``asyncio.Event`` hooks the proxy
  tests use to assert ordering without wall-clock sleeps.

Nothing here imports the daemon, so the harness is as cheap as the process it
stands in for.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

#: The subprotocol the daemon's terminal selects when a browser offers it.
TERMINAL_PROTOCOL = "aq-terminal-v1"


class _TestServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to pytest and stops on an event.

    ``main_loop`` waits on an event instead of ticking every 100 ms, so a test's
    teardown is not rounded up to the tick.  (The tick also refreshes the cached
    ``Date`` header, which the harness turns off by default.)
    """

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.stop_requested = asyncio.Event()

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def main_loop(self) -> None:
        await self.stop_requested.wait()


def _bind(host: str) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    return sock


def base_url(host: str, port: int, scheme: str = "http") -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}:{port}"


@asynccontextmanager
async def serve_asgi(app: Any, *, host: str = "127.0.0.1", **config: Any) -> AsyncIterator[str]:
    """Run ``app`` under a real ``uvicorn.Server`` on an ephemeral port in this loop.

    Yields ``"http://127.0.0.1:<port>"``.  The lifespan runs (``lifespan="on"``),
    so an app with startup/shutdown hooks gets them.  Defaults match how the
    dashboard server runs -- no access log, and no ``Server``/``Date`` headers of
    uvicorn's own, so relayed responses stay verbatim -- and any keyword is
    passed to :class:`uvicorn.Config` to override them.
    """
    options: dict[str, Any] = {
        "lifespan": "on",
        "log_level": "warning",
        "access_log": False,
        "server_header": False,
        "date_header": False,
        "proxy_headers": False,
        "ws": "websockets-sansio",
        "timeout_graceful_shutdown": 2,
    }
    options.update(config)
    sock = _bind(host)
    port = sock.getsockname()[1]
    server = _TestServer(uvicorn.Config(app, **options))
    serving = asyncio.create_task(server.serve(sockets=[sock]), name=f"uvicorn:{port}")
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    serving.result()  # re-raise a startup failure
                    raise RuntimeError("uvicorn exited before it started serving")
                await asyncio.sleep(0.005)
        yield base_url(host, port)
    finally:
        server.stop_requested.set()
        try:
            async with asyncio.timeout(10):
                await asyncio.shield(serving)
        except BaseException:
            serving.cancel()
            with contextlib.suppress(BaseException):
                await serving
            raise
        finally:
            # A connection a test left open outlives the graceful timeout;
            # drop it so nothing leaks into the next test's loop.
            for connection in list(server.server_state.connections):
                transport = getattr(connection, "transport", None)
                if transport is not None:
                    transport.abort()
            sock.close()


def unused_port(host: str = "127.0.0.1") -> int:
    """A port that was free a moment ago and has nothing listening on it now."""
    sock = _bind(host)
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# The fake daemon
# ---------------------------------------------------------------------------


def _header_pairs(raw: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    return [(name.decode("latin-1").lower(), value.decode("latin-1")) for name, value in raw]


@dataclass
class RecordedRequest:
    """One HTTP request as the fake daemon received it."""

    method: str
    raw_path: bytes
    query_string: bytes
    #: ``(lower-cased name, value)`` in arrival order, repeats kept.
    headers: list[tuple[str, str]]

    def header(self, name: str) -> str | None:
        values = self.headers_named(name)
        return values[0] if values else None

    def headers_named(self, name: str) -> list[str]:
        name = name.lower()
        return [value for key, value in self.headers if key == name]


@dataclass
class RecordedWebSocket:
    """One WebSocket handshake (and, once it ends, its close) at the fake daemon."""

    raw_path: bytes
    query_string: bytes
    headers: list[tuple[str, str]]
    #: ``Sec-WebSocket-Protocol`` as offered, in order.
    subprotocols: list[str]
    #: What the fake daemon selected (``None`` also when it refused).
    accepted_subprotocol: str | None = None
    refused: bool = False
    #: Messages received, ``str`` for text frames and ``bytes`` for binary.
    received: list[str | bytes] = field(default_factory=list)
    #: The close code and reason the peer sent (``websocket.disconnect``).
    close_code: int | None = None
    close_reason: str | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def header(self, name: str) -> str | None:
        name = name.lower()
        return next((value for key, value in self.headers if key == name), None)


class FakeDaemon:
    """A stand-in for the daemon's API: records requests, streams on cue.

    HTTP routes (all under ``/api`` unless noted):

    * ``/api/echo`` and ``/api/echo/{rest:path}`` (any method) -- records the
      request and answers ``200`` JSON describing it.
    * ``/api/fixed`` -- a constant JSON body with ``x-fake-daemon`` and
      ``cache-control`` headers, for comparing a relayed response with a direct one.
    * ``/api/cookies`` -- ``200`` with two ``Set-Cookie`` headers.
    * ``/api/status/{code}`` -- JSON ``{"ok": false, "error": "status_<code>"}``
      with that status (``500`` and ``404`` in the tests).
    * ``/api/sse`` -- ``text/event-stream`` with ``Cache-Control: no-cache`` and
      ``X-Accel-Buffering: no``.  Sends ``data: first``, sets :attr:`sse_first_sent`,
      waits for :attr:`sse_release`, sends ``data: second`` and ends.  If the
      stream is torn down early it sets :attr:`sse_cancelled`; :attr:`sse_closed`
      is set however it ends.
    * ``/api/upload`` (POST/PUT) -- streams the body, setting
      :attr:`upload_started` at the first byte, and answers ``{"bytes",
      "sha256", "content_length", "transfer_encoding"}`` (also appended to
      :attr:`uploads`).  A body cut off by a disconnect sets
      :attr:`upload_aborted` instead.
    * ``/api/slow`` -- sets :attr:`slow_started`, then sends no headers until
      :attr:`slow_release` is set; sets :attr:`slow_abandoned` if the client
      disconnects first.
    * ``/health`` and ``/ready`` -- ``{"status": "ok"}`` with
      :attr:`health_status` (default ``200``).

    WebSocket routes:

    * ``/ws/echo`` and ``/ws/terminal/{session}`` -- selects ``aq-terminal-v1``
      when offered (as the daemon's terminal does), accepts, and echoes each
      message with its type.  A text message ``close:<code>:<reason>`` makes it
      close with that code and reason instead.
    * ``/ws/refuse`` -- closes before accepting (``?code=`` defaults to ``4403``),
      which uvicorn answers with HTTP ``403``, like a refused terminal.
    * ``/ws/flood?count=&size=`` -- accepts and sends ``count`` binary frames of
      ``size`` bytes, each starting with its 4-byte big-endian index; counts sends
      that completed in :attr:`flood_sent`.

    Recorded state: :attr:`requests` (:class:`RecordedRequest`),
    :attr:`websockets` (:class:`RecordedWebSocket`), :attr:`uploads` (the upload
    route's answers).  :attr:`app` is the ASGI app to serve.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.websockets: list[RecordedWebSocket] = []
        self.uploads: list[dict[str, Any]] = []
        self.health_status = 200
        self.sse_first_sent = asyncio.Event()
        self.sse_release = asyncio.Event()
        self.sse_cancelled = asyncio.Event()
        self.sse_closed = asyncio.Event()
        self.upload_started = asyncio.Event()
        self.upload_aborted = asyncio.Event()
        self.slow_started = asyncio.Event()
        self.slow_release = asyncio.Event()
        self.slow_abandoned = asyncio.Event()
        self.flood_sent = 0
        self.flood_done = asyncio.Event()
        methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
        self.app = Starlette(
            routes=[
                Route("/health", self._health),
                Route("/ready", self._health),
                Route("/api/echo", self._echo, methods=methods),
                Route("/api/echo/{rest:path}", self._echo, methods=methods),
                Route("/api/fixed", self._fixed),
                Route("/api/cookies", self._cookies),
                Route("/api/status/{code:int}", self._status),
                Route("/api/sse", self._sse),
                Route("/api/upload", self._upload, methods=["POST", "PUT"]),
                Route("/api/slow", self._slow),
                WebSocketRoute("/ws/echo", self._ws_echo),
                WebSocketRoute("/ws/terminal/{session}", self._ws_echo),
                WebSocketRoute("/ws/refuse", self._ws_refuse),
                WebSocketRoute("/ws/flood", self._ws_flood),
            ]
        )

    # -- recording -------------------------------------------------------------

    def _record(self, request: Request) -> RecordedRequest:
        recorded = RecordedRequest(
            method=request.method,
            raw_path=request.scope.get("raw_path", b""),
            query_string=request.scope.get("query_string", b""),
            headers=_header_pairs(request.headers.raw),
        )
        self.requests.append(recorded)
        return recorded

    def _record_ws(self, websocket: WebSocket) -> RecordedWebSocket:
        recorded = RecordedWebSocket(
            raw_path=websocket.scope.get("raw_path", b""),
            query_string=websocket.scope.get("query_string", b""),
            headers=_header_pairs(websocket.headers.raw),
            subprotocols=list(websocket.scope.get("subprotocols", [])),
        )
        self.websockets.append(recorded)
        return recorded

    @property
    def last_request(self) -> RecordedRequest:
        return self.requests[-1]

    # -- HTTP ------------------------------------------------------------------

    async def _health(self, request: Request) -> Response:
        self._record(request)
        return JSONResponse({"status": "ok"}, status_code=self.health_status)

    async def _echo(self, request: Request) -> Response:
        recorded = self._record(request)
        body = await request.body()
        return JSONResponse(
            {
                "method": recorded.method,
                "raw_path": recorded.raw_path.decode("latin-1"),
                "query_string": recorded.query_string.decode("latin-1"),
                "headers": recorded.headers,
                "body_bytes": len(body),
            }
        )

    async def _fixed(self, request: Request) -> Response:
        self._record(request)
        return JSONResponse(
            {"ok": True, "items": [1, 2, 3]},
            headers={"x-fake-daemon": "1", "cache-control": "private, max-age=5"},
        )

    async def _cookies(self, request: Request) -> Response:
        self._record(request)
        response = JSONResponse({"ok": True})
        response.set_cookie("first", "1", path="/")
        response.set_cookie("second", "2", path="/", httponly=True)
        return response

    async def _status(self, request: Request) -> Response:
        self._record(request)
        code = request.path_params["code"]
        return JSONResponse({"ok": False, "error": f"status_{code}"}, status_code=code)

    async def _sse(self, request: Request) -> Response:
        self._record(request)

        async def events() -> AsyncIterator[bytes]:
            try:
                yield b"data: first\n\n"
                self.sse_first_sent.set()
                await self.sse_release.wait()
                yield b"data: second\n\n"
            except BaseException:
                self.sse_cancelled.set()
                raise
            finally:
                self.sse_closed.set()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def _upload(self, request: Request) -> Response:
        self._record(request)
        digest = hashlib.sha256()
        total = 0
        try:
            async for chunk in request.stream():
                if chunk:
                    self.upload_started.set()
                digest.update(chunk)
                total += len(chunk)
        except ClientDisconnect:
            self.upload_aborted.set()
            return Response(status_code=499)
        answer = {
            "bytes": total,
            "sha256": digest.hexdigest(),
            "content_length": request.headers.get("content-length"),
            "transfer_encoding": request.headers.get("transfer-encoding"),
        }
        self.uploads.append(answer)
        return JSONResponse(answer)

    async def _slow(self, request: Request) -> Response:
        self._record(request)
        self.slow_started.set()

        async def disconnected() -> None:
            while (await request.receive())["type"] != "http.disconnect":
                pass

        released = asyncio.ensure_future(self.slow_release.wait())
        gone = asyncio.ensure_future(disconnected())
        done, pending = await asyncio.wait({released, gone}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if gone in done:
            self.slow_abandoned.set()
        return JSONResponse({"ok": True})

    # -- WebSocket ---------------------------------------------------------------

    async def _ws_echo(self, websocket: WebSocket) -> None:
        recorded = self._record_ws(websocket)
        offered = websocket.scope.get("subprotocols", [])
        recorded.accepted_subprotocol = TERMINAL_PROTOCOL if TERMINAL_PROTOCOL in offered else None
        await websocket.accept(subprotocol=recorded.accepted_subprotocol)
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    recorded.close_code = message.get("code")
                    recorded.close_reason = message.get("reason") or ""
                    return
                text = message.get("text")
                if text is not None:
                    recorded.received.append(text)
                    if text.startswith("close:"):
                        _, code, reason = text.split(":", 2)
                        await websocket.close(code=int(code), reason=reason)
                        return
                    await websocket.send_text(text)
                else:
                    data = message.get("bytes") or b""
                    recorded.received.append(data)
                    await websocket.send_bytes(data)
        finally:
            recorded.closed.set()

    async def _ws_refuse(self, websocket: WebSocket) -> None:
        recorded = self._record_ws(websocket)
        recorded.refused = True
        await websocket.close(code=int(websocket.query_params.get("code", "4403")))
        recorded.closed.set()

    async def _ws_flood(self, websocket: WebSocket) -> None:
        recorded = self._record_ws(websocket)
        count = int(websocket.query_params.get("count", "1024"))
        size = int(websocket.query_params.get("size", str(64 * 1024)))
        filler = b"\xa5" * (size - 4)
        await websocket.accept()
        try:
            for index in range(count):
                await websocket.send_bytes(index.to_bytes(4, "big") + filler)
                self.flood_sent = index + 1
            self.flood_done.set()
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    recorded.close_code = message.get("code")
                    return
        except (WebSocketDisconnect, OSError, RuntimeError):
            return
        finally:
            recorded.closed.set()
