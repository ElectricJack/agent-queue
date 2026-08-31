"""M0 messaging strip: the daemon boots and schedules with no messaging adapter.

Covers docs/specs/implementation/messaging-rework.md §6 M0 checklist item:
"Tests: the daemon boots and schedules with messaging_platform: 'none'".

Exercises the same construction sequence as ``src/main.py``'s ``run()`` —
Orchestrator + a daemon-wide CommandHandler + ``NullMessagingAdapter`` +
the ``get_command_handler()`` fallback the M0 strip added — plus one
scheduler cycle (``orch.run_one_cycle()``).  It does not
go through ``main.run()``'s process-lifecycle wrapper (SIGTERM/SIGINT
handler registration there is not portable to a pytest process on every
platform, and is orthogonal to the messaging decoupling under test).
"""

from __future__ import annotations

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.messaging.factory import create_messaging_adapter
from src.messaging.null_adapter import NullMessagingAdapter
from src.orchestrator import Orchestrator
from src.tools.registry import ToolRegistry
from src.runtimes import default_registry


@pytest.mark.asyncio
async def test_daemon_boots_and_schedules_with_none_platform(tmp_path):
    """messaging_platform: 'none' — no adapter, daemon still boots and schedules."""
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
        messaging_platform="none",
    )

    orch = Orchestrator(config, runtimes=None)
    # Mirrors src/main.py: an empty runtime registry (every agent runs as a
    # tmux session), then initialize, then the daemon-wide handler.
    orch._runtimes = default_registry(config=config)
    await orch.initialize()
    daemon_handler = CommandHandler(orch, config)

    try:
        adapter = create_messaging_adapter(config, orch)
        assert isinstance(adapter, NullMessagingAdapter)
        assert adapter.platform_name == "none"

        await adapter.start()
        await adapter.wait_until_ready()
        assert adapter.is_connected() is True

        # The M0 decoupling: NullMessagingAdapter owns no CommandHandler, so
        # main.py keeps the daemon-wide one it wired before the adapter.
        assert adapter.get_command_handler() is None
        assert adapter.get_supervisor() is None
        handler = adapter.get_command_handler() or daemon_handler
        assert handler is daemon_handler
        orch.set_command_handler(handler)
        orch.set_tool_registry(ToolRegistry())

        # The scheduler cycle must not depend on a messaging adapter at all.
        await orch.run_one_cycle()

        await adapter.close()
    finally:
        await orch.shutdown()
