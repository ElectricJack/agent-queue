"""Daemon is API-only: no static dashboard mount, and /dashboard points elsewhere.

docs/specs/dashboard-server.md §5: ``src/api/app.py`` drops the
``mount_dashboard`` call; ``GET /dashboard`` and ``/dashboard/{rest}`` answer
with a machine-readable body naming the configured dashboard server URL -- a
``307`` to ``<dashboard_url><rest>?<query>`` while ``dashboard.server.enabled``
is true, a ``404`` with ``dashboard_url: null`` when it is false.  These cases
pin that answer and the daemon's route table against regressing into a
static-file mount.  The pre-change updater's probe through the redirect is in
``tests/test_update.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from tests.db_fixtures import lease_dsn

POINTER_BODY = {
    "ok": False,
    "error": "dashboard_not_served_here",
    "dashboard_url": "http://127.0.0.1:8082/",
    "hint": "The daemon is API-only. Run `aq dashboard status`.",
}


def test_daemon_startup_cannot_import_or_host_the_dashboard(tmp_path):
    """Exercise the real API/MCP assembly in a clean interpreter, without sockets or DB IO.

    A sys.modules check in pytest would be polluted by dashboard/CLI tests.
    Record even attempted imports here: an optional import caught by startup
    must fail this guard too. Only uvicorn's socket-serving method is replaced;
    the daemon entrypoint imports, app factory, MCP mount and lifespans are real.
    """
    probe = r'''
import asyncio
import importlib.abc
import inspect
from pathlib import Path
import sys
from unittest.mock import patch

forbidden = ("src.dashboard_server", "src.dashboard_assets.runtime")
attempts = []

class NoDashboard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in forbidden):
            attempts.append(fullname)
            raise ImportError("dashboard code in daemon: " + fullname)

sys.meta_path.insert(0, NoDashboard())

import src.main
from src.config import AppConfig, DatabaseConfig
from src.embedded_mcp import run_mcp_server
from src.orchestrator import Orchestrator
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles
import uvicorn

async def check():
    root = Path(sys.argv[1])
    config = AppConfig(
        data_dir=str(root / "data"), workspace_dir=str(root / "work"),
        database=DatabaseConfig(url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"),
    )
    orch = Orchestrator(config)
    shutdown = asyncio.Event()
    errors = []
    served = []
    # Plan markdown is data; the daemon declares no HTML endpoint.
    allowed_html_routes = set()
    html_routes = set()

    def effective_routes(app):
        for route in getattr(app, "routes", ()):
            contexts = getattr(route, "effective_route_contexts", None)
            if callable(contexts):
                # New FastAPI versions retain lazy include wrappers. Contexts
                # carry combined prefixes and inherited response classes;
                # mounts instead live on the resolved Starlette route.
                for context in contexts():
                    yield context.starlette_route or context
                for context in route.effective_low_priority_routes():
                    yield context.starlette_route or context
            else:
                yield route

    def check_routes(app, prefix=""):
        assert not isinstance(app, StaticFiles), type(app)
        assert not type(app).__module__.startswith(forbidden), type(app)
        for route in effective_routes(app):
            path = prefix + route.path
            if isinstance(route, Mount):
                check_routes(route.app, path)
            endpoint = getattr(route, "endpoint", None)
            response_class = getattr(route, "response_class", None)
            returns_html = isinstance(response_class, type) and issubclass(
                response_class, HTMLResponse
            )
            if endpoint is not None:
                returns_html |= "HTMLResponse" in str(inspect.signature(endpoint).return_annotation)
            if returns_html:
                assert path in allowed_html_routes, path
                html_routes.add(path)

    # Prove that lazy includes are inspected, not silently skipped. Even an
    # otherwise allowed path is forbidden under an include prefix, including
    # when the HTML response class is inherited from the containing router.
    nested = APIRouter(default_response_class=HTMLResponse)
    nested.add_api_route("/plans/{task_id}", lambda: "unexpected HTML")
    parent = APIRouter()
    parent.include_router(nested, prefix="/nested")
    invalid_app = FastAPI(docs_url=None, redoc_url=None)
    invalid_app.include_router(parent, prefix="/unexpected")
    try:
        check_routes(invalid_app)
    except AssertionError as exc:
        assert str(exc) == "/unexpected/nested/plans/{task_id}", exc
    else:
        raise AssertionError("guard missed HTML in a nested include")

    async def plan_content(task_id):
        return "# Legacy plan"

    async def serve(server, *args, **kwargs):
        try:
            app = server.config.app
            check_routes(app)
            assert html_routes == allowed_html_routes, html_routes
            assert app.docs_url is None and app.redoc_url is None
            async with app.router.lifespan_context(app):
                async with AsyncClient(
                    transport=ASGITransport(app), base_url="http://127.0.0.1:8081",
                    follow_redirects=False,
                ) as client:
                    for path in ("/", "/index.html", "/assets/app.js", "/focus", "/docs", "/redoc"):
                        response = await client.get(path)
                        assert response.status_code == 404, (path, response.status_code)
                        assert "text/html" not in response.headers.get("content-type", ""), path
                    for path in ("/dashboard", "/dashboard/index.html", "/dashboard/assets/app.js"):
                        response = await client.get(path)
                        assert response.status_code == 307, (path, response.status_code)
                        assert response.json()["error"] == "dashboard_not_served_here"
                        assert response.headers["location"].startswith("http://127.0.0.1:8082/")
                    response = await client.get("/api/plans/solid-grove.13")
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("application/json")
                    assert response.json()["content"] == "# Legacy plan"
                    for path, status in (
                        ("/plans", 404), ("/plans/solid-grove/extra", 404),
                        ("/plans/solid..grove", 404),
                        ("/plans/solid-grove.13", 404),
                        ("/api/plans/solid..grove", 400),
                    ):
                        response = await client.get(path)
                        assert response.status_code == status, (path, response.status_code)
                        assert "text/html" not in response.headers.get("content-type", ""), path
            served.append(True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            shutdown.set()

    # Startup cannot hide a dashboard launch behind a new subprocess either.
    with patch.object(uvicorn.Server, "serve", serve), patch(
        "subprocess.Popen", side_effect=AssertionError("daemon launched a hosting process")
    ):
        await asyncio.wait_for(
            run_mcp_server(orch, config, shutdown, plan_content_provider=plan_content), timeout=60
        )
    assert not errors, errors
    assert served == [True], served
    assert not attempts, attempts
    assert not [name for name in sys.modules if name.startswith(forbidden)]

asyncio.run(check())
'''
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@asynccontextmanager
async def daemon_app(
    tmp_path: Path, *, dashboard_server: Any = None, name: str = "dashboard_pointer.db",
    health_provider: Any = None, plan_content_provider: Any = None,
) -> AsyncIterator[Any]:
    """A real ``create_app()`` application over a PostgreSQL-backed orchestrator.

    Mirrors the fixture in ``tests/test_api_client_contract.py`` so the
    daemon's actual route table (not a hand-built one) is what is inspected.
    ``dashboard_server`` is a :class:`DashboardServerConfig`; the default is the
    shipped one (enabled, ``127.0.0.1:8082``).
    """
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.database import Database
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    db = Database(lease_dsn(name))
    await db.initialize()
    options: dict[str, Any] = {}
    if dashboard_server is not None:
        options["dashboard_server"] = dashboard_server
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database=DatabaseConfig(url=lease_dsn(name)),
        data_dir=str(tmp_path / "d"),
        **options,
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus()
    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
        deps._health_provider,
        deps._health_monitor,
        deps._plan_content_provider,
    )
    try:
        yield create_app(
            orch, config, health_provider=health_provider,
            plan_content_provider=plan_content_provider,
        )
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
            deps._health_provider,
            deps._health_monitor,
            deps._plan_content_provider,
        ) = saved
        await db.close()


@pytest.fixture
async def live_app(tmp_path):
    async with daemon_app(tmp_path) as app:
        yield app


async def test_api_lifespan_starts_health_collection_and_cancels_it(tmp_path):
    import asyncio
    from httpx import ASGITransport, AsyncClient
    from src.api import dependencies as deps

    started = asyncio.Event()
    stopped = asyncio.Event()

    async def provider():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with daemon_app(tmp_path, health_provider=provider) as app:
        monitor = deps._health_monitor
        monitor.timeout = 60
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), 5)
            assert monitor._task is not None
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                response = await client.get("/health")
            assert response.status_code == 503
            assert response.json()["status"] == "busy"
            assert not stopped.is_set()
        assert stopped.is_set() and monitor._task is None


async def test_api_restart_restores_websocket_event_subscription(tmp_path):
    from src.api import dependencies as deps

    async with daemon_app(tmp_path) as app:
        bus = deps._orchestrator.bus
        assert len(bus._handlers["*"]) == 1
        for _ in range(2):
            async with app.router.lifespan_context(app):
                assert len(bus._handlers["*"]) == 1
            assert bus._handlers["*"] == []


def _server(**overrides: Any) -> Any:
    from src.config import DashboardServerConfig

    return DashboardServerConfig(**{"enabled": True, "host": "127.0.0.1", "port": 8082, **overrides})


async def test_daemon_registers_no_static_file_mount(live_app):
    """Acceptance: the daemon's route table holds no static-file mount.

    Before smart-meadow.4, the daemon mounted a ``StaticFiles`` app at
    /dashboard (a starlette ``Mount`` with a ``StaticFiles`` app).  The daemon
    is now API-only, and the dashboard lives in its own process.
    """
    app = live_app
    static_mounts = [
        route
        for route in app.router.routes
        if isinstance(route, Mount)
        and type(route.app).__module__.startswith(("starlette.", "fastapi."))
        and type(route.app).__name__ in ("StaticFiles", "_DashboardStaticFiles")
    ]
    assert static_mounts == [], f"daemon still serves a static-file mount: {static_mounts!r}"

    dashboard_routes = [
        route for route in app.router.routes if getattr(route, "path", "") in {"/dashboard"}
    ]
    # The /dashboard route now resolves to the pointer handler, not a Mount.
    assert all(not isinstance(route, Mount) for route in dashboard_routes)


async def test_daemon_disables_docs_ui_but_still_serves_openapi(live_app):
    """The API-only daemon offers the schema JSON but no generated HTML UI."""
    app = live_app
    assert app.docs_url is None
    assert app.redoc_url is None

    with TestClient(app) as client:
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            response = client.get(path, headers={"Accept": "text/html"})
            assert response.status_code == 404, path
            assert "text/html" not in response.headers.get("content-type", ""), path

        response = client.get("/openapi.json", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == app.openapi()


async def test_daemon_serves_no_text_html_routes(tmp_path):
    """Guard the complete app's declarations and the former live HTML endpoint."""
    async def plan_provider(task_id):
        return "# A real plan\n<script>alert('untrusted markdown')</script>"

    async with daemon_app(tmp_path, plan_content_provider=plan_provider) as app:
        def routes(router):
            for route in router.routes:
                contexts = getattr(route, "effective_route_contexts", None)
                if contexts is not None:
                    yield from contexts()
                elif isinstance(route, Mount) and hasattr(route.app, "routes"):
                    yield from routes(route.app)
                else:
                    yield route

        for route in routes(app):
            response_class = getattr(route, "response_class", None)
            response_class = getattr(response_class, "value", response_class)
            assert getattr(response_class, "media_type", None) != "text/html", route.path
        for path, operations in app.openapi()["paths"].items():
            for operation in operations.values():
                for response in operation.get("responses", {}).values():
                    assert "text/html" not in response.get("content", {}), path

        with TestClient(app, follow_redirects=False) as client:
            for path in ("/plans/solid-grove.13", "/docs", "/redoc"):
                response = client.get(path, headers={"accept": "text/html"})
                assert response.status_code == 404, path
                assert "text/html" not in response.headers.get("content-type", ""), path
            response = client.get("/api/plans/solid-grove.13", headers={"accept": "text/html"})
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
            assert response.json()["task_id"] == "solid-grove.13"
            assert response.json()["content"].startswith("# A real plan")


