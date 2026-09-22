"""Tests for the task archiving feature.

Covers the database layer (archive, list, restore, delete, auto-archive),
the command handler layer (archive_tasks, archive_task, list_archived,
restore_task, archive_settings, markdown-note export), and the
orchestrator's automatic archiving logic.
"""

import os
import time

import pytest
from sqlalchemy import select, text
from unittest.mock import MagicMock

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, ArchiveConfig, DiscordConfig
from src.database import Database
from src.models import (
    Agent,
    AgentOutput,
    AgentResult,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskCompletion,
    TaskStatus,
    TaskType,
    Workspace,
)
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path, request):
    database = Database(lease_dsn("test.db"))
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

        await db.create_workspace(
            Workspace(
                id=f"ws-{uuid.uuid4().hex[:8]}",
                project_id=pid,
                workspace_path=workspace_path,
                source_type=RepoSourceType.LINK,
            )
        )


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
        id=tid,
        project_id=pid,
        title=title,
        description=f"Description of {title}",
        status=status,
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
            id="t-full",
            project_id="p-1",
            title="Full Task",
            description="Lots of details",
            priority=42,
            status=TaskStatus.COMPLETED,
            max_retries=5,
            branch_name="feature/foo",
            pr_url="https://github.com/pr/123",
        )
        await db.create_task(task)
        await db.archive_task("t-full")

        archived = await db.get_archived_task("t-full")
        assert archived["priority"] == 42
        assert archived["max_retries"] == 5
        assert archived["branch_name"] == "feature/foo"
        assert archived["pr_url"] == "https://github.com/pr/123"
        assert archived["description"] == "Lots of details"

    async def test_archive_task_refuses_open_child(self, db):
        """Archiving a parent with a non-terminal child is refused — the
        subtree only archives together once every descendant is terminal
        (spec §7)."""
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        await _seed_task(db, "t-parent", status=TaskStatus.COMPLETED)
        await _seed_task(
            db,
            "t-child",
            status=TaskStatus.READY,
            title="Child",
            parent_task_id="t-parent",
        )

        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("t-parent")
        assert exc.value.code == "open_descendants"

        # Nothing was archived or removed.
        assert await db.get_task("t-parent") is not None
        assert await db.get_task("t-child") is not None
        assert await db.get_archived_task("t-parent") is None

    async def test_archive_task_with_subtasks_archives_subtree_together(self, db):
        """Archiving a parent task should not fail due to FK constraints
        from subtasks that still reference it via parent_task_id — once
        every descendant is terminal, the whole subtree archives together."""
        await _seed_project(db)
        await _seed_task(db, "t-parent", status=TaskStatus.COMPLETED)
        await _seed_task(
            db,
            "t-child",
            status=TaskStatus.COMPLETED,
            title="Child",
            parent_task_id="t-parent",
        )

        # This used to raise "FOREIGN KEY constraint failed"
        result = await db.archive_task("t-parent")
        assert result is True

        # Parent and child are both archived together.
        assert await db.get_task("t-parent") is None
        assert await db.get_archived_task("t-parent") is not None

        assert await db.get_task("t-child") is None
        archived_child = await db.get_archived_task("t-child")
        assert archived_child is not None
        assert archived_child["parent_task_id"] == "t-parent"

    async def test_archive_task_clears_agent_current_task(self, db):
        """Archiving a task should NULL out agents.current_task_id
        so the DELETE doesn't violate the FK constraint."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        agent = Agent(
            id="a-1",
            name="test-agent",
            profile_id="claude",
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
            id="ws-1",
            project_id="p-1",
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


class TestDeleteArchivedTask:
    async def test_delete_archived_task(self, db):
        from src.models import TaskCompletion

        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.save_task_completion(
            TaskCompletion(id="close-1", task_id="t-1", outcome="pass", completed_at=1.0)
        )
        await db.archive_task("t-1")

        result = await db.delete_archived_task("t-1")
        assert result is True

        # Should be gone from everywhere
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is None
        assert await db.get_task_completion("t-1") is None

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
            database=DatabaseConfig(url=lease_dsn("test.db")),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    async def test_archive_tasks_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")

        result = await handler.execute("archive_task", {"project_id": "p-1"})
        assert "error" not in result
        assert result["archived_count"] == 2
        assert set(result["archived_ids"]) == {"t-1", "t-2"}

    async def test_archive_tasks_nothing_to_archive(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.READY)

        result = await handler.execute("archive_task", {"project_id": "p-1"})
        assert "message" in result
        assert "No completed tasks" in result["message"]

    async def test_archive_include_failed(self, handler, db):
        """With include_failed=True, FAILED and BLOCKED tasks are also archived."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.FAILED, title="Failed")
        await _seed_task(db, "t-3", status=TaskStatus.BLOCKED, title="Blocked")
        await _seed_task(db, "t-4", status=TaskStatus.READY, title="Active")

        result = await handler.execute(
            "archive_task",
            {
                "project_id": "p-1",
                "include_failed": True,
            },
        )
        assert "error" not in result
        assert result["archived_count"] == 3
        assert set(result["archived_ids"]) == {"t-1", "t-2", "t-3"}

        # Active task should remain
        assert await db.get_task("t-4") is not None

        # All three should be in the archive table
        assert await db.get_archived_task("t-1") is not None
        assert await db.get_archived_task("t-2") is not None
        assert await db.get_archived_task("t-3") is not None

    async def test_archive_bulk_skips_root_with_open_descendant(self, handler, db):
        """A bulk selection lists every terminal task individually, but a
        COMPLETED parent with an open child is refused by ``archive_task``'s
        subtree check — the batch must not abort, just skip that root and
        report it (controller ruling on task 7 review)."""
        await _seed_project(db)
        await _seed_task(db, "t-lone", status=TaskStatus.COMPLETED, title="Lone")
        await _seed_task(db, "t-parent", status=TaskStatus.COMPLETED, title="Parent")
        await _seed_task(
            db,
            "t-child",
            status=TaskStatus.READY,
            title="Child",
            parent_task_id="t-parent",
        )

        result = await handler.execute("archive_task", {"project_id": "p-1"})
        assert "error" not in result
        assert result["archived_count"] == 1
        assert result["archived_ids"] == ["t-lone"]
        assert result["skipped"] == [
            {"task_id": "t-parent", "code": "hierarchy.open_descendants", "detail": "t-child"}
        ]

        assert await db.get_archived_task("t-lone") is not None
        assert await db.get_archived_task("t-parent") is None
        assert await db.get_task("t-parent") is not None
        assert await db.get_task("t-child") is not None

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

    async def test_archive_settings_omits_history_that_can_now_archive(self, handler, db):
        """A completed audit-only root no longer appears as a blocked backlog item."""
        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, "root")
        await _enable_hierarchy_mode(db)
        await _age(db, "root")
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        result = await handler.execute("archive_settings", {})
        assert result["blocked_count"] == 0
        assert result["blocked"] == []
        assert await db.get_archived_task("root") is not None

    async def test_abandon_undelivered_requires_reason_and_operator_and_records_event(
        self, handler, db
    ):
        """The public escape hatch is deliberate, auditable, and never worker-run."""
        await _seed_development_project(db)
        await _seed_task(
            db,
            "undelivered",
            pid="p-dev",
            status=TaskStatus.COMPLETED,
            repo_id="web-repo",
            branch_name="aq/undelivered",
        )
        await db.save_task_completion(
            TaskCompletion(
                id="close-undelivered",
                task_id="undelivered",
                outcome="pass",
                commits=["a" * 40],
                completed_at=time.time(),
            )
        )

        missing_reason = await handler._cmd_archive_task(
            {"task_id": "undelivered", "abandon_undelivered": True}
        )
        assert missing_reason["error"] == "--abandon-undelivered requires --reason."

        handler._current_scope = {
            "kind": "session",
            "session_id": "worker",
            "project_id": "p-dev",
            "elevated": False,
        }
        worker = await handler._cmd_archive_task(
            {
                "task_id": "undelivered",
                "abandon_undelivered": True,
                "reason": "superseded by manual patch",
            },
        )
        assert worker["code"] == "hierarchy.abandon_not_for_sessions"

        handler._current_scope = None
        archived = await handler._cmd_archive_task(
            {
                "task_id": "undelivered",
                "abandon_undelivered": True,
                "reason": "superseded by manual patch",
            },
        )
        assert archived["archived"] == "undelivered"
        comments = (await db.list_task_comments("undelivered", project_id="p-dev"))["comments"]
        assert any(
            "Delivery abandoned before archive by operator: superseded by manual patch" in row["body"]
            for row in comments
        )
        assert len(
            await db.get_recent_events(
                event_type="task.delivery_abandoned", task_id="undelivered"
            )
        ) == 1


