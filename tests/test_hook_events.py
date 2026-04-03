"""Tests for new hook event types: note events, file/folder watches, and new context steps."""

from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, HookEngineConfig
from src.database import Database
from src.event_bus import EventBus
from src.hooks import HookEngine
from src.models import Hook, HookRun


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
    cfg.hook_engine = HookEngineConfig(
        enabled=True,
        max_concurrent_hooks=2,
        file_watcher_enabled=True,
        file_watcher_poll_interval=0.0,
        file_watcher_debounce_seconds=0.1,
    )
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
        prompt_template="Test prompt: {{step_0}}",
        cooldown_seconds=60,
    )
    defaults.update(overrides)
    hook = Hook(**defaults)
    await db.create_hook(hook)
    return hook


# --- Project scoping ---


class TestHookProjectScoping:
    @pytest.mark.asyncio
    async def test_event_hook_ignores_other_project(self, db, bus, engine):
        """Hook should NOT fire on events from a different project."""
        await _create_project(db, "project-a")
        await _create_project(db, "project-b")
        hook = await _create_hook(
            db,
            id="scoped-hook",
            project_id="project-a",
            name="scoped-hook",
            trigger='{"type": "event", "event_type": "task.completed"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo done", "skip_llm_if_exit_zero": true}]',
        )

        # Emit event for project-b — hook belongs to project-a, should NOT fire
        await bus.emit(
            "task.completed",
            {
                "_event_type": "task.completed",
                "task_id": "t1",
                "project_id": "project-b",
            },
        )
        await asyncio.sleep(0.1)

        assert hook.id not in engine._running
        assert len(await db.list_hook_runs(hook.id)) == 0

    @pytest.mark.asyncio
    async def test_event_hook_fires_for_own_project(self, db, bus, engine):
        """Hook SHOULD fire on events from its own project."""
        await _create_project(db, "project-a")
        hook = await _create_hook(
            db,
            id="own-proj-hook",
            project_id="project-a",
            name="own-proj-hook",
            trigger='{"type": "event", "event_type": "task.completed"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo done", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit(
            "task.completed",
            {
                "_event_type": "task.completed",
                "task_id": "t2",
                "project_id": "project-a",
            },
        )
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0


# --- Note event hooks ---


