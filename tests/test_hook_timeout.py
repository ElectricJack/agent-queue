"""Tests for hook execution timeout (2.3)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, HookEngineConfig
from src.database import Database
from src.hooks import HookEngine
from src.models import Hook


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.hook_engine = HookEngineConfig(
        enabled=True,
        max_concurrent_hooks=2,
        hook_timeout_seconds=1,  # 1 second for fast tests
    )
    return cfg


@pytest.fixture
async def engine(db, config):
    from src.event_bus import EventBus

    engine = HookEngine(db, EventBus(), config)
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
        id="timeout-hook",
        project_id="test-project",
        name="timeout-test",
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


class TestHookTimeout:
    async def test_hook_times_out_and_records_failure(self, db, engine):
        """A stuck LLM invocation should be cancelled after hook_timeout_seconds."""
        await _create_project(db)
        hook = await _create_hook(db)

        # Mock _invoke_llm to hang forever
        async def _stuck_llm(*args, **kwargs):
            await asyncio.sleep(999)
            return ("should not reach", 0)

        engine._invoke_llm = _stuck_llm

        start = time.monotonic()
        await engine._execute_hook_inner(hook, "test-trigger")
        elapsed = time.monotonic() - start

        # Should return within ~2s (timeout is 1s + overhead)
        assert elapsed < 5, f"Hook took {elapsed:.1f}s, expected <5s"

        # Verify hook run was recorded as failed with timeout message
        runs = await db.list_hook_runs(hook_id=hook.id)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "failed"
        assert "timed out" in run.llm_response

    async def test_hook_times_out_notifies_thread(self, db, engine):
        """Timeout should post a notification to the thread."""
        await _create_project(db)
        hook = await _create_hook(db)

        async def _stuck_llm(*args, **kwargs):
            await asyncio.sleep(999)
            return ("unreachable", 0)

        engine._invoke_llm = _stuck_llm

        # Set up thread_send via _create_thread mock
        thread_send = AsyncMock()
        engine._orchestrator._create_thread = AsyncMock(return_value=(thread_send, AsyncMock()))

        await engine._execute_hook_inner(hook, "test-trigger")

        # thread_send should have been called with timeout message
        timeout_calls = [call for call in thread_send.call_args_list if "timed out" in str(call)]
        assert len(timeout_calls) == 1

    async def test_hook_times_out_notifies_channel_without_thread(self, db, engine):
        """Without a thread, timeout notification goes through the event bus."""
        await _create_project(db)
        hook = await _create_hook(db)

        async def _stuck_llm(*args, **kwargs):
            await asyncio.sleep(999)
            return ("unreachable", 0)

        engine._invoke_llm = _stuck_llm

        # No thread support
        engine._orchestrator._create_thread = None

        # Capture events emitted on the bus
        emitted_events: list[dict] = []
        engine.bus.subscribe("notify.text", lambda data: emitted_events.append(data))

        await engine._execute_hook_inner(hook, "test-trigger")

        # Bus should have received a timeout notification
        timeout_events = [e for e in emitted_events if "timed out" in e.get("message", "")]
        assert len(timeout_events) >= 1

    async def test_successful_hook_unaffected_by_timeout(self, db, engine):
        """A fast hook should complete normally despite the timeout wrapper."""
        await _create_project(db)
        hook = await _create_hook(db)

        async def _fast_llm(*args, **kwargs):
            return ("done", 42)

        engine._invoke_llm = _fast_llm

        await engine._execute_hook_inner(hook, "test-trigger")

        runs = await db.list_hook_runs(hook_id=hook.id)
        assert len(runs) == 1
        assert runs[0].status == "completed"
        assert runs[0].llm_response == "done"
        assert runs[0].tokens_used == 42


class TestHookTimeoutConfig:
    def test_default_timeout(self):
        cfg = HookEngineConfig()
        assert cfg.hook_timeout_seconds == 300

    def test_custom_timeout(self):
        cfg = HookEngineConfig(hook_timeout_seconds=60)
        assert cfg.hook_timeout_seconds == 60