# ---------------------------------------------------------------------------
# Markdown note export tests
# ---------------------------------------------------------------------------


class TestArchiveMarkdownNotes:
    """Tests that archiving writes task summary notes to the vault."""

    @pytest.fixture
    async def handler(self, db, tmp_path):
        ws_dir = str(tmp_path / "workspaces")
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=ws_dir,
            data_dir=str(tmp_path / "data"),
            database=DatabaseConfig(url=lease_dsn("test.db")),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    @staticmethod
    def _find_note(vault_root: str, project_id: str, task_id: str) -> str | None:
        """Find a task summary note by task ID using glob."""
        import glob as _glob

        pattern = os.path.join(
            vault_root, "projects", project_id, "tasks", "**", f"*({task_id}).md"
        )
        matches = _glob.glob(pattern, recursive=True)
        return matches[0] if matches else None

    async def test_archive_creates_markdown_files(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", title="Done task", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", title="Also done", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_task", {"project_id": "p-1"})
        assert "error" not in result
        assert result["archived_count"] == 2

        vault_root = handler.config.vault_root
        note1 = self._find_note(vault_root, "p-1", "t-1")
        note2 = self._find_note(vault_root, "p-1", "t-2")
        assert note1 is not None
        assert note2 is not None

        with open(note1) as f:
            content = f.read()
        assert "Done task" in content
        assert "t-1" in content
        assert "COMPLETED" in content

    async def test_archive_removes_from_active_keeps_in_archive_table(
        self,
        handler,
        db,
        tmp_path,
    ):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        await handler.execute("archive_task", {"project_id": "p-1"})

        # Gone from active, present in archive table
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_archive_note_includes_result(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", title="Result task", status=TaskStatus.COMPLETED)
        await db.create_agent(Agent(id="a-1", name="Test Agent", profile_id="claude"))
        await db.save_task_result(
            "t-1",
            "a-1",
            AgentOutput(
                result=AgentResult.COMPLETED,
                summary="Implemented the feature successfully",
                files_changed=["src/main.py", "tests/test_main.py"],
                tokens_used=5000,
            ),
        )

        await handler.execute("archive_task", {"project_id": "p-1"})
        vault_root = handler.config.vault_root
        note = self._find_note(vault_root, "p-1", "t-1")
        assert note is not None
        with open(note) as f:
            content = f.read()

        assert "Implemented the feature successfully" in content
        assert "`src/main.py`" in content
        assert "`tests/test_main.py`" in content
        assert "5,000" in content

    async def test_archive_note_preserves_metadata(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(
            db,
            "t-meta",
            title="Metadata task",
            status=TaskStatus.COMPLETED,
            task_type=TaskType.FEATURE,
            branch_name="feat/metadata-task",
            pr_url="https://github.com/org/repo/pull/42",
        )

        await handler.execute("archive_task", {"project_id": "p-1"})
        vault_root = handler.config.vault_root
        note = self._find_note(vault_root, "p-1", "t-meta")
        assert note is not None
        # Notes now use a flat per-project tasks/ layout (no task-type subdirs).
        assert "/tasks/" in note
        with open(note) as f:
            content = f.read()

        # task_type is no longer rendered in the archive note body;
        # branch and PR URL still are.
        assert "`feat/metadata-task`" in content
        assert "https://github.com/org/repo/pull/42" in content

    async def test_archive_note_preserves_dependencies(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-up", title="Upstream", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-down", title="Downstream", status=TaskStatus.COMPLETED)
        await db.add_dependency("t-down", "t-up")

        await handler.execute("archive_task", {"project_id": "p-1"})
        vault_root = handler.config.vault_root
        note = self._find_note(vault_root, "p-1", "t-down")
        assert note is not None
        with open(note) as f:
            content = f.read()

        assert "`t-up`" in content
        assert "Dependencies" in content

    async def test_archive_note_without_result(self, handler, db, tmp_path):
        """When no execution result exists, the summary falls back to the task description."""
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        await handler.execute("archive_task", {"project_id": "p-1"})
        vault_root = handler.config.vault_root
        note = self._find_note(vault_root, "p-1", "t-1")
        assert note is not None
        with open(note) as f:
            content = f.read()

        # No agent result → falls back to task description
        assert "Description of Test Task" in content

    async def test_single_task_archive_writes_note(self, handler, db, tmp_path):
        ws = str(tmp_path / "workspaces" / "p-1")
        await _seed_project(db, workspace_path=ws)
        await _seed_task(db, "t-1", title="Single", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result

        vault_root = handler.config.vault_root
        note = self._find_note(vault_root, "p-1", "t-1")
        assert note is not None
        with open(note) as f:
            content = f.read()
        assert "Single" in content


# ---------------------------------------------------------------------------
# Database: archive_old_terminal_tasks tests
# ---------------------------------------------------------------------------


class TestArchiveOldTerminalTasks:
    async def test_archive_old_completed_tasks(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Manually backdate updated_at to simulate an old task
        old_time = time.time() - 86400 * 2  # 2 days ago
        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                {"t": old_time, "id": "t-1"},
            )

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"],
            older_than_seconds=3600,
        )
        assert archived_ids == ["t-1"]
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_recent_tasks_not_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Task was just created — should not be archived with a 1-hour threshold
        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"],
            older_than_seconds=3600,
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
        async with db._engine.begin() as conn:
            for tid in ("t-1", "t-2", "t-3"):
                await conn.execute(
                    text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                    {"t": old_time, "id": tid},
                )

        # Only archive COMPLETED (not FAILED or READY)
        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"],
            older_than_seconds=3600,
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
        async with db._engine.begin() as conn:
            for tid in ("t-1", "t-2", "t-3"):
                await conn.execute(
                    text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                    {"t": old_time, "id": tid},
                )

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED", "FAILED", "BLOCKED"],
            older_than_seconds=3600,
        )
        assert set(archived_ids) == {"t-1", "t-2", "t-3"}

    async def test_empty_statuses_returns_empty(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=[],
            older_than_seconds=0,
        )
        assert archived_ids == []


# ---------------------------------------------------------------------------
# Orchestrator auto-archive tests
# ---------------------------------------------------------------------------


class TestAutoArchive:
    @pytest.fixture
    async def orchestrator(self, db, tmp_path):
        config = AppConfig(
            data_dir=str(tmp_path / "data"),
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            database=DatabaseConfig(url=lease_dsn("test.db")),
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
        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                {"t": old_time, "id": "t-1"},
            )

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
        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                {"t": old_time, "id": "t-1"},
            )

        # Set _last_auto_archive to now — should skip
        orchestrator._last_auto_archive = time.time()
        await orchestrator._auto_archive_tasks()

        # Task should still be active because rate limit prevented archiving
        assert await db.get_task("t-1") is not None

    async def test_auto_archive_disabled(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        old_time = time.time() - 86400
        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                {"t": old_time, "id": "t-1"},
            )

        orchestrator.config.archive.enabled = False
        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        # Task should still be active because auto-archive is disabled
        assert await db.get_task("t-1") is not None


class TestArchiveReferences:
    @pytest.mark.parametrize("resolved,shared", [(True, False), (False, False), (False, True)])
    async def test_archive_unlinks_only_its_gate_waiters(self, db, resolved, shared):
        from sqlalchemy import update
        from src.database.tables import gates

        await _seed_project(db)
        await _seed_task(db, "archiving")
        await _seed_task(db, "remaining", status=TaskStatus.READY)
        waiters = ["archiving", "remaining"] if shared else ["archiving"]
        gate_id, _ = await db.create_gate("p-1", "human", "Review", waiter_task_ids=waiters)
        if resolved:
            async with db._engine.begin() as conn:
                await conn.execute(
                    update(gates)
                    .where(gates.c.id == gate_id)
                    .values(status="resolved", resolution="approved")
                )
        assert await db.archive_task("archiving") is True
        assert await db.get_task("archiving") is None
        assert await db.get_archived_task("archiving") is not None
        assert await db.get_gate_waiters(gate_id) == ({"remaining"} if shared else set())
        gate = await db.get_gate(gate_id)
        assert gate["status"] == ("resolved" if resolved else "open" if shared else "expired")
        if resolved:
            assert gate["resolution"] == "approved"
        if shared:
            assert (await db.get_task("remaining")).is_blocked is True

    async def test_archive_preserves_history_and_releases_resource_references(self, db):
        from sqlalchemy import insert, select
        from src.database.tables import task_workspace_requirements
        from src.models import AgentProfile, SessionRecord

        await _seed_project(db)
        await db.create_profile(AgentProfile(id="worker", name="Worker"))
        await db.create_agent(Agent(id="history-agent", name="Worker", profile_id="worker"))
        await _seed_task(db, "history-task", assigned_agent_id="history-agent")
        await db.create_session(
            SessionRecord(
                id="history-session",
                task_id="history-task",
                project_id="p-1",
                agent_id="history-agent",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="history-session",
                lifecycle="task",
                state="stopped",
                work_dir="/tmp",
                epoch="test",
                instance_token="test",
                started_at=time.time(),
            )
        )
        await db.create_workspace(
            Workspace(
                id="history-workspace",
                project_id="p-1",
                workspace_path="/tmp/history-workspace",
                source_type=RepoSourceType.LINK,
                locked_by_task_id="history-task",
                locked_by_agent_id="history-agent",
                locked_at=time.time(),
            )
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(task_workspace_requirements).values(task_id="history-task", kind_id="repo")
            )
        comment = await db.add_task_comment(
            "history-task", "Durable finding", author_kind="user", author_id="local"
        )
        assert await db.archive_completed_tasks("p-1") == ["history-task"]
        session = await db.get_session("history-session")
        assert session is not None and session.task_id is None and session.state == "stopped"
        workspace = await db.get_workspace("history-workspace")
        assert workspace.locked_by_task_id is None
        assert workspace.locked_by_agent_id is None
        assert workspace.locked_at is None
        async with db._engine.connect() as conn:
            assert (await conn.execute(select(task_workspace_requirements))).first() is None
        assert (await db.list_task_comments("history-task", project_id="p-1"))["comments"] == [
            comment
        ]


class TestArchiveLiveSessions:
    @pytest.mark.parametrize("state", ["starting", "running", "draining"])
    @pytest.mark.parametrize("subtree", [False, True])
    async def test_archive_refuses_live_workers_without_mutating_history(self, db, state, subtree):
        from sqlalchemy import update
        from src.database.queries.hierarchy_queries import HierarchyError
        from src.database.tables import tasks
        from src.models import AgentProfile, SessionRecord

        await _seed_project(db)
        await db.create_profile(AgentProfile(id="worker", name="Worker"))
        await _seed_task(db, "root", status=TaskStatus.IN_PROGRESS)
        target = "root"
        if subtree:
            await _seed_task(db, "child", status=TaskStatus.READY)
            await db.add_dependency("child", "root", "parent-child")
            target = "child"
        async with db._engine.begin() as conn:
            await conn.execute(update(tasks).values(status="COMPLETED"))
        await db.create_session(
            SessionRecord(
                id="live-session",
                task_id=target,
                project_id="p-1",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="live-session",
                lifecycle="task",
                state=state,
                work_dir="/tmp",
                epoch="test",
                instance_token="test",
                started_at=time.time(),
            )
        )
        await db.create_workspace(
            Workspace(
                id="held",
                project_id="p-1",
                workspace_path="/tmp/held",
                source_type=RepoSourceType.LINK,
                locked_by_task_id=target,
                locked_at=123.0,
            )
        )
        comment = await db.add_task_comment(
            target, "Still working", author_kind="user", author_id="local"
        )
        gate_id, _ = await db.create_gate("p-1", "human", "Review", waiter_task_ids=[target])

        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("root")
        assert exc.value.code == "live_descendants"
        assert (await db.get_session("live-session")).task_id == target
        assert (await db.get_workspace("held")).locked_by_task_id == target
        assert (await db.get_workspace("held")).locked_at == 123.0
        assert (await db.get_gate(gate_id))["status"] == "open"
        assert await db.get_gate_waiters(gate_id) == {target}
        assert (await db.list_task_comments(target, project_id="p-1"))["comments"] == [comment]
        for tid in {"root", target}:
            assert await db.get_task(tid) is not None
            assert await db.get_archived_task(tid) is None

    @pytest.mark.parametrize("aged", [False, True])
    async def test_bulk_archive_skips_live_worker_and_continues(self, db, aged):
        from sqlalchemy import update
        from src.database.tables import tasks
        from src.models import AgentProfile, SessionRecord

        await _seed_project(db)
        await db.create_profile(AgentProfile(id="worker", name="Worker"))
        await _seed_task(db, "busy")
        await _seed_task(db, "finished")
        await db.create_session(
            SessionRecord(
                id="live-session",
                task_id="busy",
                project_id="p-1",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="live-session",
                lifecycle="task",
                state="draining",
                work_dir="/tmp",
                epoch="test",
                instance_token="test",
                started_at=time.time(),
            )
        )
        async with db._engine.begin() as conn:
            await conn.execute(update(tasks).values(updated_at=time.time() - 10000))
        archived = (
            await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=1)
            if aged
            else await db.archive_completed_tasks("p-1")
        )
        assert archived == ["finished"]
        assert await db.get_task("busy") is not None
        assert (await db.get_session("live-session")).task_id == "busy"


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS])
@pytest.mark.parametrize("aged", [False, True])
async def test_bulk_archive_rechecks_root_status_after_selection(db, monkeypatch, status, aged):
    from sqlalchemy import update
    from src.database.tables import tasks

    await _seed_project(db)
    await _seed_task(db, "reopened")
    await _seed_task(db, "finished")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).values(updated_at=time.time() - 10000))
    archive = db.archive_task

    async def reopen_before_archive(task_id, **kwargs):
        if task_id == "reopened":
            await db.update_task(task_id, status=status)
        return await archive(task_id, **kwargs)

    monkeypatch.setattr(db, "archive_task", reopen_before_archive)
    archived = (
        await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=1)
        if aged
        else await db.archive_completed_tasks("p-1")
    )
    assert archived == ["finished"]
    assert (await db.get_task("reopened")).status == status
    assert await db.get_archived_task("reopened") is None


