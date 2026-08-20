"""M0 messaging strip: the daemon boots and schedules with no messaging adapter.

Covers docs/specs/implementation/messaging-rework.md §6 M0 checklist item:
"Tests: the daemon boots and schedules with messaging_platform: 'none'".

Exercises the same construction sequence as ``src/main.py``'s ``run()`` —
Orchestrator + a daemon-wide shared Supervisor + ``NullMessagingAdapter`` +
the ``get_command_handler()``/``get_supervisor()`` fallback the M0 strip
added — plus one scheduler cycle (``orch.run_one_cycle()``).  It does not
go through ``main.run()``'s process-lifecycle wrapper (SIGTERM/SIGINT
handler registration there is not portable to a pytest process on every
platform, and is orthogonal to the messaging decoupling under test).
"""

from __future__ import annotations

import pytest

from src.config import AppConfig
from src.messaging.factory import create_messaging_adapter
from src.messaging.null_adapter import NullMessagingAdapter
from src.orchestrator import Orchestrator
from src.runtimes import default_registry
from src.runtimes.supervisor import Supervisor


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
    # Mirrors src/main.py: construct the daemon-wide shared Supervisor before
    # the registry so default_registry() can register the singleton.
    shared_supervisor = Supervisor(orch, config, llm_logger=orch.llm_logger)
    registry = default_registry(supervisor=shared_supervisor)
    orch._runtimes = registry
    await orch.initialize()

    try:
        adapter = create_messaging_adapter(config, orch)
        assert isinstance(adapter, NullMessagingAdapter)
        assert adapter.platform_name == "none"

        await adapter.start()
        await adapter.wait_until_ready()
        assert adapter.is_connected() is True

        # The M0 decoupling: NullMessagingAdapter owns neither a
        # CommandHandler nor a Supervisor, so main.py falls back to the
        # daemon-wide shared_supervisor for both.
        assert adapter.get_command_handler() is None
        assert adapter.get_supervisor() is None
        handler = adapter.get_command_handler() or shared_supervisor.handler
        supervisor = adapter.get_supervisor() or shared_supervisor
        assert handler is shared_supervisor.handler
        assert supervisor is shared_supervisor
        orch.set_command_handler(handler)
        orch.set_supervisor(supervisor)

        # The scheduler cycle must not depend on a messaging adapter at all.
        await orch.run_one_cycle()

        await adapter.close()
    finally:
        await orch.shutdown()
