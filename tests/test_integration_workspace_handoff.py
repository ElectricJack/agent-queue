"""Server-side stop and checkout-detachment proofs for owner handoff."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import integration_branch_owners, sessions, workspaces
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


async def _release_owner_for_retry(
    orchestrator,
    monkeypatch,
    *,
    owner_role: str,
    events: list[str],
    pool: bool = False,
    session_lifecycle: str | None = None,
    dirty: bool = False,
):
    """Run the close-path release against an ``attached`` owner in *owner_role*.

    ``pool=True`` takes the pull-model leg, which proves the checkout detached
    instead of stopping the session.  ``session_lifecycle`` overrides what the
    session row says independently of the leg, so the mismatch can be tested.
    """
    await orchestrator.db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
    )
    lifecycle = session_lifecycle or ("pool" if pool else "task")
    async with orchestrator.db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "owner")
            .values(owner_role=owner_role, handoff_state="attached")
        )
        await conn.execute(
            update(sessions).where(sessions.c.id == "session").values(lifecycle=lifecycle)
        )
    provider = _provider(events)
    monkeypatch.setattr(orchestrator.session_providers, "create", lambda *_args: provider)
    current_branch, run = _clean_git(events, dirty=dirty)
    orchestrator.git.aget_current_branch = AsyncMock(side_effect=current_branch)
    orchestrator.git._arun_unlocked = AsyncMock(side_effect=run)
    task = await orchestrator.db.get_task("task")
    return await orchestrator.arelease_integration_writer_for_retry(
        task, reason="integration_repair_delegate_closed", pool=pool
    )


async def test_pool_release_detaches_the_checkout_without_stopping_the_loop(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A pool worker's release is a detach proof, not a session kill.

    Both shipped repair profiles are ``lifecycle: pool``, so the ladder has to
    work under the pull model.  Stopping the session would kill the worker
    loop mid-``aq task close``; what a pool close actually provides is the
    checkout coming off the branch plus the claim release that follows, and
    the release therefore runs *here*, while the claim evidence still exists
    (amber-delta).
    """
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []

    released = await _release_owner_for_retry(
        orchestrator, monkeypatch, owner_role="repair", events=events, pool=True
    )

    assert released is True
    # No "stop"/"confirm": the loop that is closing keeps running.
    assert events == ["clean-check", "fetch", "detach"]
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(orchestrator.db).get_owner(target)
    assert owner["owner_id"] == "task"
    assert owner["owner_role"] == "repair"
    assert owner["handoff_state"] == "reserved"
    assert int(owner["fence_token"]) == 5
    assert owner["session_id"] is None
    assert owner["workspace_id"] is None
    assert owner["confirmed_workspace_id"] == "slot"
    # ``release_claim`` unwinds these moments later on its own terms; clearing
    # them here would race it and destroy the evidence this check reads.
    workspace = await orchestrator.db.get_workspace("slot")
    assert workspace.locked_by_task_id == "task"
    assert workspace.locked_by_agent_id == "agent"
    session = await orchestrator.db.get_session("session")
    assert session.state == "running" and session.task_id == "task"
    assert (await orchestrator.db.get_agent("agent")).state is AgentState.BUSY

    successor = await BranchOwnership(orchestrator.db).transfer(
        Fence(target=target, owner_id="task", token=5), "operation", "collector"
    )
    assert successor == Fence(target=target, owner_id="operation", token=6)


async def test_pool_release_on_a_dirty_checkout_is_not_release_evidence(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Unprovable detach keeps the branch fenced and every resource held."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []

    released = await _release_owner_for_retry(
        orchestrator,
        monkeypatch,
        owner_role="repair",
        events=events,
        pool=True,
        dirty=True,
    )

    assert released is False
    assert "detach" not in events
    owner = await BranchOwnership(orchestrator.db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert owner["handoff_state"] == "handoff_pending"
    assert owner["session_id"] == "session"
    assert owner["workspace_id"] == "slot"
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"
    assert (await orchestrator.db.get_session("session")).task_id == "task"


async def test_pool_release_refuses_a_task_lifecycle_session(
    orchestrator_factory, tmp_path, monkeypatch
):
    """The pull-model proof is only valid for a session that survives its close.

    A ``lifecycle: task`` session is torn down after the close, so "the loop
    will not touch the branch again" is not something its claim can promise --
    that leg owes the full stop-and-confirm proof instead.
    """
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []

    released = await _release_owner_for_retry(
        orchestrator,
        monkeypatch,
        owner_role="repair",
        events=events,
        pool=True,
        session_lifecycle="task",
    )

    assert released is False
    assert events == []
    owner = await BranchOwnership(orchestrator.db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert owner["handoff_state"] == "handoff_pending"
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"


async def test_release_for_retry_restores_a_closed_repair_delegates_reservation(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A repair delegate's close must not leave its owner row attached forever.

    ``arelease_integration_writer_for_retry`` used to require
    ``owner_role == "worker"``, so the repair leg of the session close was a
    silent no-op: the row stayed ``attached`` to a session the close then
    stopped, and because the confirmer needs the workspace lock and the
    session/task binding that ``release_session_task_resources`` clears, no
    later transfer could confirm it either (amber-delta).
    """
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []

    released = await _release_owner_for_retry(
        orchestrator, monkeypatch, owner_role="repair", events=events
    )

    assert released is True
    assert events == ["validate-branch", "stop", "confirm", "clean-check", "fetch", "detach"]
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(orchestrator.db).get_owner(target)
    assert owner is not None
    # The delegate keeps its own role and gains a fresh reserved fence.
    assert owner["owner_id"] == "task"
    assert owner["owner_role"] == "repair"
    assert owner["handoff_state"] == "reserved"
    assert int(owner["fence_token"]) == 5
    assert owner["session_id"] is None
    assert owner["workspace_id"] is None
    assert owner["confirmed_workspace_id"] == "slot"
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id is None

    # Which is exactly what makes the successor's transfer provable: a
    # reserved owner needs no further stop/detach evidence.
    successor = await BranchOwnership(orchestrator.db).transfer(
        Fence(target=target, owner_id="task", token=5), "operation", "collector"
    )
    assert successor == Fence(target=target, owner_id="operation", token=6)


async def test_release_for_retry_is_idempotent_for_a_repair_delegate(
    orchestrator_factory, tmp_path, monkeypatch
):
    """A second release of an already-reserved delegate is a no-op success."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []
    assert (
        await _release_owner_for_retry(
            orchestrator, monkeypatch, owner_role="repair", events=events
        )
        is True
    )

    task = await orchestrator.db.get_task("task")
    again = await orchestrator.arelease_integration_writer_for_retry(
        task, reason="integration_repair_delegate_closed"
    )

    assert again is True
    assert events.count("stop") == 1
    owner = await BranchOwnership(orchestrator.db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert int(owner["fence_token"]) == 5


async def test_release_for_retry_leaves_a_collector_owner_alone(
    orchestrator_factory, tmp_path, monkeypatch
):
    """Only roles a *task* owns are self-transferable; collectors keep the rule."""
    orchestrator = await _orchestrator(orchestrator_factory, tmp_path)
    events: list[str] = []

    released = await _release_owner_for_retry(
        orchestrator, monkeypatch, owner_role="collector", events=events
    )

    assert released is False
    assert events == []
    owner = await BranchOwnership(orchestrator.db).get_owner(
        BranchKey(repository_id="repo", branch="aq/parent")
    )
    assert owner["handoff_state"] == "attached"
    assert (await orchestrator.db.get_workspace("slot")).locked_by_task_id == "task"