async def test_completion_history_survives_archive_and_restore(db):
    from src.models import TaskCompletion

    await _seed_project(db)
    await db.create_task(
        Task(
            id="history",
            project_id="p-1",
            title="Done",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id="completion",
            task_id="history",
            outcome="pass",
            summary="Keep findings",
            completed_at=1.0,
        )
    )
    await db.archive_task("history")
    assert (await db.get_task_completion("history")).summary == "Keep findings"

    # Restoration recreates the active identity before deleting its archive snapshot.
    await db.create_task(
        Task(
            id="history",
            project_id="p-1",
            title="Restored",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.delete_archived_task("history")
    assert (await db.get_task_completion("history")).summary == "Keep findings"
    await db.delete_task("history")
    assert await db.get_task_completion("history") is None


# ---------------------------------------------------------------------------
# Integration bookkeeping that outlives the task it describes
# ---------------------------------------------------------------------------


async def _seed_hierarchy_project(db: Database, pid: str = "p-hier") -> None:
    """A hierarchy-mode project with one repo, as the operator's install has."""
    from src.models import RepoConfig

    await db.create_project(Project(id=pid, name=f"project-{pid}"))
    await db.create_repo(
        RepoConfig(id=f"repo-{pid}", project_id=pid, source_type=RepoSourceType.LINK)
    )


async def _enable_hierarchy_mode(db: Database, pid: str = "p-hier") -> None:
    """Switch the project to hierarchy mode once its task graph is built."""
    await db.update_project(
        pid,
        hierarchical_integration_mode="hierarchy",
        integration_repository_id=f"repo-{pid}",
    )


async def _seed_parent_episode(db: Database, parent_task_id: str, pid: str = "p-hier") -> str:
    """Write the append-only parent-episode row hierarchy completion writes."""
    from sqlalchemy import insert

    from src.database.tables import integration_parent_episodes

    episode_id = f"ep-{parent_task_id}"
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id=episode_id,
                parent_task_id=parent_task_id,
                repository_id=f"repo-{pid}",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
    return episode_id


async def _age(db: Database, *task_ids: str) -> None:
    old = time.time() - 86400
    async with db._engine.begin() as conn:
        for tid in task_ids:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :t WHERE id = :id"), {"t": old, "id": tid}
            )


class TestArchiveWithIntegrationBookkeeping:
    """The production failure: one integration-tracked root killed the sweep."""

    async def _seed(self, db):
        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.IN_PROGRESS)
        await _seed_task(db, "kid", pid="p-hier", status=TaskStatus.READY)
        await db.add_dependency("kid", "root", "parent-child")
        async with db._engine.begin() as conn:
            for tid in ("root", "kid"):
                await conn.execute(
                    text("UPDATE tasks SET status = 'COMPLETED' WHERE id = :id"), {"id": tid}
                )
        await _seed_parent_episode(db, "root")
        await _seed_task(db, "solo", pid="p-hier", status=TaskStatus.COMPLETED)
        await _enable_hierarchy_mode(db)
        await _age(db, "root", "kid", "solo")

    async def test_sweep_survives_an_integration_tracked_root(self, db):
        await self._seed(db)

        archived = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600
        )

        # Audit history no longer pins a finished root in the active table.
        assert archived == ["root", "solo"]
        assert await db.get_archived_task("solo") is not None
        assert await db.get_archived_task("root") is not None
        assert await db.get_archived_task("kid") is not None

    async def test_archive_task_retains_history_instead_of_raising_integrityerror(self, db):
        await self._seed(db)
        assert await db.archive_task("root") is True
        assert await db.get_archived_task("root") is not None

    async def test_delete_task_refuses_instead_of_raising_integrityerror(self, db):
        from src.database.queries.hierarchy_queries import HierarchyError

        await self._seed(db)

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("root", cascade=True, branch_policy="keep")
        assert exc.value.code == "integration_history_retained"
        assert await db.get_task("root") is not None

    async def test_blocked_roots_are_reported_read_only(self, db):
        """The report reads what the sweep actually recorded — it derives nothing."""
        await self._seed(db)
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        blocked = await db.list_archive_blocked_roots(
            statuses=["COMPLETED"], older_than_seconds=3600
        )

        assert blocked.total == 0
        assert blocked.roots == []
        # Read-only: the report itself moved nothing the sweep had not.
        assert await db.get_task("root") is None
        assert await db.get_archived_task("solo") is not None

    async def test_nothing_is_reported_before_the_sweep_has_tried(self, db):
        """No record, no report — the sweep is the only thing that decides."""
        await self._seed(db)

        blocked = await db.list_archive_blocked_roots(
            statuses=["COMPLETED"], older_than_seconds=3600
        )

        assert blocked.total == 0
        assert blocked.roots == []


