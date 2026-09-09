"""Tests for platform-aware health checks and startup diagnostics.

Covers:
- Health check ``messaging`` field reports correct platform name
- Health check ``messaging`` field reports connection status via adapter
- Health check ``messaging.ok`` reflects adapter ``is_connected()``
- Ready endpoint checks ``messaging`` (not ``discord``)
- ``_health_checks`` uses adapter, not orchestrator._notify
- Discord and Null adapters expose ``is_connected`` and ``platform_name``
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.messaging.base import MessagingAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAdapter(MessagingAdapter):
    """Test adapter with controllable connection state."""

    def __init__(self, platform: str = "test", connected: bool = True) -> None:
        self._platform = platform
        self._connected = connected

    async def start(self) -> None:
        pass

    async def wait_until_ready(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send_message(self, text, project_id=None, *, embed=None, view=None):
        pass

    async def create_task_thread(self, thread_name, initial_message, project_id=None, task_id=None):
        return (AsyncMock(), AsyncMock())

    def get_command_handler(self) -> Any:
        return None

    def get_supervisor(self) -> Any:
        return None

    def is_connected(self) -> bool:
        return self._connected

    @property
    def platform_name(self) -> str:
        return self._platform


def _make_orchestrator(**overrides):
    """Create a minimal mock orchestrator for health check tests."""
    orch = MagicMock()
    orch._paused = overrides.get("paused", False)
    orch._running_tasks = overrides.get("running_tasks", {})
    orch._notify = overrides.get("notify", None)

    # Database stubs
    orch.db = AsyncMock()
    orch.db.list_agents = AsyncMock(return_value=overrides.get("agents", []))
    in_progress = overrides.get("in_progress_tasks", [])
    ready_tasks = overrides.get("ready_tasks", [])

    async def mock_list_tasks(status=None):
        from src.models import TaskStatus

        if status == TaskStatus.IN_PROGRESS:
            return in_progress
        elif status == TaskStatus.READY:
            return ready_tasks
        return []

    orch.db.list_tasks = mock_list_tasks
    return orch


# ---------------------------------------------------------------------------
# _health_checks tests
# ---------------------------------------------------------------------------


class TestHealthChecksMessaging:
    """Verify _health_checks returns platform-aware messaging status."""

    @pytest.mark.asyncio
    async def test_messaging_field_present(self):
        """Health checks include a 'messaging' key (not 'discord')."""
        from src.main import _health_checks

        orch = _make_orchestrator()
        adapter = FakeAdapter(platform="discord", connected=True)

        checks = await _health_checks(orch, adapter)

        assert "messaging" in checks
        assert "discord" not in checks  # old key should be gone

    @pytest.mark.asyncio
    async def test_messaging_reports_discord_platform(self):
        """When using Discord adapter, platform is 'discord'."""
        from src.main import _health_checks

        orch = _make_orchestrator()
        adapter = FakeAdapter(platform="discord", connected=True)

        checks = await _health_checks(orch, adapter)

        assert checks["messaging"]["platform"] == "discord"
        assert checks["messaging"]["connected"] is True
        assert checks["messaging"]["ok"] is True

    @pytest.mark.asyncio
    async def test_messaging_reports_none_platform(self):
        """When using the null adapter, platform is 'none'."""
        from src.main import _health_checks

        orch = _make_orchestrator()
        adapter = FakeAdapter(platform="none", connected=True)

        checks = await _health_checks(orch, adapter)

        assert checks["messaging"]["platform"] == "none"
        assert checks["messaging"]["connected"] is True
        assert checks["messaging"]["ok"] is True

    @pytest.mark.asyncio
    async def test_messaging_disconnected(self):
        """When adapter reports disconnected, ok and connected are False."""
        from src.main import _health_checks

        orch = _make_orchestrator()
        adapter = FakeAdapter(platform="none", connected=False)

        checks = await _health_checks(orch, adapter)

        assert checks["messaging"]["ok"] is False
        assert checks["messaging"]["connected"] is False
        assert checks["messaging"]["platform"] == "none"

    @pytest.mark.asyncio
    async def test_other_checks_still_present(self):
        """Database, orchestrator, agents, and tasks checks still work."""
        from src.main import _health_checks

        orch = _make_orchestrator()
        adapter = FakeAdapter()

        checks = await _health_checks(orch, adapter)

        assert "database" in checks
        assert "orchestrator" in checks
        assert "agents" in checks
        assert "tasks" in checks


# ---------------------------------------------------------------------------
# HealthCheckServer ready endpoint
# ---------------------------------------------------------------------------


class TestReadyEndpointMessaging:
    """Verify the /ready endpoint uses 'messaging' instead of 'discord'."""

    @pytest.mark.asyncio
    async def test_ready_checks_messaging_key(self):
        """Ready endpoint uses 'messaging' check, not 'discord'."""
        from httpx import ASGITransport, AsyncClient
        from src.api import dependencies as deps
        from src.api.health import router

        health_data = {
            "database": {"ok": True},
            "messaging": {"ok": True, "platform": "none", "connected": True},
            "orchestrator": {"ok": True},
        }

        # Temporarily set the health provider
        old_prov = deps._health_provider
        deps._health_provider = AsyncMock(return_value=health_data)
        try:
            from fastapi import FastAPI

            app = FastAPI()
            app.include_router(router)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/ready")
            body = resp.json()
        finally:
            deps._health_provider = old_prov

        assert resp.status_code == 200
        assert body["ready"] is True
        assert "messaging" in body["checks"]
        assert "discord" not in body["checks"]
        assert body["checks"]["messaging"]["platform"] == "none"

    @pytest.mark.asyncio
    async def test_ready_fails_when_messaging_disconnected(self):
        """Ready returns 503 when messaging is not connected."""
        from httpx import ASGITransport, AsyncClient
        from src.api import dependencies as deps
        from src.api.health import router

        health_data = {
            "database": {"ok": True},
            "messaging": {"ok": False, "platform": "discord", "connected": False},
        }

        old_prov = deps._health_provider
        deps._health_provider = AsyncMock(return_value=health_data)
        try:
            from fastapi import FastAPI

            app = FastAPI()
            app.include_router(router)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/ready")
            body = resp.json()
        finally:
            deps._health_provider = old_prov

        assert resp.status_code == 503
        assert body["ready"] is False


# ---------------------------------------------------------------------------
# Adapter is_connected and platform_name
# ---------------------------------------------------------------------------


class TestAdapterHealthMethods:
    """Verify is_connected() and platform_name on concrete adapters."""

    def test_discord_adapter_platform_name(self):
        """DiscordMessagingAdapter.platform_name is 'discord'."""
        with patch("src.discord.bot.AgentQueueBot", autospec=False):
            from src.discord.adapter import DiscordMessagingAdapter

            adapter = DiscordMessagingAdapter(MagicMock(), MagicMock())
            assert adapter.platform_name == "discord"

    def test_discord_adapter_is_connected_when_ready(self):
        """DiscordMessagingAdapter.is_connected() returns True when bot is ready."""
        with patch("src.discord.bot.AgentQueueBot", autospec=False):
            from src.discord.adapter import DiscordMessagingAdapter

            adapter = DiscordMessagingAdapter(MagicMock(), MagicMock())
            adapter._bot = MagicMock()
            adapter._bot.is_ready.return_value = True
            adapter._bot.is_closed.return_value = False
            assert adapter.is_connected() is True

    def test_discord_adapter_not_connected_when_closed(self):
        """DiscordMessagingAdapter.is_connected() returns False when bot is closed."""
        with patch("src.discord.bot.AgentQueueBot", autospec=False):
            from src.discord.adapter import DiscordMessagingAdapter

            adapter = DiscordMessagingAdapter(MagicMock(), MagicMock())
            adapter._bot = MagicMock()
            adapter._bot.is_ready.return_value = True
            adapter._bot.is_closed.return_value = True
            assert adapter.is_connected() is False

    def test_discord_adapter_not_connected_when_not_ready(self):
        """DiscordMessagingAdapter.is_connected() returns False when bot not ready."""
        with patch("src.discord.bot.AgentQueueBot", autospec=False):
            from src.discord.adapter import DiscordMessagingAdapter

            adapter = DiscordMessagingAdapter(MagicMock(), MagicMock())
            adapter._bot = MagicMock()
            adapter._bot.is_ready.return_value = False
            adapter._bot.is_closed.return_value = False
            assert adapter.is_connected() is False

    def test_null_adapter_platform_name(self):
        """NullMessagingAdapter.platform_name is 'none'."""
        from src.messaging.null_adapter import NullMessagingAdapter

        adapter = NullMessagingAdapter(MagicMock(), MagicMock())
        assert adapter.platform_name == "none"

    def test_null_adapter_always_connected(self):
        """NullMessagingAdapter.is_connected() is always True — nothing to lose."""
        from src.messaging.null_adapter import NullMessagingAdapter

        adapter = NullMessagingAdapter(MagicMock(), MagicMock())
        assert adapter.is_connected() is True

    def test_null_adapter_owns_no_command_handler_or_supervisor(self):
        """NullMessagingAdapter returns None so main.py falls back to its own instances."""
        from src.messaging.null_adapter import NullMessagingAdapter

        adapter = NullMessagingAdapter(MagicMock(), MagicMock())
        assert adapter.get_command_handler() is None
        assert adapter.get_supervisor() is None


# ---------------------------------------------------------------------------
# Plan viewer task id validation
# ---------------------------------------------------------------------------


async def _get_plan(task_id: str, *, content: str | None = "# Plan\n"):
    """Call GET /plans/{task_id} against a router-only app with a stub provider."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api import dependencies as deps
    from src.api.health import router

    seen: list[str] = []

    async def provider(requested: str) -> str | None:
        seen.append(requested)
        return content

    old_prov = deps._plan_content_provider
    deps._plan_content_provider = provider
    try:
        app = FastAPI()
        app.include_router(router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/plans/{task_id}")
    finally:
        deps._plan_content_provider = old_prov

    return resp, seen


class TestPlanViewerTaskIds:
    """The plan viewer must accept hierarchical ids without opening a traversal."""

    @pytest.mark.asyncio
    async def test_root_task_id_is_served(self):
        """A root id (``adjective-noun``) reaches the provider and renders."""
        resp, seen = await _get_plan("solid-grove")

        assert resp.status_code == 200
        assert seen == ["solid-grove"]

    @pytest.mark.asyncio
    async def test_hierarchical_task_id_is_served(self):
        """``<parent>.<ordinal>`` is a real task id, not an invalid one."""
        resp, seen = await _get_plan("solid-grove.13")

        assert resp.status_code == 200
        assert seen == ["solid-grove.13"]
        assert "solid-grove.13" in resp.text

    @pytest.mark.asyncio
    async def test_deeply_nested_task_id_is_served(self):
        """Nesting is unbounded, so every dot-separated level is accepted."""
        resp, seen = await _get_plan("solid-grove-42.13.2")

        assert resp.status_code == 200
        assert seen == ["solid-grove-42.13.2"]

    @pytest.mark.asyncio
    async def test_missing_plan_for_hierarchical_id_is_404(self):
        """A well-formed child id with no plan row is 'not found', not 'invalid'."""
        resp, seen = await _get_plan("solid-grove.13", content=None)

        assert resp.status_code == 404
        assert resp.json() == {"error": "plan not found"}
        assert seen == ["solid-grove.13"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "task_id",
        [
            "solid-grove..13",
            "..solid-grove",
            ".solid-grove",
            "solid-grove.",
            "solid grove",
            "solid-grove;13",
        ],
    )
    async def test_malformed_task_ids_are_rejected(self, task_id: str):
        """Dots are only a separator: an empty segment or a stray character is 400."""
        resp, seen = await _get_plan(task_id)

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid task id"}
        assert seen == []

    @pytest.mark.parametrize(
        "candidate",
        ["..", "../etc/passwd", "solid-grove/../etc", "solid/grove", "solid-grove/", "", "."],
    )
    def test_pattern_rejects_traversal_shapes(self, candidate: str):
        """Separators never make it through the allowlist, whatever routing does first."""
        from src.api.health import _TASK_ID_RE

        assert _TASK_ID_RE.match(candidate) is None
