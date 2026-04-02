"""Tests for the task archiving feature.

Covers the database layer (archive, list, restore, delete, auto-archive),
the command handler layer (archive_tasks, archive_task, list_archived,
restore_task, archive_settings, markdown-note export), and the
orchestrator's automatic archiving logic.
"""

import os
import time

import pytest
from unittest.mock import MagicMock

from src.command_handler import CommandHandler
from src.config import AppConfig, ArchiveConfig, DiscordConfig
from src.database import Database
from src.models import (
    Agent, AgentOutput, AgentResult, Project, RepoSourceType, Task,
    TaskStatus, TaskType, Workspace,
)
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def _seed_project(
    db: Database,
    pid: str = "p-1",
    workspace_path: str | None = None,
) -> None:
    """Create a simple project for test tasks to reference.

    If *workspace_path* is given, a workspace row is also created in the
    workspaces table so that archive-note tests have a directory to write to.
    """
    await db.create_project(Project(id=pid, name=f"project-{pid}"))
    if workspace_path:
        import uuid
        await db.create_workspace(Workspace(
            id=f"ws-{uuid.uuid4().hex[:8]}",
            project_id=pid,
            workspace_path=workspace_path,
            source_type=RepoSourceType.LINK,
        ))


async def _seed_task(
    db: Database,
    tid: str = "t-1",
    pid: str = "p-1",
    title: str = "Test Task",
    status: TaskStatus = TaskStatus.COMPLETED,
    **kwargs,
) -> Task:
    """Create and persist a task, returning it."""
    task = Task(
        id=tid, project_id=pid, title=title,
        description=f"Description of {title}", status=status,
        **kwargs,
    )
    await db.create_task(task)
    return task


# ---------------------------------------------------------------------------
# Database layer tests
# ---------------------------------------------------------------------------

class TestArchiveTask:
    async def test_archive_completed_task(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        result = await db.archive_task("t-1")
        assert result is True

        # Task should be gone from active table
        assert await db.get_task("t-1") is None

        # Task should exist in archive
        archived = await db.get_archived_task("t-1")
        assert archived is not None
        assert archived["id"] == "t-1"
        assert archived["title"] == "Test Task"
        assert archived["status"] == "COMPLETED"
        assert archived["archived_at"] > 0

    async def test_archive_nonexistent_task_returns_false(self, db):
        result = await db.archive_task("no-such-task")
        assert result is False

    async def test_archive_preserves_task_fields(self, db):
        await _seed_project(db)
        task = Task(
            id="t-full", project_id="p-1", title="Full Task",
            description="Lots of details", priority=42,
            status=TaskStatus.COMPLETED, max_retries=5,
            branch_name="feature/foo", pr_url="https://github.com/pr/123",
        )
        await db.create_task(task)
        await db.archive_task("t-full")

        archived = await db.get_archived_task("t-full")
        assert archived["priority"] == 42
        assert archived["max_retries"] == 5
        assert archived["branch_name"] == "feature/foo"
        assert archived["pr_url"] == "https://github.com/pr/123"
        assert archived["description"] == "Lots of details"

    async def test_archive_task_with_subtasks_nulls_parent_ref(self, db):
        """Archiving a parent task should not fail due to FK constraints
        from subtasks that still reference it via parent_task_id."""
        await _seed_project(db)
        await _seed_task(db, "t-parent", status=TaskStatus.COMPLETED)
        await _seed_task(
            db, "t-child", status=TaskStatus.READY,
            title="Child", parent_task_id="t-parent",
        )

        # This used to raise "FOREIGN KEY constraint failed"
        result = await db.archive_task("t-parent")
        assert result is True

        # Parent is archived
        assert await db.get_task("t-parent") is None
        assert await db.get_archived_task("t-parent") is not None

        # Child still exists but parent_task_id is now NULL
        child = await db.get_task("t-child")
        assert child is not None
        assert child.parent_task_id is None

    async def test_archive_task_clears_agent_current_task(self, db):
        """Archiving a task should NULL out agents.current_task_id
        so the DELETE doesn't violate the FK constraint."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        agent = Agent(
            id="a-1", name="test-agent", agent_type="claude",
            current_task_id="t-1",
        )
        await db.create_agent(agent)

        result = await db.archive_task("t-1")
        assert result is True

        updated_agent = await db.get_agent("a-1")
        assert updated_agent.current_task_id is None

    async def test_archive_task_clears_workspace_lock(self, db):
        """Archiving a task should NULL out workspaces.locked_by_task_id
        so the DELETE doesn't violate the FK constraint."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        ws = Workspace(
            id="ws-1", project_id="p-1",
            workspace_path="/tmp/ws",
            source_type=RepoSourceType.LINK,
            locked_by_task_id="t-1",
            locked_at=time.time(),
        )
        await db.create_workspace(ws)

        result = await db.archive_task("t-1")
        assert result is True

        updated_ws = await db.get_workspace("ws-1")
        assert updated_ws.locked_by_task_id is None
        assert updated_ws.locked_at is None


