"""Server-side stop and checkout-detachment proofs for owner handoff."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import integration_branch_owners, workspaces
from src.git.manager import GitError
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.models import (
    Agent,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    Workspace,
)


pytestmark = pytest.mark.asyncio


async def _orchestrator(orchestrator_factory, tmp_path):
    orchestrator = await orchestrator_factory()
    db = orchestrator.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE)
    )
    await db.create_task(
        Task(
            id="task",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Writer",
            description="",
        )
    )
    await db.create_agent(
        Agent(
            id="agent",
            name="Worker",
            profile_id="worker",
            state=AgentState.BUSY,
            current_task_id="task",
        )
    )
    base = Workspace(
        id="base",
        project_id="p",
        workspace_path=str(tmp_path / "base"),
        source_type=RepoSourceType.CLONE,
    )
    slot = Workspace(
        id="slot",
        project_id="p",
        workspace_path=str(tmp_path / "slot"),
        source_type=RepoSourceType.WORKTREE,
        locked_by_agent_id="agent",
        locked_by_task_id="task",
        slot_index=0,
        base_workspace_id="base",
    )
    await db.create_workspace(base)
    await db.create_workspace(slot)
    await db.create_session(
        SessionRecord(
            id="session",
            task_id="task",
            agent_id="agent",
            project_id="p",
            profile_id="worker",
            harness="codex",
            provider="fake",
            name="s-task",
            lifecycle="task",
            work_dir=slot.workspace_path,
            epoch="epoch",
            instance_token="instance",
            started_at=time.time(),
            state="running",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="task",
                owner_role="worker",
                fence_token=4,
                handoff_state="handoff_pending",
                session_id="session",
                workspace_id="slot",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    return orchestrator


def _owner(**overrides) -> dict:
    return {
        "id": "owner",
        "repository_id": "repo",
        "ref": "aq/parent",
        "owner_id": "task",
        "owner_role": "worker",
        "fence_token": 4,
        "handoff_state": "handoff_pending",
        "session_id": "session",
        "workspace_id": "slot",
        "confirmed_workspace_id": None,
        **overrides,
    }


def _provider(events: list[str], *, stopped: bool = True, stop_error=None):
    async def stop(_handle, *, grace):
        events.append("stop")
        if stop_error is not None:
            raise stop_error

    async def confirm_stopped(_handle):
        events.append("confirm")
        return stopped

    return SimpleNamespace(stop=stop, confirm_stopped=confirm_stopped)


def _clean_git(
    events: list[str],
    *,
    dirty: bool = False,
    pushed: bool = True,
    detach_error: Exception | None = None,
    already_detached: bool = False,
    detached_head_matches: bool = True,
):
    head = "a" * 40
    remote = head if pushed else "b" * 40
    detached = already_detached
    checkout_head = head if detached_head_matches else "c" * 40

    async def current_branch(_path, *, strict=False):
        events.append("validate-branch")
        return "HEAD" if detached else "aq/parent"

    async def run(args, *, cwd):
        nonlocal detached
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "HEAD" if detached else "aq/parent"
        if args[:2] == ["status", "--porcelain"]:
            events.append("clean-check")
            return " M changed.py" if dirty else ""
        if args[:2] == ["fetch", "origin"]:
            events.append("fetch")
            return ""
        if args[:2] == ["rev-parse", "refs/heads/aq/parent"]:
            return head
        if args[:2] == ["rev-parse", "refs/remotes/origin/aq/parent"]:
            return remote
        if args[:2] == ["rev-parse", "HEAD"]:
            return checkout_head if detached else head
        if args[:2] == ["switch", "--detach"]:
            events.append("detach")
            if detach_error is not None:
                raise detach_error
            detached = True
            return ""
        raise AssertionError(f"unexpected git call: {args!r} in {cwd}")

    return current_branch, run


async def test_stop_timeout_is_not_release_evidence(orchestrator_factory, tmp_path, monkeypatch):
    """A timed-out stop request must leave the checkout locked."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events, stop_error=TimeoutError("still stopping"))
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is False
    assert events == ["validate-branch", "stop"]
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_dirty_slot_is_not_detached_or_released(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Resetting a dirty handoff checkout would destroy the writer's evidence."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events, dirty=True)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is False
    assert events == ["validate-branch", "stop", "confirm", "clean-check"]
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_unpushed_slot_is_not_detached_or_released(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A clean local tip that differs from origin must remain attached."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events, pushed=False)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is False
    assert events == ["validate-branch", "stop", "confirm", "clean-check", "fetch"]
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_foreign_checked_out_branch_is_rejected_before_stopping(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A stale ownership row must not stop the writer of another branch."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    orchestrator.git.aget_current_branch = AsyncMock(return_value="aq/other")

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is False
    assert events == []
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_failed_detach_keeps_the_slot_locked(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A failed checkout detach is not workspace-release evidence."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(
        events, detach_error=GitError("could not detach")
    )
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is False
    assert events[-1] == "detach"
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_success_stops_confirms_detaches_then_releases(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Reordering release before detach would expose a live branch checkout."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)
    from src.orchestrator import workspace_attachments

    real_release = workspace_attachments.mark_integration_handoff_released

    async def release(*args, **kwargs):
        events.append("release")
        return await real_release(*args, **kwargs)

    monkeypatch.setattr(
        workspace_attachments, "mark_integration_handoff_released", release
    )

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is True
    assert events == [
        "validate-branch",
        "stop",
        "confirm",
        "clean-check",
        "fetch",
        "detach",
        "release",
    ]
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id is None
    assert (await orchestrator.db.get_session("session")).state == "stopped"
    agent = await orchestrator.db.get_agent("agent")
    assert agent.state is AgentState.IDLE
    assert agent.current_task_id is None


async def test_handoff_release_does_not_clear_a_reused_agent_binding(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Delayed proof must not clear an agent that was rebound to another task."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    await orchestrator.db.create_task(
        Task(
            id="replacement",
            project_id="p",
            repo_id="repo",
            branch_name="aq/replacement",
            title="Replacement",
            description="",
        )
    )
    await orchestrator.db.update_agent(
        "agent", state=AgentState.BUSY, current_task_id="replacement"
    )
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is True
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id is None
    agent = await orchestrator.db.get_agent("agent")
    assert agent.state is AgentState.BUSY
    assert agent.current_task_id == "replacement"


async def test_released_handoff_recovers_after_crash_without_touching_a_new_holder(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A crash after release but before token advance must not wedge or stop a successor."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    assert await orchestrator.aconfirm_integration_owner_handoff(_owner()) is True
    async with orchestrator.db._engine.connect() as conn:
        released = (
            await conn.execute(select(integration_branch_owners))
        ).mappings().one()
    assert released["handoff_state"] == "released"
    assert released["fence_token"] == 4
    assert released["confirmed_workspace_id"] == "slot"

    await orchestrator.db.create_task(
        Task(
            id="unrelated",
            project_id="p",
            repo_id="repo",
            branch_name="aq/unrelated",
            title="Unrelated",
            description="",
        )
    )
    async with orchestrator.db.immediate() as conn:
        await conn.execute(
            update(workspaces)
            .where(workspaces.c.id == "slot")
            .values(locked_by_task_id="unrelated")
        )

    events.clear()
    assert await orchestrator.aconfirm_integration_owner_handoff(_owner()) is True
    assert events == []
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "unrelated"

    await orchestrator.db.create_task(
        Task(
            id="next",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Next",
            description="",
        )
    )
    result = await orchestrator.command_handler.execute(
        "integration_transfer_owner",
        {
            "target": {"repository_id": "repo", "branch": "aq/parent"},
            "expected_token": 4,
            "next_owner_id": "next",
            "next_role": "worker",
        },
    )
    assert result["outcome"] == "transferred"
    assert result["fence"]["token"] == 5
    assert events == []


async def test_detached_handoff_retries_after_crash_before_durable_release(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Requiring the named branch on retry would wedge a proven detached HEAD."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)
    from src.orchestrator import workspace_attachments

    real_mark = workspace_attachments.mark_integration_handoff_released
    attempts = 0

    async def fail_once_before_mark(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("crash before durable release")
        return await real_mark(*args, **kwargs)

    monkeypatch.setattr(
        workspace_attachments,
        "mark_integration_handoff_released",
        fail_once_before_mark,
    )
    target = BranchKey(repository_id="repo", branch="aq/parent")
    old_fence = Fence(target=target, owner_id="task", token=4)

    with pytest.raises(RuntimeError, match="crash before durable release"):
        await BranchOwnership(
            orchestrator.db,
            confirm_handoff=orchestrator.aconfirm_integration_owner_handoff,
        ).transfer(old_fence, "next", "worker")

    pending = await BranchOwnership(orchestrator.db).get_owner(target)
    assert pending is not None
    assert pending["handoff_state"] == "handoff_pending"
    assert pending["fence_token"] == 4
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"

    fresh_ownership = BranchOwnership(
        orchestrator.db,
        confirm_handoff=orchestrator.aconfirm_integration_owner_handoff,
    )
    transferred = await fresh_ownership.transfer(old_fence, "next", "worker")

    assert transferred == Fence(target=target, owner_id="next", token=5)
    assert attempts == 2
    assert events.count("detach") == 1
    assert events.count("confirm") == 2
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id is None


async def test_detached_handoff_with_wrong_head_remains_busy(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Detached state alone must not release a checkout at an unrelated SHA."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(
        events,
        already_detached=True,
        detached_head_matches=False,
    )
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is False
    assert events == ["validate-branch", "stop", "confirm", "clean-check", "fetch"]
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_detached_non_slot_releases_only_after_exact_git_proof(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A non-slot retry after detach must re-prove the exact clean pushed HEAD."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    async with orchestrator.db.immediate() as conn:
        await conn.execute(
            update(workspaces)
            .where(workspaces.c.id == "slot")
            .values(
                source_type=RepoSourceType.CLONE.value,
                slot_index=None,
                base_workspace_id=None,
            )
        )
    events: list[str] = []
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events, already_detached=True)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)

    confirmed = await orchestrator.aconfirm_integration_owner_handoff(_owner())

    assert confirmed is True
    assert events == ["validate-branch", "stop", "confirm", "clean-check", "fetch"]
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id is None
