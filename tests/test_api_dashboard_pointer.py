"""Daemon is API-only: no static dashboard mount, and /dashboard yields a 404 hint.

docs/specs/dashboard-server.md §5: ``src/api/app.py`` drops the
``mount_dashboard`` call; ``GET /dashboard`` and ``/dashboard/{rest}`` answer
404 with a machine-readable body naming the configured dashboard server URL
(``dashboard_url`` is ``null`` when the server is disabled).  These cases pin
the daemon's route table against regressing into a static-file mount.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from tests.db_fixtures import lease_dsn


@pytest.fixture
async def live_app(tmp_path):
    """A real ``create_app()`` application over a SQLite-backed orchestrator.

    Mirrors the fixture in ``tests/test_api_client_contract.py`` so the
    daemon's actual route table (not a hand-built one) is what is inspected.
    """
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.database import Database
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    db = Database(lease_dsn("dashboard_pointer.db"))
    await db.initialize()
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database=DatabaseConfig(url=lease_dsn("dashboard_pointer.db")),
        data_dir=str(tmp_path / "d"),
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
        yield create_app(orch, config), db
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
        await db.close()


async def test_daemon_registers_no_static_file_mount(live_app):
    """Acceptance: the daemon's route table holds no static-file mount.

    Before smart-meadow.4, the daemon mounted a ``StaticFiles`` app at
    /dashboard (a starlette ``Mount`` with a ``StaticFiles`` app).  The daemon
    is now API-only, and the dashboard lives in its own process.
    """
    app, _ = live_app
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
    # The /dashboard route now resolves to our 404 hint handler, not a Mount.
    assert all(not isinstance(route, Mount) for route in dashboard_routes)


async def test_dashboard_paths_answer_404_with_the_hint_body(live_app):
    """GET /dashboard and /dashboard/<anything> give the spec §5 JSON hint."""
    app, _ = live_app
    with TestClient(app) as client:
        bare = client.get("/dashboard")
        slashed = client.get("/dashboard/")
        nested = client.get("/dashboard/projects/example/graph")

        for response in (bare, slashed, nested):
            assert response.status_code == 404, f"{response.request.url} -> {response.status_code}"
            body = response.json()
            assert body["ok"] is False
            assert body["error"] == "dashboard_not_served_here"
            # dashboard.server defaults to enabled with host 127.0.0.1:8082,
            # so the hint must name that URL.
            assert body["dashboard_url"] == "http://127.0.0.1:8082/"
            assert body["hint"] == "The daemon is API-only. Run `aq dashboard status`."


async def test_dashboard_url_is_null_when_the_server_is_disabled(tmp_path):
    """spec §5: ``dashboard_url`` is ``null`` when ``dashboard.server.enabled`` is false."""
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import (
        AppConfig,
        DashboardServerConfig,
        DatabaseConfig,
        DiscordConfig,
    )
    from src.database import Database
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    db = Database(lease_dsn("dashboard_pointer_disabled.db"))
    await db.initialize()
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database=DatabaseConfig(url=lease_dsn("dashboard_pointer_disabled.db")),
        data_dir=str(tmp_path / "d"),
        dashboard_server=DashboardServerConfig(enabled=False, host="127.0.0.1", port=8082),
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
        app = create_app(orch, config)
        with TestClient(app) as client:
            response = client.get("/dashboard")
            assert response.status_code == 404
            body = response.json()
            assert body["ok"] is False
            assert body["error"] == "dashboard_not_served_here"
            assert body["dashboard_url"] is None
            assert body["hint"] == "The daemon is API-only. Run `aq dashboard status`."
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
        await db.close()