class TestArchiveCompletedTasks:
    async def test_archive_all_completed_for_project(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")
        await _seed_task(db, "t-3", status=TaskStatus.READY, title="Active")

        archived_ids = await db.archive_completed_tasks(project_id="p-1")
        assert set(archived_ids) == {"t-1", "t-2"}

        # Active task should still be there
        assert await db.get_task("t-3") is not None

        # Archived tasks should be gone from active
        assert await db.get_task("t-1") is None
        assert await db.get_task("t-2") is None

        # Both should appear in archive
        archive = await db.list_archived_tasks(project_id="p-1")
        assert len(archive) == 2

    async def test_archive_completed_across_projects(self, db):
        await _seed_project(db, "p-1")
        await _seed_project(db, "p-2")
        await _seed_task(db, "t-1", pid="p-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", pid="p-2", status=TaskStatus.COMPLETED)

        archived_ids = await db.archive_completed_tasks()
        assert set(archived_ids) == {"t-1", "t-2"}

    async def test_archive_returns_empty_when_no_completed(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.READY)

        archived_ids = await db.archive_completed_tasks(project_id="p-1")
        assert archived_ids == []


class TestListArchivedTasks:
    async def test_list_archived_tasks_empty(self, db):
        tasks = await db.list_archived_tasks()
        assert tasks == []

    async def test_list_archived_tasks_by_project(self, db):
        await _seed_project(db, "p-1")
        await _seed_project(db, "p-2")
        await _seed_task(db, "t-1", pid="p-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", pid="p-2", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")
        await db.archive_task("t-2")

        p1_archive = await db.list_archived_tasks(project_id="p-1")
        assert len(p1_archive) == 1
        assert p1_archive[0]["id"] == "t-1"

    async def test_list_archived_respects_limit(self, db):
        await _seed_project(db)
        for i in range(5):
            await _seed_task(db, f"t-{i}", title=f"Task {i}", status=TaskStatus.COMPLETED)
        await db.archive_completed_tasks(project_id="p-1")

        limited = await db.list_archived_tasks(limit=2)
        assert len(limited) == 2


class TestCountArchivedTasks:
    async def test_count_archived_tasks(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")
        await db.archive_completed_tasks(project_id="p-1")

        assert await db.count_archived_tasks() == 2
        assert await db.count_archived_tasks(project_id="p-1") == 2
        assert await db.count_archived_tasks(project_id="p-2") == 0


class TestRestoreArchivedTask:
    async def test_restore_archived_task(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        # Verify it's archived
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

        # Restore it
        result = await db.restore_archived_task("t-1")
        assert result is True

        # Should be back in active with DEFINED status
        task = await db.get_task("t-1")
        assert task is not None
        assert task.status == TaskStatus.DEFINED
        assert task.title == "Test Task"
        assert task.retry_count == 0  # reset

        # Should be gone from archive
        assert await db.get_archived_task("t-1") is None

    async def test_restore_nonexistent_returns_false(self, db):
        result = await db.restore_archived_task("no-such-task")
        assert result is False


class TestDeleteArchivedTask:
    async def test_delete_archived_task(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await db.delete_archived_task("t-1")
        assert result is True

        # Should be gone from everywhere
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is None

    async def test_delete_nonexistent_archived_returns_false(self, db):
        result = await db.delete_archived_task("no-such-task")
        assert result is False


# ---------------------------------------------------------------------------
# Command handler tests
# ---------------------------------------------------------------------------

class TestArchiveCommands:
    """Integration tests for archive commands through the CommandHandler.

    Uses a real Orchestrator with a real database wired in.
    """

    @pytest.fixture
    async def handler(self, db, tmp_path):
        """Create a CommandHandler with a real database."""
        ws_dir = str(tmp_path / "workspaces")
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=ws_dir,
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "test.db"),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    async def test_archive_tasks_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        assert "error" not in result
        assert result["archived_count"] == 2
        assert set(result["archived_ids"]) == {"t-1", "t-2"}

    async def test_archive_tasks_nothing_to_archive(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.READY)

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        assert "message" in result
        assert "No completed tasks" in result["message"]

    async def test_archive_include_failed(self, handler, db):
        """With include_failed=True, FAILED and BLOCKED tasks are also archived."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.FAILED, title="Failed")
        await _seed_task(db, "t-3", status=TaskStatus.BLOCKED, title="Blocked")
        await _seed_task(db, "t-4", status=TaskStatus.READY, title="Active")

        result = await handler.execute("archive_tasks", {
            "project_id": "p-1",
            "include_failed": True,
        })
        assert "error" not in result
        assert result["archived_count"] == 3
        assert set(result["archived_ids"]) == {"t-1", "t-2", "t-3"}

        # Active task should remain
        assert await db.get_task("t-4") is not None

        # All three should be in the archive table
        assert await db.get_archived_task("t-1") is not None
        assert await db.get_archived_task("t-2") is not None
        assert await db.get_archived_task("t-3") is not None

    async def test_archive_single_task(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["archived"] == "t-1"
        assert result["title"] == "Test Task"

    async def test_archive_single_task_active_rejected(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.IN_PROGRESS)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" in result
        assert "IN_PROGRESS" in result["error"]

    async def test_archive_single_task_not_found(self, handler, db):
        result = await handler.execute("archive_task", {"task_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_archive_failed_task(self, handler, db):
        """Failed and blocked tasks can also be archived."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.FAILED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["status"] == "FAILED"

    async def test_archive_blocked_task(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.BLOCKED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["status"] == "BLOCKED"

    async def test_list_archived_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await handler.execute("list_archived", {"project_id": "p-1"})
        assert "error" not in result
        assert result["count"] == 1
        assert result["total"] == 1
        assert result["tasks"][0]["id"] == "t-1"

    async def test_list_archived_empty(self, handler, db):
        result = await handler.execute("list_archived", {})
        assert result["count"] == 0
        assert result["tasks"] == []

    async def test_restore_task_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await handler.execute("restore_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["restored"] == "t-1"
        assert result["new_status"] == "DEFINED"

        # Task should be back in active
        task = await db.get_task("t-1")
        assert task is not None
        assert task.status == TaskStatus.DEFINED

    async def test_restore_task_not_found(self, handler, db):
        result = await handler.execute("restore_task", {"task_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_archive_settings_command(self, handler, db):
        result = await handler.execute("archive_settings", {})
        assert "error" not in result
        assert result["enabled"] is True
        assert result["after_hours"] == 24.0
        assert "COMPLETED" in result["statuses"]
        assert result["archived_count"] == 0

    async def test_archive_settings_with_archived_tasks(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await handler.execute("archive_settings", {})
        assert result["archived_count"] == 1


# ---------------------------------------------------------------------------
# Markdown note export tests
# ---------------------------------------------------------------------------

class TestArchiveMarkdownNotes:
    """Tests that archiving writes markdown reference notes to data dir."""

    @pytest.fixture
    async def handler(self, db, tmp_path):
        ws_dir = str(tmp_path / "workspaces")
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=ws_dir,
            data_dir=str(tmp_path / "data"),
            database_path=str(tmp_path / "test.db"),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    async def test_archive_creates_markdown_files(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", title="Done task", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", title="Also done", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        assert "error" not in result
        assert result["archived_count"] == 2

        archive_dir = result["archive_dir"]
        assert archive_dir is not None
        assert os.path.isdir(archive_dir)
        assert os.path.isfile(os.path.join(archive_dir, "t-1.md"))
        assert os.path.isfile(os.path.join(archive_dir, "t-2.md"))

        with open(os.path.join(archive_dir, "t-1.md")) as f:
            content = f.read()
        assert "# Done task" in content
        assert "`t-1`" in content
        assert "COMPLETED" in content

    async def test_archive_removes_from_active_keeps_in_archive_table(
        self, handler, db, tmp_path,
    ):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        await handler.execute("archive_tasks", {"project_id": "p-1"})

        # Gone from active, present in archive table
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_archive_note_includes_result(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", title="Result task", status=TaskStatus.COMPLETED)
        await db.create_agent(Agent(id="a-1", name="Test Agent", agent_type="claude"))
        await db.save_task_result(
            "t-1", "a-1",
            AgentOutput(
                result=AgentResult.COMPLETED,
                summary="Implemented the feature successfully",
                files_changed=["src/main.py", "tests/test_main.py"],
                tokens_used=5000,
            ),
        )

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        archive_dir = result["archive_dir"]
        with open(os.path.join(archive_dir, "t-1.md")) as f:
            content = f.read()

        assert "Implemented the feature successfully" in content
        assert "`src/main.py`" in content
        assert "`tests/test_main.py`" in content
        assert "5,000" in content

    async def test_archive_note_preserves_metadata(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(
            db, "t-meta", title="Metadata task",
            status=TaskStatus.COMPLETED,
            task_type=TaskType.FEATURE,
            branch_name="feat/metadata-task",
            pr_url="https://github.com/org/repo/pull/42",
        )

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        archive_dir = result["archive_dir"]
        with open(os.path.join(archive_dir, "t-meta.md")) as f:
            content = f.read()

        assert "feature" in content
        assert "`feat/metadata-task`" in content
        assert "https://github.com/org/repo/pull/42" in content

    async def test_archive_note_preserves_dependencies(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-up", title="Upstream", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-down", title="Downstream", status=TaskStatus.COMPLETED)
        await db.add_dependency("t-down", "t-up")

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        archive_dir = result["archive_dir"]
        with open(os.path.join(archive_dir, "t-down.md")) as f:
            content = f.read()

        assert "`t-up`" in content
        assert "Dependencies" in content

    async def test_archive_note_without_result(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        archive_dir = result["archive_dir"]
        with open(os.path.join(archive_dir, "t-1.md")) as f:
            content = f.read()

        assert "No execution result recorded" in content

    async def test_single_task_archive_writes_note(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", title="Single", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result

        data_dir = str(tmp_path / "data")
        note_path = os.path.join(data_dir, "archived_tasks", "p-1", "t-1.md")
        assert os.path.isfile(note_path)
        with open(note_path) as f:
            content = f.read()
        assert "# Single" in content


# ---------------------------------------------------------------------------
# Database: archive_old_terminal_tasks tests
# ---------------------------------------------------------------------------

class TestArchiveOldTerminalTasks:
    async def test_archive_old_completed_tasks(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Manually backdate updated_at to simulate an old task
        old_time = time.time() - 86400 * 2  # 2 days ago
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600,
        )
        assert archived_ids == ["t-1"]
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_recent_tasks_not_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Task was just created — should not be archived with a 1-hour threshold
        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600,
        )
        assert archived_ids == []
        assert await db.get_task("t-1") is not None

    async def test_only_matching_statuses_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.FAILED, title="Failed")
        await _seed_task(db, "t-3", status=TaskStatus.READY, title="Active")

        # Backdate all tasks
        old_time = time.time() - 86400
        for tid in ("t-1", "t-2", "t-3"):
            await db._db.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, tid),
            )
        await db._db.commit()

        # Only archive COMPLETED (not FAILED or READY)
        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600,
        )
        assert archived_ids == ["t-1"]
        assert await db.get_task("t-2") is not None  # FAILED still active
        assert await db.get_task("t-3") is not None  # READY still active

    async def test_multiple_statuses_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.FAILED, title="Failed")
        await _seed_task(db, "t-3", status=TaskStatus.BLOCKED, title="Blocked")

        old_time = time.time() - 86400
        for tid in ("t-1", "t-2", "t-3"):
            await db._db.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, tid),
            )
        await db._db.commit()

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED", "FAILED", "BLOCKED"],
            older_than_seconds=3600,
        )
        assert set(archived_ids) == {"t-1", "t-2", "t-3"}

    async def test_empty_statuses_returns_empty(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=[], older_than_seconds=0,
        )
        assert archived_ids == []


# ---------------------------------------------------------------------------
# Orchestrator auto-archive tests
# ---------------------------------------------------------------------------

class TestAutoArchive:
    @pytest.fixture
    async def orchestrator(self, db, tmp_path):
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            database_path=str(tmp_path / "test.db"),
            archive=ArchiveConfig(
                enabled=True,
                after_hours=1.0,
                statuses=["COMPLETED", "FAILED", "BLOCKED"],
            ),
        )
        orch = Orchestrator(config)
        orch.db = db
        orch.git = MagicMock()
        return orch

    async def test_auto_archive_archives_old_tasks(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Backdate task to 2 hours ago
        old_time = time.time() - 7200
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        # Force _last_auto_archive to 0 so it runs immediately
        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_auto_archive_skips_recent_tasks(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        # Task was just created — still in active table
        assert await db.get_task("t-1") is not None

    async def test_auto_archive_respects_rate_limit(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        old_time = time.time() - 7200
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        # Set _last_auto_archive to now — should skip
        orchestrator._last_auto_archive = time.time()
        await orchestrator._auto_archive_tasks()

        # Task should still be active because rate limit prevented archiving
        assert await db.get_task("t-1") is not None

    async def test_auto_archive_disabled(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        old_time = time.time() - 86400
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        orchestrator.config.archive.enabled = False
        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        # Task should still be active because auto-archive is disabled
        assert await db.get_task("t-1") is not None
