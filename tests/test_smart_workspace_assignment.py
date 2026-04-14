"""Tests for smart workspace assignment for merge conflict handling.

Covers:
  - find_merge_conflict_workspaces command detection
  - preferred_workspace_id in task creation and workspace acquisition
  - Orchestrator _prepare_workspace honoring preferred_workspace_id
  - End-to-end: detect conflict → create task → assign correct workspace
"""

from __future__ import annotations

import os
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database import Database
from src.git.manager import GitManager
from src.models import (
    Agent,
    AgentState,
    Project,
    ProjectStatus,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _git_commit(cwd: str, filename: str, content: str, message: str) -> str:
    filepath = os.path.join(cwd, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    _git(cwd, "add", filename)
    _git(cwd, "-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def git_repo_with_conflict(tmp_path):
    """Create a bare remote + clone with a conflicting branch."""
    bare = str(tmp_path / "bare.git")
    clone = str(tmp_path / "clone")

    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", bare)
    _git(str(tmp_path), "clone", bare, clone)
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-b", "main", check=False)

    # Initial commit on main
    _git_commit(clone, "file.txt", "line 1\nline 2\n", "Initial commit")
    _git(clone, "push", "-u", "origin", "main")

    # Create conflicting branch
    _git(clone, "checkout", "-b", "brave-fox/fix-auth")
    _git_commit(clone, "file.txt", "branch change\nline 2\n", "Branch change")
    _git(clone, "push", "origin", "brave-fox/fix-auth")

    # Update main to conflict
    _git(clone, "checkout", "main")
    _git_commit(clone, "file.txt", "main change\nline 2\n", "Main change")
    _git(clone, "push", "origin", "main")

    return clone


@pytest.fixture
def git_repo_clean(tmp_path):
    """Create a repo with no merge conflicts."""
    bare = str(tmp_path / "bare_clean.git")
    clone = str(tmp_path / "clone_clean")

    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", bare)
    _git(str(tmp_path), "clone", bare, clone)
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-b", "main", check=False)

    _git_commit(clone, "file.txt", "line 1\n", "Initial commit")
    _git(clone, "push", "-u", "origin", "main")

    # Clean branch (no conflict)
    _git(clone, "checkout", "-b", "clean-task/add-feature")
    _git_commit(clone, "new_file.txt", "new content\n", "Add new file")
    _git(clone, "push", "origin", "clean-task/add-feature")
    _git(clone, "checkout", "main")

    return clone


# ---------------------------------------------------------------------------
# Database: acquire_workspace with preferred_workspace_id
# ---------------------------------------------------------------------------


class TestAcquireWorkspacePreferred:
    """Test that acquire_workspace respects preferred_workspace_id."""

    @pytest.fixture
    async def db(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # Create a project
        project = Project(
            id="test-proj",
            name="Test Project",
            credit_weight=1.0,
            max_concurrent_agents=2,
            status=ProjectStatus.ACTIVE,
        )
        await db.create_project(project)
        # Create agents (needed for FK constraints)
        for aid in ("agent-0", "agent-1"):
            await db.create_agent(
                Agent(
                    id=aid,
                    name=aid,
                    agent_type="claude",
                    state=AgentState.IDLE,
                )
            )
        # Create tasks (needed for FK constraints on locked_by_task_id)
        for tid in ("task-0", "task-1"):
            await db.create_task(
                Task(
                    id=tid,
                    project_id="test-proj",
                    title=f"Task {tid}",
                    description="test",
                    status=TaskStatus.READY,
                )
            )
        # Create two workspaces
        ws1 = Workspace(
            id="ws-1",
            project_id="test-proj",
            workspace_path="/tmp/ws1",
            source_type=RepoSourceType.LINK,
        )
        ws2 = Workspace(
            id="ws-2",
            project_id="test-proj",
            workspace_path="/tmp/ws2",
            source_type=RepoSourceType.LINK,
        )
        await db.create_workspace(ws1)
        await db.create_workspace(ws2)
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_preferred_workspace_acquired_first(self, db):
        """When preferred_workspace_id is set, that workspace should be acquired."""
        ws = await db.acquire_workspace(
            "test-proj",
            "agent-1",
            "task-1",
            preferred_workspace_id="ws-2",
        )
        assert ws is not None
        assert ws.id == "ws-2"
        assert ws.locked_by_agent_id == "agent-1"
        assert ws.locked_by_task_id == "task-1"

    @pytest.mark.asyncio
    async def test_fallback_when_preferred_locked(self, db):
        """If preferred workspace is locked, fall back to any available."""
        # Lock the preferred workspace
        await db.acquire_workspace("test-proj", "agent-0", "task-0", preferred_workspace_id="ws-2")

        # Now try to acquire with ws-2 as preferred — should fall back to ws-1
        ws = await db.acquire_workspace(
            "test-proj",
            "agent-1",
            "task-1",
            preferred_workspace_id="ws-2",
        )
        assert ws is not None
        assert ws.id == "ws-1"

    @pytest.mark.asyncio
    async def test_no_preferred_uses_first_available(self, db):
        """Without preferred_workspace_id, any unlocked workspace is returned."""
        ws = await db.acquire_workspace("test-proj", "agent-1", "task-1")
        assert ws is not None
        assert ws.id in ("ws-1", "ws-2")

    @pytest.mark.asyncio
    async def test_preferred_wrong_project_ignored(self, db):
        """A preferred_workspace_id from a different project should fall back."""
        # Create workspace in different project
        project2 = Project(
            id="other-proj",
            name="Other",
            credit_weight=1.0,
            max_concurrent_agents=1,
            status=ProjectStatus.ACTIVE,
        )
        await db.create_project(project2)
        ws3 = Workspace(
            id="ws-3",
            project_id="other-proj",
            workspace_path="/tmp/ws3",
            source_type=RepoSourceType.LINK,
        )
        await db.create_workspace(ws3)

        # Try to acquire with ws-3 (wrong project) as preferred
        ws = await db.acquire_workspace(
            "test-proj",
            "agent-1",
            "task-1",
            preferred_workspace_id="ws-3",
        )
        assert ws is not None
        assert ws.id in ("ws-1", "ws-2")  # Falls back to correct project


# ---------------------------------------------------------------------------
# Task model: preferred_workspace_id persistence
# ---------------------------------------------------------------------------


class TestTaskPreferredWorkspace:
    """Test preferred_workspace_id persists through create/read cycle."""

    @pytest.fixture
    async def db(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        project = Project(
            id="test-proj",
            name="Test",
            credit_weight=1.0,
            max_concurrent_agents=1,
            status=ProjectStatus.ACTIVE,
        )
        await db.create_project(project)
        # Create a workspace to satisfy FK constraint
        ws = Workspace(
            id="ws-conflict",
            project_id="test-proj",
            workspace_path="/tmp/ws-conflict",
            source_type=RepoSourceType.LINK,
        )
        await db.create_workspace(ws)
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_preferred_workspace_persisted(self, db):
        task = Task(
            id="task-1",
            project_id="test-proj",
            title="Fix conflicts",
            description="Fix merge conflicts",
            status=TaskStatus.READY,
            preferred_workspace_id="ws-conflict",
        )
        await db.create_task(task)

        loaded = await db.get_task("task-1")
        assert loaded is not None
        assert loaded.preferred_workspace_id == "ws-conflict"

    @pytest.mark.asyncio
    async def test_preferred_workspace_none_by_default(self, db):
        task = Task(
            id="task-2",
            project_id="test-proj",
            title="Normal task",
            description="Regular task",
            status=TaskStatus.READY,
        )
        await db.create_task(task)

        loaded = await db.get_task("task-2")
        assert loaded is not None
        assert loaded.preferred_workspace_id is None


# ---------------------------------------------------------------------------
# CommandHandler: find_merge_conflict_workspaces
# ---------------------------------------------------------------------------


class TestFindMergeConflictWorkspacesCommand:
    """Test _cmd_find_merge_conflict_workspaces via CommandHandler."""

    @pytest.fixture
    async def handler_and_db(self, tmp_path):
        """Create a minimal CommandHandler with real DB and real GitManager."""
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        project = Project(
            id="test-proj",
            name="Test",
            credit_weight=1.0,
            max_concurrent_agents=1,
            status=ProjectStatus.ACTIVE,
            repo_default_branch="main",
        )
        await db.create_project(project)

        config = MagicMock()
        config.workspace_dir = str(tmp_path / "workspaces")
        orchestrator = MagicMock()
        orchestrator.db = db
        # Use real GitManager so async git methods work with real repos
        orchestrator.git = GitManager()

        from src.commands.handler import CommandHandler

        handler = CommandHandler(orchestrator=orchestrator, config=config)

        yield handler, db
        await db.close()

    @pytest.mark.asyncio
    async def test_detects_conflict_workspace(self, handler_and_db, git_repo_with_conflict):
        handler, db = handler_and_db

        ws = Workspace(
            id="ws-conflict",
            project_id="test-proj",
            workspace_path=git_repo_with_conflict,
            source_type=RepoSourceType.LINK,
            name="conflict-workspace",
        )
        await db.create_workspace(ws)

        result = await handler.execute(
            "find_merge_conflict_workspaces",
            {
                "project_id": "test-proj",
            },
        )

        assert "error" not in result
        assert result["workspaces_scanned"] == 1
        assert result["workspaces_with_conflicts"] == 1
        assert len(result["conflicts"]) == 1

        conflict = result["conflicts"][0]
        assert conflict["workspace_id"] == "ws-conflict"
        assert conflict["workspace_name"] == "conflict-workspace"
        assert len(conflict["branch_conflicts"]) == 1
        assert conflict["branch_conflicts"][0]["branch"] == "brave-fox/fix-auth"
        assert conflict["branch_conflicts"][0]["task_id"] == "brave-fox"

    @pytest.mark.asyncio
    async def test_clean_workspace_not_reported(self, handler_and_db, git_repo_clean):
        handler, db = handler_and_db

        ws = Workspace(
            id="ws-clean",
            project_id="test-proj",
            workspace_path=git_repo_clean,
            source_type=RepoSourceType.LINK,
            name="clean-workspace",
        )
        await db.create_workspace(ws)

        result = await handler.execute(
            "find_merge_conflict_workspaces",
            {
                "project_id": "test-proj",
            },
        )

        assert "error" not in result
        assert result["workspaces_scanned"] == 1
        assert result["workspaces_with_conflicts"] == 0
        assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_no_project_returns_error(self, handler_and_db):
        handler, _ = handler_and_db

        result = await handler.execute(
            "find_merge_conflict_workspaces",
            {
                "project_id": "nonexistent",
            },
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_multiple_workspaces_mixed(
        self,
        handler_and_db,
        git_repo_with_conflict,
        git_repo_clean,
    ):
        handler, db = handler_and_db

        ws1 = Workspace(
            id="ws-1",
            project_id="test-proj",
            workspace_path=git_repo_with_conflict,
            source_type=RepoSourceType.LINK,
            name="conflict-ws",
        )
        ws2 = Workspace(
            id="ws-2",
            project_id="test-proj",
            workspace_path=git_repo_clean,
            source_type=RepoSourceType.LINK,
            name="clean-ws",
        )
        await db.create_workspace(ws1)
        await db.create_workspace(ws2)

        result = await handler.execute(
            "find_merge_conflict_workspaces",
            {
                "project_id": "test-proj",
            },
        )

        assert result["workspaces_scanned"] == 2
        assert result["workspaces_with_conflicts"] == 1
        # Only the conflict workspace should appear
        assert result["conflicts"][0]["workspace_id"] == "ws-1"


# ---------------------------------------------------------------------------
# CommandHandler: create_task with preferred_workspace_id
# ---------------------------------------------------------------------------


class TestCreateTaskWithPreferredWorkspace:
    """Test that create_task accepts and validates preferred_workspace_id."""

    @pytest.fixture
    async def handler_and_db(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        project = Project(
            id="test-proj",
            name="Test",
            credit_weight=1.0,
            max_concurrent_agents=1,
            status=ProjectStatus.ACTIVE,
        )
        await db.create_project(project)

        ws = Workspace(
            id="ws-target",
            project_id="test-proj",
            workspace_path="/tmp/ws",
            source_type=RepoSourceType.LINK,
        )
        await db.create_workspace(ws)

        config = MagicMock()
        config.workspace_dir = str(tmp_path / "workspaces")
        orchestrator = MagicMock()
        orchestrator.db = db

        from src.commands.handler import CommandHandler

        handler = CommandHandler(orchestrator=orchestrator, config=config)

        yield handler, db
        await db.close()

    @pytest.mark.asyncio
    async def test_create_task_with_preferred_workspace(self, handler_and_db):
        handler, db = handler_and_db

        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Fix merge conflicts",
                "preferred_workspace_id": "ws-target",
            },
        )

        assert "error" not in result
        assert result["preferred_workspace_id"] == "ws-target"

        # Verify persisted
        task = await db.get_task(result["created"])
        assert task.preferred_workspace_id == "ws-target"

    @pytest.mark.asyncio
    async def test_create_task_invalid_workspace_returns_error(self, handler_and_db):
        handler, _ = handler_and_db

        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Fix merge conflicts",
                "preferred_workspace_id": "ws-nonexistent",
            },
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_create_task_wrong_project_workspace_returns_error(self, handler_and_db):
        handler, db = handler_and_db

        # Create workspace in different project
        project2 = Project(
            id="other-proj",
            name="Other",
            credit_weight=1.0,
            max_concurrent_agents=1,
            status=ProjectStatus.ACTIVE,
        )
        await db.create_project(project2)
        ws = Workspace(
            id="ws-other",
            project_id="other-proj",
            workspace_path="/tmp/other",
            source_type=RepoSourceType.LINK,
        )
        await db.create_workspace(ws)

        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Fix conflicts",
                "preferred_workspace_id": "ws-other",
            },
        )

        assert "error" in result
        assert "other-proj" in result["error"]