async def _seed_settled_batch_repair(db: Database, verifier_task_id: str) -> str:
    """A *completed* batch repair operation whose verifier was *verifier_task_id*.

    Settled on purpose: ``archive_task``'s active-repair guard only looks at
    ``active``/``escalated``/``human_required`` operations, so a refusal can
    only come from the foreign-key guard.
    """
    from sqlalchemy import insert

    from src.database.tables import integration_repair_operations

    operation_id = f"op-{verifier_task_id}"
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_repair_operations).values(
                id=operation_id,
                target_kind="batch",
                batch_id="b-1",
                parent_task_id=None,
                episode_id="ep-batch",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                verifier_task_id=verifier_task_id,
                created_at=1.0,
                updated_at=1.0,
            )
        )
    return operation_id


class TestArchiveIntegrationReferenceScope:
    """How far the integration-reference refusal reaches.

    The class above and ``tests/test_hierarchy_archive_delete.py`` cover a task
    the bookkeeping names directly.  These pin the rest: a named *descendant*
    holds its root in either integration mode, the explicit bulk path skips the
    held task like the hourly sweep does, and a batch repair's verifier is
    named as well as a parent repair's.
    """

    @pytest.mark.parametrize("hierarchical", [False, True], ids=["legacy", "hierarchy"])
    async def test_a_held_descendant_pins_its_root(self, db, hierarchical):
        """The subtree moves together, so a child the bookkeeping names holds the root."""
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_task(
            db, "kid", pid="p-hier", status=TaskStatus.COMPLETED, parent_task_id="root"
        )
        await _seed_parent_episode(db, "kid")
        if hierarchical:
            await _enable_hierarchy_mode(db)

        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("root", cascade=True, branch_policy="keep")
        assert exc.value.code == "integration_history_retained"
        assert exc.value.context["references"] == [
            {"task_id": "kid", "table": "integration_parent_episodes", "column": "parent_task_id"}
        ]
        assert await db.get_task("root") is not None
        assert await db.get_task("kid") is not None

    async def test_archive_completed_tasks_skips_the_held_task(self, db):
        """The explicit bulk command archives the rest instead of stopping."""
        await _seed_hierarchy_project(db)
        await _seed_task(db, "held", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_task(db, "other", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, "held")

        archived = await db.archive_completed_tasks(project_id="p-hier")

        assert sorted(archived) == ["held", "other"]
        assert await db.get_archived_task("other") is not None
        assert await db.get_archived_task("held") is not None

    async def test_a_settled_batch_repairs_verifier_is_archived_with_history(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_settled_batch_repair(db, "t-1")

        assert await db.archive_task("t-1") is True
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None


class TestArchiveRefusalRecord:
    """The sweep records why each root it skipped was refused (F3)."""

    async def _eligible(self, db, *task_ids: str) -> None:
        await _age(db, *task_ids)

    async def test_records_the_refusal_the_sweep_actually_hit(self, db):
        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, "root")
        await _enable_hierarchy_mode(db)
        await self._eligible(db, "root")

        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        assert await db.get_task_meta("root", "archive_refusal") is None
        assert await db.get_archived_task("root") is not None

    async def test_records_a_refusal_the_report_could_not_have_derived(self, db):
        """A sealed root: the old report re-derived conditions and missed this one."""
        from sqlalchemy import insert

        from src.database.tables import (
            integration_batch_members,
            integration_batches,
            integration_review_evidence,
        )

        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _enable_hierarchy_mode(db)
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(integration_batches).values(
                    id="b",
                    project_id="p-hier",
                    repository_id="repo-p-hier",
                    request_id="r",
                    trigger="manual",
                    source_manifest_digest="sha256:" + "4" * 64,
                    base_sha="a" * 40,
                    lifecycle="sealing",
                    current_revision=0,
                    integration_branch="refs/heads/aq/integration/p-hier/1",
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state="pending",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_review_evidence).values(
                    id="rev",
                    source_task_id="root",
                    repository_id="repo-p-hier",
                    source_base="a" * 40,
                    reviewed_head_sha="b" * 40,
                    reviewed_tree_sha="c" * 40,
                    reviewer_task_id="reviewer",
                    reviewer_session_attempt_id=None,
                    review_kind="leaf",
                    generation=1,
                    verdict="approved",
                    evidence={"decision": "approved"},
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_batch_members).values(
                    batch_id="b",
                    ordinal=0,
                    task_id="root",
                    repository_id="repo-p-hier",
                    source_base_sha="a" * 40,
                    reviewed_head_sha="b" * 40,
                    reviewed_tree_sha="c" * 40,
                    review_evidence_id="rev",
                    review_evidence={},
                )
            )
        await self._eligible(db, "root")

        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        record = await db.get_task_meta("root", "archive_refusal")
        assert record["code"] == "sealed"
        blocked = await db.list_archive_blocked_roots(
            statuses=["COMPLETED"], older_than_seconds=3600
        )
        assert [r["reason"] for r in blocked.roots] == ["sealed"]

    async def test_records_an_unexpected_failure_with_a_one_line_signature(self, db, monkeypatch):
        await _seed_project(db)
        await _seed_task(db, "root", status=TaskStatus.COMPLETED)
        await self._eligible(db, "root")

        async def boom(task_id, **_kwargs):
            raise RuntimeError("something\nwith newlines\nand detail")

        monkeypatch.setattr(db, "archive_task", boom)
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        record = await db.get_task_meta("root", "archive_refusal")
        assert record["code"] == "unexpected"
        assert record["detail"].startswith("RuntimeError")
        assert "\n" not in record["detail"]

    async def test_a_history_only_root_leaves_the_active_updated_at_projection(self, db):
        """Archive moves the root rather than maintaining a stale refusal record."""
        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, "root")
        await _enable_hierarchy_mode(db)
        await self._eligible(db, "root")
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        assert await db.get_task_updated_at("root") is None
        assert await db.get_task_meta("root", "archive_refusal") is None
        blocked = await db.list_archive_blocked_roots(
            statuses=["COMPLETED"], older_than_seconds=3600
        )
        assert blocked.total == 0

    async def test_an_unchanged_refusal_is_not_rewritten_every_sweep(self, db, monkeypatch):
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        await _seed_task(db, "root", status=TaskStatus.COMPLETED)
        await self._eligible(db, "root")

        async def refused(*_args, **_kwargs):
            raise HierarchyError("sealed", "still sealed")

        monkeypatch.setattr(db, "archive_task", refused)

        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)
        first = await db.get_task_meta("root", "archive_refusal")
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        assert await db.get_task_meta("root", "archive_refusal") == first

    async def test_the_record_goes_when_the_refusal_does(self, db, monkeypatch):
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        await _seed_task(db, "root", status=TaskStatus.COMPLETED)
        await self._eligible(db, "root")

        actual_archive = db.archive_task

        async def refused(*_args, **_kwargs):
            raise HierarchyError("sealed", "still sealed")

        monkeypatch.setattr(db, "archive_task", refused)
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)
        assert await db.get_task_meta("root", "archive_refusal") is not None

        monkeypatch.setattr(db, "archive_task", actual_archive)

        archived = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600
        )

        assert archived == ["root"]
        assert await db.get_task_meta("root", "archive_refusal") is None
        blocked = await db.list_archive_blocked_roots(
            statuses=["COMPLETED"], older_than_seconds=3600
        )
        assert blocked.total == 0

    async def test_a_failed_record_write_does_not_abandon_the_rest_of_the_sweep(
        self, db, monkeypatch, caplog
    ):
        """The reporting write is not allowed to reintroduce the bug it reports."""
        import logging
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, "root")
        await _seed_task(db, "solo", pid="p-hier", status=TaskStatus.COMPLETED)
        await _enable_hierarchy_mode(db)
        await self._eligible(db, "root", "solo")

        async def boom(task_id, code, detail):
            raise RuntimeError("metadata write failed")

        actual_archive = db.archive_task

        async def root_refused(task_id, **kwargs):
            if task_id == "root":
                raise HierarchyError("sealed", "still sealed")
            return await actual_archive(task_id, **kwargs)

        monkeypatch.setattr(db, "archive_task", root_refused)
        monkeypatch.setattr(db, "_record_archive_refusal", boom)
        with caplog.at_level(logging.WARNING, logger="src.database.queries.archive_queries"):
            archived = await db.archive_old_terminal_tasks(
                statuses=["COMPLETED"], older_than_seconds=3600
            )

        # The unrelated root still leaves the graph and the call returns.
        assert archived == ["solo"]
        assert await db.get_task("root") is not None
        warnings = [r for r in caplog.records if "root" in r.getMessage()]
        assert len(warnings) == 1
        assert "RuntimeError" in warnings[0].getMessage()

    async def test_a_failed_clear_does_not_abandon_the_rest_of_the_sweep(
        self, db, monkeypatch, caplog
    ):
        import logging

        await _seed_project(db)
        await _seed_task(db, "first", status=TaskStatus.COMPLETED)
        await _seed_task(db, "second", status=TaskStatus.COMPLETED)
        await self._eligible(db, "first", "second")

        async def boom(task_id):
            raise RuntimeError("metadata clear failed")

        monkeypatch.setattr(db, "_clear_archive_refusal", boom)
        with caplog.at_level(logging.WARNING, logger="src.database.queries.archive_queries"):
            archived = await db.archive_old_terminal_tasks(
                statuses=["COMPLETED"], older_than_seconds=3600
            )

        # Both really archived, and the failed tidy did not unmake either.
        assert sorted(archived) == ["first", "second"]
        assert await db.get_archived_task("first") is not None
        assert await db.get_archived_task("second") is not None
        assert len([r for r in caplog.records if "RuntimeError" in r.getMessage()]) == 2

    async def test_blocked_count_is_the_true_total_not_the_page_size(self, db, monkeypatch):
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        for n in range(4):
            await _seed_task(db, f"root-{n}", status=TaskStatus.COMPLETED)
        await self._eligible(db, *[f"root-{n}" for n in range(4)])

        async def refused(*_args, **_kwargs):
            raise HierarchyError("sealed", "still sealed")

        monkeypatch.setattr(db, "archive_task", refused)
        await db.archive_old_terminal_tasks(statuses=["COMPLETED"], older_than_seconds=3600)

        blocked = await db.list_archive_blocked_roots(
            statuses=["COMPLETED"], older_than_seconds=3600, limit=2
        )

        assert blocked.total == 4
        assert len(blocked.roots) == 2


