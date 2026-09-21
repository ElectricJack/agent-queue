"""The dashboard server's reverse proxy to the daemon (docs/specs/dashboard-server.md §2).

:class:`DaemonProxy` relays the requests :mod:`src.dashboard_server.app` has
already classified and gated -- ``/api``, ``/health``, ``/ready`` and ``/ws``
-- to the daemon's API base, which is what the Vite dev server's proxy does in
a source checkout.  It trusts the scope it is handed and forwards it.

What it guarantees:

* **HTTP (§2.2).** Method, raw path and query string go byte-for-byte to the
  daemon.  Request headers pass end to end -- ``Host`` is *not* rewritten,
  because the daemon's terminal origin rule compares ``Origin`` with ``Host``
  -- minus hop-by-hop headers and minus every inbound forwarding header, and
  nothing is added.  Bodies stream both ways in chunks of at most 64 KiB and
  are never re-encoded; status and response headers come back verbatim.
* **Back-pressure (§2.3).** There is no queue.  Every relay awaits the send on
  one side before it reads the next chunk or frame from the other, so a slow
  browser stops the proxy reading and TCP flow control reaches the daemon.
* **WebSockets (§2.4).** The upstream handshake happens *first*, offering the
  browser's subprotocols verbatim, and the browser is accepted with exactly the
  subprotocol the daemon chose -- or refused the way the daemon refused.  Frames
  are relayed one to one, and close codes and reasons cross in both directions.
* **Daemon down (§2.5).** ``503 daemon_unreachable``, ``504 daemon_timeout``
  and ``502 daemon_bad_response`` are the proxy's own answers, and every answer
  the proxy generates carries ``X-AQ-Dashboard-Server``; a relayed daemon
  response never does.

``Authorization``, ``Cookie`` and ``Sec-WebSocket-Protocol`` (which can carry
``aq-bearer.<token>``) are never logged, and there is no access log.

Import boundary (§1): the standard library, aiohttp (with its own ``yarl``) and
nothing from the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

__all__ = [
    "CHUNK_SIZE",
    "DASHBOARD_SERVER_HEADER",
    "DaemonProxy",
    "downstream_response_headers",
    "upstream_request_headers",
    "wire_close_code",
]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

#: Marks every response the dashboard server generates itself (§2.5).
DASHBOARD_SERVER_HEADER = "x-aq-dashboard-server"
#: The largest piece of a body the proxy holds at once, in either direction.
CHUNK_SIZE = 64 * 1024

#: The daemon's uvicorn closes an idle keep-alive connection after 5 s; the
#: pool gives up on one sooner so it never sends a request down a socket the
#: daemon is in the middle of closing.
_KEEPALIVE_SECONDS = 4.0
#: Bounds each half of a WebSocket's closing handshake, so neither a browser
#: that stopped reading nor a wedged daemon can hold a relay open.
_CLOSE_SECONDS = 1.0
#: After one pump finds its peer gone, how long the other gets to report the
#: close code that explains it.
_PEER_CLOSE_GRACE_SECONDS = 2.0
#: How long :meth:`DaemonProxy.close` waits for relays to wind down.
_SHUTDOWN_GRACE_SECONDS = 3.0
_PROBE_SECONDS = 1.0

_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)
#: The daemon trusts no forwarding header and must not start trusting one a
#: client can forge, so inbound ones are dropped and none are added (§2.2).
_FORWARDING = frozenset({b"forwarded", b"x-real-ip"})
_FORWARDING_PREFIX = b"x-forwarded-"
#: ``100-continue`` is answered on the browser's hop (by uvicorn, when the body
#: is first read); the daemon gets an ordinary request.
_REQUEST_ONLY = frozenset({b"expect"})
#: The upstream handshake is aiohttp's own: key, version and protocol list are
#: set by it, and compression is negotiated per hop.
_WEBSOCKET_PREFIX = b"sec-websocket-"
#: A ``Connection`` token can make a header hop-by-hop, but never these: the
#: daemon's origin rule reads ``Host``, and framing must not change under it.
_NEVER_CONNECTION_LISTED = frozenset({b"host", b"content-length"})
#: aiohttp adds these unless told not to; the daemon must see only the browser's.
_SKIP_AUTO_HEADERS = ("Accept", "Accept-Encoding", "Content-Type", "User-Agent")

logger = logging.getLogger("aq.dashboard_server.proxy")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _connection_tokens(headers: Iterable[tuple[bytes, bytes]]) -> frozenset[bytes]:
    tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            tokens.update(token.strip().lower() for token in value.split(b","))
    tokens.discard(b"")
    return frozenset(tokens - _NEVER_CONNECTION_LISTED)


def _header_text(value: bytes) -> str:
    # aiohttp writes header values as UTF-8, so a UTF-8 value round-trips
    # byte-for-byte; anything else is at least carried rather than refused.
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


def upstream_request_headers(
    headers: Iterable[tuple[bytes, bytes]], *, websocket: bool = False
) -> list[tuple[str, str]]:
    """The browser's request headers as they go to the daemon, in order.

    Hop-by-hop headers (and any header ``Connection`` lists), inbound
    ``Forwarded`` / ``X-Forwarded-*`` / ``X-Real-IP`` and ``Expect`` are
    dropped; everything else -- ``Host``, ``Origin``, ``Cookie``,
    ``Authorization``, ``Last-Event-ID`` included -- passes unchanged.  For a
    WebSocket the ``Sec-WebSocket-*`` handshake headers are left to aiohttp.
    """
    headers = list(headers)
    listed = _connection_tokens(headers)
    forwarded: list[tuple[str, str]] = []
    for name, value in headers:
        key = name.lower()
        if (
            key in _HOP_BY_HOP
            or key in listed
            or key in _FORWARDING
            or key.startswith(_FORWARDING_PREFIX)
            or key in _REQUEST_ONLY
            or (websocket and key.startswith(_WEBSOCKET_PREFIX))
        ):
            continue
        forwarded.append((name.decode("latin-1"), _header_text(value)))
    return forwarded


def downstream_response_headers(
    raw_headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """The daemon's response headers as the browser gets them: verbatim, in
    order and with repeats (``Set-Cookie``), minus hop-by-hop headers."""
    raw_headers = list(raw_headers)
    listed = _connection_tokens(raw_headers)
    relayed: list[tuple[bytes, bytes]] = []
    for name, value in raw_headers:
        key = name.lower()
        if key in _HOP_BY_HOP or key in listed:
            continue
        relayed.append((key, value))
    return relayed


def wire_close_code(code: int | None) -> int:
    """A close code that may be sent in a close frame.

    ``1005`` (no status) and a missing code become ``1000``; ``1006``, ``1015``
    and anything else no endpoint may send become ``1011``.  The ``4400``-range
    codes the dashboard displays pass unchanged.
    """
    if code is None or code in (0, 1005):
        return 1000
    if 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999:
        return code
    return 1011


def _close_reason(reason: str | None) -> str:
    """A close reason that fits a control frame (123 bytes of UTF-8)."""
    if not reason:
        return ""
    encoded = reason.encode("utf-8")
    if len(encoded) <= 123:
        return reason
    return encoded[:123].decode("utf-8", errors="ignore")


def _slices(chunk: bytes) -> Iterable[bytes]:
    if len(chunk) <= CHUNK_SIZE:
        if chunk:
            yield chunk
        return
    view = memoryview(chunk)
    for start in range(0, len(view), CHUNK_SIZE):
        yield bytes(view[start : start + CHUNK_SIZE])


class _BrowserGone(Exception):
    """The browser disconnected while its request body was still arriving."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class _Exchange:
    """The browser's half of one HTTP relay: what has been sent so far."""

    __slots__ = ("_gone", "bounded", "complete", "send", "started")

    def __init__(self, send: Send, gone: asyncio.Event) -> None:
        self.send = send
        self.started = False
        self.complete = False
        #: The response declared a Content-Length, so a short body must not be
        #: presented as a complete one.
        self.bounded = False
        self._gone = gone

    @property
    def browser_gone(self) -> bool:
        return self._gone.is_set()

    async def start(self, status: int, headers: list[tuple[bytes, bytes]]) -> None:
        self.started = True
        self.bounded = any(name == b"content-length" for name, _ in headers)
        await self.send({"type": "http.response.start", "status": status, "headers": headers})

    async def body(self, chunk: bytes) -> None:
        await self.send({"type": "http.response.body", "body": chunk, "more_body": True})

    async def finish(self) -> None:
        self.complete = True
        await self.send({"type": "http.response.body", "body": b"", "more_body": False})

    async def respond(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        await self.start(status, headers)
        self.complete = True
        await self.send({"type": "http.response.body", "body": body, "more_body": False})

    async def end_early(self, generated: tuple[int, list[tuple[bytes, bytes]], bytes]) -> None:
        """End a relay that cannot finish: before headers, answer ``generated``;
        mid-body, end the stream -- unless its declared length is unmet, where
        returning leaves the server to drop the connection instead of passing
        off a truncated body as whole."""
        if self.complete or self.browser_gone:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_SECONDS):
                if not self.started:
                    await self.respond(*generated)
                elif not self.bounded:
                    await self.finish()


