"""Root-level test fixtures shared across all test modules."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig
from src.models import TaskContext


@pytest.fixture(scope="session")
def claude_cli_path() -> str:
    """Resolve the ``claude`` CLI binary. Skip if missing."""
    path = shutil.which("claude")
    if path is None:
        pytest.skip("claude CLI not found on PATH — skipping functional tests")
    return path


@pytest.fixture(scope="session")
def claude_cli_authenticated(claude_cli_path: str, tmp_path_factory) -> str:
    """Verify the Claude Agent SDK can authenticate and complete a trivial prompt.

    Uses the same code path as the real application (ClaudeAdapter + SDK),
    not subprocess. Runs once per session; skips all functional tests if
    authentication fails.
    """
    workspace = str(tmp_path_factory.mktemp("auth_check"))
    adapter = ClaudeAdapter(
        ClaudeAdapterConfig(
            model="claude-haiku-4-5-20251001",
            permission_mode="bypassPermissions",
            allowed_tools=[],
        )
    )
    ctx = TaskContext(
        description="respond with only: ok",
        task_id="auth-check",
        checkout_path=workspace,
    )

    async def _check():
        await adapter.start(ctx)
        return await adapter.wait()

    try:
        result = asyncio.get_event_loop().run_until_complete(_check())
    except RuntimeError:
        # No running loop — create one
        result = asyncio.run(_check())
    except Exception as exc:
        pytest.skip(f"Claude SDK auth check failed: {exc}")
        return claude_cli_path  # unreachable, keeps type checker happy

    from src.models import AgentResult

    if result.result == AgentResult.FAILED:
        pytest.skip(f"Claude SDK auth check failed: {result.error_message or result.summary}")

    return claude_cli_path


@pytest.fixture(scope="session")
def npm_available() -> str:
    """Resolve ``npx`` binary. Skip if missing."""
    path = shutil.which("npx")
    if path is None:
        pytest.skip("npx not found on PATH — skipping MCP functional tests")
    return path


# ---------------------------------------------------------------------------
# Plugin system fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_registry(tmp_path: Path):
    """Bare PluginRegistry with no plugins loaded.

    Backed by AsyncMock db / MagicMock bus / MagicMock config so tests can
    exercise registry behavior without spinning up real subsystems.
    """
    from src.plugins.registry import PluginRegistry

    db = AsyncMock()
    db.get_plugin = AsyncMock(return_value=None)
    db.create_plugin = AsyncMock()
    db.update_plugin = AsyncMock()
    db.delete_plugin = AsyncMock()
    db.list_plugins = AsyncMock(return_value=[])
    db.get_plugin_data = AsyncMock(return_value=None)
    db.set_plugin_data = AsyncMock()
    db.delete_plugin_data = AsyncMock()
    db.delete_plugin_data_all = AsyncMock()

    bus = MagicMock()
    bus.emit = AsyncMock()
    bus.subscribe = MagicMock()

    config = MagicMock()
    config.data_dir = str(tmp_path / "data")
    os.makedirs(config.data_dir, exist_ok=True)

    return PluginRegistry(db=db, bus=bus, config=config)


@pytest.fixture
def plugin_context_factory(tmp_path: Path):
    """Build a PluginContext with the given trust_level / services for unit tests."""
    from src.plugins.base import PluginContext, TrustLevel

    def _make(
        *,
        trust_level: TrustLevel = TrustLevel.EXTERNAL,
        services: dict | None = None,
        plugin_name: str = "testplugin",
    ):
        db = AsyncMock()
        bus = MagicMock()
        bus.emit = AsyncMock()
        bus.subscribe = MagicMock()
        return PluginContext(
            plugin_name=plugin_name,
            install_path=str(tmp_path / "install"),
            data_path=str(tmp_path / "data"),
            db=db,
            bus=bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            trust_level=trust_level,
            services=services or {},
        )

    return _make


@pytest.fixture
def plugin_registry_with_plugin(plugin_registry):
    """Helper that loads an in-memory plugin class into the registry.

    Usage::

        async def test_x(plugin_registry_with_plugin):
            registry = await plugin_registry_with_plugin(MyPluginCls)
            ...
    """

    async def _load(plugin_cls):
        await plugin_registry.register_in_memory_plugin(plugin_cls)
        return plugin_registry

    return _load
