"""Tests for the hook engine."""

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
from src.models import Hook, HookRun, ProjectStatus


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
    from src.models import Project

    project = Project(id=project_id, name="Test Project")
    await db.create_project(project)
    return project


async def _create_hook(db, **overrides) -> Hook:
    defaults = dict(
        id="test-hook",
        project_id="test-project",
        name="test-hook",
        enabled=True,
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        context_steps="[]",
        prompt_template="Test prompt",
        cooldown_seconds=60,
    )
    defaults.update(overrides)
    hook = Hook(**defaults)
    await db.create_hook(hook)
    return hook


# --- Prompt rendering ---


class TestPromptRendering:
    def test_render_event_field(self, engine):
        result = engine._render_prompt(
            "Task: {{event.task_id}}",
            event_data={"task_id": "abc123"},
        )
        assert result == "Task: abc123"

    def test_render_event_full(self, engine):
        result = engine._render_prompt(
            "Event: {{event}}",
            event_data={"task_id": "abc", "status": "done"},
        )
        parsed = json.loads(result.replace("Event: ", ""))
        assert parsed["task_id"] == "abc"

    def test_render_no_event(self, engine):
        result = engine._render_prompt("{{event.task_id}}", event_data=None)
        assert result == ""

    def test_render_unknown_placeholder_unchanged(self, engine):
        result = engine._render_prompt("{{unknown}}", event_data=None)
        assert result == "{{unknown}}"

    def test_render_plain_text(self, engine):
        result = engine._render_prompt("Check the tunnel status.")
        assert result == "Check the tunnel status."


# --- Cooldown ---


class TestCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_blocks_refire(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(db, cooldown_seconds=3600)
        now = time.time()

        # First check: should pass (no previous run)
        assert engine._check_cooldown(hook, now) is True

        # Record a recent run
        engine._last_run_time[hook.id] = now

        # Second check: should fail (within cooldown)
        assert engine._check_cooldown(hook, now + 10) is False

        # After cooldown expires
        assert engine._check_cooldown(hook, now + 3601) is True


# --- Concurrent dedup ---


class TestConcurrentDedup:
    @pytest.mark.asyncio
    async def test_same_hook_not_run_twice(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(db)

        # Simulate an in-flight hook
        engine._running[hook.id] = asyncio.create_task(asyncio.sleep(100))

        # tick should not launch another
        engine._last_run_time[hook.id] = 0  # Force it to be "due"
        await engine.tick()

        # Should still only have 1 task
        assert len(engine._running) == 1

        # Cleanup
        engine._running[hook.id].cancel()
        try:
            await engine._running[hook.id]
        except asyncio.CancelledError:
            pass


# --- Periodic scheduling ---


class TestPeriodicScheduling:
    @pytest.mark.asyncio
    async def test_periodic_hook_fires_when_due(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=5,
            context_steps='[{"type": "shell", "command": "echo test", "skip_llm_if_exit_zero": true}]',
        )

        # No previous run — should fire
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id in engine._running

    @pytest.mark.asyncio
    async def test_periodic_hook_not_due(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=3600,
        )

        # Recent run
        engine._last_run_time[hook.id] = time.time()
        await engine.tick()
        assert hook.id not in engine._running


# --- Periodic hook timing context ---


class TestPeriodicTimingContext:
    @pytest.mark.asyncio
    async def test_periodic_hook_passes_timing_event_data(self, db, engine):
        """Periodic hooks should receive last_run_time and current_time in event_data."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo test", "skip_llm_if_exit_zero": true}]',
        )

        # Set a previous run time
        previous_run = time.time() - 100
        engine._last_run_time[hook.id] = previous_run

        # Capture what _launch_hook receives
        captured = {}
        original_launch = engine._launch_hook

        def capturing_launch(h, reason, event_data=None):
            captured["event_data"] = event_data
            captured["hook"] = h
            original_launch(h, reason, event_data=event_data)

        engine._launch_hook = capturing_launch

        # Force the interval to have elapsed
        engine._last_run_time[hook.id] = time.time() - 20
        await engine.tick()

        assert "event_data" in captured
        ed = captured["event_data"]
        assert "current_time" in ed
        assert "current_time_epoch" in ed
        assert "last_run_time" in ed
        assert "last_run_time_epoch" in ed
        assert "seconds_since_last_run" in ed
        # ISO format check
        assert "T" in ed["current_time"]
        assert "T" in ed["last_run_time"]

    @pytest.mark.asyncio
    async def test_periodic_first_run_no_last_run_time(self, db, engine):
        """First periodic run should have current_time but no last_run_time."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo test", "skip_llm_if_exit_zero": true}]',
        )

        # No previous run
        engine._last_run_time.pop(hook.id, None)

        captured = {}
        original_launch = engine._launch_hook

        def capturing_launch(h, reason, event_data=None):
            captured["event_data"] = event_data
            original_launch(h, reason, event_data=event_data)

        engine._launch_hook = capturing_launch
        await engine.tick()

        assert "event_data" in captured
        ed = captured["event_data"]
        assert "current_time" in ed
        assert "last_run_time" not in ed
        assert "seconds_since_last_run" not in ed

    def test_timing_available_in_prompt_template(self, engine):
        """Timing data should be resolvable as {{event.current_time}} in prompts."""
        event_data = {
            "current_time": "2026-03-14T00:00:00+00:00",
            "last_run_time": "2026-03-13T23:50:00+00:00",
            "seconds_since_last_run": 600,
        }
        result = engine._render_prompt(
            "Check for changes since {{event.last_run_time}} (now: {{event.current_time}})",
            event_data,
        )
        assert "2026-03-13T23:50:00+00:00" in result
        assert "2026-03-14T00:00:00+00:00" in result


