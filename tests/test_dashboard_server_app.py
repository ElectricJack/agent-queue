"""The dashboard server as a whole: routing, identity, startup and `aq dashboard serve`.

The proxy's relaying is covered by ``tests/test_dashboard_server_proxy.py`` and
the gates by ``tests/test_dashboard_server_edge.py``; this file proves the
dispatcher sends each path to the right one (docs/specs/dashboard-server.md
§1, §2.1, §4), that startup fails closed, and -- end to end, over real sockets
-- that the process serves the bundle and a deep route and proxies ``/api``,
``/health``, ``/ready`` and a WebSocket to a running daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient, WebSocketDenialResponse
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from src.dashboard_server import __main__ as server_main
from src.dashboard_server.app import DashboardServerApp, classify, create_app
from src.dashboard_server.settings import (
    DashboardServerSettings,
    SettingsError,
    load_settings,
)
from tests.dashboard_server_helpers import (
    TERMINAL_PROTOCOL,
    FakeDaemon,
    serve_asgi,
    unused_port,
)

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = '<!doctype html><script type="module" src="/assets/app.js"></script>'
OWN_HEADER = "x-aq-dashboard-server"


@pytest.fixture(autouse=True)
def _pg_backend():
    """The dashboard server has no database; neither do its tests."""


def _release_builder():
    spec = importlib.util.spec_from_file_location(
        "build_release_artifact", ROOT / "scripts" / "build_release_artifact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage_bundle(tmp_path: Path, **kwargs: Any) -> Path:
    """A bundle exactly as the release script stages it."""
    source = tmp_path / "vite-dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (source / "assets" / "app.js").write_text("console.log('AQ')", encoding="utf-8")
    destination = tmp_path / "package-data"
    _release_builder().stage_dashboard(source, destination, version="9.9.9", **kwargs)
    return destination


def _manifest_digest(bundle: Path) -> str:
    return hashlib.sha256((bundle / "aq-dashboard-manifest.json").read_bytes()).hexdigest()


class StubProxy:
    """Stands in for DaemonProxy: records what reached it and answers 299."""

    def __init__(self, upstream_ok: bool = True) -> None:
        self.http_paths: list[str] = []
        self.ws_paths: list[str] = []
        self.started = 0
        self.closed = 0
        self._upstream_ok = upstream_ok

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def upstream_ok(self) -> bool:
        return self._upstream_ok

    async def http(self, scope, receive, send) -> None:
        self.http_paths.append(scope["path"])
        await send({"type": "http.response.start", "status": 299, "headers": []})
        await send({"type": "http.response.body", "body": b"proxied"})

    async def websocket(self, scope, receive, send) -> None:
        self.ws_paths.append(scope["path"])
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": "proxied"})
        await send({"type": "websocket.close", "code": 1000})


def _app(tmp_path: Path, **settings: Any) -> tuple[DashboardServerApp, StubProxy]:
    proxy = StubProxy()
    config = DashboardServerSettings(bundle_directory=stage_bundle(tmp_path), **settings)
    return create_app(config, proxy=proxy, version="1.2.3"), proxy  # type: ignore[arg-type]


def _client(app: DashboardServerApp, peer: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer, 50000))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8082")


# -- Routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/__aq/health", "identity"),
        ("/__aq", "reserved"),
        ("/__aq/other", "reserved"),
        ("/api", "proxy"),
        ("/api/tasks", "proxy"),
        ("/health", "proxy"),
        ("/ready", "proxy"),
        ("/ws", "proxy"),
        ("/ws/events", "proxy"),
        ("/apix", "static"),
        ("/healthz", "static"),
        ("/wsx/y", "static"),
        ("/mcp", "not_served"),
        ("/mcp/sse", "not_served"),
        ("/docs", "not_served"),
        ("/redoc", "not_served"),
        ("/openapi.json", "not_served"),
        ("/plans/abc", "not_served"),
        ("/dashboard", "not_served"),
        ("/dashboard/tasks", "not_served"),
        ("/", "static"),
        ("/tasks/abc", "static"),
    ],
)
def test_the_path_table_matches_on_segment_boundaries(path, kind):
    assert classify(path) == kind


async def test_the_bundle_and_every_deep_route_answer_the_same_index(tmp_path):
    app, proxy = _app(tmp_path)
    async with _client(app) as client:
        pages = [await client.get(path) for path in ("/", "/tasks", "/projects/p/graph", "/apix")]
        asset = await client.get("/assets/app.js")
        missing = await client.get("/nope.js")
    for page in pages:
        assert page.status_code == 200
        assert page.text == INDEX_HTML
        assert page.headers[OWN_HEADER] == "1.2.3"
    assert asset.text == "console.log('AQ')"
    assert missing.status_code == 404
    assert missing.headers[OWN_HEADER] == "1.2.3"
    assert proxy.http_paths == []


@pytest.mark.parametrize(
    "path",
    ["/mcp", "/mcp/x", "/docs", "/redoc", "/openapi.json", "/plans/p", "/dashboard", "/dashboard/",
     "/dashboard/tasks", "/__aq", "/__aq/nope"],
)
async def test_the_daemons_other_surfaces_are_a_json_404_never_html(tmp_path, path):
    app, proxy = _app(tmp_path)
    async with _client(app) as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"] == "not_found"
    assert response.headers[OWN_HEADER] == "1.2.3"
    assert proxy.http_paths == []


@pytest.mark.parametrize("path", ["/api", "/api/tasks?x=1", "/health", "/ready", "/ws/events"])
async def test_the_proxied_prefixes_reach_the_proxy(tmp_path, path):
    app, proxy = _app(tmp_path)
    async with _client(app) as client:
        response = await client.get(path)
    assert response.status_code == 299
    assert proxy.http_paths == [path.split("?")[0]]


async def _raw_get(app: DashboardServerApp, raw_path: bytes) -> tuple[int, dict]:
    """One GET with the request target exactly as given (clients normalise dot segments)."""
    from urllib.parse import unquote

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": unquote(raw_path.decode()), "raw_path": raw_path,
        "query_string": b"", "root_path": "", "headers": [(b"host", b"127.0.0.1:8082")],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8082),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(body)


@pytest.mark.parametrize(
    "raw", [b"/api/../mcp", b"/api/./tasks", b"/api/%2e%2e/mcp", b"/ws/%2E/x", b"/api/x/.."],
)
async def test_a_dot_segment_under_a_proxied_prefix_is_a_400(tmp_path, raw):
    app, proxy = _app(tmp_path)
    status, body = await _raw_get(app, raw)
    assert (status, body["error"]) == (400, "bad_path")
    assert proxy.http_paths == []


async def test_an_edge_denial_never_reaches_the_daemon(tmp_path):
    app, proxy = _app(tmp_path)
    async with _client(app) as client:
        rebound = await client.get("/api/tasks", headers={"host": "evil.example:8082"})
        foreign = await client.get("/api/tasks", headers={"origin": "http://evil.example"})
    assert (rebound.status_code, rebound.json()["error"]) == (421, "misdirected_host")
    assert (foreign.status_code, foreign.json()["error"]) == (403, "origin_not_allowed")
    assert rebound.headers[OWN_HEADER] == "1.2.3"
    assert proxy.http_paths == []


async def test_a_lan_peer_is_refused_bearer_tokens_but_not_the_api(tmp_path):
    app, proxy = _app(tmp_path, host="192.168.1.5")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.168.1.9", 50000)),
        base_url="http://192.168.1.5:8082",
    ) as client:
        plain = await client.get("/api/tasks")
        bearer = await client.get("/api/tasks", headers={"authorization": "Bearer aqs_x"})
    assert plain.status_code == 299
    assert (bearer.status_code, bearer.json()["error"]) == (403, "loopback_only")
    assert proxy.http_paths == ["/api/tasks"]


async def test_the_identity_endpoint_names_the_process_and_its_bundle(tmp_path):
    app, _ = _app(tmp_path, api_url="http://127.0.0.1:9999")
    async with _client(app) as client:
        health = await client.get("/__aq/health")
        head = await client.head("/__aq/health")
        post = await client.post("/__aq/health")
    body = health.json()
    assert body == {
        "service": "aq-dashboard-server",
        "version": "1.2.3",
        "pid": os.getpid(),
        "bundle": {
            "version": "9.9.9",
            "files": 2,
            "verified": True,
            "manifest_sha256": _manifest_digest(tmp_path / "package-data"),
        },
        "api_url": "http://127.0.0.1:9999",
        "upstream_ok": True,
    }
    assert health.headers[OWN_HEADER] == "1.2.3"
    assert head.status_code == 200 and head.content == b""
    assert post.status_code == 405


def test_websockets_are_proxied_only_under_ws_and_only_past_the_gates(tmp_path):
    app, proxy = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8082") as client:
        local = {"host": "127.0.0.1:8082"}
        with client.websocket_connect("/ws/events", headers=local) as ws:
            assert ws.receive_text() == "proxied"
        for path, headers, status in (
            ("/api/tasks", local, 404),
            ("/", local, 404),
            ("/ws/events", {**local, "origin": "http://evil.example"}, 403),
            ("/ws/events", {"host": "evil.example"}, 421),
        ):
            with (
                pytest.raises(WebSocketDenialResponse) as denied,
                client.websocket_connect(path, headers=headers),
            ):
                pass
            assert denied.value.status_code == status
            assert denied.value.headers[OWN_HEADER] == "1.2.3"
    assert proxy.ws_paths == ["/ws/events"]
    assert (proxy.started, proxy.closed) == (1, 1)


def test_create_app_fails_closed(tmp_path):
    bundle = stage_bundle(tmp_path)
    (bundle / "assets" / "app.js").write_text("altered", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        create_app(DashboardServerSettings(bundle_directory=bundle), proxy=StubProxy())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        create_app(DashboardServerSettings(bundle_directory=tmp_path / "absent"))


# -- Settings ----------------------------------------------------------------


def test_settings_come_from_the_config_file_and_the_flags_win(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_server: {host: 127.0.0.1, port: 9001}\n"
        "dashboard: {server: {host: '::1', port: 9002}}\n"
        "api_auth: {trusted_dashboard_origins: ['https://aq.example.com']}\n",
        encoding="utf-8",
    )
    settings = load_settings(config, environ={})
    assert (settings.host, settings.port) == ("::1", 9002)
    assert settings.api_url == "http://127.0.0.1:9001"
    assert settings.trusted_origins == ("https://aq.example.com",)
    assert settings.url == "http://[::1]:9002/"

    assert load_settings(config, environ={"AQ_API_URL": "http://10.0.0.2:81/"}).api_url == (
        "http://10.0.0.2:81"
    )
    flagged = load_settings(
        config, environ={"AQ_API_URL": "http://10.0.0.2:81"}, host="127.0.0.1", port=9003,
        api_url="http://127.0.0.1:9004",
    )
    assert (flagged.host, flagged.port, flagged.api_url) == (
        "127.0.0.1", 9003, "http://127.0.0.1:9004",
    )


def test_a_missing_config_file_means_defaults(tmp_path):
    settings = load_settings(tmp_path / "absent.yaml", environ={})
    assert (settings.host, settings.port, settings.api_url) == (
        "127.0.0.1", 8082, "http://127.0.0.1:8081",
    )
    assert DashboardServerSettings(host="0.0.0.0").url == "http://127.0.0.1:8082/"


def test_the_default_port_steps_aside_for_a_daemon_on_it_as_the_daemon_resolves_it(tmp_path):
    """The process and ``load_config`` share the rule, so ``aq status`` and
    ``aq doctor`` name the port the server actually binds."""
    config = tmp_path / "config.yaml"
    config.write_text("mcp_server: {port: 8082}\n", encoding="utf-8")
    settings = load_settings(config, environ={})
    assert (settings.port, settings.api_url) == (8083, "http://127.0.0.1:8082")
    with pytest.raises(SettingsError, match=r"mcp_server\.port \(8082\)"):
        load_settings(config, environ={}, port=8082)


@pytest.mark.parametrize(
    ("text", "overrides", "fragment"),
    [
        ("dashboard: {server: {port: 8081}}\n", {}, "dashboard.server.port"),
        ("", {"port": 8081}, "mcp_server.port"),
        ("", {"host": "dashboard.example.com"}, "dashboard.server.host"),
        ("", {"api_url": "ftp://x"}, "api_url"),
        ("api_auth: {trusted_dashboard_origins: ['*']}\n", {}, "trusted_dashboard_origins"),
        ("dashboard: [1, 2\n", {}, "not valid YAML"),
    ],
)
def test_a_bad_setting_names_its_key(tmp_path, text, overrides, fragment):
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError, match=fragment.replace(".", r"\.")):
        load_settings(config, environ={}, **overrides)


# -- Process entry point -----------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_no_bundle_exits_2_pointing_at_vite_and_the_installer(tmp_path, capsys):
    code = server_main.main([
        "--config", str(tmp_path / "absent.yaml"), "--bundle-dir", str(tmp_path / "none"),
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "npm -w dashboard run dev" in err
    assert "aq install --restart-from dashboard.build" in err


def test_a_bundle_that_does_not_verify_exits_1_with_the_reason(tmp_path, capsys):
    bundle = stage_bundle(tmp_path)
    manifest = json.loads((bundle / "aq-dashboard-manifest.json").read_text(encoding="utf-8"))
    manifest.pop("base")
    (bundle / "aq-dashboard-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    code = server_main.main([
        "--config", str(tmp_path / "absent.yaml"), "--bundle-dir", str(bundle),
    ])
    assert code == 1
    assert "failed verification" in capsys.readouterr().err


def test_a_busy_port_is_a_startup_failure_naming_the_key_never_the_next_port(tmp_path, capsys):
    bundle = stage_bundle(tmp_path)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        code = server_main.main([
            "--config", str(tmp_path / "absent.yaml"), "--bundle-dir", str(bundle),
            "--port", str(port),
        ])
    assert code == 1
    assert "dashboard.server.port" in capsys.readouterr().err


def test_a_bad_setting_exits_1(tmp_path, capsys):
    code = server_main.main(["--config", str(tmp_path / "absent.yaml"), "--port", "8081"])
    assert code == 1
    assert "mcp_server.port" in capsys.readouterr().err


# -- aq dashboard serve --------------------------------------------------------


def test_aq_dashboard_serve_hands_its_flags_and_the_global_api_url_to_the_server(monkeypatch):
    from src.cli.app import cli

    calls: list[list[str]] = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(server_main, "main", fake_main)
    runner = CliRunner()
    result = runner.invoke(cli, ["dashboard", "serve", "--host", "::1", "--port", "9100"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli, ["dashboard", "serve", "--api-url", "http://127.0.0.1:9200"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ["--host", "::1", "--port", "9100"],
        ["--api-url", "http://127.0.0.1:9200"],
    ]


def test_aq_dashboard_serve_exits_with_the_servers_status_and_refuses_json(monkeypatch):
    from src.cli.app import cli

    monkeypatch.setattr(server_main, "main", lambda argv: 2)
    runner = CliRunner()
    assert runner.invoke(cli, ["dashboard", "serve"]).exit_code == 2
    assert runner.invoke(cli, ["dashboard", "serve", "--json"]).exit_code == 2
    assert runner.invoke(cli, ["dashboard", "serve", "--port", "0"]).exit_code == 2


def test_the_generated_state_commands_still_live_in_the_dashboard_group():
    from src.cli.app import cli

    group = cli.commands["dashboard"]
    assert {"serve", "state-get", "state-list", "state-put", "state-reset"} <= set(group.commands)


# -- Import boundary (spec §1) -----------------------------------------------


def test_the_dashboard_server_imports_nothing_from_the_daemon():
    probe = (
        "import sys, src.dashboard_server.app, src.dashboard_server.__main__;"
        "bad = [m for m in sys.modules if m == 'fastapi' or m.startswith("
        "('fastapi.', 'src.api', 'src.orchestrator', 'src.database', 'src.commands'))];"
        "print(bad)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "[]"


# -- End to end over real sockets --------------------------------------------


@asynccontextmanager
async def _served(tmp_path: Path, api_url: str) -> AsyncIterator[str]:
    """The real app (real DaemonProxy) under uvicorn, pointed at ``api_url``."""
    settings = DashboardServerSettings(bundle_directory=stage_bundle(tmp_path), api_url=api_url)
    async with serve_asgi(create_app(settings, version="1.2.3")) as url:
        yield url


def _ws(url: str) -> str:
    return "ws" + url.removeprefix("http")


async def test_the_server_serves_the_bundle_and_relays_the_daemon(tmp_path):
    """Acceptance: the bundle and a deep route are 200; /api, /health, /ready are the daemon's."""
    daemon = FakeDaemon()
    async with (
        serve_asgi(daemon.app) as daemon_url,
        _served(tmp_path, daemon_url) as url,
        aiohttp.ClientSession() as client,
    ):
        for path in ("/", "/tasks/abc", "/projects/p/graph"):
            async with client.get(url + path) as page:
                assert (page.status, await page.text()) == (200, INDEX_HTML)
        async with client.get(url + "/api/echo/a%2Fb?x=1&y=%20") as echo:
            body = await echo.json()
            assert echo.status == 200 and OWN_HEADER not in echo.headers
        assert (body["raw_path"], body["query_string"]) == ("/api/echo/a%2Fb", "x=1&y=%20")
        assert dict(body["headers"])["host"] == url.removeprefix("http://")
        for path in ("/health", "/ready"):
            async with client.get(url + path) as probe:
                assert (probe.status, await probe.json()) == (200, {"status": "ok"})
        # An error from the daemon is the daemon's, untouched.
        async with client.get(url + "/api/status/500") as failed:
            assert failed.status == 500
            assert await failed.json() == {"ok": False, "error": "status_500"}
            assert OWN_HEADER not in failed.headers
        # A streamed response: the first event arrives while the daemon still holds the second.
        async with client.get(url + "/api/sse") as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            first = await asyncio.wait_for(stream.content.readuntil(b"\n\n"), 5)
            assert first == b"data: first\n\n" and not daemon.sse_release.is_set()
            daemon.sse_release.set()
            assert await asyncio.wait_for(stream.content.read(), 5) == b"data: second\n\n"
        async with client.get(url + "/__aq/health") as identity:
            assert (await identity.json())["upstream_ok"] is True


