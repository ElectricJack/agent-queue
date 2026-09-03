"""A worker token may push and open a PR for its own task branch, only.

Every worker profile's close protocol ends with "push the branch and open a
PR", but ``AGENT_COMMAND_SET`` carries no ``git_*`` command, so the aq path
answered ``out of scope: git_create_pr`` and workers fell back to the raw
``gh`` CLI.  The carve-out here is verified against persisted state (session →
task → agent) and reaches exactly the branch the task records.
"""

from __future__ import annotations

import time

import pytest

from src.api.auth import RequestScope
from src.api.scope import (
    _WORKER_GIT_COMMANDS,
    check_request_scope,
    worker_branches_for_session,
)
from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    SessionRecord,
    Task,
    TaskStatus,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["task", "pool"])
async def env(tmp_path, request):
    db = Database(str(tmp_path / "worker-git.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p", repo_default_branch="main"))
    await db.create_project(Project(id="other", name="other"))
    await db.upsert_profile(AgentProfile(
        id="coder", name="coder", harness="claude", needs_workspace=False,
        default_class="standard-low",
    ))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="coder"))
    await db.create_task(Task(
        id="t1", project_id="p", title="work", description="work",
        status=TaskStatus.IN_PROGRESS, profile_id="coder", assigned_agent_id="a1",
        branch_name="aq/calm-ember-48",
    ))
    await db.update_agent("a1", state=AgentState.BUSY, current_task_id="t1")
    await db.create_session(SessionRecord(
        id="s1", task_id="t1", project_id="p", agent_id="a1", profile_id="coder",
        harness="claude", provider="fake", name="s1", lifecycle=request.param,
        state="running", work_dir=str(tmp_path), epoch="test",
        instance_token="instance-a1", started_at=time.time(), last_claim_epoch=0,
    ))
    scope = RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p")
    yield db, scope
    await db.close()


# --- the grant ------------------------------------------------------------


async def test_worker_creates_a_pr_for_its_own_branch(env):
    db, scope = env
    args = {"title": "Fix it", "branch": "aq/calm-ember-48"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None
    assert args["project_id"] == "p"
    assert args["session_id"] == "s1"


async def test_worker_creates_a_pr_with_defaults(env):
    """The common case: no explicit branch (the worktree is already on it)."""
    db, scope = env
    args = {"title": "Fix it"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None


async def test_conventional_branch_name_is_also_accepted(env):
    db, scope = env
    args = {"title": "Fix it", "branch": "aq/t1"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None


async def test_worker_pushes_its_own_branch(env):
    db, scope = env
    args = {"branch": "aq/calm-ember-48"}
    assert await check_request_scope("git_push", args, scope, db=db) is None


@pytest.mark.parametrize(
    "command,args",
    [
        ("get_git_status", {}),
        ("git_diff", {}),
        ("git_log", {"count": 5}),
        ("git_branch", {}),
        ("git_changed_files", {}),
    ],
)
async def test_read_only_git_commands_are_reachable(env, command, args):
    db, scope = env
    assert await check_request_scope(command, args, scope, db=db) is None


async def test_pr_base_may_be_the_project_default_branch(env):
    db, scope = env
    args = {"title": "Fix it", "base": "main"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None


# --- the limits -----------------------------------------------------------


async def test_worker_cannot_create_a_pr_for_another_branch(env):
    db, scope = env
    args = {"title": "Sneaky", "branch": "aq/someone-else-99"}
    assert (
        await check_request_scope("git_create_pr", args, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_push_another_branch(env):
    db, scope = env
    assert (
        await check_request_scope("git_push", {"branch": "main"}, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_retarget_a_pr_base(env):
    db, scope = env
    args = {"title": "Sneaky", "base": "release/1.0"}
    assert (
        await check_request_scope("git_create_pr", args, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_create_an_unrelated_branch(env):
    db, scope = env
    assert (
        await check_request_scope("git_branch", {"name": "wip/whatever"}, scope, db=db)
        == "out of scope: branch mismatch"
    )


@pytest.mark.parametrize("command", ["git_merge", "pr_merge", "git_checkout", "git_commit"])
async def test_merge_and_checkout_stay_out_of_scope(env, command):
    db, scope = env
    assert command not in _WORKER_GIT_COMMANDS
    assert (
        await check_request_scope(command, {"branch_name": "aq/calm-ember-48"}, scope, db=db)
        == f"out of scope: {command}"
    )


async def test_another_projects_id_is_rejected(env):
    db, scope = env
    args = {"title": "Fix it", "project_id": "other"}
    assert (
        await check_request_scope("git_create_pr", args, scope, db=db)
        == "out of scope: project_id mismatch"
    )


async def test_a_stale_session_grants_nothing(env):
    """The task moved on; the still-running session must lose the grant."""
    db, scope = env
    await db.transition_task("t1", TaskStatus.COMPLETED, context="test")
    assert await worker_branches_for_session(db, scope) is None
    assert (
        await check_request_scope("git_push", {}, scope, db=db)
        == "out of scope: git_push"
    )


async def test_a_token_for_an_unknown_session_grants_nothing(env):
    db, _ = env
    scope = RequestScope(kind="session", session_id="ghost", task_id="t1", project_id="p")
    assert await worker_branches_for_session(db, scope) is None