async def test_dashboard_paths_redirect_307_to_the_dashboard_server_with_the_pointer_body(
    live_app,
):
    """spec §5: ``307 Location: <dashboard_url><rest>?<query>`` plus the JSON pointer."""
    with TestClient(live_app, follow_redirects=False) as client:
        cases = {
            "/dashboard": "http://127.0.0.1:8082/",
            "/dashboard/": "http://127.0.0.1:8082/",
            "/dashboard/projects/example/graph": "http://127.0.0.1:8082/projects/example/graph",
            "/dashboard/tasks?focus=smart-meadow.9&tab=log": (
                "http://127.0.0.1:8082/tasks?focus=smart-meadow.9&tab=log"
            ),
        }
        for path, location in cases.items():
            response = client.get(path)
            assert response.status_code == 307, f"{path} -> {response.status_code}"
            assert response.headers["location"] == location, path
            assert response.json() == POINTER_BODY, path

            head = client.head(path)
            assert head.status_code == 307, f"HEAD {path} -> {head.status_code}"
            assert head.headers["location"] == location, path


async def test_the_redirect_names_the_configured_server_never_the_request_host(tmp_path):
    """``dashboard_url`` comes from ``dashboard.server`` alone; a wildcard bind is 127.0.0.1."""
    async with daemon_app(tmp_path, dashboard_server=_server(host="0.0.0.0", port=9444)) as app:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get(
                "/dashboard/", headers={"Host": "attacker.example:8081"}
            )
            assert response.status_code == 307
            assert response.headers["location"] == "http://127.0.0.1:9444/"
            assert response.json()["dashboard_url"] == "http://127.0.0.1:9444/"


