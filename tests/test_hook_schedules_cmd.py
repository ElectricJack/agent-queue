"""Tests for hook_schedules and fire_all_scheduled_hooks commands.

These commands were removed when hooks were replaced by the rules abstraction.
All tests are skipped until equivalent rule-based tests are added.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(
    reason="Hook schedule commands removed — replaced by rules abstraction"
)

from src.command_handler import CommandHandler, _format_interval
from src.config import AppConfig, HookEngineConfig
from src.database import Database
from src.models import Hook, Project


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def orchestrator(db):
    orch = MagicMock()
    orch.db = db
    orch.hooks = MagicMock()
    orch.hooks.fire_hook = AsyncMock()
    return orch


@pytest.fixture
def handler(orchestrator):
    h = CommandHandler.__new__(CommandHandler)
    h.orchestrator = orchestrator
    h.config = AppConfig()
    h._active_project_id = None
    return h


async def _setup_project(db, project_id="test-project"):
    project = Project(id=project_id, name="Test Project")
    await db.create_project(project)
    return project


async def _create_hook(db, **overrides) -> Hook:
    defaults = dict(
        id="test-hook",
        project_id="test-project",
        name="Test Hook",
        enabled=True,
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        context_steps="[]",
        prompt_template="Test",
        cooldown_seconds=60,
    )
    defaults.update(overrides)
    hook = Hook(**defaults)
    await db.create_hook(hook)
    return hook


# ---------------------------------------------------------------------------
# _format_interval helper
# ---------------------------------------------------------------------------


class TestFormatInterval:
    def test_seconds(self):
        assert _format_interval(30) == "30s"

    def test_minutes(self):
        assert _format_interval(300) == "5m"

    def test_hours(self):
        assert _format_interval(7200) == "2h"

    def test_hours_and_minutes(self):
        assert _format_interval(5400) == "1h 30m"

    def test_days(self):
        assert _format_interval(86400) == "1d"

    def test_days_hours_minutes(self):
        assert _format_interval(90060) == "1d 1h 1m"

    def test_zero(self):
        assert _format_interval(0) == "0s"


# ---------------------------------------------------------------------------
# hook_schedules command
# ---------------------------------------------------------------------------


class TestHookSchedulesCommand:
    @pytest.mark.asyncio
    async def test_no_hooks(self, db, handler):
        result = await handler.execute("hook_schedules", {})
        assert result["hooks"] == []

    @pytest.mark.asyncio
    async def test_periodic_hook_no_schedule(self, db, handler):
        """Pure interval hook shows interval info."""
        await _setup_project(db)
        await _create_hook(db)
        result = await handler.execute("hook_schedules", {})
        hooks = result["hooks"]
        assert len(hooks) == 1
        assert hooks[0]["hook_id"] == "test-hook"
        assert "1h" in hooks[0]["interval"]
        assert hooks[0]["last_run"] == "never"

    @pytest.mark.asyncio
    async def test_periodic_hook_with_schedule(self, db, handler):
        """Scheduled hook shows schedule description and next run."""
        await _setup_project(db)
        await _create_hook(
            db,
            trigger=json.dumps(
                {
                    "type": "periodic",
                    "interval_seconds": 3600,
                    "schedule": {"times": ["02:00"], "days_of_week": ["mon"]},
                }
            ),
        )
        result = await handler.execute("hook_schedules", {})
        hooks = result["hooks"]
        assert len(hooks) == 1
        assert "02:00" in hooks[0]["schedule"]
        assert "mon" in hooks[0]["schedule"]

    @pytest.mark.asyncio
    async def test_periodic_hook_with_last_run(self, db, handler):
        """Hook with last_triggered_at shows ago time."""
        await _setup_project(db)
        await _create_hook(db)
        await db.update_hook("test-hook", last_triggered_at=time.time() - 120)
        result = await handler.execute("hook_schedules", {})
        hooks = result["hooks"]
        assert "2m ago" in hooks[0]["last_run"]

    @pytest.mark.asyncio
    async def test_event_hooks_excluded(self, db, handler):
        """Event-driven hooks are not included in schedule view."""
        await _setup_project(db)
        await _create_hook(
            db,
            trigger='{"type": "event", "event_type": "task_completed"}',
        )
        result = await handler.execute("hook_schedules", {})
        assert result["hooks"] == []

    @pytest.mark.asyncio
    async def test_disabled_hooks_excluded(self, db, handler):
        """Disabled hooks are excluded."""
        await _setup_project(db)
        await _create_hook(db, enabled=False)
        result = await handler.execute("hook_schedules", {})
        assert result["hooks"] == []

    @pytest.mark.asyncio
    async def test_project_filter(self, db, handler):
        """Only hooks from specified project are returned."""
        await _setup_project(db, "proj-a")
        await _setup_project(db, "proj-b")
        await _create_hook(db, id="h1", name="H1", project_id="proj-a")
        await _create_hook(db, id="h2", name="H2", project_id="proj-b")
        result = await handler.execute("hook_schedules", {"project_id": "proj-a"})
        hooks = result["hooks"]
        assert len(hooks) == 1
        assert hooks[0]["project_id"] == "proj-a"

    @pytest.mark.asyncio
    async def test_cron_schedule_display(self, db, handler):
        """Cron-based schedule shows cron description."""
        await _setup_project(db)
        await _create_hook(
            db,
            trigger=json.dumps(
                {
                    "type": "periodic",
                    "interval_seconds": 3600,
                    "schedule": {"cron": "0 2 * * *"},
                }
            ),
        )
        result = await handler.execute("hook_schedules", {})
        assert "Cron:" in result["hooks"][0]["schedule"]

    @pytest.mark.asyncio
    async def test_last_run_hours_ago(self, db, handler):
        """Last run > 1 hour ago shows hours and minutes."""
        await _setup_project(db)
        await _create_hook(db)
        await db.update_hook("test-hook", last_triggered_at=time.time() - 5400)
        result = await handler.execute("hook_schedules", {})
        assert "1h" in result["hooks"][0]["last_run"]


# ---------------------------------------------------------------------------
# fire_all_scheduled_hooks command
# ---------------------------------------------------------------------------


class TestFireAllScheduledHooks:
    @pytest.mark.asyncio
    async def test_fires_periodic_hooks(self, db, handler, orchestrator):
        await _setup_project(db)
        await _create_hook(db, id="h1", name="H1")
        await _create_hook(db, id="h2", name="H2")
        result = await handler.execute("fire_all_scheduled_hooks", {})
        assert result["count"] == 2
        assert orchestrator.hooks.fire_hook.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_event_hooks(self, db, handler, orchestrator):
        await _setup_project(db)
        await _create_hook(
            db,
            trigger='{"type": "event", "event_type": "task_completed"}',
        )
        result = await handler.execute("fire_all_scheduled_hooks", {})
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_no_hook_engine(self, db, handler, orchestrator):
        orchestrator.hooks = None
        result = await handler.execute("fire_all_scheduled_hooks", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_project_filter(self, db, handler, orchestrator):
        await _setup_project(db, "proj-a")
        await _setup_project(db, "proj-b")
        await _create_hook(db, id="h1", name="H1", project_id="proj-a")
        await _create_hook(db, id="h2", name="H2", project_id="proj-b")
        result = await handler.execute("fire_all_scheduled_hooks", {"project_id": "proj-a"})
        assert result["count"] == 1
        assert "H1" in result["fired"]
