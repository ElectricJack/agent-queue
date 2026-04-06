"""FastAPI application factory for the agent-queue daemon.

Creates the FastAPI app with all routes mounted, including:
- Backward-compat /api/execute, /api/tools, /api/health
- Health/ready/plans endpoints (consolidated from old TCP server)
- MCP streamable-http sub-app (mounted at /)

The app is created by ``create_app()`` which is called from
``src.embedded_mcp.run_mcp_server()`` during daemon startup.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import FastAPI, WebSocket

from src.api import dependencies as deps
from src.api.execute import router as execute_router
from src.api.health import router as health_router
from src.api.middleware import RequestContextMiddleware
from src.api.websocket import WebSocketManager

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.orchestrator import Orchestrator


def create_app(
    orchestrator: Orchestrator,
    config: AppConfig,
    health_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    plan_content_provider: Callable[[str], Awaitable[str | None]] | None = None,
) -> FastAPI:
    """Build the FastAPI application with all routes.

    Args:
        orchestrator: The shared Orchestrator instance.
        config: The daemon's AppConfig.
        health_provider: Async callback returning health check results.
        plan_content_provider: Async callback returning plan markdown for a task_id.

    Returns:
        A configured FastAPI app ready to be served by uvicorn.
    """
    from src.commands.handler import CommandHandler

    app = FastAPI(
        title="AgentQueue API",
        description="REST API for the agent-queue daemon.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Wire up shared state via the dependencies module
    deps._orchestrator = orchestrator

    ch = orchestrator._command_handler
    if ch is None:
        ch = CommandHandler(orchestrator, config)
        orchestrator.set_command_handler(ch)
    deps._command_handler = ch

    deps._health_provider = health_provider
    deps._plan_content_provider = plan_content_provider
    deps._started_at = time.monotonic()
    deps._base_url = (
        config.health_check.base_url
        if hasattr(config, "health_check") and config.health_check.base_url
        else ""
    )

    # Add request context middleware for structured logging
    app.add_middleware(RequestContextMiddleware)

    # Register routers — backward-compat and health first
    app.include_router(execute_router)
    app.include_router(health_router)

    # Auto-generated typed command routes (POST /api/{category}/{command})
    from src.api.routers import register_all_routers

    register_all_routers(app)

    # WebSocket event stream — forward notify.* events to connected clients
    ws_manager = WebSocketManager(orchestrator.bus)
    ws_manager.start()

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        await ws_manager.handle(websocket)

    @app.on_event("shutdown")
    async def _shutdown_ws():
        ws_manager.shutdown()

    return app
