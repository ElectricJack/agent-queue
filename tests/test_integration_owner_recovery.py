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

from src.database import Database
from src.database.tables import (
    agents,
    events,
    integration_branch_owners,
    integration_owner_recoveries,
    projects,
    sessions,
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
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.db_fixtures import lease_dsn


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def remote_sha(origin, ref) -> str | None:
    out = git(origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{ref}")
    return out or None


@pytest.fixture
async def env(tmp_path):
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
    db = Database(lease_dsn("owner-recovery.db"))
    await db.initialize()
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
    await db.close()


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
    ) -> None:
        async with self.db.immediate() as conn:
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


async def test_a_local_branch_ahead_of_origin_is_preserved_then_released(env):
    tip = env.branch("aq/t2", extra=1)
    await env.task("t2")
    await env.session("s2", work_dir=env.tmp / "gone")
    await env.owner("o2", "aq/t2", "t2", state="handoff_pending", session_id="s2")

    result = await env.service().recover("o2", principal="sweep")

    assert result.outcome == "preserved_and_released"
    assert remote_sha(env.origin, "aq/preserved/o2") == tip
    assert result.evidence["preserved_ref"] == "aq/preserved/o2"
    assert result.evidence["preserved_sha"] == tip
    assert result.evidence["local_sha"] == tip
    assert result.evidence["worktree"] is None
    assert (await env.row("o2"))["handoff_state"] == "released"
    # The task branch itself was never rewritten on origin.
    assert remote_sha(env.origin, "aq/t2") != tip


async def test_a_preserved_ref_already_at_the_same_sha_is_not_a_failure(env):
    tip = env.branch("aq/t2", extra=1)
    git(env.base, "push", "-q", "origin", f"{tip}:refs/heads/aq/preserved/o2")
    await env.session("s2", work_dir=env.tmp / "gone")
    await env.owner("o2", "aq/t2", "t2", session_id="s2")

    result = await env.service().recover("o2", principal="sweep")

    assert result.outcome == "preserved_and_released"
    assert remote_sha(env.origin, "aq/preserved/o2") == tip


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
    preserved = remote_sha(env.origin, "aq/preserved/o3")
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
    assert remote_sha(env.origin, "aq/preserved/o9") == tip
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
    assert {"action": "push", "ref": "aq/preserved/o10", "source": f"snapshot of {slot}"} in [
        {k: step.get(k) for k in ("action", "ref", "source")} for step in planned
    ]
    assert {"action": "detach", "worktree": str(slot)} in planned
    assert remote_sha(env.origin, "aq/preserved/o10") is None
    assert not detached(slot)
    assert await env.audits("o10") == []
    assert (await env.row("o10"))["handoff_state"] == "attached"


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
