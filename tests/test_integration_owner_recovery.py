"""``OwnerRecovery`` — release a stranded integration branch owner, with evidence.

Real PostgreSQL and real Git: a bare ``origin.git``, a base clone, and a
``git worktree add`` slot standing in for a worker's checkout.  Owner rows and
sessions are inserted directly, as the development-integration tests do, so
each case states exactly the durable facts the recovery check reads.

See docs/superpowers/specs/2026-09-21-integration-owner-recovery-design.md §3.
"""

from __future__ import annotations

import importlib.util
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, text, update

from src.database.tables import (
    agents,
    events,
    integration_branch_owners,
    integration_owner_recoveries,
    projects,
    sessions,
    task_branch_origins,
    task_comments,
    tasks,
    workspaces,
)
from src.git.manager import GitManager
from src.integration.owner_recovery import (
    RECOVERABLE_STATES,
    OwnerRecovery,
    RecoveryOutcome,
    owner_recovery_for,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus, WorktreeSentinel
from src.orchestrator.worktree_manager import WorktreeSlotManager


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def remote_sha(origin, ref) -> str | None:
    out = git(origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{ref}")
    return out or None


@pytest.fixture
async def env(tmp_path, reuse_database):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    base = tmp_path / "base"
    git(tmp_path, "clone", str(origin), str(base))
    git(base, "config", "user.name", "Tester")
    git(base, "config", "user.email", "tester@example.test")
    (base / "base.txt").write_text("base\n")
    git(base, "add", ".")
    git(base, "commit", "-m", "base")
    git(base, "push", "origin", "main")
    db = await reuse_database("owner-recovery.db")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                integration_repository_id="r",
                hierarchical_integration_mode="development",
                hierarchical_integration_desired_mode="development",
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="ws-base", project_id="p", workspace_path=str(base), created_at=1.0
            )
        )
    yield Env(db=db, origin=origin, base=base, tmp=tmp_path)


class Env:
    def __init__(self, *, db, origin, base, tmp):
        self.db, self.origin, self.base, self.tmp = db, origin, base, tmp

    def branch(self, name: str, *, push: bool = True, extra: int = 0) -> str:
        """Create *name* from main with one commit (pushed) plus *extra* unpushed ones."""
        git(self.base, "checkout", "-q", "-b", name, "main")
        (self.base / f"{name.replace('/', '-')}.txt").write_text("work\n")
        git(self.base, "add", ".")
        git(self.base, "commit", "-q", "-m", name)
        if push:
            git(self.base, "push", "-q", "origin", name)
        for n in range(extra):
            (self.base / f"{name.replace('/', '-')}-{n}.txt").write_text("more\n")
            git(self.base, "add", ".")
            git(self.base, "commit", "-q", "-m", f"{name} {n}")
        tip = git(self.base, "rev-parse", "HEAD")
        git(self.base, "checkout", "-q", "main")
        return tip

    async def slot(self, name: str, branch: str, *, workspace_id: str = "ws-slot", **values):
        path = self.tmp / name
        git(self.base, "worktree", "add", "-q", str(path), branch)
        # Model the identity and control-file exclusion written by slot provisioning.
        WorktreeSlotManager.ensure_git_exclude_path(
            WorktreeSlotManager.resolve_exclude_path(path)
        )
        WorktreeSlotManager.write_sentinel(path, WorktreeSentinel(
            slot="slot-0", slot_index=0, base_workspace_id="ws-base",
            project_id="p", workspace_id=workspace_id, branch=branch,
            task_id=branch.removeprefix("aq/"), created_at=1.0, assigned_at=1.0,
            daemon_epoch="e", setup_hash="",
        ))
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    project_id="p",
                    workspace_path=str(path),
                    base_workspace_id="ws-base",
                    slot_index=0,
                    created_at=1.0,
                    **values,
                )
            )
        return path

    async def task(self, task_id: str, status: TaskStatus = TaskStatus.BLOCKED) -> None:
        await self.db.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
        await self.db.update_task(task_id, status=status)

    async def session(
        self,
        session_id: str,
        *,
        work_dir,
        state: str = "stopped",
        desired_state: str = "stopped",
        task_id: str | None = None,
        **values,
    ) -> None:
        values.setdefault("claim_phase_at", 1.0)
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(sessions).values(
                    id=session_id,
                    task_id=task_id,
                    project_id="p",
                    profile_id="worker",
                    harness="claude",
                    provider="fake",
                    name=f"n-{session_id}",
                    lifecycle="pool",
                    state=state,
                    desired_state=desired_state,
                    work_dir=str(work_dir),
                    epoch="e",
                    instance_token=f"tok-{session_id}",
                    started_at=1.0,
                    **values,
                )
            )

    async def owner(
        self,
        row_id: str,
        ref: str,
        owner_id: str,
        *,
        state: str = "attached",
        role: str = "worker",
        session_id: str | None = None,
        workspace_id: str | None = None,
        fence: int = 3,
        updated_at: float = 1.0,
        provenance: bool = True,
        origin_base: str | None = None,
    ) -> None:
        async with self.db.immediate() as conn:
            if provenance and not await conn.scalar(select(task_branch_origins.c.id).where(
                task_branch_origins.c.task_id == owner_id,
                task_branch_origins.c.repository_id == "r",
                task_branch_origins.c.retired_at.is_(None),
            )):
                await conn.execute(insert(task_branch_origins).values(
                    id=f"origin-{row_id}", task_id=owner_id, repository_id="r",
                    branch_name=ref.removeprefix("refs/heads/"),
                    base_sha=origin_base or git(self.base, "rev-parse", "main"), creation_generation=0,
                    reserved=True, materialized=True, created_at=1.0,
                ))
            await conn.execute(
                insert(integration_branch_owners).values(
                    id=row_id,
                    repository_id="r",
                    ref=ref,
                    owner_id=owner_id,
                    owner_role=role,
                    fence_token=fence,
                    handoff_state=state,
                    session_id=session_id,
                    workspace_id=workspace_id,
                    created_at=1.0,
                    updated_at=updated_at,
                )
            )

    async def row(self, row_id: str) -> dict:
        async with self.db._engine.connect() as conn:
            return dict(
                (
                    await conn.execute(
                        select(integration_branch_owners).where(
                            integration_branch_owners.c.id == row_id
                        )
                    )
                )
                .mappings()
                .one()
            )

    async def audits(self, row_id: str) -> list[dict]:
        async with self.db._engine.connect() as conn:
            return [
                dict(r)
                for r in (
                    await conn.execute(
                        select(integration_owner_recoveries)
                        .where(integration_owner_recoveries.c.owner_row_id == row_id)
                        .order_by(integration_owner_recoveries.c.created_at)
                    )
                ).mappings()
            ]

    async def events(self, event_type: str) -> list[dict]:
        async with self.db._engine.connect() as conn:
            return [
                dict(r)
                for r in (
                    await conn.execute(select(events).where(events.c.event_type == event_type))
                ).mappings()
            ]

    async def comments(self, task_id: str) -> list[str]:
        async with self.db._engine.connect() as conn:
            return list(
                (
                    await conn.execute(
                        select(task_comments.c.body).where(task_comments.c.task_id == task_id)
                    )
                ).scalars()
            )

    def service(self, *, confirm_stopped=None, clock=time.time) -> OwnerRecovery:
        return OwnerRecovery(
            self.db,
            GitManager(),
            None,
            confirm_stopped=confirm_stopped or AsyncMock(return_value=True),
            clock=clock,
        )


def detached(path) -> bool:
    return git(path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


async def test_stopped_writer_on_a_clean_published_checkout_is_released(env):
    env.branch("aq/t1")
    slot = await env.slot("slot", "aq/t1", locked_by_task_id=None)
    await env.task("t1")
    await env.session("s1", work_dir=slot)
    await env.owner("o1", "aq/t1", "t1", session_id="s1", workspace_id="ws-slot")

    result = await env.service().recover("o1", principal="human:local-operator")

    assert isinstance(result, RecoveryOutcome)
    assert (result.outcome, result.reason, result.dry_run) == ("released", None, False)
    row = await env.row("o1")
    assert row["handoff_state"] == "released"
    assert row["fence_token"] == 4
    assert row["session_id"] is None and row["workspace_id"] is None
    assert row["confirmed_workspace_id"] == "ws-slot"
    assert detached(slot)
    assert result.evidence["origin_sha"] == result.evidence["local_sha"]
    assert result.evidence["worktree"] == str(slot)
    assert result.evidence["preserved_ref"] is None
    assert result.evidence["stop_proof"]["session_id"] == "s1"
    [audit] = await env.audits("o1")
    assert audit["outcome"] == "released"
    assert audit["principal"] == "human:local-operator"
    assert (audit["repository_id"], audit["ref"], audit["task_id"]) == ("r", "aq/t1", "t1")
    assert audit["evidence"]["worktree"] == str(slot)
    [event] = await env.events("integration.owner_recovered")
    assert event["task_id"] == "t1" and event["project_id"] == "p"
    [comment] = await env.comments("t1")
    assert "o1" in comment and "released" in comment
    # Nothing was preserved, so nothing was pushed.
    assert remote_sha(env.origin, "aq/preserved/o1") is None


async def test_thrash_disposition_and_real_owner_recovery_serialize(env):
    import asyncio

    from src.models import Agent, AgentState
    from src.sessions.worker_recovery import (
        STREAK_KEY,
        pool_recovery_disposition,
        record_failure,
    )

    tip = env.branch("aq/thrash")
    await env.task("thrash", status=TaskStatus.IN_PROGRESS)
    await env.db.create_agent(Agent(
        id="writer-agent", name="Writer", profile_id="worker", state=AgentState.BUSY,
        current_task_id="thrash",
    ))
    await env.db.update_task(
        "thrash", assigned_agent_id="writer-agent", branch_name="aq/thrash", repo_id="r",
        claim_epoch=1,
    )
    slot = await env.slot(
        "thrash-slot", "aq/thrash", locked_by_task_id="thrash", locked_by_agent_id="writer-agent",
    )
    await env.session(
        "thrash-writer", work_dir=slot, task_id="thrash", agent_id="writer-agent",
        state="running", desired_state="running", claim_phase="active", last_claim_epoch=1,
    )
    await env.owner(
        "thrash-owner", "aq/thrash", "thrash", session_id="thrash-writer", workspace_id="ws-slot",
    )
    session = await env.db.get_session("thrash-writer")
    task = await env.db.get_task("thrash")
    failure = await record_failure(env.db, session, task, "context_thrash", now=time.time())
    await env.db.update_session(
        session.id, state="stopped", desired_state="stopped", end_reason="context_thrash",
    )
    receipt, released = await asyncio.wait_for(asyncio.gather(
        pool_recovery_disposition(env.db, session, task, failure),
        env.service().recover("thrash-owner", principal="sweep"),
    ), timeout=15)
    assert released.outcome == "released", released
    assert released.evidence["origin_sha"] == tip
    assert released.evidence["stop_proof"]["instance_token"] == session.instance_token
    assert (await env.row("thrash-owner"))["handoff_state"] == "released"
    assert (await env.db.get_task_meta("thrash", STREAK_KEY))["count"] == 1
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id is None
    assert (await env.db.get_session(session.id)).task_id is None
    # Either serial order is safe. A deferred disposition can precede release;
    # an owner release that wins first produces an actually ready continuation.
    assert receipt["disposition"] in {"handoff_deferred", "retry_ready"}
    expected = TaskStatus.BLOCKED if receipt["disposition"] == "handoff_deferred" else TaskStatus.READY
    assert (await env.db.get_task("thrash")).status is expected


async def test_a_local_branch_ahead_of_origin_is_preserved_then_released(env):
    tip = env.branch("aq/t2", extra=1)
    await env.task("t2")
    await env.session("s2", work_dir=env.tmp / "gone")
    await env.owner("o2", "aq/t2", "t2", state="handoff_pending", session_id="s2")

    result = await env.service().recover("o2", principal="sweep")

    assert result.outcome == "preserved_and_released"
    assert remote_sha(env.origin, "aq/t2") == tip
    assert result.evidence["preserved_ref"] == "aq/t2"
    assert result.evidence["preserved_sha"] == tip
    assert result.evidence["local_sha"] == tip
    assert result.evidence["worktree"] is None
    assert (await env.row("o2"))["handoff_state"] == "released"
    assert remote_sha(env.origin, "aq/t2") == tip
    assert git(env.origin, "for-each-ref", "refs/heads/aq/preserved/") == ""


async def test_a_preserved_ref_already_at_the_same_sha_is_not_a_failure(env):
    tip = env.branch("aq/t2", extra=1)
    git(env.base, "push", "-q", "origin", f"{tip}:refs/heads/aq/preserved/o2")
    await env.session("s2", work_dir=env.tmp / "gone")
    await env.owner("o2", "aq/t2", "t2", session_id="s2")

    result = await env.service().recover("o2", principal="sweep")

    assert result.outcome == "preserved_and_released"
    assert remote_sha(env.origin, "aq/t2") == tip


async def test_a_dirty_worktree_is_snapshotted_without_touching_it_then_detached(env):
    head = env.branch("aq/t3")
    slot = await env.slot("slot", "aq/t3", locked_by_task_id=None)
    await env.task("t3")
    async with env.db.immediate() as conn:
        await conn.execute(
            update(workspaces).where(workspaces.c.id == "ws-slot").values(locked_by_task_id="t3")
        )
    (slot / "base.txt").write_text("edited\n")
    (slot / "untracked.txt").write_text("brand new\n")
    await env.session("s3", work_dir=slot)
    await env.owner("o3", "aq/t3", "t3", session_id="s3", workspace_id="ws-slot")

    result = await env.service().recover("o3", principal="sweep")

    assert result.outcome == "preserved_and_released"
    preserved = remote_sha(env.origin, "aq/t3")
    assert preserved is not None and preserved == result.evidence["preserved_sha"]
    assert git(env.origin, "show", f"{preserved}:base.txt") == "edited"
    assert git(env.origin, "show", f"{preserved}:untracked.txt") == "brand new"
    assert git(env.origin, "rev-parse", f"{preserved}^") == head
    # The working tree and the live index are exactly as the writer left them.
    assert (slot / "base.txt").read_text() == "edited\n"
    assert (slot / "untracked.txt").read_text() == "brand new\n"
    assert git(slot, "diff", "--cached", "--name-only") == ""
    assert git(slot, "rev-parse", "HEAD") == head
    assert detached(slot)
    workspace = await env.db.get_workspace("ws-slot")
    assert workspace.locked_by_task_id is None
    # A dirty checkout must not be recycled by allocation.
    async with env.db._engine.connect() as conn:
        enabled = (
            await conn.execute(select(workspaces.c.enabled).where(workspaces.c.id == "ws-slot"))
        ).scalar_one()
    assert enabled is False


async def test_an_origin_ref_deleted_after_landing_on_main_is_safe(env):
    tip = env.branch("aq/t4", push=False)
    git(env.base, "push", "-q", "origin", f"{tip}:refs/heads/main")
    await env.session("s4", work_dir=env.tmp / "gone")
    await env.owner("o4", "aq/t4", "t4", session_id="s4")

    result = await env.service().recover("o4", principal="sweep")

    assert result.outcome == "released"
    assert result.evidence["origin_sha"] is None
    assert result.evidence["local_sha"] == tip
    assert git(env.origin, "for-each-ref", "refs/heads/aq/preserved/") == ""


async def test_a_deleted_task_row_does_not_block_release(env):
    env.branch("aq/t5")
    await env.session("s5", work_dir=env.tmp / "gone")
    await env.owner("o5", "aq/t5", "t5-deleted", session_id="s5")

    result = await env.service().recover("o5", principal="doctor")

    assert result.outcome == "released"
    assert (await env.row("o5"))["handoff_state"] == "released"
    [audit] = await env.audits("o5")
    assert audit["task_id"] == "t5-deleted"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["running", "unconfirmed", "newer_live_session"])
