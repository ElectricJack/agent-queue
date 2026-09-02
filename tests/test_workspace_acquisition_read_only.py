"""``profile.read_only`` no longer changes workspace acquisition.

It used to: a read-only profile attached the ``project-repo`` kind's *first*
workspace without taking a lock, so a reviewer could not silently own the
repo.  What that actually did was hand every read-only agent the kind's
**base** row — under worktree mode the registry root, routinely a ``LINK``
pointing at a human's own checkout — and the caller then wrote
``.agent-queue-lock`` into it anyway, so read-only agents both ran their
tools in the operator's tree and serialized on its sentinel.

The contract now: read-only is a statement of write *intent*, enforced
declaratively by the profile's tool list.  A read-only task acquires a
disposable slot exactly like any other task, and never the base.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_WORKTREE,
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


async def _project_repo(db, *, mode: str = KIND_MODE_WORKTREE):
    await db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE,
            id="project-repo",
            is_git_repo=True,
            lockable=True,
            default_lock_mode="exclusive",
            mode=mode,
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


async def _add_slot(db, ws_id: str, index: int):
    await db.create_workspace(
        Workspace(
            id=ws_id,
            project_id="p1",
            workspace_path=f"/tmp/repo/.aq/worktrees/slot-{index}",
            source_type=RepoSourceType.WORKTREE,
            kind_id="project-repo",
            slot_index=index,
            base_workspace_id="ws-repo",
        )
    )


class TestReadOnlyAcquisition:
    async def test_read_only_takes_a_slot_not_the_base(self, db):
        """The regression this file exists for."""
        await _project_repo(db)
        await _add_slot(db, "ws-slot-0", 0)
        agent = await _mkagent(db)
        task = await _mktask(db)

        att = await acquire_for_task(
            db, task, agent.id, worktrees_enabled=True, worktree_slot_cap=1
        )
        attachment = att.first_of_kind("project-repo")
        assert attachment is not None
        assert attachment.workspace.id == "ws-slot-0"
        assert attachment.workspace.is_slot

        # ...and the base is untouched: no lock, so no session is ever
        # routed into the developer's checkout.
        assert (await db.get_workspace("ws-repo")).locked_by_task_id in (None, "")

    async def test_read_only_locks_its_slot(self, db):
        """A read-only agent owns its disposable slot for the task's life.

        Sharing an unlocked workspace was the old behaviour; it let a second
        agent reset the tree out from under the reader mid-read.
        """
        await _project_repo(db)
        await _add_slot(db, "ws-slot-0", 0)
        agent = await _mkagent(db)
        task = await _mktask(db)

        await acquire_for_task(
            db, task, agent.id, worktrees_enabled=True, worktree_slot_cap=1
        )
        assert (await db.get_workspace("ws-slot-0")).locked_by_task_id == task.id

    async def test_writable_profile_still_locks(self, db):
        # Baseline: a normal (read_only=False) profile keeps the write lock.
        await db.upsert_profile(AgentProfile(id="worker", name="worker"))
        agent = Agent(id="a2", name="a2", profile_id="worker")
        await db.create_agent(agent)
        await _project_repo(db)
        task = await _mktask(db, tid="t2")

        await acquire_for_task(db, task, agent.id)
        ws = await db.first_workspace_of_kind(project_id="p1", kind_id="project-repo")
        assert ws.locked_by_task_id == "t2"


class TestReviewerProfileDeclarative:
    """The declarative half is now the *whole* of ``read_only``: the shipped
    reviewer profile must not list write tools."""

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
