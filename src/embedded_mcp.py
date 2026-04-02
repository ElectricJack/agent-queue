"""Embedded MCP server — runs inside the daemon as a supervised asyncio task.

Shares the daemon's Orchestrator, Database, EventBus, and CommandHandler
instead of creating its own.  Serves on streamable-http transport via uvicorn.

This module must be imported lazily (after orchestrator initialization)
because importing the ``mcp`` SDK takes ~3 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

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
) -> None:
    """Run the embedded MCP server with supervised restart.

    1. Lazy-imports the MCP SDK and uvicorn.
    2. Creates a FastMCP instance whose lifespan yields the daemon's existing
       orchestrator, DB, event bus, and command handler — no duplication.
    3. Registers tools, resources, and prompts via the shared registration
       functions in ``packages.mcp_server.mcp_server``.
    4. Runs uvicorn in a supervised loop with exponential backoff.
       If the MCP server crashes, the orchestrator is unaffected.
    """
    # --- Lazy imports (MCP SDK ~3s, uvicorn is its transitive dep) ---------
    import uvicorn
    from mcp.server import FastMCP

    from packages.mcp_server.mcp_server import (
        get_effective_exclusions,
        register_command_tools,
        register_prompts,
        register_resources,
    )
    from src.command_handler import CommandHandler

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
    register_command_tools(mcp, excluded=exclusions)
    register_resources(mcp)
    register_prompts(mcp)

    logger.info(
        "Embedded MCP server starting on %s:%d (streamable-http)",
        mcp_config.host,
        mcp_config.port,
    )

    # --- Supervised uvicorn loop -------------------------------------------

    restart_delay = 1.0
    max_restart_delay = 30.0

    while not shutdown_event.is_set():
        try:
            starlette_app = mcp.streamable_http_app()
            uv_config = uvicorn.Config(
                starlette_app,
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