async def test_a_writer_that_is_not_proven_gone_is_refused(env, case):
    env.branch("aq/t6")
    await env.task("t6")
    state = "running" if case == "running" else "stopped"
    await env.session("s6", work_dir=env.tmp / "gone", state=state, desired_state=state)
    if case == "newer_live_session":
        await env.session(
            "s6-new",
            work_dir=env.tmp / "other",
            state="running",
            desired_state="running",
            task_id="t6",
        )
    await env.owner("o6", "aq/t6", "t6", session_id="s6")
    confirm = AsyncMock(return_value=case != "unconfirmed")

    result = await env.service(confirm_stopped=confirm).recover("o6", principal="sweep")

    assert (result.outcome, result.reason) == ("not_eligible", "writer_live")
    live = "s6-new" if case == "newer_live_session" else "s6"
    assert result.evidence["session_id"] == live
    row = await env.row("o6")
    assert (row["handoff_state"], row["fence_token"], row["session_id"]) == ("attached", 3, "s6")
    [audit] = await env.audits("o6")
    assert (audit["outcome"], audit["reason"]) == ("not_eligible", "writer_live")
    assert len(await env.events("integration.owner_recovery_refused")) == 1
    if case == "running":
        confirm.assert_not_awaited()


async def test_a_branch_checked_out_where_a_live_session_works_is_refused(env):
    env.branch("aq/t7")
    slot = await env.slot("slot", "aq/t7")
    await env.session("s7", work_dir=slot)
    await env.session("s7-live", work_dir=slot, state="running", desired_state="running")
    await env.owner("o7", "aq/t7", "t7", session_id="s7", workspace_id="ws-slot")

    result = await env.service().recover("o7", principal="sweep")

    assert (result.outcome, result.reason) == ("not_eligible", "checkout_in_use")
    assert result.evidence["worktree"] == str(slot)
    assert not detached(slot)
    assert (await env.row("o7"))["handoff_state"] == "attached"


async def test_stale_base_attachment_releases_without_touching_operator_checkout(env):
    env.branch("aq/base-owner")
    env.branch("aq/unrelated")
    await env.slot("unrelated-slot", "aq/unrelated")
    await env.session("old", work_dir=env.tmp / "gone")
    await env.session("supervisor", work_dir=env.base, state="running", desired_state="running")
    await env.owner("base-owner", "aq/base-owner", "old-task",
                    session_id="old", workspace_id="ws-base")
    (env.base / "operator.txt").write_text("uncommitted operator work\n")
    before = git(env.base, "status", "--porcelain")

    result = await env.service().recover("base-owner", principal="supervisor")

    assert result.outcome == "released", result
    assert result.evidence["ignored_base_workspace"] == "ws-base"
    assert result.evidence["workspace_unlocked"] is False
    assert git(env.base, "branch", "--show-current") == "main"
    assert git(env.base, "status", "--porcelain") == before
    assert not result.evidence.get("snapshots")
    row = await env.row("base-owner")
    assert row["workspace_id"] is None
    assert row["confirmed_workspace_id"] is None
    workspace = await env.db.get_workspace("ws-base")
    assert workspace.enabled


async def test_an_unreachable_origin_is_refused(env):
    env.branch("aq/t8")
    git(env.base, "remote", "set-url", "origin", str(env.tmp / "missing.git"))
    await env.session("s8", work_dir=env.tmp / "gone")
    await env.owner("o8", "aq/t8", "t8", session_id="s8")

    result = await env.service().recover("o8", principal="sweep")

    assert (result.outcome, result.reason) == ("not_eligible", "origin_unreachable")
    assert (await env.row("o8"))["handoff_state"] == "attached"


async def test_a_row_that_changes_before_the_release_is_refused_as_stale(env):
    tip = env.branch("aq/t9", extra=1)
    await env.session("s9", work_dir=env.tmp / "gone")
    await env.owner("o9", "aq/t9", "t9", session_id="s9")

    async def confirm_and_bump(session_row):
        async with env.db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.id == "o9")
                .values(fence_token=integration_branch_owners.c.fence_token + 1)
            )
        return True

    result = await env.service(confirm_stopped=confirm_and_bump).recover("o9", principal="sweep")

    assert (result.outcome, result.reason) == ("not_eligible", "stale_fence")
    row = await env.row("o9")
    assert (row["handoff_state"], row["fence_token"]) == ("attached", 4)
    # The preservation already pushed is kept, and the evidence says where.
    assert remote_sha(env.origin, "aq/t9") == tip
    assert result.evidence["preserved_sha"] == tip
    [audit] = await env.audits("o9")
    assert audit["reason"] == "stale_fence"


async def test_a_dry_run_plans_the_preservation_and_changes_nothing(env):
    env.branch("aq/t10")
    slot = await env.slot("slot", "aq/t10")
    (slot / "base.txt").write_text("edited\n")
    (slot / "untracked.txt").write_text("brand new\n")
    await env.session("s10", work_dir=slot)
    await env.owner("o10", "aq/t10", "t10", session_id="s10", workspace_id="ws-slot")

    result = await env.service().recover("o10", principal="sweep", dry_run=True)

    assert (result.outcome, result.dry_run) == ("preserved_and_released", True)
    planned = result.evidence["planned"]
    assert {"action": "push", "ref": "aq/t10", "source": f"snapshot of {slot}"} in [
        {k: step.get(k) for k in ("action", "ref", "source")} for step in planned
    ]
    assert {"action": "detach", "worktree": str(slot)} in planned
    assert remote_sha(env.origin, "aq/t10") == git(slot, "rev-parse", "HEAD")
    assert not detached(slot)
    assert await env.audits("o10") == []
    assert (await env.row("o10"))["handoff_state"] == "attached"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("matching_sentinel", [False, True])
@pytest.mark.parametrize("locked", [False, True])
async def test_missing_task_ref_never_preserves_a_predecessors_detached_head(
    env, dry_run, dirty, matching_sentinel, locked,
):
    """The failed preparation retained a slot still containing another task's work."""
    original = git(env.base, "rev-parse", "main")
    git(env.base, "branch", "aq/failed", original)
    git(env.base, "push", "origin", "aq/failed")
    predecessor = env.branch("aq/predecessor", extra=2)
    await env.task("failed")
    slot = await env.slot(
        "slot", "aq/predecessor", locked_by_task_id="failed" if locked else None,
    )
    if matching_sentinel:
        sentinel = WorktreeSlotManager.read_sentinel(slot)
        sentinel.task_id, sentinel.branch = "failed", "aq/failed"
        WorktreeSlotManager.write_sentinel(slot, sentinel)
    git(slot, "switch", "--detach")
    git(env.base, "branch", "-D", "aq/failed")
    if dirty:
        (slot / "private-progress.txt").write_text("predecessor's work\n")
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/failed", "failed", session_id="writer", workspace_id="ws-slot")
    before = git(slot, "status", "--porcelain")

    result = await env.service().recover("owner", principal="supervisor", dry_run=dry_run)

    assert result.outcome == "released"
    assert result.evidence["local_sha"] is None
    assert {"action": "skip_preserve", "reason": "missing_local_ref",
            "ref": "aq/failed"} in result.evidence["planned"]
    assert {"action": "retain_checkout", "worktree": str(slot),
            "reason": "missing_local_ref" if matching_sentinel else "sentinel_mismatch",
            "disable_workspace": True} in result.evidence["planned"]
    # Ancestry alone would have admitted this predecessor's unrelated work.
    git(env.base, "merge-base", "--is-ancestor", original, predecessor)
    assert remote_sha(env.origin, "aq/failed") == original
    assert git(slot, "rev-parse", "HEAD") == predecessor
    assert git(slot, "status", "--porcelain") == before
    assert git(env.origin, "for-each-ref", "refs/heads/aq/recovery/") == ""
    assert len(await env.audits("owner")) == (0 if dry_run else 1)
    async with env.db._engine.connect() as conn:
        workspace = (await conn.execute(
            select(workspaces).where(workspaces.c.id == "ws-slot")
        )).mappings().one()
    assert workspace["enabled"] is dry_run
    assert workspace["locked_by_task_id"] == ("failed" if dry_run and locked else None)
    if not dry_run:
        assert result.evidence["workspace_disabled"] is True


