"""Tests for scheduled (one-shot) hooks."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, HookEngineConfig
from src.database import Database
from src.event_bus import EventBus
from src.hooks import HookEngine
from src.models import Hook, Project


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.hook_engine = HookEngineConfig(enabled=True, max_concurrent_hooks=2)
    return cfg


@pytest.fixture
async def engine(db, bus, config):
    engine = HookEngine(db, bus, config)
    engine._orchestrator = MagicMock()
    engine._orchestrator._notify_channel = AsyncMock()
    engine._orchestrator.db = db
    engine._orchestrator.hooks = engine
    await engine.initialize()
    yield engine
    await engine.shutdown()


async def _create_project(db, project_id="test-project"):
    project = Project(id=project_id, name="Test Project")
    await db.create_project(project)
    return project


async def _create_scheduled_hook(db, fire_at=None, **overrides) -> Hook:
    if fire_at is None:
        fire_at = time.time() + 3600  # 1 hour from now
    defaults = dict(
        id="sched-test-abc123",
        project_id="test-project",
        name="test-scheduled",
        enabled=True,
        trigger=json.dumps({"type": "scheduled", "fire_at": fire_at}),
        context_steps="[]",
        prompt_template="Run this scheduled task",
        cooldown_seconds=0,
        created_at=time.time(),
        updated_at=time.time(),
    )
    defaults.update(overrides)
    hook = Hook(**defaults)
    await db.create_hook(hook)
    return hook


# --- HookEngine.tick() scheduled hook handling ---


class TestScheduledHookTick:
    """Test that tick() correctly fires and auto-deletes scheduled hooks."""

    @pytest.mark.asyncio
    async def test_scheduled_hook_fires_when_due(self, engine, db):
        """A scheduled hook whose fire_at is in the past should fire."""
        await _create_project(db)
        fire_at = time.time() - 10  # 10 seconds ago
        hook = await _create_scheduled_hook(db, fire_at=fire_at)

        with patch.object(engine, "_launch_hook") as mock_launch:
            await engine.tick()

            mock_launch.assert_called_once()
            call_args = mock_launch.call_args
            assert call_args[0][0].id == hook.id
            assert call_args[0][1] == "scheduled"
            # event_data should contain timing info
            event_data = call_args[1].get("event_data") or call_args[0][2]
            assert "scheduled_for_epoch" in event_data

    @pytest.mark.asyncio
    async def test_scheduled_hook_not_fired_when_future(self, engine, db):
        """A scheduled hook whose fire_at is in the future should not fire."""
        await _create_project(db)
        fire_at = time.time() + 3600  # 1 hour from now
        await _create_scheduled_hook(db, fire_at=fire_at)

        with patch.object(engine, "_launch_hook") as mock_launch:
            await engine.tick()
            mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_hook_auto_deleted_after_fire(self, engine, db):
        """After firing, the scheduled hook should be deleted from the DB."""
        await _create_project(db)
        fire_at = time.time() - 10
        hook = await _create_scheduled_hook(db, fire_at=fire_at)

        with patch.object(engine, "_execute_hook", new_callable=AsyncMock):
            await engine.tick()
            # Give the fire-and-forget delete task time to run
            await asyncio.sleep(0.1)

        # Hook should be deleted
        result = await db.get_hook(hook.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_scheduled_hook_skipped_when_paused(self, engine, db):
        """Scheduled hooks for paused projects should not fire."""
        from src.models import ProjectStatus

        project = await _create_project(db)
        await db.update_project(project.id, status=ProjectStatus.PAUSED.value)

        fire_at = time.time() - 10
        await _create_scheduled_hook(db, fire_at=fire_at)

        with patch.object(engine, "_launch_hook") as mock_launch:
            await engine.tick()
            mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_hook_skipped_when_disabled(self, engine, db):
        """Disabled scheduled hooks should not fire."""
        await _create_project(db)
        fire_at = time.time() - 10
        await _create_scheduled_hook(db, fire_at=fire_at, enabled=False)

        with patch.object(engine, "_launch_hook") as mock_launch:
            await engine.tick()
            mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_hook_respects_concurrency_cap(self, engine, db):
        """Scheduled hooks should respect the global concurrency cap."""
        await _create_project(db)
        fire_at = time.time() - 10

        # Create real hooks to fill concurrency slots (max_concurrent=2)
        blocker1 = Hook(
            id="blocker-1",
            project_id="test-project",
            name="blocker-1",
            trigger='{"type": "periodic", "interval_seconds": 999999}',
            prompt_template="Block",
            cooldown_seconds=999999,
        )
        blocker2 = Hook(
            id="blocker-2",
            project_id="test-project",
            name="blocker-2",
            trigger='{"type": "periodic", "interval_seconds": 999999}',
            prompt_template="Block",
            cooldown_seconds=999999,
        )
        await db.create_hook(blocker1)
        await db.create_hook(blocker2)
        # Mark them as running with non-done futures
        engine._running["blocker-1"] = asyncio.Future()
        engine._running["blocker-2"] = asyncio.Future()

        await _create_scheduled_hook(db, fire_at=fire_at)

        with patch.object(engine, "_launch_hook") as mock_launch:
            await engine.tick()
            mock_launch.assert_not_called()

        # Cleanup
        engine._running.pop("blocker-1")
        engine._running.pop("blocker-2")


# --- _parse_delay ---


class TestParseDelay:
    def test_seconds(self):
        from src.command_handler import _parse_delay

        assert _parse_delay("30s") == 30

    def test_minutes(self):
        from src.command_handler import _parse_delay

        assert _parse_delay("5m") == 300

    def test_hours(self):
        from src.command_handler import _parse_delay

        assert _parse_delay("2h") == 7200

    def test_days(self):
        from src.command_handler import _parse_delay

        assert _parse_delay("1d") == 86400

    def test_combined(self):
        from src.command_handler import _parse_delay

        assert _parse_delay("2h30m") == 9000

    def test_plain_integer(self):
        from src.command_handler import _parse_delay

        assert _parse_delay("120") == 120

    def test_invalid_raises(self):
        from src.command_handler import _parse_delay

        with pytest.raises(ValueError, match="Cannot parse delay"):
            _parse_delay("foo")


# --- Command handler: schedule_hook ---


class TestScheduleHookCommand:
    @pytest.fixture
    async def handler(self, db):
        from src.command_handler import CommandHandler

        orch = MagicMock()
        orch.db = db
        orch.hooks = MagicMock()
        handler = CommandHandler(orch, db)
        return handler

    @pytest.mark.asyncio
    async def test_schedule_with_delay(self, handler, db):
        await _create_project(db)
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "test-project",
                "prompt_template": "Check deploy status",
                "delay": "30m",
                "name": "check-deploy",
            },
        )
        assert "created" in result
        assert result["name"] == "check-deploy"
        assert "fires_in" in result
        # Verify hook was created in DB
        hook = await db.get_hook(result["created"])
        assert hook is not None
        trigger = json.loads(hook.trigger)
        assert trigger["type"] == "scheduled"
        assert trigger["fire_at"] > time.time()

    @pytest.mark.asyncio
    async def test_schedule_with_fire_at_epoch(self, handler, db):
        await _create_project(db)
        future_time = time.time() + 7200
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "test-project",
                "prompt_template": "Do something later",
                "fire_at": future_time,
            },
        )
        assert "created" in result
        assert result["fire_at_epoch"] == future_time

    @pytest.mark.asyncio
    async def test_schedule_with_fire_at_iso(self, handler, db):
        await _create_project(db)
        from datetime import datetime, timezone, timedelta

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "test-project",
                "prompt_template": "Do something later",
                "fire_at": future.isoformat(),
            },
        )
        assert "created" in result

    @pytest.mark.asyncio
    async def test_schedule_rejects_past_fire_at(self, handler, db):
        await _create_project(db)
        past_time = time.time() - 100
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "test-project",
                "prompt_template": "Too late",
                "fire_at": past_time,
            },
        )
        assert "error" in result
        assert "future" in result["error"]

    @pytest.mark.asyncio
    async def test_schedule_rejects_both_fire_at_and_delay(self, handler, db):
        await _create_project(db)
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "test-project",
                "prompt_template": "Confused",
                "fire_at": time.time() + 100,
                "delay": "5m",
            },
        )
        assert "error" in result
        assert "not both" in result["error"]

    @pytest.mark.asyncio
    async def test_schedule_rejects_neither_fire_at_nor_delay(self, handler, db):
        await _create_project(db)
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "test-project",
                "prompt_template": "Missing time",
            },
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_schedule_rejects_invalid_project(self, handler, db):
        result = await handler.execute(
            "schedule_hook",
            {
                "project_id": "nonexistent",
                "prompt_template": "No project",
                "delay": "5m",
            },
        )
        assert "error" in result


# --- Command handler: list_scheduled ---


class TestListScheduledCommand:
    @pytest.fixture
    async def handler(self, db):
        from src.command_handler import CommandHandler

        orch = MagicMock()
        orch.db = db
        orch.hooks = MagicMock()
        handler = CommandHandler(orch, db)
        return handler

    @pytest.mark.asyncio
    async def test_list_scheduled_empty(self, handler, db):
        result = await handler.execute("list_scheduled", {})
        assert result["count"] == 0
        assert result["scheduled_hooks"] == []

    @pytest.mark.asyncio
    async def test_list_scheduled_returns_scheduled_hooks(self, handler, db):
        await _create_project(db)
        fire_at = time.time() + 1800
        await _create_scheduled_hook(db, fire_at=fire_at)

        result = await handler.execute("list_scheduled", {})
        assert result["count"] == 1
        assert result["scheduled_hooks"][0]["hook_id"] == "sched-test-abc123"
        assert result["scheduled_hooks"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_list_scheduled_excludes_periodic(self, handler, db):
        await _create_project(db)
        # Create a periodic hook — should not show up
        periodic = Hook(
            id="periodic-hook",
            project_id="test-project",
            name="periodic",
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            prompt_template="Test",
        )
        await db.create_hook(periodic)

        result = await handler.execute("list_scheduled", {})
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_list_scheduled_filters_by_project(self, handler, db):
        await _create_project(db, "proj-a")
        await _create_project(db, "proj-b")
        await _create_scheduled_hook(db, id="sched-a-111", project_id="proj-a")
        await _create_scheduled_hook(db, id="sched-b-222", project_id="proj-b")

        result = await handler.execute("list_scheduled", {"project_id": "proj-a"})
        assert result["count"] == 1
        assert result["scheduled_hooks"][0]["hook_id"] == "sched-a-111"


# --- Command handler: cancel_scheduled ---


class TestCancelScheduledCommand:
    @pytest.fixture
    async def handler(self, db):
        from src.command_handler import CommandHandler

        orch = MagicMock()
        orch.db = db
        orch.hooks = MagicMock()
        orch.hooks._running = {}
        handler = CommandHandler(orch, db)
        return handler

    @pytest.mark.asyncio
    async def test_cancel_scheduled_hook(self, handler, db):
        await _create_project(db)
        hook = await _create_scheduled_hook(db)

        result = await handler.execute("cancel_scheduled", {"hook_id": hook.id})
        assert result["cancelled"] == hook.id

        # Verify deleted
        assert await db.get_hook(hook.id) is None

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_hook(self, handler, db):
        result = await handler.execute("cancel_scheduled", {"hook_id": "nope"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_cancel_rejects_periodic_hook(self, handler, db):
        await _create_project(db)
        periodic = Hook(
            id="periodic-hook",
            project_id="test-project",
            name="periodic",
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            prompt_template="Test",
        )
        await db.create_hook(periodic)

        result = await handler.execute("cancel_scheduled", {"hook_id": "periodic-hook"})
        assert "error" in result
        assert "not a scheduled hook" in result["error"]


# --- hook_schedules includes scheduled hooks ---


class TestHookSchedulesIncludesScheduled:
    @pytest.fixture
    async def handler(self, db):
        from src.command_handler import CommandHandler

        orch = MagicMock()
        orch.db = db
        orch.hooks = MagicMock()
        handler = CommandHandler(orch, db)
        return handler

    @pytest.mark.asyncio
    async def test_hook_schedules_shows_scheduled_hooks(self, handler, db):
        await _create_project(db)
        fire_at = time.time() + 1800
        await _create_scheduled_hook(db, fire_at=fire_at)

        result = await handler.execute("hook_schedules", {})
        assert len(result["hooks"]) == 1
        entry = result["hooks"][0]
        assert entry["schedule"] == "one-shot"
        assert entry["type"] == "scheduled"
        assert "in" in entry["next_run"]