# --- Event-driven firing ---


class TestEventDriven:
    @pytest.mark.asyncio
    async def test_event_hook_fires(self, db, bus, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="event-hook",
            name="event-hook",
            trigger='{"type": "event", "event_type": "task_completed"}',
            cooldown_seconds=0,
        )

        # Emit an event
        await bus.emit("task_completed", {"task_id": "t1"})

        # Give async task a moment
        await asyncio.sleep(0.1)

        # Hook should have been launched
        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_event_hook_ignores_wrong_type(self, db, bus, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="event-hook2",
            name="event-hook2",
            trigger='{"type": "event", "event_type": "task_completed"}',
            cooldown_seconds=0,
        )

        await bus.emit("task_failed", {"task_id": "t1"})
        await asyncio.sleep(0.1)

        assert hook.id not in engine._running


# --- Full pipeline with mock LLM ---


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_execute_hook_with_llm(self, db, engine):
        """Hook should render prompt and invoke LLM."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            prompt_template="Check the tunnel for {{event.project_id}}",
        )

        with patch.object(engine, "_invoke_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = ("Tunnel is running.", 150)
            await engine._execute_hook(
                hook,
                "manual",
                event_data={"project_id": "test-project"},
            )

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert runs[0].status == "completed"
        assert runs[0].tokens_used == 150
        assert runs[0].prompt_sent is not None
        assert "Check the tunnel for test-project" in runs[0].prompt_sent

    @pytest.mark.asyncio
    async def test_execute_hook_failure(self, db, engine):
        """Hook execution failure should be recorded."""
        await _create_project(db)
        hook = await _create_hook(db)

        with patch.object(engine, "_invoke_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("LLM provider down")
            await engine._execute_hook(hook, "manual")

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert runs[0].status == "failed"


# --- DB CRUD ---


class TestHookCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_hook(self, db):
        await _create_project(db)
        hook = await _create_hook(db)
        fetched = await db.get_hook(hook.id)
        assert fetched is not None
        assert fetched.name == "test-hook"
        assert fetched.enabled is True

    @pytest.mark.asyncio
    async def test_list_hooks(self, db):
        await _create_project(db)
        await _create_hook(db, id="h1", name="h1")
        await _create_hook(db, id="h2", name="h2", enabled=False)

        all_hooks = await db.list_hooks()
        assert len(all_hooks) == 2

        enabled = await db.list_hooks(enabled=True)
        assert len(enabled) == 1
        assert enabled[0].id == "h1"

    @pytest.mark.asyncio
    async def test_update_hook(self, db):
        await _create_project(db)
        await _create_hook(db)
        await db.update_hook("test-hook", enabled=False, cooldown_seconds=120)
        hook = await db.get_hook("test-hook")
        assert hook.enabled is False
        assert hook.cooldown_seconds == 120

    @pytest.mark.asyncio
    async def test_delete_hook(self, db):
        await _create_project(db)
        hook = await _create_hook(db)

        # Add a run
        run = HookRun(
            id="run1",
            hook_id=hook.id,
            project_id="test-project",
            trigger_reason="manual",
            started_at=time.time(),
        )
        await db.create_hook_run(run)

        await db.delete_hook(hook.id)
        assert await db.get_hook(hook.id) is None
        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 0

    @pytest.mark.asyncio
    async def test_hook_run_crud(self, db):
        await _create_project(db)
        hook = await _create_hook(db)

        run = HookRun(
            id="run1",
            hook_id=hook.id,
            project_id="test-project",
            trigger_reason="periodic",
            started_at=time.time(),
        )
        await db.create_hook_run(run)

        await db.update_hook_run("run1", status="completed", tokens_used=100)

        last = await db.get_last_hook_run(hook.id)
        assert last is not None
        assert last.status == "completed"
        assert last.tokens_used == 100

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1


# --- Max concurrent ---


class TestMaxConcurrent:
    @pytest.mark.asyncio
    async def test_max_concurrent_respected(self, db, engine):
        await _create_project(db)
        engine.config.hook_engine.max_concurrent_hooks = 1

        await _create_hook(
            db,
            id="h1",
            name="h1",
            trigger='{"type": "periodic", "interval_seconds": 1}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "sleep 10"}]',
        )
        await _create_hook(
            db,
            id="h2",
            name="h2",
            trigger='{"type": "periodic", "interval_seconds": 1}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "sleep 10"}]',
        )

        await engine.tick()

        # Only 1 should be running
        assert len(engine._running) == 1


# --- create_task step ---


# --- Paused project skipping ---


class TestPausedProjectSkipping:
    @pytest.mark.asyncio
    async def test_periodic_hook_skipped_when_project_paused(self, db, engine):
        """Periodic hooks should not fire when their project is paused."""
        project = await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=0,
        )
        engine._last_run_time.pop(hook.id, None)

        # Pause the project
        await db.update_project(project.id, status=ProjectStatus.PAUSED)

        await engine.tick()
        assert hook.id not in engine._running

    @pytest.mark.asyncio
    async def test_periodic_hook_fires_when_project_active(self, db, engine):
        """Periodic hooks should fire normally when project is active."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=0,
        )
        engine._last_run_time.pop(hook.id, None)

        await engine.tick()
        assert hook.id in engine._running

    @pytest.mark.asyncio
    async def test_event_hook_skipped_when_project_paused(self, db, bus, engine):
        """Event-driven hooks should not fire when their project is paused."""
        project = await _create_project(db)
        hook = await _create_hook(
            db,
            id="event-paused-hook",
            name="event-paused-hook",
            trigger='{"type": "event", "event_type": "task_completed"}',
            cooldown_seconds=0,
        )

        # Pause the project
        await db.update_project(project.id, status=ProjectStatus.PAUSED)

        await bus.emit("task_completed", {"task_id": "t1", "project_id": project.id})
        await asyncio.sleep(0.1)

        assert hook.id not in engine._running