@pytest.mark.parametrize("dry_run", [False, True])
async def test_a_foreign_head_in_the_rows_slot_is_never_pushed_to_its_branch(env, dry_run):
    """sharp-nexus-43: its slot was reused and held nimble-rapids-30's unpublished commit."""
    published = env.branch("aq/owner")
    foreign = env.branch("aq/foreign", push=False)
    await env.task("owner")
    slot = await env.slot("slot", "aq/owner", locked_by_task_id="owner")
    sentinel = WorktreeSlotManager.read_sentinel(slot)
    sentinel.task_id, sentinel.branch = "foreign", "aq/foreign"
    WorktreeSlotManager.write_sentinel(slot, sentinel)
    git(slot, "switch", "--detach", foreign)
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/owner", "owner", session_id="writer", workspace_id="ws-slot")

    result = await env.service().recover("owner", principal="supervisor", dry_run=dry_run)

    assert result.outcome == "released", result
    planned = result.evidence["planned"]
    assert {"action": "skip_preserve", "reason": "sentinel_mismatch",
            "worktree": str(slot)} in planned
    assert {"action": "retain_checkout", "worktree": str(slot),
            "reason": "sentinel_mismatch", "disable_workspace": True} in planned
    assert not [step for step in planned if step["action"] == "push"]
    assert remote_sha(env.origin, "aq/owner") == published
    assert remote_sha(env.origin, "aq/foreign") is None
    assert git(env.origin, "for-each-ref", "refs/heads/aq/recovery/") == ""
    assert git(slot, "rev-parse", "HEAD") == foreign
    assert (await env.db.get_workspace("ws-slot")).enabled is dry_run


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("sentinel_owner", ["owner", "foreign"])
async def test_no_push_releases_and_leaves_unpublished_work_in_place(
    env, dry_run, sentinel_owner,
):
    tip = env.branch("aq/owner", extra=1)
    published = remote_sha(env.origin, "aq/owner")
    await env.task("owner")
    slot = await env.slot("slot", "aq/owner", locked_by_task_id="owner")
    if sentinel_owner != "owner":
        # Unprovable work refuses a publishing release; --no-push needs no proof.
        sentinel = WorktreeSlotManager.read_sentinel(slot)
        sentinel.task_id = sentinel_owner
        WorktreeSlotManager.write_sentinel(slot, sentinel)
    (slot / "dirty.txt").write_text("keep my work\n")
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/owner", "owner", session_id="writer", workspace_id="ws-slot")
    before = git(slot, "status", "--porcelain")

    result = await env.service().recover(
        "owner", principal="human:local-operator", dry_run=dry_run, no_push=True,
    )

    assert (result.outcome, result.reason) == ("released", None), result
    unpublished = {"source": f"snapshot of {slot}", "sha": tip}
    assert result.evidence["unpublished"] == [unpublished]
    planned = result.evidence["planned"]
    assert {"action": "skip_preserve", "reason": "no_push", **unpublished} in planned
    assert {"action": "retain_checkout", "worktree": str(slot), "reason": "no_push",
            "disable_workspace": True} in planned
    assert not [step for step in planned if step["action"] == "push"]
    assert remote_sha(env.origin, "aq/owner") == published
    assert git(env.origin, "for-each-ref", "refs/heads/aq/recovery/") == ""
    assert git(env.base, "rev-parse", "aq/owner") == tip
    assert git(slot, "rev-parse", "HEAD") == tip
    assert git(slot, "status", "--porcelain") == before
    assert (await env.db.get_workspace("ws-slot")).enabled is dry_run
    row = await env.row("owner")
    assert row["handoff_state"] == ("attached" if dry_run else "released")
    if not dry_run:
        assert detached(slot)
        assert result.evidence["workspace_disabled"] is True
        [comment] = [c for c in await env.comments("owner") if "without publishing" in c]
        assert f"snapshot of {slot} (`{tip}`)" in comment


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("broken", [
    "origin", "ancestor", "claim", "date", "sentinel", "assignment", "head",
])
async def test_unproved_task_work_refuses_before_any_git_mutation(env, broken, dry_run):
    tip = env.branch("aq/proof", extra=1)
    original = remote_sha(env.origin, "aq/proof")
    slot = await env.slot("slot", "aq/proof")
    if broken == "head":
        git(slot, "switch", "--detach", "main")
    (slot / "dirty.txt").write_text("keep my work\n")
    await env.session("writer", work_dir=slot, claim_phase_at=None if broken == "claim" else 1.0)
    other = env.branch("aq/unrelated", push=False) if broken == "ancestor" else None
    await env.owner("owner", "aq/proof", "proof", session_id="writer", workspace_id="ws-slot",
                    provenance=broken != "origin", origin_base=other)
    if broken == "date":
        # A future claim cannot own the commits or this slot assignment.
        future = time.time() + 60
        await env.db.update_session("writer", claim_phase_at=future)
        sentinel = WorktreeSlotManager.read_sentinel(slot)
        sentinel.assigned_at = future
        WorktreeSlotManager.write_sentinel(slot, sentinel)
    if broken in {"sentinel", "assignment"}:
        sentinel = WorktreeSlotManager.read_sentinel(slot)
        if broken == "sentinel":
            sentinel.task_id = "some-other-task"
        else:
            sentinel.assigned_at = 0
        WorktreeSlotManager.write_sentinel(slot, sentinel)
    before = git(slot, "status", "--porcelain")
    before_head = git(slot, "rev-parse", "HEAD")

    result = await env.service().recover("owner", principal="supervisor", dry_run=dry_run)

    assert (result.outcome, result.reason) == ("not_eligible", "unproven_task_work"), result
    assert remote_sha(env.origin, "aq/proof") == original
    assert git(slot, "rev-parse", "HEAD") == before_head
    assert git(env.base, "rev-parse", "aq/proof") == tip
    assert detached(slot) is (broken == "head")
    assert git(slot, "status", "--porcelain") == before
    assert (await env.row("owner"))["fence_token"] == 3
    assert git(env.origin, "for-each-ref", "refs/heads/aq/recovery/") == ""


@pytest.mark.parametrize("role", ["worker", "repair"])
async def test_integration_writer_rows_preserve_without_a_task_origin(env, role):
    """Repair writers lease a shared integration branch that has no task origin."""
    tip = env.branch("aq/integration/batch", extra=1)
    published = remote_sha(env.origin, "aq/integration/batch")
    slot = await env.slot("slot", "aq/integration/batch")
    (slot / "dirty.txt").write_text("unfinished repair\n")
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/integration/batch", "repair-task", role=role,
                    session_id="writer", workspace_id="ws-slot", provenance=False)

    result = await env.service().recover("owner", principal="supervisor")

    if role == "worker":
        assert (result.outcome, result.reason) == ("not_eligible", "unproven_task_work"), result
        assert remote_sha(env.origin, "aq/integration/batch") == published
        assert (await env.row("owner"))["fence_token"] == 3
        return
    assert result.outcome == "preserved_and_released", result
    assert "task_work_proof" not in result.evidence
    preserved = remote_sha(env.origin, "aq/integration/batch")
    git(env.base, "fetch", "-q", "origin")
    git(env.base, "merge-base", "--is-ancestor", tip, preserved)
    assert git(env.base, "show", f"{preserved}:dirty.txt") == "unfinished repair"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("qualified_ref", [False, True])
async def test_restore_task_origin_previews_and_applies_exact_recorded_base(
    env, managed, qualified_ref,
):
    from src.commands.contracts.integration import IntegrationRestoreTaskOriginArgs

    base = git(env.base, "rev-parse", "main")
    tip = env.branch("aq/contaminated")
    await env.task("contaminated")
    await env.db.update_task("contaminated", repo_id="r", branch_name="aq/contaminated")
    ref = "refs/heads/aq/contaminated" if qualified_ref else "aq/contaminated"
    await env.owner("owner", ref, "contaminated", state="released")
    if managed:
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(fence=3))
    service = env.service()
    preview = await service.restore_task_origin(
        IntegrationRestoreTaskOriginArgs(task_id="contaminated"), principal="supervisor",
    )
    assert preview["outcome"] == "preview"
    detail = preview["evidence"]
    assert (detail["before_sha"], detail["after_sha"]) == (tip, base)
    assert remote_sha(env.origin, detail["backup_ref"]) is None
    assert (await env.row("owner"))["fence_token"] == 3
    assert await env.events("integration.task_origin_restore_started") == []
    request = IntegrationRestoreTaskOriginArgs(
        task_id="contaminated", dry_run=False, origin_id=detail["origin_id"],
        expected_fence_token=3, expected_remote_oid=tip, reason="remove unrelated slot history",
    )
    restored = await service.restore_task_origin(request, principal="supervisor")
    assert restored["outcome"] == "restored", restored
    assert remote_sha(env.origin, "aq/contaminated") == base
    assert git(env.base, "rev-parse", "aq/contaminated") == base
    assert remote_sha(env.origin, detail["backup_ref"]) == tip
    assert (await env.row("owner"))["fence_token"] == 4
    assert (await env.row("owner"))["fence"] == (4 if managed else None)
    assert len(await env.events("integration.task_origin_restored")) == 1
    # A retry carrying the previous generation cannot overwrite a new owner.
    refused = await service.restore_task_origin(request, principal="supervisor")
    assert refused["outcome"] == "not_eligible"


@pytest.mark.parametrize("fault", ["backup", "lease"])
async def test_origin_restore_never_overwrites_after_backup_or_remote_race(env, monkeypatch, fault):
    from src.commands.contracts.integration import IntegrationRestoreTaskOriginArgs
    from src.git.manager import GitError

    tip = env.branch("aq/restore")
    competitor = env.branch("aq/competitor")
    await env.task("restore")
    await env.db.update_task("restore", repo_id="r", branch_name="aq/restore")
    await env.owner("owner", "aq/restore", "restore", state="released")
    service = env.service()
    push = service.git.apush_validated_ref

    async def interfere(checkout, sha, ref, **kwargs):
        if fault == "backup" and ref.startswith("aq/origin-restore/"):
            raise GitError("backup unavailable")
        if fault == "lease" and ref == "aq/restore":
            git(env.base, "push", "--force", "origin", f"{competitor}:refs/heads/{ref}")
        return await push(checkout, sha, ref, **kwargs)

    monkeypatch.setattr(service.git, "apush_validated_ref", interfere)
    result = await service.restore_task_origin(IntegrationRestoreTaskOriginArgs(
        task_id="restore", dry_run=False, origin_id="origin-owner",
        expected_fence_token=3, expected_remote_oid=tip, reason="repair",
    ), principal="supervisor")
    assert result["outcome"] == "not_eligible", result
    assert remote_sha(env.origin, "aq/restore") == (competitor if fault == "lease" else tip)
    assert git(env.base, "rev-parse", "aq/restore") == tip
    assert (await env.row("owner"))["fence_token"] == 3
    assert len(await env.events("integration.task_origin_restore_started")) == 1
    assert await env.events("integration.task_origin_restored") == []


@pytest.mark.parametrize("caller", ["supervisor", "worker", "stopped_supervisor"])
async def test_origin_restore_handler_requires_a_live_supervisor(env, tmp_path, caller):
    from dataclasses import asdict

    from src.api.auth import RequestScope
    from src.models import AgentProfile

    env.branch("aq/restore")
    await env.task("restore")
    await env.db.update_task("restore", repo_id="r", branch_name="aq/restore")
    await env.owner("owner", "aq/restore", "restore", state="released")
    profile = "worker" if caller == "worker" else "supervisor"
    await env.db.create_profile(AgentProfile(
        id=profile, name=profile, aq_commands=["integration_restore_task_origin"],
    ))
    await env.session("caller", work_dir=tmp_path, state="running", desired_state="running")
    await env.db.update_session(
        "caller", profile_id=profile, lifecycle="named",
        state="stopped" if caller == "stopped_supervisor" else "running",
    )
    _, _, _, handler, _ = await _pool_daemon(env, tmp_path, env.base)
    result = await handler.execute("integration_restore_task_origin", {
        "task_id": "restore", "_scope": asdict(RequestScope(
            kind="session", session_id="caller", project_id="p", elevated=True,
        )),
    })
    assert result["success"] is (caller == "supervisor"), result
    if caller == "supervisor":
        assert result["outcome"] == "preview", result
        assert result["evidence"]["principal"] == "supervisor session:caller"
    assert (await env.row("owner"))["fence_token"] == 3


@pytest.mark.parametrize("broken", [
    "origin", "fence", "remote", "attached", "writer", "workspace", "checked_out", "local",
])
async def test_restore_task_origin_refuses_changed_preview_or_writer(env, broken):
    from src.commands.contracts.integration import IntegrationRestoreTaskOriginArgs

    tip = env.branch("aq/restore")
    await env.task("restore")
    await env.db.update_task("restore", repo_id="r", branch_name="aq/restore")
    await env.owner("owner", "aq/restore", "restore", state="released")
    if broken == "attached":
        async with env.db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(handoff_state="attached"))
    elif broken == "writer":
        await env.session("live", task_id="restore", work_dir=env.tmp / "elsewhere",
                          state="running", desired_state="running")
    elif broken == "workspace":
        await env.slot("slot", "aq/restore", locked_by_task_id="restore")
    elif broken == "checked_out":
        await env.slot("slot", "aq/restore")
    elif broken == "local":
        git(env.base, "update-ref", "refs/heads/aq/restore", "main")
        # Both the base and the observed remote are allowed; a third tip is not.
        git(env.base, "commit", "--allow-empty", "-m", "unrelated local work")
        git(env.base, "update-ref", "refs/heads/aq/restore", "HEAD")
    request = IntegrationRestoreTaskOriginArgs(
        task_id="restore", dry_run=False, origin_id="wrong" if broken == "origin" else "origin-owner",
        expected_fence_token=2 if broken == "fence" else 3,
        expected_remote_oid="a" * 40 if broken == "remote" else tip, reason="repair",
    )
    result = await env.service().restore_task_origin(request, principal="supervisor")
    assert result["outcome"] == "not_eligible", result
    assert remote_sha(env.origin, "aq/restore") == tip
    assert (await env.row("owner"))["fence_token"] == 3


async def test_a_stopped_writers_claim_and_workspace_lock_are_released_with_the_owner(env):
    env.branch("aq/t11")
    await env.task("t11", TaskStatus.BLOCKED)
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(agents).values(
                id="a11",
                name="worker",
                profile_id="worker",
                state="BUSY",
                current_task_id="t11",
                created_at=1.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "t11").values(claim_epoch=7))
    slot = await env.slot("slot", "aq/t11", locked_by_agent_id="a11", locked_by_task_id="t11")
    await env.session(
        "s11",
        work_dir=slot,
        task_id="t11",
        agent_id="a11",
        claim_phase="active",
        last_claim_epoch=7,
    )
    await env.owner("o11", "aq/t11", "t11", session_id="s11", workspace_id="ws-slot")

    result = await env.service().recover("o11", principal="delegate_retirement")

    assert result.outcome == "released"
    assert result.evidence["claim_released"] is True
    session = await env.db.get_session("s11")
    assert session.claim_phase is None and session.task_id is None
    workspace = await env.db.get_workspace("ws-slot")
    assert workspace.locked_by_agent_id is None and workspace.locked_by_task_id is None
    assert (await env.db.get_task("t11")).status == TaskStatus.BLOCKED
    async with env.db._engine.connect() as conn:
        enabled = (
            await conn.execute(select(workspaces.c.enabled).where(workspaces.c.id == "ws-slot"))
        ).scalar_one()
    assert enabled is True


