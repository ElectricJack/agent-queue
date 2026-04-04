"""Unit tests for _cmd_reopen_with_feedback command handler."""

import pytest
from sqlalchemy import text
from unittest.mock import MagicMock

from src.command_handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real in-memory database for tests."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
async def handler(db, config):
    """Create a CommandHandler with mocked orchestrator."""
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


@pytest.fixture
async def completed_task(db):
    """Create a completed task in the database."""
    project_id = "test-proj"
    await db.create_project(Project(id=project_id, name="Test Project"))
    task = Task(
        id="t-1",
        project_id=project_id,
        title="Fix the login page",
        description="The login page has a bug.",
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/org/repo/pull/42",
    )
    await db.create_task(task)
    return task


@pytest.fixture
async def approval_task(db):
    """Create a completed task that requires approval."""
    project_id = "test-proj-approval"
    await db.create_project(Project(id=project_id, name="Approval Project"))
    task = Task(
        id="t-approval",
        project_id=project_id,
        title="Feature needing review",
        description="Important feature.",
        status=TaskStatus.AWAITING_APPROVAL,
        requires_approval=True,
        pr_url="https://github.com/org/repo/pull/99",
    )
    await db.create_task(task)
    return task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReopenWithFeedback:
    """Tests for _cmd_reopen_with_feedback."""

    async def test_reopen_completed_task(self, handler, completed_task, db):
        """Reopening a completed task transitions it to READY."""
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Tests are still failing"},
        )
        assert "error" not in result
        assert result["reopened"] == "t-1"
        assert result["previous_status"] == "COMPLETED"
        assert result["status"] == "READY"
        assert result["feedback_added"] is True

        # Verify task was actually updated in the database
        task = await db.get_task("t-1")
        assert task.status == TaskStatus.READY
        assert task.retry_count == 0
        assert task.assigned_agent_id is None

    async def test_feedback_appended_to_description(self, handler, completed_task, db):
        """Feedback is appended to the task description."""
        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Needs better error handling"},
        )
        task = await db.get_task("t-1")
        assert "The login page has a bug." in task.description
        assert "Needs better error handling" in task.description
        assert "**Reopen Feedback:**" in task.description

    async def test_feedback_stored_as_task_context(self, handler, completed_task, db):
        """Feedback is stored as a structured task_context entry."""
        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Edge case not handled"},
        )
        contexts = await db.get_task_contexts("t-1")
        assert len(contexts) == 1
        assert contexts[0]["type"] == "reopen_feedback"
        assert contexts[0]["label"] == "Reopen Feedback"
        assert contexts[0]["content"] == "Edge case not handled"

    async def test_pr_url_cleared(self, handler, completed_task, db):
        """PR URL is cleared so the agent creates a fresh PR."""
        # Verify task starts with a PR URL
        task = await db.get_task("t-1")
        assert task.pr_url is not None

        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "PR had issues"},
        )
        task = await db.get_task("t-1")
        assert task.pr_url is None

    async def test_error_missing_task_id(self, handler, completed_task):
        """Returns error when task_id is missing."""
        result = await handler.execute(
            "reopen_with_feedback",
            {"feedback": "some feedback"},
        )
        assert "error" in result
        assert "task_id" in result["error"]

    async def test_error_missing_feedback(self, handler, completed_task):
        """Returns error when feedback is missing."""
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": ""},
        )
        assert "error" in result
        assert "feedback" in result["error"]

    async def test_error_task_not_found(self, handler, completed_task):
        """Returns error when task does not exist."""
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "nonexistent", "feedback": "some feedback"},
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_error_in_progress_task(self, handler, completed_task, db):
        """Returns error when task is currently in progress."""
        await db.update_task("t-1", status=TaskStatus.IN_PROGRESS)
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "some feedback"},
        )
        assert "error" in result
        assert "in progress" in result["error"].lower()

    async def test_reopen_failed_task(self, handler, completed_task, db):
        """Can also reopen a FAILED task."""
        await db.update_task("t-1", status=TaskStatus.FAILED)
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Fix the failure"},
        )
        assert "error" not in result
        assert result["previous_status"] == "FAILED"
        assert result["status"] == "READY"

    async def test_multiple_reopens_accumulate_feedback(self, handler, completed_task, db):
        """Multiple reopens append feedback each time."""
        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "First issue"},
        )
        # Simulate task completing again
        await db.update_task("t-1", status=TaskStatus.COMPLETED)
        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Second issue"},
        )

        task = await db.get_task("t-1")
        assert "First issue" in task.description
        assert "Second issue" in task.description

        # Both feedback entries stored as separate context entries
        contexts = await db.get_task_contexts("t-1")
        assert len(contexts) == 2

    async def test_event_logged(self, handler, completed_task, db):
        """An event is logged for audit trail."""
        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Audit trail test"},
        )
        # Verify event was logged by checking the events table
        async with db._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT * FROM events WHERE event_type = 'reopen_with_feedback'"
                    " AND task_id = 't-1'"
                )
            )
            rows = result.mappings().fetchall()
        assert len(rows) == 1
        assert "Audit trail test" in rows[0]["payload"]

    async def test_requires_approval_preserved(self, handler, approval_task, db):
        """requires_approval=True persists through reopen cycles."""
        # Verify initial state
        task = await db.get_task("t-approval")
        assert task.requires_approval is True
        assert task.status == TaskStatus.AWAITING_APPROVAL

        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-approval", "feedback": "Please fix the edge case"},
        )
        assert "error" not in result
        assert result["requires_approval"] is True

        # Verify in database
        task = await db.get_task("t-approval")
        assert task.status == TaskStatus.READY
        assert task.requires_approval is True
        assert task.pr_url is None  # PR cleared for fresh creation

    async def test_requires_approval_preserved_across_multiple_reopens(
        self, handler, approval_task, db
    ):
        """requires_approval stays True across multiple reopen cycles."""
        # First reopen
        await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-approval", "feedback": "First round of feedback"},
        )
        task = await db.get_task("t-approval")
        assert task.requires_approval is True

        # Simulate agent completing again
        await db.update_task(
            "t-approval",
            status=TaskStatus.AWAITING_APPROVAL,
            pr_url="https://github.com/org/repo/pull/100",
        )

        # Second reopen
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-approval", "feedback": "Second round of feedback"},
        )
        assert result["requires_approval"] is True

        task = await db.get_task("t-approval")
        assert task.requires_approval is True
        assert task.pr_url is None

    async def test_requires_approval_false_stays_false(self, handler, completed_task, db):
        """A task without requires_approval keeps it as False after reopen."""
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": "t-1", "feedback": "Some feedback"},
        )
        assert result["requires_approval"] is False

        task = await db.get_task("t-1")
        assert task.requires_approval is False
