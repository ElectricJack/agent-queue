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


@asynccontextmanager
async def daemon_app(
    tmp_path: Path, *, dashboard_server: Any = None, name: str = "dashboard_pointer.db"
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
    )
    try:
        yield create_app(orch, config)
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
        await db.close()


@pytest.fixture
async def live_app(tmp_path):
    async with daemon_app(tmp_path) as app:
        yield app


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