async def test_an_in_flight_task_whose_writer_stopped_goes_back_to_ready(env):
    env.branch("aq/t11")
    await env.task("t11", TaskStatus.IN_PROGRESS)
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(agents).values(
                id="a11",
                name="worker",
                profile_id="worker",
                state="BUSY",
                current_task_id="t11",
                created_at=1.0,
            )
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "t11").values(claim_epoch=7, assigned_agent_id="a11")
        )
    slot = await env.slot("slot", "aq/t11", locked_by_agent_id="a11", locked_by_task_id="t11")
    await env.session(
        "s11",
        work_dir=slot,
        task_id="t11",
        agent_id="a11",
        claim_phase="active",
        last_claim_epoch=7,
    )
    await env.owner("o11", "aq/t11", "t11", session_id="s11", workspace_id="ws-slot")

    result = await env.service().recover("o11", principal="sweep")

    assert result.outcome == "released"
    assert result.evidence["claim_released"] is True
    assert result.evidence["claim_task_status"] == "IN_PROGRESS"
    task = await env.db.get_task("t11")
    assert task.status == TaskStatus.READY and task.assigned_agent_id is None
    assert (await env.db.get_session("s11")).claim_phase is None


@pytest.mark.parametrize(
    "scenario",
    [
        "continue-preserved-tip",
        "operator-drained",
        "held-incident",
        "ref-moved",
        "non-slot",
        "published-descendant",
        "canonical-diverged",
    ],
)
async def test_failed_operator_stop_release_reroute_resume_preserves_successor_git(
    env, tmp_path, scenario
):
    """K07: failed stop, productive death, release-owner, override, then guarded Resume."""
    from types import SimpleNamespace

    from src.database.tables import (
        task_branch_origins,
        task_integration_checkpoints,
        task_session_attempts,
    )
    from src.git.manager import GitError
    from src.models import AgentProfile
    from src.sessions.provider import SessionHandle

    base_sha = git(env.base, "rev-parse", "main")
    tip = env.branch("aq/producer", extra=1)
    await env.task("producer", TaskStatus.IN_PROGRESS)
    await env.db.create_profile(AgentProfile(id="worker", name="Worker"))
    await env.db.create_profile(AgentProfile(id="codex-worker", name="Codex"))
    now = time.time()
    await env.db.update_task(
        "producer",
        repo_id="r",
        branch_name="aq/producer",
        profile_id="worker",
        route_source="legacy",
        created_at=now - 600,
    )
    slot = await env.slot("slot", "aq/producer", locked_by_task_id="producer")
    sentinel = WorktreeSlotManager.read_sentinel(slot)
    sentinel.assigned_at = now - 400
    WorktreeSlotManager.write_sentinel(slot, sentinel)
    await env.session(
        "writer",
        work_dir=slot,
        task_id="producer",
        state="running",
        desired_state="running",
        claim_phase="active",
        last_claim_epoch=0,
        claim_phase_at=now - 100,
    )
    await env.owner("owner", "aq/producer", "producer", session_id="writer", workspace_id="ws-slot",
                    provenance=False)
    async with env.db.immediate() as conn:
        await conn.execute(update(projects).values(hierarchical_integration_mode="train"))
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="producer",
                repository_id="r",
                branch_name="aq/producer",
                base_sha=base_sha,
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=now - 600,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="producer",
                repository_id="r",
                branch="aq/producer",
                checkpoint_sha=base_sha,
                updated_at=now,
            )
        )
        await conn.execute(
            insert(task_session_attempts).values(
                id="attempt",
                task_id="producer",
                session_id="writer",
                project_id="p",
                profile_id="worker",
                name="n-writer",
                lifecycle="pool",
                harness="claude",
                provider="fake",
                state="running",
                work_dir=str(slot),
                started_at=now - 500,
                session_started_at=1,
            )
        )
    provider, _, orch, handler, _ = await _pool_daemon(env, tmp_path, slot)

    async def confirm_stopped(row):
        return await provider.confirm_stopped(
            SessionHandle(row["name"], row["provider"], row["instance_token"])
        )

    orch.development_integration = SimpleNamespace(confirm_stopped=confirm_stopped)
    orch.arelease_integration_writer_for_retry = AsyncMock(return_value=False)
    refused = await handler.execute("stop_task", {"task_id": "producer"})
    assert "handoff is unproven" in refused["error"]
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"
    await provider.stop(SessionHandle("n-writer", "fake", "tok-writer"), grace=0)
    await env.db.update_session(
        "writer",
        state="stopped",
        desired_state="stopped",
        ended_at=time.time(),
        end_reason="drained" if scenario == "operator-drained" else "productive_death",
    )
    await env.db.transition_task(
        "producer", TaskStatus.BLOCKED, force=True, context="session_not_live"
    )
    await env.db.set_task_meta("producer", "needs_attention", "session_not_live")
    await env.db.queue_task_recovery_notifications()
    released = await handler.execute(
        "integration_release_owner", {"task_id": "producer", "dry_run": False},
    )
    assert released["outcome"] == "preserved_and_released", released
    assert remote_sha(env.origin, "aq/producer") == tip
    await env.db.update_task(
        "producer",
        profile_id="codex-worker",
        intelligence_class="standard-high",
        route_source="override",
        provider_intent="pinned",
        route={
            "profile_id": "codex-worker",
            "intelligence_class": "standard-high",
            "override": {
                "by": "local operator",
                "at": time.time(),
                "reason": "Continue the preserved producer on Codex",
            },
        },
    )
    if scenario == "held-incident":
        from dataclasses import asdict
        from src.api.auth import RequestScope

        await env.db.create_profile(AgentProfile(id="supervisor", name="Supervisor"))
        await env.session("supervisor", work_dir=tmp_path, state="running", desired_state="running")
        await env.db.update_session("supervisor", profile_id="supervisor", lifecycle="named")
        scope = asdict(
            RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True)
        )
        current = await env.db.get_task_meta("producer", "supervisor_recovery_incident")
        args = {
            "task_id": "producer",
            "incident_id": current["id"],
            "decision": "hold",
            "reason": "Hold the failed recovery pending an audited handoff",
            "_scope": scope,
        }
        assert "error" not in await handler.execute("task_recover", args)
        held = await env.db.get_task_meta("producer", "supervisor_recovery_incident")
        assert "error" in await handler.execute("resume_task", {"task_id": "producer"})
        resumed = await handler.execute(
            "task_recover",
            {
                **args,
                "decision": "retry",
                "expected_hold_at": held["decided_at"],
                "reason": "Explicitly release the supervisor hold and continue preserved work",
            },
        )
    else:
        resumed = await handler.execute("resume_task", {"task_id": "producer"})
    assert resumed.get("status") == "READY", resumed
    task = await env.db.get_task("producer")
    project = await env.db.get_project("p")
    # A delivered prerequisite advances preparation, not the immutable origin.
    # The preserved producer already contains that prerequisite's head.
    orch.db.hierarchy_prerequisite_delivery_head = AsyncMock(return_value=tip)
    origin, fence, _ = await orch._hierarchy_origin_and_fence(task, project)
    expected_tip = tip
    if scenario == "non-slot":
        async with env.db.immediate() as conn:
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.id == "ws-slot")
                .values(
                    slot_index=None,
                    base_workspace_id=None,
                )
            )
    elif scenario in {"published-descendant", "canonical-diverged"}:
        start = tip if scenario == "published-descendant" else base_sha
        git(env.base, "checkout", "-b", "aq/updated", start)
        (env.base / "new-publication.txt").write_text("published update\n")
        git(env.base, "add", "new-publication.txt")
        git(env.base, "commit", "-m", "published update")
        expected_tip = git(env.base, "rev-parse", "HEAD")
        git(env.base, "push", "--force", "origin", "HEAD:refs/heads/aq/producer")
        git(env.base, "checkout", "main")
    if scenario == "ref-moved":
        git(env.base, "push", "--force", "origin", f"{base_sha}:refs/heads/aq/producer")
        with pytest.raises(GitError, match="preserved ref changed"):
            await orch._prepare_exact_origin_workspace(
                task,
                project,
                SimpleNamespace(workspace=await env.db.get_workspace("ws-slot"), kind=None),
                origin,
                fence,
            )
        assert git(slot, "rev-parse", "HEAD") == tip
    elif scenario == "canonical-diverged":
        with pytest.raises(GitError, match="task branch lost audited progress"):
            await orch._prepare_exact_origin_workspace(
                task,
                project,
                SimpleNamespace(workspace=await env.db.get_workspace("ws-slot"), kind=None),
                origin,
                fence,
            )
        assert git(slot, "rev-parse", "HEAD") == tip
    else:
        await orch._prepare_exact_origin_workspace(
            task,
            project,
            SimpleNamespace(workspace=await env.db.get_workspace("ws-slot"), kind=None),
            origin,
            fence,
        )
        assert git(slot, "rev-parse", "HEAD") == expected_tip
        assert git(slot, "rev-parse", "--abbrev-ref", "HEAD") == "aq/producer"
        if scenario != "published-descendant":
            assert remote_sha(env.origin, "aq/producer") == tip


@pytest.mark.parametrize(("state", "role"), [("attached", "collector"), ("reserved", "worker")])
async def test_rows_outside_the_recoverable_states_are_refused(env, state, role):
    await env.owner("o12", "aq/t12", "t12", state=state, role=role)

    result = await env.service().recover("o12", principal="sweep")

    assert (result.outcome, result.reason) == ("not_eligible", "not_recoverable_state")
    assert (await env.row("o12"))["handoff_state"] == state


async def test_a_missing_row_is_not_found(env):
    result = await env.service().recover("nope", principal="sweep")

    assert (result.outcome, result.reason) == ("not_eligible", "not_found")


async def test_a_repeated_refusal_is_recorded_once_per_throttle_window(env):
    await env.task("t13")
    await env.session("s13", work_dir=env.tmp / "gone", state="running", desired_state="running")
    await env.owner("o13", "aq/t13", "t13", session_id="s13")
    now = [1_000_000.0]
    service = env.service(clock=lambda: now[0])

    for _ in range(3):
        assert (await service.recover("o13", principal="sweep")).reason == "writer_live"
        now[0] += 60
    assert len(await env.audits("o13")) == 1
    assert len(await env.comments("t13")) == 1

    now[0] += 300
    assert (await service.recover("o13", principal="sweep")).reason == "writer_live"
    assert len(await env.audits("o13")) == 2
    # The task hears about a refusal reason once, not once per sweep.
    assert len(await env.comments("t13")) == 1


async def test_recover_many_runs_each_row(env):
    env.branch("aq/ta")
    await env.session("sa", work_dir=env.tmp / "gone")
    await env.owner("oa", "aq/ta", "ta", session_id="sa")

    results = await env.service().recover_many(["oa", "nope"], principal="sweep")

    assert [(r.owner_row_id, r.outcome, r.reason) for r in results] == [
        ("oa", "released", None),
        ("nope", "not_eligible", "not_found"),
    ]


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


async def test_candidates_are_quiet_rows_whose_writer_is_not_live(env):
    now = time.time()
    await env.session("stopped", work_dir=env.tmp / "a")
    await env.session("live", work_dir=env.tmp / "b", state="running", desired_state="running")
    await env.session("draining", work_dir=env.tmp / "c", desired_state="running")
    await env.owner("quiet-stopped", "aq/a", "a", session_id="stopped", updated_at=now - 900)
    await env.owner("quiet-pending", "aq/b", "b", state="handoff_pending", updated_at=now - 1200)
    await env.owner("live-writer", "aq/c", "c", session_id="live", updated_at=now - 900)
    await env.owner("half-stopped", "aq/d", "d", session_id="draining", updated_at=now - 900)
    await env.owner("recent", "aq/e", "e", session_id="stopped", updated_at=now - 60)
    await env.owner("collector", "aq/f", "f", role="collector", updated_at=now - 900)
    await env.owner("reserved", "aq/g", "g", state="reserved", updated_at=now - 900)
    await env.owner("released", "aq/h", "h", state="released", updated_at=now - 900)

    found = await env.service(clock=lambda: now).candidates(quiet_seconds=600)

    assert [row["id"] for row in found] == ["quiet-pending", "quiet-stopped"]
    assert set(RECOVERABLE_STATES) == {"attached", "handoff_pending"}
    assert [row["id"] for row in await env.service(clock=lambda: now).candidates(limit=1)] == [
        "quiet-pending"
    ]