async def test_the_redirect_path_is_re_encoded_and_cannot_leave_the_dashboard_server(live_app):
    """The path arrives decoded: it is encoded again, and a ``//host`` stays a path."""
    with TestClient(live_app, follow_redirects=False) as client:
        cases = {
            "/dashboard/a%20b/c%25d": "http://127.0.0.1:8082/a%20b/c%25d",
            "/dashboard/x%09y": "http://127.0.0.1:8082/x%09y",
            # "//evil.example" after the authority would still be a path, but it
            # is kept relative so no reading of the Location can switch hosts.
            "/dashboard//evil.example/x": "http://127.0.0.1:8082/evil.example/x",
        }
        for path, location in cases.items():
            response = client.get(path)
            assert response.status_code == 307, path
            assert response.headers["location"] == location, path

        # A CR/LF smuggled into the path never becomes a header line.  (Starlette's
        # ``path`` convertor does not match a decoded newline, so this one is not
        # routed to the pointer at all; were it routed, ``quote`` would encode it.)
        smuggled = client.get("/dashboard/x%0d%0aSet-Cookie:%20pwned=1")
        assert "set-cookie" not in smuggled.headers
        assert not {"\r", "\n"} & set(smuggled.headers.get("location", ""))


async def test_a_disabled_dashboard_server_gets_a_404_with_a_null_url(tmp_path):
    """spec §5: ``404`` and ``dashboard_url: null`` when ``dashboard.server.enabled`` is false."""
    async with daemon_app(
        tmp_path, dashboard_server=_server(enabled=False), name="dashboard_pointer_disabled.db"
    ) as app:
        with TestClient(app, follow_redirects=False) as client:
            for path in ("/dashboard", "/dashboard/", "/dashboard/projects/example"):
                response = client.get(path)
                assert response.status_code == 404, path
                assert "location" not in response.headers, path
                assert response.json() == {**POINTER_BODY, "dashboard_url": None}, path


async def test_the_first_dashboard_request_builds_no_route_contexts(live_app, monkeypatch):
    """``create_app`` resolves every included router before the first request.

    Otherwise the first ``/dashboard/`` -- matched past every include -- rebuilt
    them all on the daemon's event loop: ~0.5 s on a quiet host, past the 2 s
    the pre-change updater's probe allows on a loaded CI runner.
    """
    from fastapi import routing

    included_router = getattr(routing, "_IncludedRouter", None)
    if included_router is None:
        # Supported older FastAPI versions copy included routes eagerly.
        assert any(
            isinstance(route, routing.APIRoute) and route.path == "/api/health"
            for route in live_app.routes
        )
        builder_type = routing.APIRouter
        builder_name = "add_api_route"
    else:
        assert any(isinstance(route, included_router) for route in live_app.routes)
        builder_type = included_router
        builder_name = "_build_effective_context"

    built: list[object] = []
    build = getattr(builder_type, builder_name)

    def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
        built.append(args)
        return build(self, *args, **kwargs)

    monkeypatch.setattr(builder_type, builder_name, counting)
    with TestClient(live_app, follow_redirects=False) as client:
        assert client.get("/dashboard/").status_code == 307
        assert client.get("/no-such-route").status_code == 404
    assert built == []