class TestFailureSignature:
    """F1: the signature is one line and names the constraint asyncpg reports."""

    async def test_names_the_constraint_from_a_real_wrapped_integrityerror(self, db):
        from sqlalchemy import delete as sa_delete
        from sqlalchemy.exc import IntegrityError

        from src.database.queries.archive_queries import _failure_signature
        from src.database.tables import repos

        await _seed_hierarchy_project(db)
        await _seed_task(db, "root", pid="p-hier", status=TaskStatus.COMPLETED)
        await _seed_parent_episode(db, "root")

        with pytest.raises(IntegrityError) as exc:
            async with db._engine.begin() as conn:
                await conn.execute(
                    sa_delete(repos).where(repos.c.id == "repo-p-hier")
                )

        signature = _failure_signature(exc.value)
        assert signature == "IntegrityError(fk_integration_parent_episodes_repository)"

    def test_falls_back_to_one_truncated_line(self):
        from src.database.queries.archive_queries import _failure_signature

        signature = _failure_signature(RuntimeError("first line\nsecond line\n" + "x" * 500))
        assert "\n" not in signature
        assert signature.startswith("RuntimeError: first line")
        assert len(signature) < 250


# ---------------------------------------------------------------------------
# Development integration owns its manifest members and repair sources
# ---------------------------------------------------------------------------