async def test_the_default_sweep_releases_a_stopped_writer_and_never_a_live_one(
    env, monkeypatch
):
    """The sweep ships on; the recovery's own proof still guards every live writer."""
    from types import SimpleNamespace

    from src.config import IntegrationConfig
    from src.orchestrator.core import Orchestrator

    now = time.time()
    env.branch("aq/gone")
    env.branch("aq/live")
    env.branch("aq/relaunched")
    await env.task("relaunched")
    await env.session("s-gone", work_dir=env.tmp / "gone")
    await env.session("s-live", work_dir=env.tmp / "live", state="running", desired_state="running")
    await env.session("s-old", work_dir=env.tmp / "old")
    await env.session(
        "s-new",
        work_dir=env.tmp / "new",
        state="running",
        desired_state="running",
        task_id="relaunched",
    )
    await env.owner("o-gone", "aq/gone", "gone", session_id="s-gone", updated_at=now - 900)
    await env.owner("o-live", "aq/live", "live", session_id="s-live", updated_at=now - 900)
    await env.owner(
        "o-relaunched", "aq/relaunched", "relaunched", session_id="s-old", updated_at=now - 900
    )
    service = env.service(clock=lambda: now)
    monkeypatch.setattr(
        "src.integration.owner_recovery.owner_recovery_for", lambda _orchestrator: service
    )
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(integration=IntegrationConfig()),
        _owner_recovery_next_due=0.0,
    )

    await Orchestrator._sweep_stranded_owners(orchestrator, now)

    assert (await env.row("o-gone"))["handoff_state"] == "released"
    [released] = await env.audits("o-gone")
    assert (released["outcome"], released["principal"]) == ("released", "sweep")
    live = await env.row("o-live")
    assert (live["handoff_state"], live["fence_token"], live["session_id"]) == (
        "attached",
        3,
        "s-live",
    )
    assert await env.audits("o-live") == []
    relaunched = await env.row("o-relaunched")
    assert (relaunched["handoff_state"], relaunched["fence_token"]) == ("attached", 3)
    [refused] = await env.audits("o-relaunched")
    assert (refused["outcome"], refused["reason"]) == ("not_eligible", "writer_live")


# ---------------------------------------------------------------------------
# Construction from the daemon
# ---------------------------------------------------------------------------


async def test_owner_recovery_for_builds_from_the_orchestrator(env):
    class Development:
        confirm_stopped = AsyncMock(return_value=True)

    class Orchestrator:
        db = env.db
        git = GitManager()
        development_integration = Development()

        def _git_mutex(self, path):
            raise AssertionError("not called at construction")

    assert isinstance(owner_recovery_for(Orchestrator()), OwnerRecovery)

    class NoDaemon:
        db = env.db
        git = GitManager()

    assert owner_recovery_for(NoDaemon()) is None


# ---------------------------------------------------------------------------
# Migration a00000000015
# ---------------------------------------------------------------------------

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "a00000000015_owner_recoveries.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("a00000000015", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(migration, *steps: str):
    def run(sync_conn) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        with Operations.context(MigrationContext.configure(sync_conn)):
            for step in steps:
                getattr(migration, step)()

    return run


async def _audit_shape(conn) -> tuple[bool, bool, bool]:
    table = (
        await conn.execute(text("SELECT to_regclass('integration_owner_recoveries') IS NOT NULL"))
    ).scalar_one()
    check = (
        await conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'ck_integration_owner_recoveries_outcome'"
            )
        )
    ).scalar_one()
    index = (
        await conn.execute(
            text("SELECT to_regclass('idx_integration_owner_recoveries_owner') IS NOT NULL")
        )
    ).scalar_one()
    return table, bool(check), index


async def test_the_audit_table_migration_downgrades_and_upgrades_idempotently(env):
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ("a00000000015", "a00000000014")
    async with env.db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            assert await _audit_shape(conn) == (True, True, True)
            await conn.run_sync(_apply(migration, "downgrade", "downgrade"))
            assert await _audit_shape(conn) == (False, False, False)
            await conn.run_sync(_apply(migration, "upgrade", "upgrade"))
            assert await _audit_shape(conn) == (True, True, True)
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# A retired repair delegate: close refusal -> drain-ack -> stop -> preserve
# (bold-impact-53)
# ---------------------------------------------------------------------------


async def _retired_delegate_worker(env, tmp_path, *, operation_state: str, active_stage: int):
    """A pool worker holding a delegate whose operation no longer needs it.

    Its checkout carries the pushed repair plus progress it never committed --
    the shape the supervisor had to kill by hand twice on 2026-09-30.
    """
    from src.database.tables import (
        integration_parent_episodes,
        integration_repair_operations,
        integration_repair_stages,
    )

    head = env.branch("aq/parent")
    for task_id in ("parent", "delegate"):
        await env.db.create_task(Task(
            id=task_id, project_id="p", title=task_id, description="",
            status=TaskStatus.IN_PROGRESS, repo_id="r", branch_name="aq/parent",
        ))
    async with env.db.immediate() as conn:
        await conn.execute(insert(agents).values(
            id="a1", name="worker", profile_id="worker", state="BUSY",
            current_task_id="delegate", created_at=1.0,
        ))
        await conn.execute(
            update(tasks).where(tasks.c.id == "delegate").values(assigned_agent_id="a1",
                                                                  claim_epoch=1)
        )
        await conn.execute(insert(integration_parent_episodes).values(
            id="episode", parent_task_id="parent", repository_id="r", generation=1,
            pre_collection_checkpoint_sha=head, created_at=1.0,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id="operation", target_kind="parent", parent_task_id="parent",
            episode_id="episode", active_stage=active_stage, state=operation_state,
            policy_snapshot={}, artifact_snapshot={}, required_check_version="checks-v1",
            created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(integration_repair_stages).values(
            operation_id="operation", ordinal=0, policy={}, starting_sha=head,
            repair_task_id="delegate", writer_kind="repair_delegate", attempts=1,
            state="passed" if operation_state == "completed" else "expired",
        ))
        if active_stage:
            await conn.execute(insert(integration_repair_stages).values(
                operation_id="operation", ordinal=1, policy={}, starting_sha=head,
                attempts=0, state="active",
            ))
    slot = await env.slot(
        "slot", "aq/parent", locked_by_agent_id="a1", locked_by_task_id="delegate"
    )
    sentinel = WorktreeSlotManager.read_sentinel(slot)
    sentinel.task_id = "delegate"
    WorktreeSlotManager.write_sentinel(slot, sentinel)
    (slot / "progress.txt").write_text("unsaved repair progress\n")
    await env.session(
        "writer", work_dir=slot, state="running", desired_state="running",
        task_id="delegate", agent_id="a1", claim_phase="active", last_claim_epoch=1,
    )
    await env.owner(
        "owner", "aq/parent", "delegate", role="repair", session_id="writer",
        workspace_id="ws-slot",
    )

    return (slot, *await _pool_daemon(env, tmp_path, slot))


async def _pool_daemon(env, tmp_path, slot):
    """A daemon over *env*'s database whose fake provider runs the ``writer`` session."""
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.orchestrator import Orchestrator
    from src.sessions import SessionProviderRegistry
    from src.sessions.fake import FakeProvider
    from src.sessions.provider import SessionSpec
    from tests.db_fixtures import lease_dsn

    provider = FakeProvider()
    await provider.start(SessionSpec(
        session_name="n-writer", work_dir=str(slot), command=("claude",),
        instance_token="tok-writer",
    ))

    class Registry(SessionProviderRegistry):
        def create(self, name, config=None):
            return provider

    registry = Registry({"fake": FakeProvider})
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("owner-recovery.db")),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orchestrator = Orchestrator(cfg)
    orchestrator.db = env.db
    orchestrator._agent_reconciler._db = env.db
    orchestrator.agent_questions.db = env.db
    orchestrator.bus.emit = AsyncMock()
    orchestrator.session_providers = registry
    orchestrator._get_default_branch = AsyncMock(return_value="main")
    handler = CommandHandler(orchestrator, cfg)
    orchestrator.set_command_handler(handler)
    return provider, registry, orchestrator, handler, cfg


@pytest.mark.parametrize("legacy_intent", [False, True], ids=["canonical", "legacy-sleeping"])
async def test_sleep_of_stopped_idle_pool_does_not_fence_owner_recovery(
    env, tmp_path, monkeypatch, legacy_intent,
):
    """An unrelated stopped pool row used to fence this idle checkout forever."""
    from src.sessions.provider import SessionHandle

    tip = env.branch("aq/producer")
    slot = await env.slot("slot", "aq/producer")
    await env.task("producer", TaskStatus.READY)
    await env.session("original", work_dir=slot)
    await env.owner(
        "owner", "aq/producer", "producer", session_id="original", workspace_id="ws-slot",
    )
    await env.session("writer", work_dir=slot, state="running", desired_state="running")
    provider, _, orch, handler, _ = await _pool_daemon(env, tmp_path, slot)
    await orch._terminate_pool_session(await env.db.get_session("writer"), reason="claim_loop_stalled")
    stopped = await env.db.get_session("writer")
    assert (stopped.state, stopped.desired_state) == ("stopped", "stopped")
    if legacy_intent:
        # The old public sleep control produced exactly this invalid pair.
        await env.db.update_session("writer", desired_state="sleeping")
        blocked = await handler.execute("integration_release_owner", {"task_id": "producer"})
        assert blocked["outcomes"][0]["reason"] == "checkout_in_use", blocked
        assert (await env.row("owner"))["fence_token"] == 3

    task_before = await env.db.get_task("producer")
    owner_before = await env.row("owner")
    workspace_before = await env.db.get_workspace("ws-slot")
    stop = AsyncMock(side_effect=AssertionError("sleep must never signal an ended instance"))
    monkeypatch.setattr(provider, "stop", stop)
    slept = await handler.execute("session_sleep", {"session_id": "writer"})
    assert slept["success"] and slept["desired_state"] == "stopped", slept
    current = await env.db.get_session("writer")
    assert current == stopped
    assert await env.db.get_task("producer") == task_before
    assert await env.row("owner") == owner_before
    assert await env.db.get_workspace("ws-slot") == workspace_before
    stop.assert_not_awaited()
    assert await provider.confirm_stopped(SessionHandle("n-writer", "fake", "tok-writer"))

    # Normalizing intent grants no owner-release authority of its own.
    assert (await env.row("owner"))["handoff_state"] == "attached"
    preview = await handler.execute("integration_release_owner", {"task_id": "producer"})
    assert preview["outcome"] == "released", preview
    assert (await env.row("owner"))["fence_token"] == 3
    released = await handler.execute(
        "integration_release_owner", {"task_id": "producer", "dry_run": False},
    )
    assert released["outcome"] == "released", released
    owner = await env.row("owner")
    assert (owner["handoff_state"], owner["fence_token"]) == ("released", 4)
    assert (await env.db.get_task("producer")).status is TaskStatus.READY
    assert remote_sha(env.origin, "aq/producer") == tip


async def _unprepared_writer(env, tmp_path, *, mode="train", phase="preparing"):
    """A retained failed preparation, still detached at the published default tip."""
    from src.database.tables import task_branch_origins, task_integration_checkpoints
    from src.models import Agent, AgentProfile, AgentState

    base_sha = git(env.base, "rev-parse", "HEAD")
    git(env.base, "branch", "aq/producer")
    git(env.base, "push", "origin", "aq/producer")
    git(env.base, "commit", "--allow-empty", "-m", "advance default")
    git(env.base, "push", "origin", "main")
    await env.task("producer", TaskStatus.IN_PROGRESS)
    await env.db.create_profile(AgentProfile(id="worker", name="Worker"))
    await env.db.create_agent(Agent(
        id="worker", name="Worker", profile_id="worker", state=AgentState.BUSY,
        current_task_id="producer",
    ))
    await env.db.update_project("p", hierarchical_integration_mode=mode)
    await env.db.update_task(
        "producer", repo_id="r", branch_name="aq/producer", claim_epoch=7,
        assigned_agent_id="worker", profile_id="worker", route_source="legacy",
    )
    slot = await env.slot(
        "slot", "aq/producer", locked_by_task_id="producer", locked_by_agent_id="worker",
    )
    git(slot, "switch", "--detach", "origin/main")
    await env.session(
        "writer", work_dir=slot, task_id="producer", agent_id="worker",
        state="running", desired_state="running", claim_phase=phase, last_claim_epoch=7,
    )
    # A retained preparation carries the transaction's acquisition receipt.
    await env.db.set_task_meta("producer", "claim_workspace", {
        "workspace_id": "ws-slot", "generation": 0, "session_id": "writer",
        "instance_token": "tok-writer", "claim_epoch": 7,
    })
    await env.owner(
        "owner", "aq/producer", "producer", session_id="writer", workspace_id="ws-slot",
        provenance=False,
    )
    async with env.db.immediate() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="producer", repository_id="r", branch_name="aq/producer",
            base_sha=base_sha, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time(),
        ))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="producer", repository_id="r", branch="aq/producer",
            checkpoint_sha=base_sha, updated_at=time.time(),
        ))
    await env.db.set_task_meta("producer", "needs_attention", "integration_prepare_failed_attached")
    return slot, base_sha, await _pool_daemon(env, tmp_path, slot)