async def test_a_websocket_echoes_through_the_server_with_its_origin_and_host_intact(tmp_path):
    daemon = FakeDaemon()
    async with serve_asgi(daemon.app) as daemon_url, _served(tmp_path, daemon_url) as url:
        async with connect(
            _ws(url) + "/ws/terminal/s1?cols=80&rows=24",
            origin=url,
            subprotocols=[TERMINAL_PROTOCOL],
        ) as ws:
            assert ws.subprotocol == TERMINAL_PROTOCOL
            await ws.send("hello")
            assert await asyncio.wait_for(ws.recv(), 5) == "hello"
            await ws.send(b"\x00\x01binary")
            assert await asyncio.wait_for(ws.recv(), 5) == b"\x00\x01binary"
        recorded = daemon.websockets[0]
        assert recorded.header("origin") == url
        assert recorded.header("host") == url.removeprefix("http://")
        assert recorded.query_string == b"cols=80&rows=24"

        # A foreign Origin is refused at the edge: the daemon is never contacted.
        with pytest.raises(InvalidStatus) as refused:
            async with connect(_ws(url) + "/ws/echo", origin="http://evil.example"):
                pass
        assert refused.value.response.status_code == 403
        assert len(daemon.websockets) == 1


async def test_with_the_daemon_down_the_page_loads_and_the_api_says_why(tmp_path):
    async with _served(tmp_path, f"http://127.0.0.1:{unused_port()}") as url:
        async with aiohttp.ClientSession() as client:
            for path in ("/", "/tasks"):
                async with client.get(url + path) as page:
                    assert page.status == 200
            async with client.get(url + "/api/health") as down:
                assert down.status == 503
                assert (await down.json())["error"] == "daemon_unreachable"
                assert down.headers["retry-after"] == "2"
                assert down.headers["cache-control"] == "no-store"
                assert down.headers[OWN_HEADER] == "1.2.3"
            async with client.get(url + "/__aq/health") as identity:
                assert (await identity.json())["upstream_ok"] is False
        with pytest.raises(InvalidStatus) as denied:
            async with connect(_ws(url) + "/ws/events", origin=url):
                pass
        assert denied.value.response.status_code == 503


