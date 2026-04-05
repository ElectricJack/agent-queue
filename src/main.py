"""Agent Queue daemon entry point — process lifecycle and signal handling.

Startup sequence:
  1. Configure structured logging (JSON or human-readable, with correlation IDs)
  2. Load config from YAML (default: ~/.agent-queue/config.yaml)
     - Validates all settings (fails fast on misconfiguration)
     - Applies environment-specific overlay (config.{env}.yaml) if present
  3. Initialize the Orchestrator (database, event bus, scheduler)
  4. Start the health check HTTP server (if enabled)
  5. Start the messaging adapter (Discord, Telegram, etc.) via factory
  6. Register orchestrator callbacks through the adapter (platform-agnostic)
  7. Run the scheduler loop (~5s cycle: promote tasks, assign agents, execute)
  8. Wait for SIGTERM/SIGINT to trigger graceful shutdown

On shutdown, the bot, health check server, and orchestrator are closed cleanly.
If a restart was requested (via the /restart Discord command), the process
replaces itself using os.execv() — this ensures a fresh Python interpreter
with updated code while the systemd/supervisor unit sees a continuous process.

See specs/main.md for the full specification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import time

from src.adapters import AdapterFactory
from src.config import ConfigValidationError, load_config
from src.logging_config import setup_logging
from src.messaging import create_messaging_adapter
from src.messaging.base import MessagingAdapter
from src.models import AgentState, TaskStatus
from src.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")


async def run(config_path: str, profile: str | None = None) -> bool:
    """Run the daemon. Returns True if a restart was requested."""
    # Set up structured logging early (before any other import logs)
    setup_logging(
        level=os.environ.get("AGENT_QUEUE_LOG_LEVEL", "INFO"),
        format=os.environ.get("AGENT_QUEUE_LOG_FORMAT", "dev"),
    )

    config = load_config(config_path, profile=profile)
    logger.info(
        "Starting with env=%s, profile=%s",
        config.env,
        config.profile or "no profile",
    )

    # Configure structured logging with full config (including file output)
    log_file = config.logging.log_file or os.path.join(config.data_dir, "logs", "agent-queue.log")
    setup_logging(
        level=config.logging.level,
        format=config.logging.format,
        include_source=config.logging.include_source,
        log_file=log_file,
        log_file_max_bytes=config.logging.log_file_max_bytes,
        log_file_backup_count=config.logging.log_file_backup_count,
        console_format=config.logging.console_format,
    )

    # Ensure database directory exists (SQLite only)
    if config.database.backend == "sqlite":
        db_path = config.database.url or config.database_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    orch = Orchestrator(config, adapter_factory=None)
    adapter_factory = AdapterFactory(llm_logger=orch.llm_logger)
    orch._adapter_factory = adapter_factory
    await orch.initialize()

    # Start health check server (if enabled)
    async def _plan_content(task_id: str) -> str | None:
        """Fetch raw plan content from task_context for the plan viewer."""
        contexts = await orch.db.get_task_contexts(task_id)
        raw_ctx = next((c for c in contexts if c["type"] == "plan_raw"), None)
        return raw_ctx["content"] if raw_ctx else None

    # Create the messaging adapter for the configured platform
    adapter = create_messaging_adapter(config, orch)
    logger.info(
        "Messaging platform: %s",
        adapter.platform_name,
    )

    # Health provider callback — used by the FastAPI health endpoints.
    _health_prov = lambda: _health_checks(orch, adapter)  # noqa: E731

    shutdown_event = asyncio.Event()

    def handle_signal():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    async def run_bot():
        try:
            await adapter.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Messaging adapter failed to start — triggering shutdown")
            shutdown_event.set()

    async def run_scheduler():
        # Wait for the messaging adapter to be ready before scheduling.
        # Use a timeout so the scheduler doesn't hang forever if the bot
        # fails to connect (e.g. Discord rate-limits the initial login).
        try:
            await asyncio.wait_for(adapter.wait_until_ready(), timeout=120)
        except asyncio.TimeoutError:
            logger.error("Messaging adapter not ready after 120s — triggering shutdown")
            shutdown_event.set()
            return

        # Register the event-driven notification handler.
        # The handler subscribes to notify.* events on the orchestrator's
        # EventBus and routes them to the messaging platform (Discord embeds,
        # threads, interactive views, etc.).
        bot = getattr(adapter, "bot", None)
        if bot is not None:
            from src.discord.notification_handler import DiscordNotificationHandler

            _notification_handler = DiscordNotificationHandler(bot, orch.bus)
            logger.info("Discord notification handler registered on EventBus")
        else:
            logger.info(
                "No bot instance on adapter (%s) — notification handler not registered",
                adapter.platform_name,
            )

        orch.set_command_handler(adapter.get_command_handler())
        orch.set_supervisor(adapter.get_supervisor())

        while not shutdown_event.is_set():
            await orch.run_one_cycle()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    # Start embedded MCP server (if enabled).  Lazy-imports the MCP SDK
    # (~3s) inside the task so it never blocks orchestrator startup.
    mcp_task: asyncio.Task | None = None
    if config.mcp_server.enabled:

        async def run_mcp():
            try:
                from src.embedded_mcp import run_mcp_server

                await run_mcp_server(
                    orch,
                    config,
                    shutdown_event,
                    health_provider=_health_prov,
                    plan_content_provider=_plan_content,
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Embedded MCP server failed (non-fatal)")

        mcp_task = asyncio.create_task(run_mcp())

    bot_task = asyncio.create_task(run_bot())
    scheduler_task = asyncio.create_task(run_scheduler())

    try:
        # Wait until shutdown is signaled
        await shutdown_event.wait()
    finally:
        # Shut down adapter, MCP/API server, and orchestrator
        restart = orch._restart_requested
        await adapter.close()
        if mcp_task is not None:
            mcp_task.cancel()
            try:
                await mcp_task
            except (asyncio.CancelledError, Exception):
                pass
        bot_task.cancel()
        scheduler_task.cancel()
        await orch.shutdown()

    return restart


async def _health_checks(orch: Orchestrator, adapter: MessagingAdapter) -> dict:
    """Gather health check results from the orchestrator for the health endpoint.

    Returns a dict of check names to check results, each with at minimum
    an ``ok`` key indicating whether that subsystem is healthy.
    """
    checks: dict[str, dict] = {}

    # Database check
    try:
        await orch.db.list_agents()
        checks["database"] = {"ok": True}
    except Exception as e:
        checks["database"] = {"ok": False, "error": str(e)}

    # Orchestrator status
    checks["orchestrator"] = {
        "ok": True,
        "paused": orch._paused,
        "running_tasks": len(orch._running_tasks),
    }

    # Agent status
    try:
        agents = await orch.db.list_agents()
        busy = sum(1 for a in agents if a.state == AgentState.BUSY)
        idle = sum(1 for a in agents if a.state == AgentState.IDLE)
        checks["agents"] = {"ok": True, "busy": busy, "idle": idle, "total": len(agents)}
    except Exception as e:
        checks["agents"] = {"ok": False, "error": str(e)}

    # Task counts
    try:
        in_progress = await orch.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        ready = await orch.db.list_tasks(status=TaskStatus.READY)
        checks["tasks"] = {
            "ok": True,
            "in_progress": len(in_progress),
            "ready": len(ready),
        }
    except Exception as e:
        checks["tasks"] = {"ok": False, "error": str(e)}

    # Messaging platform check — platform-agnostic via adapter
    connected = adapter.is_connected()
    checks["messaging"] = {
        "ok": connected,
        "platform": adapter.platform_name,
        "connected": connected,
    }

    # Discord rate guard — tracks invalid requests (401/403/429) toward
    # the 10,000 / 10 min Cloudflare ban threshold.
    from src.discord.rate_guard import get_tracker

    tracker = get_tracker()
    rate_state = tracker.state
    checks["discord_rate_guard"] = {
        "ok": rate_state in ("ok", "warn"),
        "state": rate_state,
        "invalid_count": tracker.count,
    }

    return checks


def _parse_args(argv: list[str]) -> tuple[str, str | None, bool]:
    """Parse CLI arguments for config path, --profile, and --validate-config flags.

    Returns (config_path, profile, validate_only).
    Profile is None if not specified.
    Precedence: --profile CLI > AGENT_QUEUE_PROFILE env var > none.
    """
    profile: str | None = None
    validate_only: bool = False
    remaining: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--profile" and i + 1 < len(argv):
            profile = argv[i + 1]
            i += 2
        elif argv[i].startswith("--profile="):
            profile = argv[i].split("=", 1)[1]
            i += 1
        elif argv[i] == "--validate-config":
            validate_only = True
            i += 1
        else:
            remaining.append(argv[i])
            i += 1
    config_path = remaining[0] if remaining else DEFAULT_CONFIG_PATH
    return config_path, profile, validate_only


def _validate_config_only(config_path: str, profile: str | None = None) -> int:
    """Load and validate config without starting services.

    Returns 0 if valid (or warnings only), 1 if errors found.
    """
    try:
        config = load_config(config_path, profile=profile)
    except ConfigValidationError as exc:
        for err in exc.errors:
            print(f"Config error: {err}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    # Also run validate() to show warnings
    errors = config.validate()
    for e in errors:
        print(f"Config {e.severity}: [{e.section}] {e.field}: {e.message}", file=sys.stderr)

    if any(e.severity == "error" for e in errors):
        return 1

    print("Configuration is valid.", file=sys.stderr)
    return 0


def main():
    config_path, profile, validate_only = _parse_args(sys.argv[1:])

    if validate_only:
        sys.exit(_validate_config_only(config_path, profile))

    restart = asyncio.run(run(config_path, profile=profile))
    if restart:
        # Wait before re-exec to let platform rate limits settle.
        # Discord in particular rate-limits the initial GET /users/@me
        # when a bot reconnects too quickly after disconnecting.
        delay = 15
        print(f"Restart requested — waiting {delay}s for rate limits to settle...")
        time.sleep(delay)
        print("exec'ing new process...")
        # Resolve the entry point to an absolute path for execv
        exe = shutil.which(sys.argv[0]) or os.path.abspath(sys.argv[0])
        os.execv(exe, sys.argv)


if __name__ == "__main__":
    main()