@pytest.mark.parametrize("mode", ["train", "hierarchy"])
@pytest.mark.parametrize("initial_state", ["running", "stopped"])
async def test_orphan_sweep_preserves_and_requeues_dead_train_writer(
    env, tmp_path, mode, initial_state,
):
    from src.claim_file import write_claim_file
    from src.models import AgentState
    from src.sessions.provider import SessionHandle
    from src.sessions.reconciler import SessionReconciler

    slot, _, (provider, registry, orch, _, cfg) = await _unprepared_writer(
        env, tmp_path, mode=mode, phase="active",
    )
    git(slot, "switch", "aq/producer")
    (slot / "progress.txt").write_text("unsaved orphan progress\n")
    write_claim_file(str(slot), {"task_id": "producer", "claim_epoch": 7, "session_id": "writer"})
    provider.script_death("n-writer")
    if initial_state == "stopped":
        await provider.stop(SessionHandle("n-writer", "fake", "tok-writer"), grace=0)
        await env.db.update_session("writer", state="stopped", desired_state="stopped")
    reconciler = SessionReconciler(
        env.db, cfg, registry, bus=orch.bus, orchestrator=orch, epoch="e",
    )
    await reconciler.tick(now=time.time())

    task = await env.db.get_task("producer")
    assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
    assert task.branch_name == "aq/producer" and task.claim_epoch == 7
    writer = await env.db.get_session("writer")
    assert (writer.state, writer.task_id, writer.claim_phase) == ("stopped", None, None)
    assert (await env.db.get_agent("worker")).state is AgentState.IDLE
    assert (await env.row("owner"))["handoff_state"] == "released"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id is None
    assert not (slot / ".aq" / "claim.json").exists()
    assert (slot / "progress.txt").read_text() == "unsaved orphan progress\n"
    assert git(env.origin, "show", "aq/producer:progress.txt") == "unsaved orphan progress"
    assert len(await env.audits("owner")) == 1
    assert any("Orphan sweep recovered" in body for body in await env.comments("producer"))
    await reconciler.tick(now=time.time())
    assert len(await env.audits("owner")) == 1


@pytest.mark.parametrize("blocker", [
    "alive", "unknown", "disabled", "epoch", "workspace", "agent", "manual_pause", "origin",
])
async def test_orphan_sweep_retains_unproven_train_attachment(env, tmp_path, monkeypatch, blocker):
    from src.git.manager import GitError
    from src.sessions.provider import SessionHandle
    from src.sessions.reconciler import SessionReconciler

    slot, _, (provider, registry, orch, _, cfg) = await _unprepared_writer(
        env, tmp_path, phase="active",
    )
    git(slot, "switch", "aq/producer")
    (slot / "progress.txt").write_text("retained progress\n")
    await env.db.update_session("writer", state="stopped", desired_state="stopped")
    if blocker != "alive":
        await provider.stop(SessionHandle("n-writer", "fake", "tok-writer"), grace=0)
    if blocker == "unknown":
        monkeypatch.setattr(provider, "confirm_stopped", AsyncMock(side_effect=RuntimeError("probe")))
    elif blocker == "disabled":
        cfg.integration.owner_recovery_sweep = False
    elif blocker == "epoch":
        await env.db.update_task("producer", claim_epoch=8)
    elif blocker == "workspace":
        await env.db.update_workspace("ws-slot", locked_by_agent_id=None)
    elif blocker == "agent":
        await env.db.update_agent("worker", current_task_id=None)
    elif blocker == "manual_pause":
        await env.db.set_task_meta("producer", "manual_pause", True)
    elif blocker == "origin":
        monkeypatch.setattr(orch.git, "afetch_origin", AsyncMock(side_effect=GitError("offline")))
    reconciler = SessionReconciler(
        env.db, cfg, registry, bus=orch.bus, orchestrator=orch, epoch="e",
    )
    await reconciler.tick(now=time.time())

    task = await env.db.get_task("producer")
    assert task.status is TaskStatus.IN_PROGRESS and task.assigned_agent_id == "worker"
    assert (await env.row("owner"))["handoff_state"] == "attached"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"
    assert (await env.db.get_session("writer")).task_id == "producer"
    assert (slot / "progress.txt").read_text() == "retained progress\n"
    assert not any("Orphan sweep recovered" in body for body in await env.comments("producer"))


async def test_orphan_recovery_retains_agent_with_another_live_instance(env, tmp_path):
    from src.sessions.provider import SessionHandle

    slot, _, (provider, _, _, handler, _) = await _unprepared_writer(
        env, tmp_path, phase="active",
    )
    await provider.stop(SessionHandle("n-writer", "fake", "tok-writer"), grace=0)
    await env.db.update_session("writer", state="stopped", desired_state="stopped")
    await env.session(
        "replacement", work_dir=tmp_path / "other", agent_id="worker",
        state="running", desired_state="running",
    )
    result = await handler.recover_orphaned_integration_task(
        "producer", await env.db.get_session("writer"),
    )
    assert result["outcome"] == "retained"
    assert (await env.row("owner"))["handoff_state"] == "attached"
    assert (await env.db.get_task("producer")).status is TaskStatus.IN_PROGRESS
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"
    assert await env.audits("owner") == []


async def test_release_owner_command_previews_by_default_without_runtime_coupling(env, tmp_path):
    from src.sessions.provider import SessionHandle

    slot, _, (provider, _, orch, handler, _) = await _unprepared_writer(
        env, tmp_path, phase="active",
    )
    await provider.stop(SessionHandle("n-writer", "fake", "tok-writer"), grace=0)
    await env.db.update_session("writer", state="stopped", desired_state="stopped")
    assert getattr(orch, "development_integration", None) is None
    preview = await handler.execute("integration_release_owner", {"task_id": "producer"})
    assert preview["outcome"] == "released", preview
    assert preview["outcomes"][0]["dry_run"] is True
    assert (await env.row("owner"))["handoff_state"] == "attached"
    assert (await env.db.get_task("producer")).status is TaskStatus.IN_PROGRESS
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"
    assert await env.audits("owner") == []
    unpushed = await handler.execute(
        "integration_release_owner", {"task_id": "producer", "no_push": True},
    )
    assert unpushed["outcome"] == "released", unpushed
    assert unpushed["outcomes"][0]["dry_run"] is True
    applied = await handler.execute(
        "integration_release_owner", {"task_id": "producer", "dry_run": False},
    )
    assert applied["outcome"] == "released", applied
    assert (await env.db.get_task("producer")).status is TaskStatus.READY
    assert (await env.row("owner"))["handoff_state"] == "released"


@pytest.mark.parametrize("mode", ["hierarchy", "train"])
@pytest.mark.parametrize("handoff_state", ["attached", "handoff_pending"])
async def test_prepare_failed_stop_restart_releases_exact_claim(
    env, tmp_path, monkeypatch, mode, handoff_state,
):
    from types import SimpleNamespace

    slot, base_sha, (provider, _, orch, handler, _) = await _unprepared_writer(
        env, tmp_path, mode=mode,
    )
    reset = AsyncMock(side_effect=RuntimeError("reset failed before task checkout"))
    monkeypatch.setattr(
        orch, "_worktree_slots", lambda: SimpleNamespace(
            ensure_slot_checkout=AsyncMock(), reset_slot_for_task=reset,
        ),
    )
    # An unavailable exit-time proof models the retained historical incident.
    # Stop must recover it once proof is available, without a task recovery incident.
    with monkeypatch.context() as unavailable:
        unavailable.setattr(orch, "arelease_integration_writer_for_retry", AsyncMock(return_value=False))
        session = await env.db.get_session("writer")
        failed = await handler._prepare_and_activate(
            session, session, await env.db.get_task("producer"),
            slot=await env.db.get_workspace("ws-slot"),
        )
    assert failed["result"] == "prepare_failed", failed
    reset.assert_awaited_once()
    assert await env.db.get_task_meta("producer", "supervisor_recovery_incident") is None
    async with env.db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(handoff_state=handoff_state))
    before = git(slot, "rev-parse", "HEAD")

    stopped = await handler.execute("stop_task", {"task_id": "producer"})

    assert stopped.get("stopped") == "producer", stopped
    task = await env.db.get_task("producer")
    session = await env.db.get_session("writer")
    workspace = await env.db.get_workspace("ws-slot")
    assert task.status is TaskStatus.READY
    assert task.assigned_agent_id is None and task.claim_epoch == 7
    assert (session.task_id, session.claim_phase, session.claims) == (None, None, 0)
    assert session.state == session.desired_state == "stopped"
    assert workspace.locked_by_task_id is None and workspace.locked_by_agent_id is None
    assert (await env.db.get_agent("worker")).state.value == "IDLE"
    assert (await env.row("owner"))["handoff_state"] == "reserved"
    assert await env.db.get_task_meta("producer", "needs_attention") is None
    assert remote_sha(env.origin, "aq/producer") == base_sha
    assert git(slot, "rev-parse", "HEAD") == before
    # A failed preparation never changes its task resume point to the default tip.
    assert (await env.db.get_integration_checkpoint("producer"))["checkpoint_sha"] == base_sha
    restarted = await handler.execute("restart_task", {"task_id": "producer"})
    assert restarted.get("restarted") == "producer", restarted
    assert (await env.db.get_task("producer")).status is TaskStatus.READY


@pytest.mark.parametrize("blocker", ["active", "dirty", "unpublished", "stale_epoch"])
async def test_failed_prepare_stop_preserves_unproven_work(env, tmp_path, blocker):
    slot, _, (_, _, _, handler, _) = await _unprepared_writer(
        env, tmp_path, phase="active" if blocker == "active" else "preparing",
    )
    if blocker == "dirty":
        (slot / "unsaved.txt").write_text("preserve me\n")
    elif blocker == "unpublished":
        git(slot, "commit", "--allow-empty", "-m", "unpublished work")
    elif blocker == "stale_epoch":
        await env.db.update_session("writer", last_claim_epoch=6)
    before = git(slot, "rev-parse", "HEAD")

    stopped = await handler.execute("stop_task", {"task_id": "producer"})

    assert "error" in stopped, stopped
    assert "aq session kill writer" in stopped["error"]
    assert "aq integration release-owner --task-id producer --dry-run" in stopped["error"]
    assert "No recovery incident is required" in stopped["error"]
    assert (await env.db.get_task("producer")).status is TaskStatus.IN_PROGRESS
    assert (await env.db.get_session("writer")).task_id == "producer"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"
    assert (await env.row("owner"))["handoff_state"] == "handoff_pending"
    assert git(slot, "rev-parse", "HEAD") == before
    if blocker == "dirty":
        assert (slot / "unsaved.txt").read_text() == "preserve me\n"


@pytest.mark.parametrize("pool", [False, True])
@pytest.mark.parametrize("claim_change", ["activated", "new_epoch"])
async def test_preparation_handoff_rechecks_claim_after_git_proof(
    env, tmp_path, monkeypatch, pool, claim_change,
):
    from src.orchestrator import workspace_attachments

    _, _, (_, _, orch, _, _) = await _unprepared_writer(env, tmp_path)
    async with env.db.immediate() as conn:
        await conn.execute(update(integration_branch_owners).values(handoff_state="handoff_pending"))
    original_detach = workspace_attachments.detach_slot_for_integration_handoff

    async def detach_then_change_claim(*args, **kwargs):
        detached = await original_detach(*args, **kwargs)
        assert detached is True
        if claim_change == "activated":
            await env.db.update_session("writer", claim_phase="active")
        else:
            await env.db.update_task("producer", claim_epoch=8)
        return detached

    monkeypatch.setattr(
        workspace_attachments, "detach_slot_for_integration_handoff", detach_then_change_claim,
    )
    owner = dict(await env.row("owner"))
    if pool:
        confirmed = await orch.aconfirm_integration_pool_owner_handoff(owner)
    else:
        confirmed = await orch.aconfirm_integration_owner_handoff(owner)

    assert confirmed is False
    assert (await env.row("owner"))["handoff_state"] == "handoff_pending"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"
    assert (await env.db.get_session("writer")).task_id == "producer"
    assert (await env.db.get_task("producer")).status is TaskStatus.IN_PROGRESS


async def test_failed_preparation_stop_keeps_a_claim_acquired_after_release(env, tmp_path, monkeypatch):
    from src.models import Agent, AgentState

    slot, _, (_, _, orch, handler, _) = await _unprepared_writer(env, tmp_path)
    original_release = orch.arelease_integration_writer_for_retry

    async def release_then_claim(*args, **kwargs):
        released = await original_release(*args, **kwargs)
        assert released is True
        assert (await env.db.get_task("producer")).status is TaskStatus.READY
        # Claim selection can acquire READY before preparation takes the
        # task control lock still held by stop. Model that exact interval.
        await env.db.create_agent(Agent(
            id="replacement", name="Replacement", profile_id="worker", state=AgentState.BUSY,
            current_task_id="producer",
        ))
        await env.db.update_task(
            "producer", status=TaskStatus.IN_PROGRESS, claim_epoch=8, assigned_agent_id="replacement",
        )
        await env.db.update_workspace(
            "ws-slot", locked_by_task_id="producer", locked_by_agent_id="replacement",
        )
        await env.session(
            "replacement", work_dir=slot, task_id="producer", agent_id="replacement",
            state="running", desired_state="running", claim_phase="preparing", last_claim_epoch=8,
        )
        return released

    monkeypatch.setattr(orch, "arelease_integration_writer_for_retry", release_then_claim)
    stopped = await handler.execute("stop_task", {"task_id": "producer"})

    assert stopped.get("stopped") == "producer", stopped
    task = await env.db.get_task("producer")
    assert (task.status, task.claim_epoch, task.assigned_agent_id) == (
        TaskStatus.IN_PROGRESS, 8, "replacement",
    )
    assert (await env.db.get_workspace("ws-slot")).locked_by_agent_id == "replacement"
    assert (await env.db.get_session("replacement")).task_id == "producer"
    assert (await env.db.get_agent("replacement")).state is AgentState.BUSY