async def _seed_development_delivery(
    db: Database,
    delivery_id: str,
    *,
    state: str,
    task_ids: list[str],
    project_id: str = "p-1",
    created_at: float = 1.0,
) -> None:
    """Insert one ``development_deliveries`` row naming *task_ids*."""
    from sqlalchemy import insert

    from src.database.tables import development_deliveries

    async with db._engine.begin() as conn:
        await conn.execute(
            insert(development_deliveries).values(
                id=delivery_id,
                project_id=project_id,
                repository_id="repo",
                target_ref="refs/heads/main",
                expected_sha=None,
                prepared_sha=None,
                state=state,
                manifest=[{"task_id": tid, "source_sha": "a" * 40} for tid in task_ids],
                evidence={},
                reason="development batch",
                created_at=created_at,
                updated_at=created_at,
            )
        )


async def _set_delivery_state(db: Database, delivery_id: str, state: str) -> None:
    from sqlalchemy import update

    from src.database.tables import development_deliveries

    async with db._engine.begin() as conn:
        await conn.execute(
            update(development_deliveries)
            .where(development_deliveries.c.id == delivery_id)
            .values(state=state)
        )


async def _backdate(db: Database, *task_ids: str, seconds: float = 86400) -> None:
    old = time.time() - seconds
    async with db._engine.begin() as conn:
        for tid in task_ids:
            await conn.execute(
                text("UPDATE tasks SET updated_at = :t WHERE id = :id"),
                {"t": old, "id": tid},
            )