async def _watch_disconnect(
    receive: Receive, body_done: asyncio.Event, gone: asyncio.Event
) -> None:
    await body_done.wait()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            gone.set()
            return


async def _request_body(
    first: bytes, receive: Receive, body_done: asyncio.Event, gone: asyncio.Event
) -> AsyncIterator[bytes]:
    chunk, more = first, True
    while True:
        for piece in _slices(chunk):
            yield piece
        if not more:
            body_done.set()
            return
        message = await receive()
        if message["type"] == "http.disconnect":
            gone.set()
            # Raise rather than return: a clean end would let aiohttp finish a
            # chunked body the browser never finished sending.
            raise _BrowserGone
        chunk = message.get("body", b"")
        more = message.get("more_body", False)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

_BROWSER = "browser"
_DAEMON = "daemon"
_SHUTDOWN = "shutdown"
_ERROR = "error"


@dataclass(frozen=True)
class _Closing:
    """Which side ended a relayed WebSocket, and with what code and reason."""

    origin: str
    code: int | None
    reason: str = ""


class _WebSocketRelay:
    """Two pumps, each ``receive -> await send``, and the close that ends them."""

    def __init__(self, upstream: aiohttp.ClientWebSocketResponse, receive: Receive, send: Send):
        self._upstream = upstream
        self._receive = receive
        self._send = send
        self._stop = asyncio.Event()
        self.finished = asyncio.Event()

    def stop(self) -> None:
        """Close both sides with ``1001`` (the dashboard server is stopping)."""
        self._stop.set()

    async def run(self) -> None:
        to_daemon = asyncio.create_task(self._browser_to_daemon(), name="aq-proxy-ws-to-daemon")
        to_browser = asyncio.create_task(self._daemon_to_browser(), name="aq-proxy-ws-to-browser")
        stopping = asyncio.create_task(self._stop.wait(), name="aq-proxy-ws-stop")
        pending: set[asyncio.Task[Any]] = {to_daemon, to_browser, stopping}
        closing: _Closing | None = None
        grace: float | None = None
        try:
            while closing is None:
                done, pending = await asyncio.wait(
                    pending, timeout=grace, return_when=asyncio.FIRST_COMPLETED
                )
                if stopping in done:
                    closing = _Closing(_SHUTDOWN, 1001, "dashboard server stopping")
                    break
                if not done:
                    closing = _Closing(_ERROR, 1011)
                    break
                for task in done:
                    closing = self._pump_outcome(task)
                    if closing is not None:
                        break
                else:
                    if not pending - {stopping}:
                        closing = _Closing(_ERROR, 1011)
                    else:
                        # One side's send failed because its peer went away;
                        # the other pump is about to learn how, and that close
                        # code is the one worth relaying.
                        grace = _PEER_CLOSE_GRACE_SECONDS
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self._finish(closing)

    @staticmethod
    def _pump_outcome(task: asyncio.Task[Any]) -> _Closing | None:
        if task.cancelled():
            return _Closing(_ERROR, 1011)
        error = task.exception()
        if error is not None:
            logger.warning("WebSocket relay failed: %s", type(error).__name__, exc_info=error)
            return _Closing(_ERROR, 1011)
        return task.result()

    async def _browser_to_daemon(self) -> _Closing | None:
        while True:
            message = await self._receive()
            kind = message["type"]
            if kind == "websocket.disconnect":
                return _Closing(_BROWSER, message.get("code", 1005), message.get("reason") or "")
            if kind != "websocket.receive":
                continue
            text = message.get("text")
            try:
                if text is not None:
                    await self._upstream.send_str(text)
                else:
                    await self._upstream.send_bytes(message.get("bytes") or b"")
            except (aiohttp.ClientError, OSError):
                return None

    async def _daemon_to_browser(self) -> _Closing | None:
        while True:
            message = await self._upstream.receive()
            if message.type is aiohttp.WSMsgType.TEXT:
                event: Message = {"type": "websocket.send", "text": message.data}
            elif message.type is aiohttp.WSMsgType.BINARY:
                event = {"type": "websocket.send", "bytes": message.data}
            elif message.type is aiohttp.WSMsgType.CLOSE:
                return _Closing(_DAEMON, message.data, message.extra or "")
            elif message.type in (
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                # The daemon's side ended without a close frame (or with one
                # the relay cannot pass on): abnormal, which is sent as 1011.
                return _Closing(_DAEMON, 1006)
            else:
                continue
            try:
                await self._send(event)
            except OSError:
                return None

    async def _finish(self, closing: _Closing) -> None:
        code = wire_close_code(closing.code)
        reason = _close_reason(closing.reason)
        if closing.origin != _BROWSER:
            await self.close_browser(code, reason)
        await self.close_daemon(code, reason)

    async def close_browser(self, code: int, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_SECONDS):
                await self._send({"type": "websocket.close", "code": code, "reason": reason})

    async def close_daemon(self, code: int, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_SECONDS):
                await self._upstream.close(code=code, message=reason.encode("utf-8"))


# ---------------------------------------------------------------------------
# The proxy
# ---------------------------------------------------------------------------


class DaemonProxy:
    """Relays HTTP and WebSocket requests to the daemon's API base.

    One :class:`aiohttp.ClientSession` serves both, created by :meth:`start` and
    closed by :meth:`close`.  ``api_url`` is a base such as
    ``http://127.0.0.1:8081``; WebSocket URLs derive from it.
    """

    def __init__(
        self,
        api_url: str,
        *,
        version: str,
        connect_timeout: float = 2.0,
        header_timeout: float = 120.0,
        max_message_size: int = 16 * 1024 * 1024,
    ) -> None:
        base = URL(api_url.rstrip("/"))
        if base.scheme not in ("http", "https") or not base.host:
            raise ValueError(f"api_url must be an http(s) URL with a host, got {api_url!r}")
        self._api_url = api_url.rstrip("/")
        self._http_scheme = base.scheme
        self._ws_scheme = "wss" if base.scheme == "https" else "ws"
        self._authority = base.raw_authority
        self._base_path = base.raw_path.rstrip("/")
        self._version = version
        self._connect_timeout = connect_timeout
        self._header_timeout = header_timeout
        self._max_message_size = max_message_size
        self._session: aiohttp.ClientSession | None = None
        self._closing = False
        self._closed = asyncio.Event()
        self._http_relays: set[asyncio.Task[None]] = set()
        self._ws_relays: set[_WebSocketRelay] = set()

    @property
    def api_url(self) -> str:
        return self._api_url

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Create the one upstream session (inside the running event loop)."""
        if self._session is not None or self._closing:
            return
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=256, keepalive_timeout=_KEEPALIVE_SECONDS),
            # The proxy is shared by every browser: it must never store a
            # cookie from one response and replay it on another's request.
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            skip_auto_headers=_SKIP_AUTO_HEADERS,
            raise_for_status=False,
            trust_env=False,
            requote_redirect_url=False,
            # Connect 2 s; the 120 s to response headers is enforced per
            # request; there is deliberately no read timeout, because SSE
            # streams are silent for as long as nothing happens.
            timeout=aiohttp.ClientTimeout(
                total=None, connect=None, sock_connect=self._connect_timeout, sock_read=None
            ),
        )

    async def close(self) -> None:
        """Close proxied WebSockets ``1001`` both ways, end in-flight HTTP and
        SSE relays, then close the session.  Idempotent and bounded."""
        if self._closing:
            await self._closed.wait()
            return
        self._closing = True
        try:
            relays = list(self._ws_relays)
            for relay in relays:
                relay.stop()
            http_relays = list(self._http_relays)
            for task in http_relays:
                task.cancel()
            waiters = [asyncio.create_task(relay.finished.wait()) for relay in relays]
            if waiters or http_relays:
                await asyncio.wait([*waiters, *http_relays], timeout=_SHUTDOWN_GRACE_SECONDS)
            for waiter in waiters:
                waiter.cancel()
        finally:
            if self._session is not None:
                await self._session.close()
            self._closed.set()

    async def upstream_ok(self) -> bool:
        """``True`` iff the daemon answered ``GET /health`` at all (any status)."""
        session = self._session
        if session is None or session.closed:
            return False
        url = self._url(self._http_scheme, "/health", "")
        try:
            async with session.get(
                url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=_PROBE_SECONDS)
            ):
                return True
        except (TimeoutError, aiohttp.ClientError, OSError):
            return False

    # -- responses the proxy generates ---------------------------------------

    def _generated(
        self,
        status: int,
        error: str,
        *,
        retry_after: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        payload = {"ok": False, "error": error, "api_url": self._api_url, **(extra or {})}
        body = json.dumps(payload).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (DASHBOARD_SERVER_HEADER.encode("ascii"), self._version.encode("latin-1", "replace")),
        ]
        if retry_after:
            headers.append((b"retry-after", b"2"))
        return status, headers, body

    def _unreachable(self) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        if self._closing:
            return self._generated(503, "dashboard_server_stopping", retry_after=True)
        return self._generated(503, "daemon_unreachable", retry_after=True)

    # -- targets ---------------------------------------------------------------

    def _url(self, scheme: str, path: str, query: str) -> URL:
        return URL.build(
            scheme=scheme,
            authority=self._authority,
            path=self._base_path + path,
            query_string=query,
            encoded=True,
        )

    def _target(self, scope: Scope, scheme: str) -> URL | None:
        raw_path = scope.get("raw_path")
        if not raw_path:
            raw_path = quote(scope.get("path", ""), safe="/:@!$&'()*+,;=-._~").encode("ascii")
        try:
            path = bytes(raw_path).decode("ascii")
            query = bytes(scope.get("query_string") or b"").decode("ascii")
        except UnicodeDecodeError:
            return None
        if not path.startswith("/") or "#" in path or "#" in query:
            return None
        return self._url(scheme, path, query)

    def _require_session(self) -> aiohttp.ClientSession | None:
        """The session, or ``None`` once :meth:`close` has begun."""
        if self._closing:
            return None
        if self._session is None:
            raise RuntimeError("DaemonProxy.start() must be awaited before relaying")
        return self._session

    # -- HTTP --------------------------------------------------------------------

    async def http(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Relay one HTTP request (ASGI ``http`` scope) to the daemon.

        The request body must not have been read: it streams from ``receive``,
        which then watches for the browser's disconnect.
        """
        gone = asyncio.Event()
        exchange = _Exchange(send, gone)
        session = self._require_session()
        if session is None:
            await exchange.respond(*self._unreachable())
            return
        target = self._target(scope, self._http_scheme)
        if target is None:
            await exchange.respond(*self._generated(400, "bad_request_target"))
            return

        first = await receive()
        if first["type"] != "http.request":
            return
        body_done = asyncio.Event()
        data: bytes | AsyncIterator[bytes] | None
        if first.get("more_body", False):
            data = _request_body(first.get("body", b""), receive, body_done, gone)
        else:
            body_done.set()
            data = bytes(first.get("body", b"")) or None

        relay = asyncio.create_task(
            self._relay_http(session, scope, target, data, exchange), name="aq-proxy-http"
        )
        watcher = asyncio.create_task(_watch_disconnect(receive, body_done, gone))
        disconnected = asyncio.create_task(gone.wait())
        self._http_relays.add(relay)
        try:
            await asyncio.wait({relay, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not relay.done():
                # The browser left: cancel the upstream request at once, which
                # closes its connection and lets the daemon's stream end.
                relay.cancel()
            watcher.cancel()
            disconnected.cancel()
            await asyncio.gather(relay, watcher, disconnected, return_exceptions=True)
            self._http_relays.discard(relay)
        if not relay.cancelled() and relay.exception() is not None:
            raise relay.exception()  # type: ignore[misc]

    async def _relay_http(
        self,
        session: aiohttp.ClientSession,
        scope: Scope,
        target: URL,
        data: bytes | AsyncIterator[bytes] | None,
        exchange: _Exchange,
    ) -> None:
        method = scope.get("method", "GET")
        headers = upstream_request_headers(scope.get("headers") or [])
        if self._closing or session.closed:
            # close() began after this request was admitted; nothing awaits
            # between this check and aiohttp's own, so the request below
            # cannot meet a closed session.
            await exchange.respond(*self._unreachable())
            return
        try:
            try:
                async with asyncio.timeout(self._header_timeout):
                    response = await session.request(
                        method, target, headers=headers, data=data, allow_redirects=False
                    )
            except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as error:
                logger.debug("daemon unreachable for %s %s: %s", method, target.path, error)
                await exchange.respond(*self._unreachable())
                return
            except TimeoutError:
                logger.debug("daemon sent no headers in time for %s %s", method, target.path)
                await exchange.respond(*self._generated(504, "daemon_timeout"))
                return
            except (aiohttp.ClientError, OSError, _BrowserGone) as error:
                if exchange.browser_gone:
                    return
                logger.debug("daemon failed %s %s: %s", method, target.path, error)
                if self._closing:
                    await exchange.respond(*self._unreachable())
                else:
                    await exchange.respond(*self._generated(502, "daemon_bad_response"))
                return
            try:
                await self._relay_response(response, exchange)
            finally:
                response.close()
        except asyncio.CancelledError:
            # Stopping (or the server cancelling us): leave the browser with a
            # finished answer rather than a hung one.  A browser that already
            # left needs nothing.
            await exchange.end_early(self._unreachable())
            raise

    async def _relay_response(self, response: aiohttp.ClientResponse, exchange: _Exchange) -> None:
        try:
            await exchange.start(response.status, downstream_response_headers(response.raw_headers))
        except OSError:
            return
        while True:
            try:
                chunk = await response.content.read(CHUNK_SIZE)
            except (aiohttp.ClientError, OSError) as error:
                logger.debug("daemon response ended early: %s", type(error).__name__)
                await exchange.end_early(self._unreachable())
                return
            if not chunk:
                break
            try:
                # Awaited before the next read: no queue, so a slow browser
                # stops the proxy reading and TCP pushes back on the daemon.
                await exchange.body(chunk)
            except OSError:
                return
        with contextlib.suppress(OSError):
            await exchange.finish()

    # -- WebSocket ---------------------------------------------------------------

    async def websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Relay one WebSocket (ASGI ``websocket`` scope), daemon handshake first.

        The ``websocket.connect`` message need not have been consumed: the
        browser pump skips it, so a caller that already read it (to gate the
        handshake) and one that did not both work.
        """
        session = self._require_session()
        if session is None:
            await self._deny(scope, send, self._unreachable())
            return
        target = self._target(scope, self._ws_scheme)
        if target is None:
            await self._deny(scope, send, self._generated(400, "bad_request_target"))
            return

        try:
            async with asyncio.timeout(self._header_timeout):
                upstream = await session.ws_connect(
                    target,
                    protocols=list(scope.get("subprotocols") or ()),
                    headers=upstream_request_headers(scope.get("headers") or [], websocket=True),
                    autoclose=False,
                    autoping=True,
                    heartbeat=None,
                    compress=0,
                    max_msg_size=self._max_message_size,
                    timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=_CLOSE_SECONDS),
                )
        except aiohttp.WSServerHandshakeError as error:
            if error.status and error.status != 101:
                # The daemon refused before accepting (a terminal's 4401/4403/
                # 4429 is an HTTP 403 on the wire): refuse the browser the same
                # way rather than accepting and closing.
                await self._deny(
                    scope,
                    send,
                    self._generated(error.status, "daemon_refused", extra={"status": error.status}),
                )
            else:
                await self._deny(scope, send, self._generated(502, "daemon_bad_response"))
            return
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError):
            await self._deny(scope, send, self._unreachable())
            return
        except TimeoutError:
            await self._deny(scope, send, self._generated(504, "daemon_timeout"))
            return
        except (aiohttp.ClientError, OSError):
            if self._closing:
                await self._deny(scope, send, self._unreachable())
            else:
                await self._deny(scope, send, self._generated(502, "daemon_bad_response"))
            return

        relay = _WebSocketRelay(upstream, receive, send)
        self._ws_relays.add(relay)
        try:
            accept: Message = {"type": "websocket.accept"}
            if upstream.protocol:
                accept["subprotocol"] = upstream.protocol
            try:
                await send(accept)
            except OSError:
                await relay.close_daemon(1001)
                return
            if self._closing:
                relay.stop()
            await relay.run()
        finally:
            self._ws_relays.discard(relay)
            if not upstream.closed:
                await relay.close_daemon(1011)
            relay.finished.set()

    async def _deny(
        self, scope: Scope, send: Send, generated: tuple[int, list[tuple[bytes, bytes]], bytes]
    ) -> None:
        status, headers, body = generated
        with contextlib.suppress(OSError):
            if "websocket.http.response" in (scope.get("extensions") or {}):
                await send(
                    {"type": "websocket.http.response.start", "status": status, "headers": headers}
                )
                await send({"type": "websocket.http.response.body", "body": body})
            else:
                # Closing before accepting is a refused handshake (HTTP 403).
                await send({"type": "websocket.close", "code": 1008 if status < 500 else 1011})