async def test_supervisor_preserves_failed_preparation_without_recovery_incident(env, tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace

    from src.api.auth import RequestScope
    from src.models import AgentProfile
    from src.sessions.provider import SessionHandle

    slot, _, (provider, _, orch, handler, _) = await _unprepared_writer(env, tmp_path)
    # This test preserves actual task work. A detached preparation at a different
    # default-branch tip has no proof tying its dirty contents to the task ref.
    git(slot, "switch", "aq/producer")
    (slot / "unsaved.txt").write_text("preserve failed preparation\n")
    refused = await handler.execute("stop_task", {"task_id": "producer"})
    assert "handoff is unproven" in refused["error"]
    assert await env.db.get_task_meta("producer", "supervisor_recovery_incident") is None

    async def confirm_stopped(row):
        return await provider.confirm_stopped(
            SessionHandle(row["name"], row["provider"], row["instance_token"])
        )

    orch.development_integration = SimpleNamespace(confirm_stopped=confirm_stopped)
    await env.db.create_profile(AgentProfile(
        id="supervisor", name="Supervisor",
        aq_commands=["session_kill", "integration_release_owner"],
    ))
    await env.session("sup", work_dir=tmp_path, state="running", desired_state="running")
    await env.db.update_session("sup", profile_id="supervisor", lifecycle="named")
    scope = asdict(RequestScope(
        kind="session", session_id="sup", project_id="p", elevated=True,
    ))
    args = {"task_id": "producer", "_scope": scope, "dry_run": False}
    live = await handler.execute("integration_release_owner", args)
    assert (live["outcome"], live["outcomes"][0]["reason"]) == ("not_eligible", "writer_live")

    killed = await handler.execute("session_kill", {"session_id": "writer", "_scope": scope})
    assert killed["success"] is True, killed
    # Observation of the fenced kill belongs to the reconciler, not session_kill.
    await env.db.update_session("writer", state="stopped", ended_at=time.time())
    preview = await handler.execute("integration_release_owner", {**args, "dry_run": True})
    assert preview["outcome"] == "preserved_and_released", preview
    assert (await env.db.get_task("producer")).status is TaskStatus.IN_PROGRESS
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "producer"

    released = await handler.execute("integration_release_owner", args)
    assert released["outcome"] == "preserved_and_released", released
    preserved = released["outcomes"][0]["evidence"]["preserved_ref"]
    assert git(env.origin, "show", f"{preserved}:unsaved.txt") == "preserve failed preparation"
    assert (slot / "unsaved.txt").read_text() == "preserve failed preparation\n"
    assert (await env.db.get_task("producer")).status is TaskStatus.READY
    assert (await env.db.get_session("writer")).task_id is None
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id is None
    assert (await env.row("owner"))["handoff_state"] == "released"
    assert (await env.audits("owner"))[-1]["principal"] == "supervisor session:sup"
    restarted = await handler.execute("restart_task", {"task_id": "producer"})
    assert restarted.get("restarted") == "producer", restarted


async def test_supervisor_releases_hard_failed_writer_only_after_provider_confirmation(
    env, tmp_path
):
    """A hard failure can release its dead attachment without retrying the task."""
    from dataclasses import asdict
    from types import SimpleNamespace

    from src.api.auth import RequestScope
    from src.models import AgentProfile
    from src.sessions.provider import SessionHandle

    tip = env.branch("aq/failed")
    await env.task("failed")
    await env.db.update_task("failed", repo_id="r", branch_name="aq/failed")
    await env.db.set_task_meta("failed", "blocked_terminal", "session_close_hard_failure")
    slot = await env.slot("slot", "aq/failed", locked_by_task_id="failed")
    # The failed close already restored the checkout onto the default tip.
    git(slot, "switch", "--detach", "origin/main")
    await env.session("writer", work_dir=slot, task_id="failed")
    await env.owner(
        "owner", "aq/failed", "failed", state="handoff_pending",
        session_id="writer", workspace_id="ws-slot",
    )
    provider, _, orch, handler, _ = await _pool_daemon(env, tmp_path, slot)

    async def confirm_stopped(row):
        return await provider.confirm_stopped(
            SessionHandle(row["name"], row["provider"], row["instance_token"])
        )

    orch.development_integration = SimpleNamespace(confirm_stopped=confirm_stopped)
    await env.db.create_profile(AgentProfile(
        id="supervisor", name="Supervisor",
        aq_commands=["integration_release_owner"],
    ))
    await env.session("sup", work_dir=tmp_path, state="running", desired_state="running")
    await env.db.update_session("sup", profile_id="supervisor", lifecycle="named")
    scope = asdict(RequestScope(
        kind="session", session_id="sup", project_id="p", elevated=True,
    ))
    args = {"task_id": "failed", "_scope": scope, "dry_run": False}

    # A stopped database row alone never proves a live process gone.
    refused = await handler.execute("integration_release_owner", args)
    assert refused["outcome"] == "not_eligible", refused
    assert refused["outcomes"][0]["reason"] == "writer_live"
    assert (await env.row("owner"))["handoff_state"] == "handoff_pending"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "failed"

    await provider.stop(SessionHandle("n-writer", "fake", "tok-writer"), grace=0)
    preview = await handler.execute("integration_release_owner", {**args, "dry_run": True})
    assert preview["outcome"] == "released", preview
    assert (await env.row("owner"))["handoff_state"] == "handoff_pending"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "failed"

    released = await handler.execute("integration_release_owner", args)
    assert released["outcome"] == "released", released
    assert (await env.row("owner"))["handoff_state"] == "released"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id is None
    assert (await env.db.get_task("failed")).status == TaskStatus.BLOCKED
    assert await env.db.get_task_meta("failed", "blocked_terminal") == "session_close_hard_failure"
    assert remote_sha(env.origin, "aq/failed") == tip
    assert (await env.audits("owner"))[-1]["principal"] == "supervisor session:sup"


@pytest.mark.parametrize(
    ("operation_state", "active_stage", "origin_reachable"),
    [("completed", 0, True), ("escalated", 1, True), ("completed", 0, False)],
    ids=["completed-operation", "expired-stage-successor", "unsaved-work-unpreservable"],
)
async def test_a_retired_delegates_drain_ack_stops_it_and_its_work_is_preserved(
    env, tmp_path, operation_state, active_stage, origin_reachable
):
    from src.integration.delegate_release import release_delegates
    from src.integration.models import BranchKey
    from src.integration.ownership import BranchBusy, BranchOwnership
    from src.sessions.provider import SessionHandle
    from src.sessions.reconciler import SessionReconciler

    slot, provider, registry, orchestrator, handler, cfg = await _retired_delegate_worker(
        env, tmp_path, operation_state=operation_state, active_stage=active_stage
    )
    target = BranchKey(repository_id="r", branch="aq/parent")
    with pytest.raises(BranchBusy):
        await BranchOwnership(env.db).acquire(target, "debug-delegate", "repair")

    # 1. The public close is refused, and says why and what to do instead.
    refused = await handler.execute("task_close", {
        "task_id": "delegate", "session_id": "writer", "outcome": "pass",
        "summary": "Repair pushed.", "claim_epoch": 1,
    })
    assert (refused["success"], refused["result"]) == (False, "verification_failed")
    assert refused["retired"]["operation_id"] == "operation"
    assert "Do not retry the close" in refused["error"]
    assert "aq session drain-ack" in refused["error"]
    assert (await env.db.get_task("delegate")).status == TaskStatus.IN_PROGRESS

    # 2. The worker obeys: drain-ack, with its claim still open.
    acked = await handler.execute("session_drain_ack", {"session_id": "writer"})
    assert acked["success"] is True

    # 3. The reconciler stops it -- no supervisor kill -- keeping every binding.
    reconciler = SessionReconciler(
        env.db, cfg, registry, bus=orchestrator.bus, orchestrator=orchestrator, epoch="e"
    )
    now = time.time()
    await reconciler._step_drain_ack(await reconciler._step_observe(now), now)
    writer = await env.db.get_session("writer")
    assert (writer.state, writer.desired_state, writer.task_id) == (
        "stopped", "stopped", "delegate"
    )
    assert await provider.confirm_stopped(SessionHandle("n-writer", "fake", "tok-writer"))
    assert (await env.row("owner"))["handoff_state"] == "attached"
    assert (slot / "progress.txt").read_text() == "unsaved repair progress\n"

    # 4. Owner recovery preserves the progress before it releases anything.
    async def confirm_stopped(session):
        return await provider.confirm_stopped(
            SessionHandle(session["name"], session["provider"], session["instance_token"])
        )

    if not origin_reachable:
        git(env.base, "remote", "set-url", "origin", str(env.tmp / "missing.git"))
    outcome = await env.service(confirm_stopped=confirm_stopped).recover(
        "owner", principal="delegate_retirement"
    )
    if not origin_reachable:
        # Unsaved work that cannot be preserved keeps the owner, claim and lock.
        assert (outcome.outcome, outcome.reason) == ("not_eligible", "origin_unreachable")
        assert (await env.row("owner"))["handoff_state"] == "attached"
        task = await env.db.get_task("delegate")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.IN_PROGRESS, "a1")
        assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "delegate"
        assert (slot / "progress.txt").read_text() == "unsaved repair progress\n"
        assert await release_delegates(env.db, now=now, released_by="integration_service") == []
        return
    assert outcome.outcome == "preserved_and_released"
    preserved = remote_sha(env.origin, outcome.evidence["preserved_ref"])
    assert git(env.origin, "show", f"{preserved}:progress.txt") == "unsaved repair progress"
    assert (slot / "progress.txt").read_text() == "unsaved repair progress\n"
    assert (await env.row("owner"))["handoff_state"] == "released"
    task = await env.db.get_task("delegate")
    assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)

    # 5. Then the successor takes the branch, or the ticket settles terminally.
    if operation_state == "completed":
        released = await release_delegates(env.db, now=now, released_by="integration_service")
        assert [row["task_id"] for row in released] == ["delegate"]
        assert (await env.db.get_task("delegate")).status == TaskStatus.FAILED
        record = await env.db.get_task_meta("delegate", "integration_retirement")
        assert record["disposition"] == "superseded"
    else:
        fence = await BranchOwnership(env.db).acquire(target, "debug-delegate", "repair")
        assert fence.owner_id == "debug-delegate"
        assert await release_delegates(env.db, now=now, released_by="integration_service") == []