async def test_the_process_serves_proxies_and_stops_on_sigterm(tmp_path):
    """``python -m src.dashboard_server`` -- what `aq dashboard serve` runs -- end to end."""
    daemon = FakeDaemon()
    bundle = stage_bundle(tmp_path)
    port = unused_port()
    url = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AQ_", "AGENT_QUEUE_"))}
    async with serve_asgi(daemon.app) as daemon_url:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "src.dashboard_server",
            "--config", str(tmp_path / "absent.yaml"), "--bundle-dir", str(bundle),
            "--port", str(port), "--api-url", daemon_url, "--log-level", "warning",
            cwd=ROOT, env=env, stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with aiohttp.ClientSession() as client:
                identity = await _wait_for_identity(client, url, process)
                assert identity["pid"] == process.pid
                assert identity["bundle"] == {
                    "version": "9.9.9",
                    "files": 2,
                    "verified": True,
                    "manifest_sha256": _manifest_digest(bundle),
                }
                async with client.get(url + "/settings/messaging") as deep:
                    assert (deep.status, await deep.text()) == (200, INDEX_HTML)
                async with client.get(url + "/api/echo") as echo:
                    assert echo.status == 200
                async with client.get(url + "/ready") as ready:
                    assert ready.status == 200
            async with connect(_ws(url) + "/ws/echo", origin=url) as ws:
                await ws.send("ping")
                assert await asyncio.wait_for(ws.recv(), 5) == "ping"
                process.send_signal(signal.SIGTERM)
                with pytest.raises(ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 5)
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 1001
            assert await asyncio.wait_for(process.wait(), 6) in (0, -signal.SIGTERM)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def _wait_for_identity(client, url: str, process) -> dict:
    for _ in range(150):
        if process.returncode is not None:
            stderr = (await process.stderr.read()).decode(errors="replace")
            pytest.fail(f"dashboard server exited {process.returncode}: {stderr}")
        try:
            async with client.get(url + "/__aq/health") as response:
                if response.status == 200:
                    return await response.json()
        except aiohttp.ClientConnectionError:
            pass
        await asyncio.sleep(0.1)
    pytest.fail("dashboard server did not answer /__aq/health within 15 s")
