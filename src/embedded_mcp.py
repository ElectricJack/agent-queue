"""Embedded MCP server — runs inside the daemon as a supervised asyncio task.

Shares the daemon's Orchestrator, Database, EventBus, and CommandHandler
instead of creating its own.  Serves on streamable-http transport via uvicorn.

Also serves the FastAPI REST API (``/api/*``, ``/health``, ``/ready``,
``/plans/*``, ``/docs``) on the same port for the ``aq`` CLI and other clients.

This module must be imported lazily (after orchestrator initialization)
because importing the ``mcp`` SDK takes ~3 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable

# Ensure project root is on sys.path so ``packages.mcp_server`` is importable
# when the daemon runs via the ``agent-queue`` entry point.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def run_mcp_server(
    orchestrator: Orchestrator,
    config: AppConfig,
    shutdown_event: asyncio.Event,
    health_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    plan_content_provider: Callable[[str], Awaitable[str | None]] | None = None,
) -> None:
    """Run the embedded MCP server with supervised restart.

    1. Lazy-imports the MCP SDK and uvicorn.
    2. Creates a FastMCP instance whose lifespan yields the daemon's existing
       orchestrator, DB, event bus, and command handler — no duplication.
    3. Registers tools, resources, and prompts via the shared registration
       functions in ``src.mcp_registration``.
    4. Builds a FastAPI app (via ``src.api.app``) and mounts the MCP sub-app.
    5. Runs uvicorn in a supervised loop with exponential backoff.
       If the server crashes, the orchestrator is unaffected.
    """
    # --- Lazy imports (MCP SDK ~3s, uvicorn is its transitive dep) ---------
    import uvicorn
    from mcp.server import FastMCP
    from starlette.routing import Mount

    from src.api.app import create_app
    from src.commands.handler import CommandHandler
    from src.mcp_registration import (
        get_effective_exclusions,
        register_command_tools,
        register_prompts,
        register_resources,
    )

    mcp_config = config.mcp_server

    # --- Lifespan: yield the daemon's existing objects ---------------------

    @asynccontextmanager
    async def embedded_lifespan(server: FastMCP):
        ch = orchestrator._command_handler
        if ch is None:
            ch = CommandHandler(orchestrator, config)
            orchestrator.set_command_handler(ch)
        yield {
            "db": orchestrator.db,
            "event_bus": orchestrator.bus,
            "orchestrator": orchestrator,
            "command_handler": ch,
        }

    # --- Create and configure the FastMCP instance -------------------------

    mcp = FastMCP(
        name="agent-queue",
        instructions=(
            "Agent Queue MCP server (embedded in daemon). Provides access to "
            "all CommandHandler operations for the agent-queue orchestrator."
        ),
        host=mcp_config.host,
        port=mcp_config.port,
        lifespan=embedded_lifespan,
    )

    exclusions = get_effective_exclusions(config=config)

    # Collect plugin-contributed tool definitions so MCP exposes them
    plugin_tools: list[dict] = []
    if hasattr(orchestrator, "plugin_registry") and orchestrator.plugin_registry:
        plugin_tools = orchestrator.plugin_registry.get_all_tool_definitions()

    register_command_tools(mcp, excluded=exclusions, plugin_tools=plugin_tools)
    register_resources(mcp)
    register_prompts(mcp)

    logger.info(
        "Embedded MCP server starting on %s:%d (streamable-http)",
        mcp_config.host,
        mcp_config.port,
    )

    # --- Build the combined FastAPI + MCP app --------------------------------

    fastapi_app = create_app(
        orchestrator=orchestrator,
        config=config,
        health_provider=health_provider,
        plan_content_provider=plan_content_provider,
    )

    # --- Supervised uvicorn loop -------------------------------------------

    restart_delay = 1.0
    max_restart_delay = 30.0

    while not shutdown_event.is_set():
        try:
            # Reset session manager so a fresh one is created on restart.
            mcp._session_manager = None
            mcp_app = mcp.streamable_http_app()

            # Mount MCP sub-app at root on the FastAPI app.
            # FastAPI's own routes (/api/*, /health, /docs, etc.) take
            # precedence; MCP handles /mcp underneath.
            fastapi_app.router.routes.append(Mount("/", app=mcp_app))

            # The MCP Starlette sub-app defines a lifespan that
            # initialises the StreamableHTTPSessionManager task group.
            # Mounted sub-apps do NOT get their lifespan triggered by
            # the parent — only the top-level ASGI app does.  We must
            # run the session manager ourselves via FastAPI's lifespan.
            @asynccontextmanager
            async def _combined_lifespan(app):
                async with mcp.session_manager.run():
                    yield

            fastapi_app.router.lifespan_context = _combined_lifespan

            uv_config = uvicorn.Config(
                fastapi_app,
                host=mcp_config.host,
                port=mcp_config.port,
                log_level="warning",
            )
            server = uvicorn.Server(uv_config)

            # Run until shutdown or crash.
            serve_task = asyncio.create_task(server.serve())
            shutdown_wait = asyncio.create_task(shutdown_event.wait())

            done, pending = await asyncio.wait(
                {serve_task, shutdown_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            if shutdown_event.is_set():
                logger.info("Embedded MCP server shutting down")
                return

            # Server exited without shutdown — check for exception.
            for task in done:
                if task is serve_task:
                    exc = task.exception()
                    if exc:
                        raise exc

            logger.warning(
                "Embedded MCP server exited unexpectedly, restarting in %.1fs",
                restart_delay,
            )

        except asyncio.CancelledError:
            logger.info("Embedded MCP server task cancelled")
            return

        except Exception:
            logger.exception(
                "Embedded MCP server crashed, restarting in %.1fs",
                restart_delay,
            )

        # Wait before restart (or exit if shutdown is signaled).
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=restart_delay)
            return  # shutdown_event was set
        except asyncio.TimeoutError:
            pass

        restart_delay = min(restart_delay * 2, max_restart_delay)
