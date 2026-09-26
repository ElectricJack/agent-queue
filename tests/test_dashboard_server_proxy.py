"""The dashboard server's reverse proxy (docs/specs/dashboard-server.md §2, §7).

Every test drives :class:`DaemonProxy` through a real uvicorn server with real
clients (aiohttp for HTTP, ``websockets`` for WebSockets) against
:class:`FakeDaemon` on its own ephemeral port.  Ordering is asserted with the
fake daemon's events, never with sleeps; every wait is bounded.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from yarl import URL

from src.dashboard_server import proxy as proxy_module
from src.dashboard_server.proxy import (
    DASHBOARD_SERVER_HEADER,
    DaemonProxy,
    downstream_response_headers,
    upstream_request_headers,
    wire_close_code,
)
from tests.dashboard_server_helpers import (
    TERMINAL_PROTOCOL,
    FakeDaemon,
    serve_asgi,
    unused_port,
)

VERSION = "9.9.9-test"
GUARD = 5.0  # seconds: an upper bound on any single wait, never an expectation


def proxy_asgi(proxy: DaemonProxy):
    """The thinnest ASGI app around the proxy: lifespan, http and websocket."""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await proxy.start()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await proxy.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http":
            await proxy.http(scope, receive, send)
        else:
            await proxy.websocket(scope, receive, send)

    return app


class Stack:
    def __init__(self, daemon: FakeDaemon, daemon_url: str, proxy: DaemonProxy, url: str):
        self.daemon = daemon
        self.daemon_url = daemon_url
        self.proxy = proxy
        self.url = url
        self.host = URL(url).raw_authority  # what a browser sends as Host

    @property
    def ws_url(self) -> str:
        return "ws" + self.url[len("http") :]


@asynccontextmanager
async def proxied(**proxy_options: Any) -> AsyncIterator[Stack]:
    daemon = FakeDaemon()
    async with serve_asgi(daemon.app) as daemon_url:
        proxy = DaemonProxy(daemon_url, version=VERSION, **proxy_options)
        async with serve_asgi(proxy_asgi(proxy)) as url:
            yield Stack(daemon, daemon_url, proxy, url)


@pytest.fixture
async def stack() -> AsyncIterator[Stack]:
    async with proxied() as value:
        yield value


@pytest.fixture
async def client() -> AsyncIterator[aiohttp.ClientSession]:
    # A client that sends only what the test asks for.
    async with aiohttp.ClientSession(
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        skip_auto_headers=("Accept", "Accept-Encoding", "User-Agent", "Content-Type"),
    ) as session:
        yield session


def ws_connect(url: str, **options: Any) -> connect:
    options.setdefault("proxy", None)
    options.setdefault("user_agent_header", None)
    options.setdefault("open_timeout", GUARD)
    options.setdefault("close_timeout", GUARD)
    return connect(url, **options)


def proxy_tasks() -> list[asyncio.Task[Any]]:
    """Tasks still running code from the proxy module (a relay that leaked)."""
    leaked = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if code is not None and code.co_filename == proxy_module.__file__ and not task.done():
            leaked.append(task)
    return leaked


async def no_proxy_tasks() -> None:
    async with asyncio.timeout(GUARD):
        while proxy_tasks():
            await asyncio.sleep(0.01)


def header_values(response: aiohttp.ClientResponse, name: str) -> list[bytes]:
    wanted = name.lower().encode()
    return [value for key, value in response.raw_headers if key.lower() == wanted]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, 1000),
        (0, 1000),
        (1000, 1000),
        (1001, 1001),
        (1005, 1000),
        (1006, 1011),
        (1011, 1011),
        (1015, 1011),
        (1004, 1011),
        (999, 1011),
        (3000, 3000),
        (4400, 4400),
        (4408, 4408),
        (4429, 4429),
        (5000, 1011),
    ],
)
def test_wire_close_code_keeps_sendable_codes_and_replaces_the_rest(code, expected):
    assert wire_close_code(code) == expected


def test_request_headers_drop_hop_by_hop_and_forwarding_and_keep_the_rest_in_order():
    raw = [
        (b"Host", b"127.0.0.1:8082"),
        (b"Connection", b"keep-alive, X-Hop, Host"),
        (b"X-Hop", b"1"),
        (b"Keep-Alive", b"timeout=5"),
        (b"TE", b"trailers"),
        (b"Transfer-Encoding", b"chunked"),
        (b"Upgrade", b"h2c"),
        (b"Proxy-Authorization", b"Basic eA=="),
        (b"Forwarded", b"for=1.2.3.4"),
        (b"X-Forwarded-For", b"1.2.3.4"),
        (b"X-Forwarded-Proto", b"https"),
        (b"X-Real-IP", b"1.2.3.4"),
        (b"Expect", b"100-continue"),
        (b"Origin", b"http://127.0.0.1:8082"),
        (b"Cookie", b"a=1"),
        (b"Cookie", b"b=2"),
        (b"Authorization", b"Bearer aqs_x"),
        (b"Sec-WebSocket-Protocol", b"aq-terminal-v1"),
        (b"Last-Event-ID", b"42"),
    ]
    kept = [
        ("Host", "127.0.0.1:8082"),
        ("Origin", "http://127.0.0.1:8082"),
        ("Cookie", "a=1"),
        ("Cookie", "b=2"),
        ("Authorization", "Bearer aqs_x"),
    ]
    assert upstream_request_headers(raw) == [
        *kept,
        ("Sec-WebSocket-Protocol", "aq-terminal-v1"),
        ("Last-Event-ID", "42"),
    ]
    # A Connection token never strips Host; on a WebSocket, aiohttp owns Sec-WebSocket-*.
    assert upstream_request_headers(raw, websocket=True) == [*kept, ("Last-Event-ID", "42")]


def test_response_headers_keep_repeats_and_drop_hop_by_hop():
    raw = [
        (b"Content-Type", b"application/json"),
        (b"Set-Cookie", b"a=1"),
        (b"Connection", b"close, X-Internal"),
        (b"X-Internal", b"1"),
        (b"Transfer-Encoding", b"chunked"),
        (b"Set-Cookie", b"b=2"),
        (b"Keep-Alive", b"timeout=5"),
    ]
    assert downstream_response_headers(raw) == [
        (b"content-type", b"application/json"),
        (b"set-cookie", b"a=1"),
        (b"set-cookie", b"b=2"),
    ]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def test_json_passes_through_with_status_body_and_headers_verbatim(stack, client):
    async with client.get(f"{stack.daemon_url}/api/fixed") as direct:
        direct_body = await direct.read()
        direct_headers = list(direct.raw_headers)
    async with client.get(f"{stack.url}/api/fixed") as relayed:
        assert relayed.status == 200
        assert await relayed.read() == direct_body
        assert [(k.lower(), v) for k, v in relayed.raw_headers] == [
            (k.lower(), v) for k, v in direct_headers
        ]
        assert DASHBOARD_SERVER_HEADER not in relayed.headers
    # HEAD keeps the declared length and carries no body.
    async with client.head(f"{stack.url}/api/fixed") as head:
        assert head.status == 200
        assert head.headers["content-length"] == str(len(direct_body))
        assert await head.read() == b""


async def test_health_and_ready_pass_through(stack, client):
    for path in ("/health", "/ready"):
        async with client.get(f"{stack.url}{path}") as response:
            assert response.status == 200
            assert await response.json() == {"status": "ok"}
    assert [r.raw_path for r in stack.daemon.requests] == [b"/health", b"/ready"]


async def test_repeated_set_cookie_headers_survive_and_are_never_replayed(stack, client):
    async with client.get(f"{stack.url}/api/cookies") as response:
        cookies = header_values(response, "set-cookie")
    assert len(cookies) == 2
    assert cookies[0].startswith(b"first=1")
    assert cookies[1].startswith(b"second=2")
    # The proxy is shared by every browser: it must not store the cookies it
    # relayed and send them on someone else's request.
    async with client.get(f"{stack.url}/api/echo") as response:
        assert response.status == 200
    assert stack.daemon.last_request.header("cookie") is None


async def test_request_headers_pass_end_to_end_and_hop_by_hop_are_dropped(stack, client):
    headers = [
        ("Host", stack.host),
        ("Origin", f"http://{stack.host}"),
        ("Cookie", "session=abc"),
        ("Authorization", "Bearer aqs_token"),
        ("Last-Event-ID", "17"),
        ("Accept", "text/event-stream"),
        ("X-Custom", "one"),
        ("X-Custom", "two"),
        ("Connection", "keep-alive, X-Hop"),
        ("X-Hop", "dropped"),
        ("Keep-Alive", "timeout=5"),
        ("TE", "trailers"),
        ("Proxy-Authorization", "Basic eA=="),
        ("Forwarded", "for=203.0.113.9"),
        ("X-Forwarded-For", "203.0.113.9"),
        ("X-Forwarded-Host", "evil.example"),
        ("X-Real-IP", "203.0.113.9"),
    ]
    async with client.get(f"{stack.daemon_url}/api/echo", headers=headers) as response:
        assert response.status == 200
    direct = stack.daemon.last_request.headers
    async with client.get(f"{stack.url}/api/echo", headers=headers) as response:
        assert response.status == 200
    relayed = stack.daemon.last_request.headers

    dropped = {
        "connection",
        "x-hop",
        "keep-alive",
        "te",
        "proxy-authorization",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-real-ip",
    }
    # Exactly what the browser sent, in order, minus the dropped headers -- so
    # nothing was added and nothing else changed.
    assert relayed == [(name, value) for name, value in direct if name not in dropped]
    request = stack.daemon.last_request
    assert request.header("host") == stack.host  # not rewritten to the daemon's address
    assert request.header("origin") == f"http://{stack.host}"
    assert request.header("cookie") == "session=abc"
    assert request.header("authorization") == "Bearer aqs_token"
    assert request.header("last-event-id") == "17"
    assert request.headers_named("x-custom") == ["one", "two"]


async def test_raw_path_and_query_reach_the_daemon_byte_for_byte(stack, client):
    raw_path = "/api/echo/a%2Fb%20c%25/%E2%9C%93;v=1//x"
    query = "q=x%26y&z=%E2%9C%93&empty=&plus=a+b&q=again"
    url = URL(f"{stack.url}{raw_path}?{query}", encoded=True)
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    async with client.post(url, data=b"payload", headers=headers) as response:
        assert response.status == 200
        echoed = await response.json()
    request = stack.daemon.last_request
    assert request.method == "POST"
    assert request.raw_path == raw_path.encode()
    assert request.query_string == query.encode()
    assert request.header("content-type") == "text/plain; charset=utf-8"
    assert echoed["body_bytes"] == len(b"payload")


async def test_sse_chunk_is_delivered_before_the_daemon_finishes(stack, client):
    daemon = stack.daemon
    async with client.get(f"{stack.url}/api/sse") as response:
        assert response.status == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), GUARD)
        assert first == b"data: first\n\n"
        # The first event reached the browser while the daemon's generator is
        # still parked on the event only this test sets.
        assert daemon.sse_first_sent.is_set()
        assert not daemon.sse_release.is_set()
        assert not daemon.sse_closed.is_set()
        daemon.sse_release.set()
        rest = await asyncio.wait_for(response.content.read(), GUARD)
        assert rest == b"data: second\n\n"
    await asyncio.wait_for(daemon.sse_closed.wait(), GUARD)
    assert not daemon.sse_cancelled.is_set()


async def test_browser_disconnect_cancels_the_upstream_stream(stack):
    daemon = stack.daemon
    async with aiohttp.ClientSession() as browser:
        response = await browser.get(f"{stack.url}/api/sse")
        first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), GUARD)
        assert first == b"data: first\n\n"
        response.close()  # the tab went away mid-stream
    # The daemon's stream generator is torn down, though nothing released it.
    await asyncio.wait_for(daemon.sse_cancelled.wait(), GUARD)
    assert not daemon.sse_release.is_set()
    await no_proxy_tasks()


async def test_browser_disconnect_before_headers_cancels_the_upstream_request(stack):
    async with aiohttp.ClientSession() as browser:
        pending = asyncio.create_task(browser.get(f"{stack.url}/api/slow"))
        await asyncio.wait_for(stack.daemon.slow_started.wait(), GUARD)
        pending.cancel()  # the tab went away while the daemon was still thinking
        with pytest.raises(asyncio.CancelledError):
            await pending
    await asyncio.wait_for(stack.daemon.slow_abandoned.wait(), GUARD)
    await no_proxy_tasks()


async def test_browser_disconnect_mid_upload_never_completes_the_upload_upstream(stack):
    daemon = stack.daemon

    class TabClosed(Exception):
        pass

    async def body() -> AsyncIterator[bytes]:
        yield b"x" * (256 * 1024)
        await asyncio.wait_for(daemon.upload_started.wait(), GUARD)
        raise TabClosed

    async with aiohttp.ClientSession() as browser:
        with pytest.raises(aiohttp.ClientError):
            await browser.post(f"{stack.url}/api/upload", data=body())
    # The daemon sees the upload cut off -- not a short body presented as whole.
    await asyncio.wait_for(daemon.upload_aborted.wait(), GUARD)
    assert daemon.uploads == []
    await no_proxy_tasks()


async def test_streamed_upload_arrives_intact_without_being_buffered_whole(stack, client):
    payload = os.urandom(4 * 1024 * 1024 + 123)
    daemon = stack.daemon

    async def body() -> AsyncIterator[bytes]:
        piece = 100 * 1024
        yield payload[:piece]
        # The daemon has bytes before the browser has finished sending: the
        # proxy streams the body rather than collecting it first.
        await asyncio.wait_for(daemon.upload_started.wait(), GUARD)
        for start in range(piece, len(payload), piece):
            yield payload[start : start + piece]

    async with client.post(f"{stack.url}/api/upload", data=body()) as response:
        assert response.status == 200
        answer = await response.json()
    assert answer["bytes"] == len(payload)
    assert answer["sha256"] == hashlib.sha256(payload).hexdigest()
    assert answer["transfer_encoding"] == "chunked"
    assert answer["content_length"] is None

    # A browser that declares its length keeps its Content-Length upstream.
    async with client.put(f"{stack.url}/api/upload", data=io.BytesIO(payload)) as response:
        assert response.status == 200
        answer = await response.json()
    assert answer["bytes"] == len(payload)
    assert answer["sha256"] == hashlib.sha256(payload).hexdigest()
    assert answer["content_length"] == str(len(payload))
    assert answer["transfer_encoding"] is None


@pytest.mark.parametrize("status", [500, 404])
async def test_daemon_errors_pass_through_unchanged(stack, client, status):
    async with client.get(f"{stack.daemon_url}/api/status/{status}") as direct:
        direct_body = await direct.read()
    async with client.get(f"{stack.url}/api/status/{status}") as relayed:
        assert relayed.status == status
        assert await relayed.read() == direct_body
        assert json.loads(direct_body) == {"ok": False, "error": f"status_{status}"}
        assert DASHBOARD_SERVER_HEADER not in relayed.headers


async def test_daemon_unreachable_is_503_with_retry_after_and_our_header(client):
    api_url = f"http://127.0.0.1:{unused_port()}"
    proxy = DaemonProxy(api_url, version=VERSION)
    async with serve_asgi(proxy_asgi(proxy)) as url:
        for path in ("/api/tasks?limit=5", "/health"):
            async with client.get(f"{url}{path}") as response:
                assert response.status == 503
                assert await response.json() == {
                    "ok": False,
                    "error": "daemon_unreachable",
                    "api_url": api_url,
                }
                assert response.headers["retry-after"] == "2"
                assert response.headers["cache-control"] == "no-store"
                assert response.headers[DASHBOARD_SERVER_HEADER] == VERSION
        assert await proxy.upstream_ok() is False
    assert proxy.stats.snapshot()["upstream_failures"]["daemon_unreachable"] == 2
    assert proxy.stats.snapshot()["http"]["count"] == 0


async def test_upstream_ok_is_true_whenever_the_daemon_answers(stack):
    assert await stack.proxy.upstream_ok() is True
    stack.daemon.health_status = 503  # degraded, but it answered
    assert await stack.proxy.upstream_ok() is True


async def test_daemon_that_sends_no_headers_in_time_is_504(client):
    async with proxied(header_timeout=0.5) as stack:
        async with client.get(f"{stack.url}/api/slow") as response:
            assert response.status == 504
            assert await response.json() == {
                "ok": False,
                "error": "daemon_timeout",
                "api_url": stack.daemon_url,
            }
            assert response.headers[DASHBOARD_SERVER_HEADER] == VERSION
            assert response.headers["cache-control"] == "no-store"
        assert stack.proxy.stats.snapshot()["upstream_failures"]["daemon_timeout"] == 1
        # The abandoned request was cancelled upstream, not left running.  (On
        # a box too loaded to deliver it within the timeout there is nothing
        # upstream to cancel; the disconnect test covers cancellation alone.)
        if stack.daemon.slow_started.is_set():
            await asyncio.wait_for(stack.daemon.slow_abandoned.wait(), GUARD)


async def test_proxy_close_ends_a_live_sse_relay(stack, client):
    async with client.get(f"{stack.url}/api/sse") as response:
        first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), GUARD)
        assert first == b"data: first\n\n"
        await asyncio.wait_for(stack.proxy.close(), GUARD)
        # The browser's stream ends cleanly instead of hanging.
        assert await asyncio.wait_for(response.content.read(), GUARD) == b""
    await asyncio.wait_for(stack.daemon.sse_cancelled.wait(), GUARD)


async def test_requests_after_close_are_refused_by_the_proxy_itself(stack, client):
    await asyncio.wait_for(stack.proxy.close(), GUARD)
    async with client.get(f"{stack.url}/api/fixed") as response:
        assert response.status == 503
        assert (await response.json())["error"] == "dashboard_server_stopping"
        assert response.headers[DASHBOARD_SERVER_HEADER] == VERSION
    with pytest.raises(InvalidStatus) as denied:
        async with ws_connect(f"{stack.ws_url}/ws/echo"):
            pytest.fail("the browser's handshake was accepted")
    assert denied.value.response.status_code == 503
    assert stack.daemon.requests == []
    assert stack.daemon.websockets == []
    assert stack.proxy.stats.snapshot()["upstream_failures"]["dashboard_server_stopping"] == 2


async def test_a_garbled_daemon_answer_is_502(client):
    async def garble(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"NOT-HTTP garbage\r\n\r\n")
        await writer.drain()
        writer.close()

    garbler = await asyncio.start_server(garble, "127.0.0.1", 0)
    api_url = f"http://127.0.0.1:{garbler.sockets[0].getsockname()[1]}"
    try:
        proxy = DaemonProxy(api_url, version=VERSION)
        async with serve_asgi(proxy_asgi(proxy)) as url:
            async with client.get(f"{url}/api/fixed") as response:
                assert response.status == 502
                assert await response.json() == {
                    "ok": False,
                    "error": "daemon_bad_response",
                    "api_url": api_url,
                }
                assert response.headers[DASHBOARD_SERVER_HEADER] == VERSION
            with pytest.raises(InvalidStatus) as denied:
                async with ws_connect("ws" + url[len("http") :] + "/ws/events"):
                    pytest.fail("the browser's handshake was accepted")
            assert denied.value.response.status_code == 502
        assert proxy.stats.snapshot()["upstream_failures"]["daemon_bad_response"] == 2
    finally:
        garbler.close()
        await garbler.wait_closed()


async def test_relay_stats_measure_headers_and_handshake_before_streams_end(monkeypatch):
    from types import SimpleNamespace

    readings = iter([0.0, 0.005, 1.0, 1.007])
    monkeypatch.setattr(
        "src.dashboard_server.proxy.time", SimpleNamespace(perf_counter=lambda: next(readings)),
    )
    async with proxied() as stack, aiohttp.ClientSession() as client:
        async with client.get(f"{stack.url}/api/sse") as response:
            await asyncio.wait_for(response.content.readuntil(b"\n\n"), GUARD)
            snapshot = stack.proxy.stats.snapshot()
            assert snapshot["http"]["count"] == 1
            assert snapshot["http"]["sum"] == pytest.approx(5)
            assert snapshot["relays_open"] == 1
            assert not stack.daemon.sse_release.is_set()
            stack.daemon.sse_release.set()
            await response.read()
        async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
            await ws.send("ping")
            assert await asyncio.wait_for(ws.recv(), GUARD) == "ping"
            snapshot = stack.proxy.stats.snapshot()
            assert snapshot["ws_handshake"]["count"] == 1
            assert snapshot["ws_handshake"]["sum"] == pytest.approx(7)
            assert snapshot["relays_open"] >= 1
    snapshot = stack.proxy.stats.snapshot()
    assert snapshot["http"]["count"] == snapshot["ws_handshake"]["count"] == 1
    assert snapshot["relays_open"] == 0
    assert not any(snapshot["upstream_failures"].values())


def test_relay_stats_ignore_unknown_failures_and_copy_cumulative_buckets():
    from src.dashboard_server.relay_stats import FAILURES, RelayStats

    stats = RelayStats(clock=lambda: 123)
    stats.observe_http(5)
    stats.observe_ws_handshake(7)
    for name in FAILURES:
        stats.failure(name)
    stats.failure("daemon_refused")
    snapshot = stats.snapshot()
    snapshot["http"]["counts"][0] = 100
    snapshot["upstream_failures"]["daemon_timeout"] = 100
    second = stats.snapshot()
    assert second["epoch"] == second["now"] == 123
    assert second["http"]["count"] == sum(second["http"]["counts"]) == 1
    assert second["upstream_failures"] == dict.fromkeys(FAILURES, 1)


async def test_relay_instrumentation_failure_does_not_fail_requests(monkeypatch, stack, client):
    def broken_observe(*args):
        raise RuntimeError("broken telemetry")

    monkeypatch.setattr("src.dashboard_server.relay_stats.observe", broken_observe)
    async with client.get(f"{stack.url}/api/fixed") as response:
        assert response.status == 200
        await response.read()
    async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
        await ws.send("still works")
        assert await asyncio.wait_for(ws.recv(), GUARD) == "still works"


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


async def test_websocket_relays_text_and_binary_frames_one_to_one(stack):
    messages: list[str | bytes] = ["hello", b"\x00\x01\x02\xff", "", b"", '{"type":"ack"}']
    messages += [bytes([n]) * (n * 997) for n in range(1, 6)]
    async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
        for message in messages:
            await ws.send(message)
        received = [await asyncio.wait_for(ws.recv(), GUARD) for _ in messages]
    assert received == messages
    assert [type(m) for m in received] == [type(m) for m in messages]
    assert stack.daemon.websockets[0].received == messages
    await no_proxy_tasks()


async def test_websocket_accepts_the_subprotocol_the_daemon_selected(stack):
    offered = [TERMINAL_PROTOCOL, "aq-bearer.aqs_token"]
    async with ws_connect(f"{stack.ws_url}/ws/terminal/s1", subprotocols=offered) as ws:
        assert ws.subprotocol == TERMINAL_PROTOCOL
        await ws.send("ping")
        assert await asyncio.wait_for(ws.recv(), GUARD) == "ping"
    recorded = stack.daemon.websockets[0]
    assert recorded.subprotocols == offered  # verbatim and in order
    assert recorded.accepted_subprotocol == TERMINAL_PROTOCOL

    # When the daemon selects nothing, the browser is accepted with nothing.
    async with ws_connect(f"{stack.ws_url}/ws/echo", subprotocols=["aq-bearer.x"]) as ws:
        assert ws.subprotocol is None
    assert stack.daemon.websockets[1].subprotocols == ["aq-bearer.x"]


async def test_websocket_handshake_forwards_host_origin_credentials_and_query(stack):
    origin = f"http://{stack.host}"
    headers = {
        "Cookie": "session=abc",
        "Authorization": "Bearer aqs_token",
        "X-Forwarded-For": "203.0.113.9",
    }
    query = "cols=120&rows=40&x=%E2%9C%93"
    async with ws_connect(
        f"{stack.ws_url}/ws/terminal/s1?{query}", origin=origin, additional_headers=headers
    ) as ws:
        await ws.send("hi")
        assert await asyncio.wait_for(ws.recv(), GUARD) == "hi"
    recorded = stack.daemon.websockets[0]
    assert recorded.header("host") == stack.host
    assert recorded.header("origin") == origin
    assert recorded.header("cookie") == "session=abc"
    assert recorded.header("authorization") == "Bearer aqs_token"
    assert recorded.header("x-forwarded-for") is None
    assert recorded.raw_path == b"/ws/terminal/s1"
    assert recorded.query_string == query.encode()


async def test_upstream_refusal_denies_the_browser_handshake(stack):
    with pytest.raises(InvalidStatus) as refused:
        async with ws_connect(f"{stack.ws_url}/ws/refuse?code=4403"):
            pytest.fail("the browser's handshake was accepted")
    response = refused.value.response
    assert response.status_code == 403
    assert response.headers[DASHBOARD_SERVER_HEADER] == VERSION
    assert json.loads(response.body)["error"] == "daemon_refused"
    assert stack.daemon.websockets[0].refused
    await no_proxy_tasks()


async def test_upstream_refusal_closes_before_accept_without_the_response_extension(stack):
    """A server without ``websocket.http.response`` gets close-before-accept."""
    sent: list[dict[str, Any]] = []

    async def receive():
        raise AssertionError("a refused handshake reads nothing from the browser")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/ws/refuse",
        "raw_path": b"/ws/refuse",
        "query_string": b"",
        "headers": [(b"host", stack.host.encode())],
        "subprotocols": [],
    }
    await asyncio.wait_for(stack.proxy.websocket(scope, receive, send), GUARD)
    assert [message["type"] for message in sent] == ["websocket.close"]


async def test_websocket_relays_when_the_caller_already_read_the_connect_message():
    daemon = FakeDaemon()
    async with serve_asgi(daemon.app) as daemon_url:
        proxy = DaemonProxy(daemon_url, version=VERSION)
        inner = proxy_asgi(proxy)

        async def gated(scope, receive, send):
            # An edge gate that inspects the handshake reads websocket.connect first.
            if scope["type"] == "websocket":
                assert (await receive())["type"] == "websocket.connect"
            await inner(scope, receive, send)

        async with (
            serve_asgi(gated) as url,
            ws_connect("ws" + url[len("http") :] + "/ws/echo") as ws,
        ):
            await ws.send(b"\x01\x02")
            assert await asyncio.wait_for(ws.recv(), GUARD) == b"\x01\x02"


async def test_close_code_and_reason_from_the_daemon_reach_the_browser(stack):
    async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
        await ws.send("close:4408:Terminal output acknowledgement timed out")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), GUARD)
        assert ws.close_code == 4408
        assert ws.close_reason == "Terminal output acknowledgement timed out"
    await no_proxy_tasks()


async def test_close_code_and_reason_from_the_browser_reach_the_daemon(stack):
    async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
        await ws.send("hi")
        assert await asyncio.wait_for(ws.recv(), GUARD) == "hi"
        await ws.close(code=4000, reason="tab closed")
    recorded = stack.daemon.websockets[0]
    await asyncio.wait_for(recorded.closed.wait(), GUARD)
    assert recorded.close_code == 4000
    assert recorded.close_reason == "tab closed"
    await no_proxy_tasks()


async def test_a_browser_that_vanishes_closes_the_daemon_side_too(stack):
    """A lost connection (no close frame) still ends the daemon's socket.

    It arrives as ``1005`` from uvicorn's websockets-sansio protocol (so the
    daemon gets ``1000``) and as ``1006`` from servers that report abnormal
    closure (so the daemon gets ``1011``); neither code may be sent as is.
    """
    async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
        await ws.send("hi")
        assert await asyncio.wait_for(ws.recv(), GUARD) == "hi"
        ws.transport.abort()
    recorded = stack.daemon.websockets[0]
    await asyncio.wait_for(recorded.closed.wait(), GUARD)
    assert recorded.close_code in (1000, 1011)
    await no_proxy_tasks()


async def test_websocket_to_an_unreachable_daemon_is_denied_503():
    api_url = f"http://127.0.0.1:{unused_port()}"
    proxy = DaemonProxy(api_url, version=VERSION)
    async with serve_asgi(proxy_asgi(proxy)) as url:
        with pytest.raises(InvalidStatus) as denied:
            async with ws_connect("ws" + url[len("http") :] + "/ws/events?after_seq=0"):
                pytest.fail("the browser's handshake was accepted")
    response = denied.value.response
    assert response.status_code == 503
    assert response.headers[DASHBOARD_SERVER_HEADER] == VERSION
    assert response.headers["retry-after"] == "2"
    assert json.loads(response.body) == {
        "ok": False,
        "error": "daemon_unreachable",
        "api_url": api_url,
    }


async def test_proxy_close_closes_a_live_websocket_1001_both_ways(stack):
    async with ws_connect(f"{stack.ws_url}/ws/echo") as ws:
        await ws.send("hi")
        assert await asyncio.wait_for(ws.recv(), GUARD) == "hi"
        await asyncio.wait_for(stack.proxy.close(), GUARD)
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), GUARD)
        assert ws.close_code == 1001
    recorded = stack.daemon.websockets[0]
    await asyncio.wait_for(recorded.closed.wait(), GUARD)
    assert recorded.close_code == 1001
    await no_proxy_tasks()


async def test_stalled_browser_blocks_the_daemon_sender(stack):
    """No queue in the proxy: a browser that stops reading stops the daemon.

    The daemon tries to send 128 MiB while the browser reads nothing.  A proxy
    that queued would let every send complete; this one stops reading upstream
    once its send downstream blocks, so the sends that complete are bounded by
    socket buffers (a few MiB here; never more than tcp_wmem + tcp_rmem on
    each hop) -- far below half the flood, which is what is asserted.  Reading
    again resumes the flow, every frame intact and in order.
    """
    daemon = stack.daemon
    count, size = 2048, 64 * 1024
    async with ws_connect(
        f"{stack.ws_url}/ws/flood?count={count}&size={size}",
        max_queue=(2, 1),
        max_size=2 * size,
        compression=None,
    ) as ws:
        # Wait until the daemon's sends stop making progress.
        async with asyncio.timeout(10):
            last, quiet = -1, 0
            while quiet < 5:
                await asyncio.sleep(0.05)
                quiet = quiet + 1 if daemon.flood_sent == last else 0
                last = daemon.flood_sent
        stalled_at = daemon.flood_sent
        assert not daemon.flood_done.is_set()
        assert stalled_at < count // 2, f"{stalled_at} of {count} frames left the daemon"

        # Reading again lets the daemon continue, frames intact and in order.
        for index in range(count):
            frame = await asyncio.wait_for(ws.recv(), GUARD)
            assert isinstance(frame, bytes) and len(frame) == size
            assert int.from_bytes(frame[:4], "big") == index
        await asyncio.wait_for(daemon.flood_done.wait(), GUARD)
    recorded = daemon.websockets[0]
    await asyncio.wait_for(recorded.closed.wait(), GUARD)
    assert recorded.close_code == 1000
    await no_proxy_tasks()