class TestNoteEventHooks:
    @pytest.mark.asyncio
    async def test_note_created_fires_hook(self, db, bus, engine):
        """Hook with event_type 'note.created' should fire on note creation events."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-hook",
            name="note-hook",
            trigger='{"type": "event", "event_type": "note.created"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo noted", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit(
            "note.created",
            {
                "project_id": "test-project",
                "note_name": "ideas.md",
                "note_path": "/tmp/notes/ideas.md",
                "title": "Ideas",
                "operation": "created",
            },
        )
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_note_updated_fires_hook(self, db, bus, engine):
        """Hook with event_type 'note.updated' should fire on note update events."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-update-hook",
            name="note-update-hook",
            trigger='{"type": "event", "event_type": "note.updated"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo updated", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit(
            "note.updated",
            {
                "project_id": "test-project",
                "note_name": "ideas.md",
                "note_path": "/tmp/notes/ideas.md",
                "title": "Ideas",
                "operation": "updated",
            },
        )
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_note_deleted_fires_hook(self, db, bus, engine):
        """Hook with event_type 'note.deleted' should fire on note deletion events."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-delete-hook",
            name="note-delete-hook",
            trigger='{"type": "event", "event_type": "note.deleted"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo deleted", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit(
            "note.deleted",
            {
                "project_id": "test-project",
                "note_name": "old-note.md",
                "note_path": "/tmp/notes/old-note.md",
                "title": "Old Note",
            },
        )
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_note_event_data_in_prompt(self, db, engine):
        """Note event data should be available in prompt templates."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-prompt-hook",
            name="note-prompt-hook",
            trigger='{"type": "event", "event_type": "note.created"}',
            cooldown_seconds=0,
            context_steps="[]",
            prompt_template="Note {{event.note_name}} was {{event.operation}} in project {{event.project_id}}",
        )

        with patch.object(engine, "_invoke_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = ("OK", 50)
            await engine._execute_hook(
                hook,
                "event:note.created",
                event_data={
                    "project_id": "test-project",
                    "note_name": "ideas.md",
                    "operation": "created",
                },
            )

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert "ideas.md" in runs[0].prompt_sent
        assert "created" in runs[0].prompt_sent


# --- File watch hook integration ---


class TestFileWatchHookIntegration:
    @pytest.mark.asyncio
    async def test_file_watch_hook_registered(self, db, engine, tmp_path):
        """Hook with file.changed trigger + watch config should register a file watch."""
        await _create_project(db)

        test_file = tmp_path / "config.yaml"
        test_file.write_text("key: value")

        trigger = json.dumps(
            {
                "type": "event",
                "event_type": "file.changed",
                "watch": {
                    "paths": [str(test_file)],
                    "project_id": "test-project",
                },
            }
        )
        await _create_hook(
            db,
            id="file-watch-hook",
            name="file-watch-hook",
            trigger=trigger,
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo changed", "skip_llm_if_exit_zero": true}]',
        )

        # Re-sync watches
        await engine._sync_file_watches()

        assert engine.file_watcher is not None
        assert engine.file_watcher.get_watch_count() == 1

    @pytest.mark.asyncio
    async def test_folder_watch_hook_registered(self, db, engine, tmp_path):
        """Hook with folder.changed trigger + watch config should register a folder watch."""
        await _create_project(db)

        src_dir = tmp_path / "src"
        src_dir.mkdir()

        trigger = json.dumps(
            {
                "type": "event",
                "event_type": "folder.changed",
                "watch": {
                    "paths": [str(src_dir)],
                    "recursive": True,
                    "extensions": [".py"],
                    "project_id": "test-project",
                },
            }
        )
        await _create_hook(
            db,
            id="folder-watch-hook",
            name="folder-watch-hook",
            trigger=trigger,
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo changed", "skip_llm_if_exit_zero": true}]',
        )

        await engine._sync_file_watches()

        assert engine.file_watcher is not None
        assert engine.file_watcher.get_watch_count() == 1

    @pytest.mark.asyncio
    async def test_stale_watches_removed(self, db, engine, tmp_path):
        """Watches for deleted/disabled hooks should be cleaned up on sync."""
        await _create_project(db)

        trigger = json.dumps(
            {
                "type": "event",
                "event_type": "file.changed",
                "watch": {
                    "paths": [str(tmp_path / "test.txt")],
                    "project_id": "test-project",
                },
            }
        )
        hook = await _create_hook(
            db,
            id="temp-hook",
            name="temp-hook",
            trigger=trigger,
            cooldown_seconds=0,
        )

        await engine._sync_file_watches()
        assert engine.file_watcher.get_watch_count() == 1

        # Disable the hook
        await db.update_hook(hook.id, enabled=False)
        await engine._sync_file_watches()
        assert engine.file_watcher.get_watch_count() == 0


# --- Named DB queries ---
# --- HookEngineConfig ---


class TestHookEngineConfigExtensions:
    def test_default_file_watcher_config(self):
        """HookEngineConfig should have file watcher defaults."""
        cfg = HookEngineConfig()
        assert cfg.file_watcher_enabled is True
        assert cfg.file_watcher_poll_interval == 10.0
        assert cfg.file_watcher_debounce_seconds == 5.0

    def test_custom_file_watcher_config(self):
        """HookEngineConfig should accept custom file watcher settings."""
        cfg = HookEngineConfig(
            file_watcher_enabled=False,
            file_watcher_poll_interval=30.0,
            file_watcher_debounce_seconds=10.0,
        )
        assert cfg.file_watcher_enabled is False
        assert cfg.file_watcher_poll_interval == 30.0
        assert cfg.file_watcher_debounce_seconds == 10.0

    @pytest.mark.asyncio
    async def test_file_watcher_disabled(self, db, bus):
        """When file_watcher_enabled=False, no FileWatcher should be created."""
        cfg = AppConfig()
        cfg.hook_engine = HookEngineConfig(
            enabled=True,
            file_watcher_enabled=False,
        )
        engine = HookEngine(db, bus, cfg)
        engine._orchestrator = MagicMock()
        await engine.initialize()

        assert engine.file_watcher is None
        await engine.shutdown()