# --- Persistence across restarts ---


class TestLastTriggeredAtPersistence:
    """Verify that hook last-run times survive daemon restarts."""

    @pytest.mark.asyncio
    async def test_last_triggered_at_persisted_on_launch(self, db, engine):
        """_launch_hook should persist last_triggered_at to the DB."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=0,
        )
        engine._last_run_time.pop(hook.id, None)

        # Fire the hook
        await engine.tick()
        assert hook.id in engine._running

        # Give the fire-and-forget persist task time to complete
        await asyncio.sleep(0.1)

        # Verify the DB was updated
        updated = await db.get_hook(hook.id)
        assert updated.last_triggered_at is not None
        assert updated.last_triggered_at > 0

    @pytest.mark.asyncio
    async def test_initialize_restores_from_last_triggered_at(self, db, bus, config):
        """A new HookEngine should read last_triggered_at from the DB on init."""
        await _create_project(db)
        # Create a hook with a known last_triggered_at
        past_time = time.time() - 500
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=3600,
        )
        await db.update_hook(hook.id, last_triggered_at=past_time)

        # Create a fresh engine (simulates daemon restart)
        engine2 = HookEngine(db, bus, config)
        engine2._orchestrator = MagicMock()
        engine2._orchestrator._notify_channel = AsyncMock()
        await engine2.initialize()

        # The engine should have restored the last run time from DB
        assert hook.id in engine2._last_run_time
        assert abs(engine2._last_run_time[hook.id] - past_time) < 0.01

        await engine2.shutdown()

    @pytest.mark.asyncio
    async def test_hook_respects_persisted_interval_after_restart(self, db, bus, config):
        """After restart, a periodic hook should NOT fire if its interval hasn't elapsed."""
        await _create_project(db)
        recent_time = time.time() - 60  # 60 seconds ago
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=0,
        )
        await db.update_hook(hook.id, last_triggered_at=recent_time)

        # Simulate a fresh daemon start
        engine2 = HookEngine(db, bus, config)
        engine2._orchestrator = MagicMock()
        engine2._orchestrator._notify_channel = AsyncMock()
        await engine2.initialize()

        await engine2.tick()

        # Hook should NOT have fired — interval not elapsed
        assert hook.id not in engine2._running

        await engine2.shutdown()

    @pytest.mark.asyncio
    async def test_hook_fires_after_interval_elapsed_post_restart(self, db, bus, config):
        """After restart, a periodic hook SHOULD fire if its interval has elapsed."""
        await _create_project(db)
        old_time = time.time() - 7200  # 2 hours ago
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=0,
        )
        await db.update_hook(hook.id, last_triggered_at=old_time)

        # Simulate a fresh daemon start
        engine2 = HookEngine(db, bus, config)
        engine2._orchestrator = MagicMock()
        engine2._orchestrator._notify_channel = AsyncMock()
        await engine2.initialize()

        await engine2.tick()

        # Hook SHOULD have fired — interval has elapsed
        assert hook.id in engine2._running

        await engine2.shutdown()

    @pytest.mark.asyncio
    async def test_fallback_to_hook_runs_if_no_last_triggered_at(self, db, bus, config):
        """Hooks without last_triggered_at should fall back to hook_runs table."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=3600,
        )
        # Don't set last_triggered_at, but create a hook_run
        run_time = time.time() - 100
        run = HookRun(
            id="run-fallback",
            hook_id=hook.id,
            project_id="test-project",
            trigger_reason="periodic",
            started_at=run_time,
        )
        await db.create_hook_run(run)

        engine2 = HookEngine(db, bus, config)
        engine2._orchestrator = MagicMock()
        engine2._orchestrator._notify_channel = AsyncMock()
        await engine2.initialize()

        # Should have fallen back to hook_run's started_at
        assert hook.id in engine2._last_run_time
        assert abs(engine2._last_run_time[hook.id] - run_time) < 0.01

        await engine2.shutdown()

    @pytest.mark.asyncio
    async def test_manual_fire_persists_last_triggered_at(self, db, engine):
        """fire_hook() should also persist last_triggered_at."""
        await _create_project(db)
        hook = await _create_hook(db, cooldown_seconds=0)

        await engine.fire_hook(hook.id)
        await asyncio.sleep(0.1)

        updated = await db.get_hook(hook.id)
        assert updated.last_triggered_at is not None
        assert updated.last_triggered_at > 0


# --- Reconciliation resilience ---


class TestReconciliationResilience:
    """Verify hooks don't re-fire after rule reconciliation regenerates hook IDs."""

    @pytest.mark.asyncio
    async def test_resolve_last_run_uses_db_timestamp_for_new_hook_id(self, db, engine):
        """After reconciliation creates a hook with a new UUID, _resolve_last_run
        should use the DB-persisted last_triggered_at instead of defaulting to 0."""
        await _create_project(db)
        recent = time.time() - 300  # 5 minutes ago
        hook = await _create_hook(
            db,
            id="rule-test-abc123",
            last_triggered_at=recent,
        )

        # Simulate reconciliation: _last_run_time has NO entry for this hook
        engine._last_run_time.pop(hook.id, None)

        result = engine._resolve_last_run(hook)
        assert result == recent
        # Should also cache it for subsequent calls
        assert engine._last_run_time[hook.id] == recent

    @pytest.mark.asyncio
    async def test_periodic_hook_respects_db_timestamp_after_reconciliation(self, db, engine):
        """A periodic hook with a recent last_triggered_at in the DB should NOT
        fire if its interval hasn't elapsed, even when the in-memory cache has
        no entry (simulating post-reconciliation state)."""
        await _create_project(db)
        recent = time.time() - 60  # 1 minute ago
        hook = await _create_hook(
            db,
            id="rule-test-def456",
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=60,
            last_triggered_at=recent,
        )

        # Clear in-memory cache to simulate reconciliation creating new hook ID
        engine._last_run_time.pop(hook.id, None)

        await engine.tick()

        # Should NOT have fired — interval (3600s) hasn't elapsed
        assert hook.id not in engine._running

    @pytest.mark.asyncio
    async def test_orphaned_running_hook_cancelled_after_reconciliation(self, db, engine):
        """When reconciliation deletes a hook from the DB, its in-flight asyncio
        task should be cancelled and removed from _running."""
        await _create_project(db)

        # Create a hook and simulate it being in-flight
        hook = await _create_hook(
            db,
            id="rule-test-old-id",
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=60,
        )

        # Put a fake running task in _running
        fake_task = asyncio.create_task(asyncio.sleep(999))
        engine._running["rule-test-old-id"] = fake_task

        # Now delete the hook from DB (simulating reconciliation)
        await db.delete_hook("rule-test-old-id")

        # tick() should detect the orphaned _running entry and cancel it
        await engine.tick()

        # Give the cancelled task time to finish
        try:
            await asyncio.wait_for(fake_task, timeout=0.1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        assert fake_task.done()
        # Next tick should clean up the finished orphaned entry
        await engine.tick()
        assert "rule-test-old-id" not in engine._running