class TestDevelopmentIntegrationArchiveGuard:
    """A development batch or repair still owes work to the tasks it names."""

    @pytest.fixture
    async def handler(self, db, tmp_path):
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
            database=DatabaseConfig(url=lease_dsn("test.db")),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    @pytest.mark.parametrize("state", ["prepared", "publishing", "parked"])
    async def test_unfinished_batch_member_is_refused_and_names_the_batch(self, db, state):
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_development_delivery(db, "batch-1", state=state, task_ids=["t-src"])

        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("t-src")
        assert exc.value.code == "integration_owned"
        assert "batch-1" in exc.value.detail
        assert state in exc.value.detail
        assert "t-src" in exc.value.detail
        assert await db.get_task("t-src") is not None

    @pytest.mark.parametrize("state", ["delivered", "adopted", "cancelled"])
    async def test_member_is_archivable_once_the_batch_settles(self, db, state):
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_development_delivery(db, "batch-1", state="parked", task_ids=["t-src"])
        await _set_delivery_state(db, "batch-1", state)

        assert await db.archive_task("t-src") is True
        assert await db.get_archived_task("t-src") is not None

    async def test_an_unrelated_task_is_untouched_by_an_open_batch(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-other", status=TaskStatus.COMPLETED, title="Other")
        await _seed_development_delivery(db, "batch-1", state="parked", task_ids=["t-src"])

        assert await db.archive_task("t-other") is True

    async def test_a_named_descendant_holds_the_whole_subtree(self, db):
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        await _seed_task(db, "t-parent", status=TaskStatus.COMPLETED)
        await _seed_task(
            db, "t-child", status=TaskStatus.COMPLETED, title="Child", parent_task_id="t-parent"
        )
        await _seed_development_delivery(db, "batch-1", state="prepared", task_ids=["t-child"])

        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("t-parent")
        assert exc.value.code == "integration_owned"
        assert "t-child" in exc.value.detail

    async def test_auto_archive_sweep_skips_the_member_then_takes_it(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-free", status=TaskStatus.COMPLETED, title="Free")
        await _seed_development_delivery(db, "batch-1", state="parked", task_ids=["t-src"])
        await _backdate(db, "t-src", "t-free")

        assert await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=3600) == [
            "t-free"
        ]
        assert await db.get_task("t-src") is not None

        await _set_delivery_state(db, "batch-1", "delivered")
        assert await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=3600) == [
            "t-src"
        ]

    async def test_manual_archive_command_reports_integration_owned(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_development_delivery(db, "batch-1", state="publishing", task_ids=["t-src"])

        result = await handler.execute("archive_task", {"task_id": "t-src"})
        assert result["code"] == "hierarchy.integration_owned"
        assert "batch-1" in result["error"]
        assert await db.get_task("t-src") is not None

        await _set_delivery_state(db, "batch-1", "delivered")
        assert (await handler.execute("archive_task", {"task_id": "t-src"}))["archived"] == "t-src"

    async def test_bulk_archive_skips_the_member_and_reports_it(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-free", status=TaskStatus.COMPLETED, title="Free")
        await _seed_development_delivery(db, "batch-1", state="parked", task_ids=["t-src"])

        result = await handler.execute("archive_task", {"project_id": "p-1"})
        assert result["archived_ids"] == ["t-free"]
        assert result["skipped"] == [
            {
                "task_id": "t-src",
                "code": "hierarchy.integration_owned",
                "detail": result["skipped"][0]["detail"],
            }
        ]
        assert "batch-1" in result["skipped"][0]["detail"]

    async def test_open_development_repair_holds_its_sources(self, db):
        from src.database.queries.hierarchy_queries import HierarchyError

        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_task(
            db, "development-repair-abc", status=TaskStatus.READY, title="Repair"
        )
        await db.set_task_meta(
            "development-repair-abc",
            "development_repair_sources",
            [{"task_id": "t-src", "source_sha": "b" * 40}],
        )

        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("t-src")
        assert exc.value.code == "integration_owned"
        assert "development-repair-abc" in exc.value.detail
        assert "t-src" in exc.value.detail

    async def test_source_is_archivable_once_the_repair_is_terminal(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_task(
            db, "development-repair-abc", status=TaskStatus.READY, title="Repair"
        )
        await db.set_task_meta(
            "development-repair-abc",
            "development_repair_sources",
            [{"task_id": "t-src", "source_sha": "b" * 40}],
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'COMPLETED' WHERE id = :id"),
                {"id": "development-repair-abc"},
            )

        assert await db.archive_task("t-src") is True

    async def test_non_list_metadata_values_do_not_break_the_guard(self, db):
        """Every task metadata value is JSON, but most are scalars, not manifests."""
        await _seed_project(db)
        await _seed_task(db, "t-src", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-noise", status=TaskStatus.READY, title="Noise")
        await db.set_task_meta("t-noise", "needs_attention", "session_exited_open")
        await db.set_task_meta("t-noise", "development_repair_sources", "not-a-manifest")

        assert await db.archive_task("t-src") is True


# ---------------------------------------------------------------------------
# Archive sweeps keep undelivered development work in the queue
# ---------------------------------------------------------------------------


async def _seed_development_project(db: Database, pid: str = "p-dev") -> None:
    """A development-mode project delivering to ``dev-repo``, plus a second project.

    ``p-web`` owns ``web-repo``: the repository ``fleet-meadow`` named even
    though it belonged to the development project.
    """
    from sqlalchemy import update

    from src.database.tables import projects

    await db.create_project(Project(id=pid, name="Development"))
    await db.create_project(Project(id="p-web", name="Web"))
    await db.create_repo(
        RepoConfig(
            id="dev-repo", project_id=pid, source_type=RepoSourceType.CLONE,
            url="https://example.test/dev.git",
        )
    )
    await db.create_repo(
        RepoConfig(
            id="web-repo", project_id="p-web", source_type=RepoSourceType.CLONE,
            url="https://example.test/web.git",
        )
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == pid)
            .values(
                integration_repository_id="dev-repo",
                hierarchical_integration_mode="development",
            )
        )


async def _completed_with_close(
    db: Database, tid: str, *, repo_id: str, commit: str, pid: str = "p-dev", **kwargs
) -> None:
    await _seed_task(
        db, tid, pid=pid, status=TaskStatus.COMPLETED, repo_id=repo_id,
        branch_name=f"aq/{tid}", **kwargs,
    )
    await db.save_task_completion(
        TaskCompletion(
            id=f"close-{tid}", task_id=tid, outcome="pass", commits=[commit],
            completed_at=time.time(),
        )
    )


async def _journal(
    db: Database, row_id: str, *, state: str, target_ref: str, members: list[tuple[str, str]],
    pid: str = "p-dev", parent_task_id: str | None = None,
) -> None:
    from sqlalchemy import insert

    from src.database.tables import development_deliveries

    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(development_deliveries).values(
                id=row_id, project_id=pid, repository_id="dev-repo", target_ref=target_ref,
                expected_sha=None, prepared_sha=None, state=state,
                manifest=[
                    {"task_id": tid, "source_sha": sha, "parent_task_id": parent_task_id}
                    for tid, sha in members
                ],
                evidence={}, reason="development batch", created_at=now, updated_at=now,
            )
        )


class TestUndeliveredDevelopmentWorkIsNotSwept:
    """quick-ridge: an archive sweep never takes COMPLETED work that has not landed."""

    FLEET = "c9fe0f80500894c94ecefd2b7dd92d5928e034db"
    NEXUS = "514463eec6acd98b3d2b731ed4e02ebd7756fda7"
    REPAIR = "991851bcb888753ff70d01655f07c6525ec579b1"

    async def _seed_fleet_meadow(self, db):
        """fleet-meadow, 2026-09-12: another project's repo id, never in any batch."""
        await _seed_development_project(db)
        # The live row named agent-queue-web's generated repository.
        await _seed_task(
            db, "fleet-meadow", pid="p-dev", status=TaskStatus.COMPLETED,
            repo_id="web-repo", branch_name="aq/fleet-meadow",
        )
        # A failed close, then a passing close of the same revision.
        for n, outcome in enumerate(("fail", "pass")):
            await db.save_task_completion(
                TaskCompletion(
                    id=f"fleet-close-{n}", task_id="fleet-meadow", outcome=outcome,
                    commits=[self.FLEET], completed_at=time.time() + n,
                )
            )
        await _backdate(db, "fleet-meadow")

    async def test_fleet_meadow_shape_is_held_by_the_auto_archive_sweep(self, db):
        from src.database.queries.blocked_state import _development_delivery_pending
        from src.database.tables import tasks as tasks_table

        await self._seed_fleet_meadow(db)
        # Readiness is unchanged: the publisher does not collect a foreign
        # repository id, which is exactly why nothing ever delivered it.
        async with db._engine.connect() as conn:
            pending = await conn.scalar(
                select(_development_delivery_pending(tasks_table)).where(
                    tasks_table.c.id == "fleet-meadow"
                )
            )
        assert pending is False

        archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=3600)

        assert archived == []
        assert await db.get_task("fleet-meadow") is not None
        blocked = await db.list_archive_blocked_roots(["COMPLETED"], older_than_seconds=3600)
        assert [(b["task_id"], b["reason"]) for b in blocked.roots] == [
            ("fleet-meadow", "integration_undelivered")
        ]

    async def test_fleet_meadow_is_swept_once_its_revision_is_on_main(self, db):
        await self._seed_fleet_meadow(db)
        assert await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=3600) == []

        # An operator adoption (``aq integration adopt``) names the revision.
        await _journal(
            db, "adopt-fleet", state="adopted", target_ref="refs/heads/main",
            members=[("fleet-meadow", self.FLEET)],
        )

        assert await db.archive_old_terminal_tasks(
            ["COMPLETED"], older_than_seconds=3600
        ) == ["fleet-meadow"]

    async def _seed_nimble_nexus(self, db):
        """nimble-nexus, 2026-09-20: validation parked it, its repair parked too."""
        await _seed_development_project(db)
        await _completed_with_close(db, "nimble-nexus", repo_id="dev-repo", commit=self.NEXUS)
        repair = "development-repair-2bfd84c0ad9f434c18e3"
        await _completed_with_close(
            db, repair, repo_id="dev-repo", commit=self.REPAIR, parent_task_id="nimble-nexus",
        )
        await _journal(
            db, "nexus-candidate", state="delivered",
            target_ref=f"refs/heads/aq/development/aeacda21cbc3/{self.NEXUS}",
            members=[("nimble-nexus", self.NEXUS)],
        )
        await _journal(
            db, "nexus-main", state="parked", target_ref="refs/heads/main",
            members=[("nimble-nexus", self.NEXUS)],
        )
        await _journal(
            db, "repair-parent", state="delivered",
            target_ref="refs/heads/aq/development/parent/97b1afbb18674463/5630e634770e",
            members=[(repair, self.REPAIR)], parent_task_id="nimble-nexus",
        )
        await _journal(
            db, "repair-main", state="parked", target_ref="refs/heads/main",
            members=[(repair, self.REPAIR)], parent_task_id="nimble-nexus",
        )
        await _backdate(db, "nimble-nexus", repair)
        return repair

    async def test_nimble_nexus_shape_is_held_while_its_batches_are_parked(self, db):
        repair = await self._seed_nimble_nexus(db)

        assert await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=3600) == []

        assert await db.get_task("nimble-nexus") is not None
        assert await db.get_task(repair) is not None
        blocked = await db.list_archive_blocked_roots(["COMPLETED"], older_than_seconds=3600)
        assert [b["task_id"] for b in blocked.roots] == ["nimble-nexus"]

    async def test_a_settled_but_undelivered_batch_still_does_not_release_it(self, db):
        """A batch that ends without landing its work is not a delivery."""
        await self._seed_nimble_nexus(db)
        await _set_delivery_state(db, "nexus-main", "cancelled")
        await _set_delivery_state(db, "repair-main", "cancelled")

        assert await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=3600) == []
        blocked = await db.list_archive_blocked_roots(["COMPLETED"], older_than_seconds=3600)
        assert [(b["task_id"], b["reason"]) for b in blocked.roots] == [
            ("nimble-nexus", "integration_undelivered")
        ]

    async def test_nimble_nexus_is_swept_once_both_revisions_are_on_main(self, db):
        repair = await self._seed_nimble_nexus(db)
        await _set_delivery_state(db, "nexus-main", "adopted")
        await _set_delivery_state(db, "repair-main", "delivered")

        assert await db.archive_old_terminal_tasks(
            ["COMPLETED"], older_than_seconds=3600
        ) == ["nimble-nexus"]
        assert await db.get_archived_task(repair) is not None

    async def test_explicit_single_archive_requires_a_recorded_abandonment(self, db):
        from src.database.queries.hierarchy_queries import HierarchyError

        await self._seed_fleet_meadow(db)

        with pytest.raises(HierarchyError, match="integration_undelivered"):
            await db.archive_task("fleet-meadow")
        assert await db.archive_task(
            "fleet-meadow",
            abandon_undelivered=True,
            abandon_reason="known foreign repository",
            abandoned_by="test-operator",
        ) is True
        comments = (await db.list_task_comments("fleet-meadow", project_id="p-dev"))["comments"]
        assert any(
            "Delivery abandoned before archive by test-operator: known foreign repository" in row["body"]
            for row in comments
        )

    async def test_bulk_archive_paths_hold_it_too(self, db):
        await self._seed_fleet_meadow(db)

        assert await db.archive_completed_tasks("p-dev") == []
        assert await db.get_task("fleet-meadow") is not None

    async def test_other_work_is_still_swept(self, db):
        await _seed_development_project(db)
        # A failed task and a branchless task have nothing to deliver; a task
        # on another repository of its own project is outside the publisher.
        await _seed_task(
            db, "failed", pid="p-dev", status=TaskStatus.FAILED, repo_id="dev-repo",
            branch_name="aq/failed",
        )
        await _seed_task(db, "branchless", pid="p-dev", status=TaskStatus.COMPLETED)
        await db.create_repo(
            RepoConfig(
                id="dev-docs", project_id="p-dev", source_type=RepoSourceType.CLONE,
                url="https://example.test/docs.git",
            )
        )
        await _completed_with_close(db, "docs", repo_id="dev-docs", commit="d" * 40)
        await _backdate(db, "failed", "branchless", "docs")

        archived = await db.archive_old_terminal_tasks(
            ["COMPLETED", "FAILED"], older_than_seconds=3600
        )

        assert sorted(archived) == ["branchless", "docs", "failed"]


class TestBulkArchiveCommandReportsDeliveryPending:
    @pytest.fixture
    async def handler(self, db, tmp_path):
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
            database=DatabaseConfig(url=lease_dsn("test.db")),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    async def test_bulk_command_skips_and_names_the_undelivered_task(self, handler, db):
        await _seed_development_project(db)
        await _completed_with_close(db, "t-undelivered", repo_id="dev-repo", commit="e" * 40)
        await _seed_task(db, "t-free", pid="p-dev", status=TaskStatus.COMPLETED, title="Free")

        result = await handler.execute("archive_task", {"project_id": "p-dev"})

        assert result["archived_ids"] == ["t-free"]
        assert [(s["task_id"], s["code"]) for s in result["skipped"]] == [
            ("t-undelivered", "hierarchy.integration_undelivered")
        ]