# ---------------------------------------------------------------------------
# A close that committed but never finished: terminal transition -> drain-ack
# -> stop -> owner recovery (wise-willow)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delivered", [True, False], ids=["delivered-sound-orbit", "undelivered-accepted-close"]
)
@pytest.mark.parametrize("unsaved", [False, True], ids=["clean-checkout", "unsaved-notes"])
async def test_an_ambiguous_close_drain_ack_stops_the_writer_and_its_owner_is_recovered(
    env, tmp_path, unsaved, delivered
):
    """sound-orbit: COMPLETED and delivered, but its worker stayed bound to the claim.

    A daemon restart fell between ``complete_session_task``'s terminal
    transition and the branch handoff, completion record and claim release
    that follow it.  The attached owner kept the claim; the drained worker sat
    idle; owner recovery answered ``writer_live`` until a supervisor killed it.
    With delivery still running there is no receipt yet (bold-impact-53 waited
    for one); the identity the accepting transition records proves the close.
    bold-impact-53 itself predates that marker, and its bare close metadata
    proves nothing (``TestSettledClaimDrain``).
    """
    from src.database.queries.claim_queries import ACCEPTED_CLOSE_KEY
    from src.database.tables import task_delivery_receipts, task_session_attempts
    from src.sessions.provider import SessionHandle
    from src.sessions.reconciler import SessionReconciler

    env.branch("aq/leaf")
    await env.db.create_task(Task(
        id="leaf", project_id="p", title="leaf", description="",
        status=TaskStatus.IN_PROGRESS, repo_id="r", branch_name="aq/leaf",
    ))
    async with env.db.immediate() as conn:
        await conn.execute(insert(agents).values(
            id="a1", name="worker", profile_id="worker", state="BUSY",
            current_task_id="leaf", created_at=1.0,
        ))
        await conn.execute(
            update(tasks).where(tasks.c.id == "leaf").values(assigned_agent_id="a1",
                                                              claim_epoch=1)
        )
    slot = await env.slot("slot", "aq/leaf", locked_by_agent_id="a1", locked_by_task_id="leaf")
    if unsaved:
        (slot / "notes.txt").write_text("notes written after the close\n")
    claimed_at = time.time() - 600
    sentinel = WorktreeSlotManager.read_sentinel(slot)
    sentinel.assigned_at = claimed_at + 1
    WorktreeSlotManager.write_sentinel(slot, sentinel)
    await env.session(
        "writer", work_dir=slot, state="running", desired_state="running",
        task_id="leaf", agent_id="a1", claim_phase="active", last_claim_epoch=1,
    )
    async with env.db.immediate() as conn:
        await conn.execute(insert(task_session_attempts).values(
            id="attempt", session_id="writer", task_id="leaf", project_id="p",
            agent_id="a1", profile_id="worker", name="n-writer", lifecycle="pool",
            harness="claude", provider="fake", state="running", work_dir=str(slot),
            started_at=claimed_at, session_started_at=1.0,
        ))
    await env.owner("owner", "aq/leaf", "leaf", session_id="writer", workspace_id="ws-slot")
    provider, registry, orchestrator, handler, cfg = await _pool_daemon(env, tmp_path, slot)

    # 1. The close writes its outcome metadata, its terminal transition commits
    #    -- the same write ``complete_session_task`` makes -- and the daemon
    #    restarts before the handoff, the completion record and the claim
    #    release.  Delivery then lands the work with a code receipt, or is
    #    still running and only the accepted-close identity the transition
    #    records in its transaction (smart-cascade) speaks for the close.
    await env.db.set_task_meta("leaf", "outcome", "pass")
    await env.db.set_task_meta("leaf", "close_session_id", "writer")
    await env.db.set_task_meta("leaf", "summary", "Pushed the fix.")
    await env.db.transition_task(
        "leaf", TaskStatus.COMPLETED, context="session_close", assigned_agent_id=None,
        expect_claim_epoch=1,
    )
    if delivered:
        async with env.db.immediate() as conn:
            await conn.execute(insert(task_delivery_receipts).values(
                id="receipt", domain_key="delivery:leaf", source_task_id="leaf",
                repository_id="r", target_branch="main", disposition="code",
                created_at=time.time(),
            ))
    else:
        await env.db.set_task_meta("leaf", ACCEPTED_CLOSE_KEY, {
            "completion_id": "close-leaf", "session_id": "writer", "claim_epoch": 1,
        })
    assert await env.db.get_task_completion("leaf") is None
    assert (await env.db.get_session("writer")).task_id == "leaf"
    assert (await env.row("owner"))["handoff_state"] == "attached"

    # 2. The worker acknowledges its drain.  Owner recovery cannot act yet:
    #    the writer is still running.
    acked = await handler.execute("session_drain_ack", {"session_id": "writer"})
    assert acked["success"] is True

    async def confirm_stopped(session):
        return await provider.confirm_stopped(
            SessionHandle(session["name"], session["provider"], session["instance_token"])
        )

    blocked = await env.service(confirm_stopped=confirm_stopped).recover(
        "owner", principal="sweep", dry_run=True
    )
    assert (blocked.outcome, blocked.reason) == ("not_eligible", "writer_live")

    # 3. The reconciler stops it -- no supervisor kill -- keeping every binding
    #    the owner holds, and the task's terminal record untouched.
    reconciler = SessionReconciler(
        env.db, cfg, registry, bus=orchestrator.bus, orchestrator=orchestrator, epoch="e"
    )
    now = time.time()
    await reconciler._step_drain_ack(await reconciler._step_observe(now), now)
    writer = await env.db.get_session("writer")
    assert (writer.state, writer.desired_state, writer.end_reason, writer.task_id) == (
        "stopped", "stopped", "settled_claim", "leaf"
    )
    assert await provider.confirm_stopped(SessionHandle("n-writer", "fake", "tok-writer"))
    assert (await env.row("owner"))["handoff_state"] == "attached"
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id == "leaf"
    task = await env.db.get_task("leaf")
    assert (task.status, task.assigned_agent_id, task.claim_epoch) == (
        TaskStatus.COMPLETED, None, 1
    )
    proof = (
        "code delivery receipt receipt" if delivered
        else "accepted close close-leaf by session writer (claim epoch 1)"
    )
    bodies = [c["body"] for c in (await env.db.list_task_comments("leaf"))["comments"]]
    assert any(f"is COMPLETED with {proof}" in body for body in bodies)

    # 4. Owner recovery now proves the writer gone, preserves anything origin
    #    lacks, and releases the owner, the workspace and the claim.
    outcome = await env.service(confirm_stopped=confirm_stopped).recover(
        "owner", principal="sweep"
    )
    assert outcome.outcome == ("preserved_and_released" if unsaved else "released")
    if unsaved:
        preserved = remote_sha(env.origin, outcome.evidence["preserved_ref"])
        assert git(env.origin, "show", f"{preserved}:notes.txt") == (
            "notes written after the close"
        )
        assert (slot / "notes.txt").read_text() == "notes written after the close\n"
    assert (await env.row("owner"))["handoff_state"] == "released"
    writer = await env.db.get_session("writer")
    assert (writer.task_id, writer.claim_phase) == (None, None)
    assert (await env.db.get_workspace("ws-slot")).locked_by_task_id is None
    task = await env.db.get_task("leaf")
    assert (task.status, task.assigned_agent_id, task.claim_epoch) == (
        TaskStatus.COMPLETED, None, 1
    )
    assert await env.db.get_task_completion("leaf") is None


@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("crash", ["before_release", "push_response_lost"])
async def test_published_progress_survives_unfinished_release(env, dirty, crash, monkeypatch):
    from src.git.manager import GitError

    env.branch("aq/resume", extra=0 if dirty else 1)
    slot = await env.slot("slot", "aq/resume")
    if dirty:
        (slot / "notes.txt").write_text("dirty progress\n")
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/resume", "resume", session_id="writer", workspace_id="ws-slot")
    service = env.service()
    if crash == "before_release":
        original = service._release
        monkeypatch.setattr(service, "_release", AsyncMock(side_effect=RuntimeError("release lost")))
        with pytest.raises(RuntimeError, match="release lost"):
            await service.recover("owner", principal="sweep")
        monkeypatch.setattr(service, "_release", original)
    else:
        original = service.git.apush_validated_ref

        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise GitError("push response lost")

        monkeypatch.setattr(service.git, "apush_validated_ref", interrupted)
        refused = await service.recover("owner", principal="sweep")
        assert (refused.outcome, refused.reason) == ("not_eligible", "origin_unreachable")
        monkeypatch.setattr(service.git, "apush_validated_ref", original)
    published = remote_sha(env.origin, "aq/resume")
    recovered = await env.service().recover("owner", principal="restart")
    assert recovered.outcome == "preserved_and_released"
    assert recovered.evidence["preserved_ref"] == "aq/resume"
    assert recovered.evidence["preserved_sha"] == published
    assert remote_sha(env.origin, "aq/resume") == published
    assert (await env.row("owner"))["handoff_state"] == "released"
    assert git(env.origin, "for-each-ref", "refs/heads/aq/preserved/") == ""
    if dirty:
        assert git(env.origin, "show", f"{published}:notes.txt") == "dirty progress"


@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("replay", ["none", "after_push", "after_delete"])
async def test_diverged_snapshot_is_merged_on_same_branch_then_deleted(env, dirty, replay, monkeypatch):
    from src.git.manager import GitError
    from src.integration.owner_recovery import resume_recovery_snapshots

    original = env.branch("aq/resume")
    slot = await env.slot("slot", "aq/resume")
    await env.task("resume")
    await env.db.update_task("resume", repo_id="r", branch_name="aq/resume")
    (slot / "local.txt").write_text("local progress\n")
    if not dirty:
        git(slot, "add", "local.txt")
        git(slot, "commit", "-m", "local progress")
    git(env.base, "checkout", "-b", "publisher", original)
    (env.base / "remote.txt").write_text("remote progress\n")
    git(env.base, "add", "remote.txt")
    git(env.base, "commit", "-m", "remote progress")
    remote = git(env.base, "rev-parse", "HEAD")
    git(env.base, "push", "origin", "HEAD:refs/heads/aq/resume")
    git(env.base, "checkout", "main")
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/resume", "resume", session_id="writer", workspace_id="ws-slot")
    service = env.service()
    recovered = await service.recover("owner", principal="sweep")
    assert recovered.outcome == "preserved_and_released"
    sha, ref = recovered.evidence["preserved_sha"], recovered.evidence["preserved_ref"]
    assert ref == f"aq/recovery/owner/{sha}"
    assert remote_sha(env.origin, "aq/resume") == remote
    assert remote_sha(env.origin, ref) == sha
    assert git(env.origin, "for-each-ref", "refs/heads/aq/preserved/") == ""

    # A retained local copy must also disappear on cleanup replay.
    git(env.base, "update-ref", f"refs/heads/{ref}", sha)
    deletion = service.git.adelete_remote_ref_exact
    if replay != "none":
        async def interrupted(*args, **kwargs):
            if replay == "after_delete":
                await deletion(*args, **kwargs)
            raise GitError("injected response loss")
        monkeypatch.setattr(service.git, "adelete_remote_ref_exact", interrupted)
        with pytest.raises(GitError, match="injected response loss"):
            await resume_recovery_snapshots(
                env.db, service.git, str(env.base), "r", "aq/resume", repository_url=str(env.origin),
            )
        monkeypatch.setattr(service.git, "adelete_remote_ref_exact", deletion)
    tip = await resume_recovery_snapshots(
        env.db, service.git, str(env.base), "r", "aq/resume", repository_url=str(env.origin),
    )
    assert remote_sha(env.origin, "aq/resume") == tip
    assert git(env.origin, "show", f"{tip}:local.txt") == "local progress"
    assert git(env.origin, "show", f"{tip}:remote.txt") == "remote progress"
    assert await service.git.ais_ancestor(str(env.base), sha, tip, strict=True)
    assert await service.git.ais_ancestor(str(env.base), remote, tip, strict=True)
    assert remote_sha(env.origin, ref) is None
    assert git(env.base, "for-each-ref", f"refs/remotes/origin/{ref}") == ""
    assert git(env.base, "for-each-ref", f"refs/heads/{ref}") == ""
    # Actual retry selects the existing identity and keeps the merged work.
    from src.config import WorktreesConfig
    from src.orchestrator.worktree_manager import WorktreeSlotManager
    import asyncio

    mgr = WorktreeSlotManager(env.db, service.git, None, WorktreesConfig(), lambda _: asyncio.Lock())
    task = await env.db.get_task("resume")
    branch = await mgr.reset_slot_for_task(await env.db.get_workspace("ws-slot"), task)
    assert branch == "aq/resume" and git(slot, "rev-parse", "HEAD") == tip
    assert (slot / "local.txt").read_text() == "local progress\n"
    assert (slot / "remote.txt").read_text() == "remote progress\n"


@pytest.mark.parametrize("guard", ["conflict", "moved_snapshot", "failed_push"])
async def test_diverged_recovery_refuses_without_losing_either_side(env, guard, monkeypatch):
    from src.git.manager import GitError
    from src.integration.owner_recovery import resume_recovery_snapshots

    original = env.branch("aq/resume")
    slot = await env.slot("slot", "aq/resume")
    (slot / "base.txt").write_text("local change\n")
    git(slot, "add", "base.txt")
    git(slot, "commit", "-m", "local change")
    local = git(slot, "rev-parse", "HEAD")
    git(env.base, "checkout", "-b", "publisher", original)
    filename = "base.txt" if guard == "conflict" else "remote.txt"
    (env.base / filename).write_text("remote change\n")
    git(env.base, "add", filename)
    git(env.base, "commit", "-m", "remote change")
    remote = git(env.base, "rev-parse", "HEAD")
    git(env.base, "push", "origin", "HEAD:refs/heads/aq/resume")
    git(env.base, "checkout", "main")
    await env.session("writer", work_dir=slot)
    await env.owner("owner", "aq/resume", "resume", session_id="writer", workspace_id="ws-slot")
    service = env.service()
    recovered = await service.recover("owner", principal="sweep")
    ref = recovered.evidence["preserved_ref"]
    if guard == "moved_snapshot":
        git(env.origin, "update-ref", "refs/heads/" + ref, original)
    if guard == "failed_push":
        monkeypatch.setattr(service.git, "apush_validated_ref", AsyncMock(side_effect=GitError("push failed")))
    with pytest.raises(GitError, match={"conflict": "conflicts", "moved_snapshot": "ref changed",
                                      "failed_push": "push failed"}[guard]):
        await resume_recovery_snapshots(
            env.db, service.git, str(env.base), "r", "aq/resume", repository_url=str(env.origin),
        )
    assert remote_sha(env.origin, "aq/resume") == remote
    assert remote_sha(env.origin, ref) == (original if guard == "moved_snapshot" else local)
    assert git(slot, "rev-parse", "HEAD") == local
    assert (slot / "base.txt").read_text() == "local change\n"
