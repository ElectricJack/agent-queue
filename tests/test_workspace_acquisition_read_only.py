"""T3 reviewer follow-up: enforce ``profile.read_only`` at workspace
acquisition.

A read-only profile must never hold a write lock on a mutable kind — the
lock is the only mechanism that prevents concurrent writers from being
silently overwritten.  ``acquire_for_task(..., read_only=True)``
attaches the workspace WITHOUT calling the lock path, so a read-only
profile can only observe the repo (git log/diff/show) and never own it.

Also verifies the declarative half of the enforcement: the default
reviewer profile lists no write/edit/commit/push tools.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator.workspace_attachments import acquire_for_task


def _now() -> float:
    return time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "ro.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="ro"))
    yield database
    await database.close()


async def _mkagent(db):
    await db.upsert_profile(
        AgentProfile(id="reviewer", name="reviewer", read_only=True)
    )
    a = Agent(id="a1", name="a1", profile_id="reviewer")
    await db.create_agent(a)
    return a


async def _mktask(db, tid="t1"):
    t = Task(
        id=tid, project_id="p1", title=tid, description="",
        created_at=_now(), updated_at=_now(),
    )
    await db.create_task(t)
    return t


async def _project_repo(db):
    await db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE,
            id="project-repo",
            is_git_repo=True,
            lockable=True,
            default_lock_mode="exclusive",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await db.create_workspace(
        Workspace(
            id="ws-repo", project_id="p1", workspace_path="/tmp/repo",
            source_type=RepoSourceType.CLONE, kind_id="project-repo",
        )
    )


class TestReadOnlyAcquisition:
    async def test_read_only_attaches_without_lock(self, db):
        await _project_repo(db)
        agent = await _mkagent(db)
        task = await _mktask(db)

        att = await acquire_for_task(db, task, agent.id, read_only=True)
        assert att.first_of_kind("project-repo") is not None

        # No write lock was acquired — the workspace row is not locked
        # to this task.
        ws = await db.first_workspace_of_kind(project_id="p1", kind_id="project-repo")
        assert ws.locked_by_task_id in (None, "")
        assert ws.locked_by_agent_id in (None, "")

    async def test_writable_profile_still_locks(self, db):
        # Baseline: a normal (read_only=False) profile keeps the write lock.
        await db.upsert_profile(AgentProfile(id="worker", name="worker"))
        agent = Agent(id="a2", name="a2", profile_id="worker")
        await db.create_agent(agent)
        await _project_repo(db)
        task = await _mktask(db, tid="t2")

        await acquire_for_task(db, task, agent.id, read_only=False)
        ws = await db.first_workspace_of_kind(project_id="p1", kind_id="project-repo")
        assert ws.locked_by_task_id == "t2"


class TestReviewerProfileDeclarative:
    """Belt-and-braces: the shipped reviewer profile must not list write
    tools even if the acquisition guard is ever bypassed."""

    def test_reviewer_tool_list_has_no_write_tools(self):
        # Locate the shipped reviewer profile relative to this test.
        repo_root = Path(__file__).resolve().parents[1]
        profile_path = repo_root / "src" / "profiles" / "defaults" / "reviewer" / "profile.md"
        text = profile_path.read_text()

        forbidden = (
            "write_file", "edit_file", "delete_file",
            "git_commit", "git_push", "git_merge", "git_reset",
            "gh_pr_merge",
        )
        for tool in forbidden:
            assert tool not in text, (
                f"reviewer profile must not list write tool {tool!r}"
            )
        # And read_only must be declared.
        assert '"read_only": true' in text
