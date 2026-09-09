"""Compute the workspace requirement set for a task and acquire its workspaces.

See spec §6.  ``effective_requirements`` materializes defaults and auto-attach
kinds so downstream code reads a single, canonically-sorted list.
``acquire_for_task`` (added in a follow-on commit) takes that list and atomically
acquires every required lock.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import or_, select, update

from src.database.tables import agents, integration_branch_owners, sessions, tasks, workspaces
from src.integration.models import REQUEUE_INTEGRATION_OWNER_ROLES
from src.models import (
    SYSTEM_KIND_SCOPE,
    AgentState,
    ResolvedRequirement,
    Task,
    TaskStatus,
    WorkspaceAttachment,
    WorkspaceAttachmentSet,
    WorkspaceKind,
)

# Auto-attach kinds get positions 10000+ so they always sort *after* explicit
# and synthesized requirements within their kind_id group.  This isn't
# load-bearing for correctness (the lock order key is per-row), but it keeps
# auto-attach behavior predictable: explicit requirements come first.
_AUTO_ATTACH_POSITION_BASE = 10_000


class AcquisitionFailed(Exception):
    """Raised when ``acquire_for_task`` cannot satisfy a required kind.

    Carries the failing ``kind_id`` so the caller can decide whether to retry,
    fall back, or surface the error.
    """

    def __init__(self, kind_id: str):
        self.kind_id = kind_id
        super().__init__(f"could not acquire workspace of kind {kind_id!r}")


async def integration_handoff_release_is_confirmed(db, owner: dict) -> bool:
    """Whether *owner* already durably released its exact old attachment."""
    owner_id = owner.get("id")
    workspace_id = owner.get("workspace_id") or owner.get("confirmed_workspace_id")
    if not owner_id or not workspace_id:
        return False
    async with db.immediate() as conn:
        row = (
            await conn.execute(
                select(integration_branch_owners).where(
                    integration_branch_owners.c.id == owner_id,
                    integration_branch_owners.c.fence_token == owner.get("fence_token"),
                    integration_branch_owners.c.owner_id == owner.get("owner_id"),
                )
            )
        ).mappings().one_or_none()
    return bool(
        row is not None
        and row["handoff_state"] == "released"
        and row["confirmed_workspace_id"] == workspace_id
    )


async def mark_integration_handoff_released(
    db,
    owner: dict,
    *,
    workspace,
    task_id: str,
) -> bool:
    """Atomically record detach proof and release the exact old DB lock.

    A stopped pool writer is different from an ordinary stopped task session:
    it still owns an active claim, and merely dropping its workspace lock
    leaves that claim pointing at a process which can never resume.  Release
    that claim in this same transaction, after recording the durable detach
    proof but before a successor can observe the freed slot.
    """
    claim_release = None
    async with db.immediate() as conn:
        session_row = (await conn.execute(select(sessions).where(
            sessions.c.id == owner.get("session_id")
        ).with_for_update())).mappings().one_or_none()
        owner_row = (
            await conn.execute(
                select(integration_branch_owners)
                .where(integration_branch_owners.c.id == owner.get("id"))
                .with_for_update()
            )
        ).mappings().one_or_none()
        workspace_row = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.id == workspace.id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        agent_row = None
        if (
            session_row is not None
            and session_row["lifecycle"] == "pool"
            and session_row["agent_id"] is not None
        ):
            agent_row = (
                await conn.execute(
                    select(agents)
                    .where(agents.c.id == session_row["agent_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
        if (
            owner_row is None
            or session_row is None
            or workspace_row is None
            or owner_row["fence_token"] != owner.get("fence_token")
            or owner_row["owner_id"] != owner.get("owner_id")
            or owner_row["handoff_state"] != "handoff_pending"
            or owner_row["session_id"] != owner.get("session_id")
            or owner_row["workspace_id"] != workspace.id
            or session_row["task_id"] != task_id
            or session_row["state"] != "stopped"
            or session_row["desired_state"] != "stopped"
            or session_row["work_dir"] != workspace_row["workspace_path"]
            or session_row["project_id"] != workspace_row["project_id"]
            or workspace_row["locked_by_task_id"] != task_id
            or workspace_row["locked_by_agent_id"] != session_row["agent_id"]
            or (
                session_row["lifecycle"] == "pool"
                and
                session_row["agent_id"] is not None
                and (
                    agent_row is None
                    or agent_row["state"] != AgentState.BUSY.value
                    or agent_row["current_task_id"] != task_id
                )
            )
        ):
            return False

        released_owner = await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.id == owner_row["id"],
                integration_branch_owners.c.fence_token == owner_row["fence_token"],
                integration_branch_owners.c.owner_id == owner_row["owner_id"],
                integration_branch_owners.c.handoff_state == "handoff_pending",
                integration_branch_owners.c.session_id == owner_row["session_id"],
                integration_branch_owners.c.workspace_id == workspace.id,
            )
            .values(
                handoff_state="released",
                session_id=None,
                workspace_id=None,
                confirmed_workspace_id=workspace.id,
                updated_at=time.time(),
            )
        )
        if released_owner.rowcount != 1:
            raise RuntimeError("integration handoff release lost its compare-and-swap")

        if session_row["lifecycle"] == "pool":
            task_row = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == task_id).with_for_update()
                )
            ).mappings().one_or_none()
            if (
                task_row is None
                or task_row["status"] != TaskStatus.IN_PROGRESS.value
                or task_row["assigned_agent_id"] != session_row["agent_id"]
                or task_row["claim_epoch"] != session_row["last_claim_epoch"]
            ):
                raise RuntimeError("integration pool handoff claim changed before release")
            claim_release = await db.release_claim(
                session_row["id"],
                # The successor decides when the old writer may run again.
                task_status=TaskStatus.PAUSED,
                context="integration_handoff",
                now=time.time(),
                expected_task_id=task_id,
                expected_claim_epoch=session_row["last_claim_epoch"],
                expected_task_status=TaskStatus.IN_PROGRESS,
                release_workspace_lock=True,
                conn=conn,
            )
            if not claim_release.released:
                raise RuntimeError("integration pool handoff claim release lost its fence")
        else:
            released_workspace = await conn.execute(
                update(workspaces)
                .where(
                    workspaces.c.id == workspace.id,
                    workspaces.c.locked_by_task_id == task_id,
                    workspaces.c.locked_by_agent_id == session_row["agent_id"],
                )
                .values(
                    locked_by_agent_id=None,
                    locked_by_task_id=None,
                    locked_at=None,
                    lock_mode=None,
                )
            )
            if session_row["agent_id"] is not None:
                await conn.execute(
                    update(agents)
                    .where(
                        agents.c.id == session_row["agent_id"],
                        agents.c.state == AgentState.BUSY.value,
                        agents.c.current_task_id == task_id,
                    )
                    .values(state=AgentState.IDLE.value, current_task_id=None)
                )
            if released_workspace.rowcount != 1:
                raise RuntimeError("integration handoff release lost its compare-and-swap")
    if claim_release is not None:
        await db._after_release(claim_release)
    return True


async def recover_stopped_integration_pool_claim(db, task_id: str) -> bool:
    """Release one historically stranded pool claim after an exact handoff.

    This is deliberately narrower than an operational cleanup sweep.  The
    branch owner must still retain the prior workspace as durable handoff
    evidence *and* be detached from any current writer.  A successor may
    already hold that slot or the old agent on another slot, so recovery may
    only clear the exact stopped session's claim -- never its former locks,
    agent state, or claim file.
    """
    claim_release = None
    async with db.immediate() as conn:
        task_row = (
            await conn.execute(select(tasks).where(tasks.c.id == task_id).with_for_update())
        ).mappings().one_or_none()
        if (
            task_row is None
            or task_row["status"] != TaskStatus.BLOCKED.value
            or task_row["assigned_agent_id"] is not None
            or await db._read_manual_pause(conn, task_id) is not None
        ):
            return False
        session_rows = (
            await conn.execute(
                select(sessions)
                .where(
                    sessions.c.task_id == task_id,
                    sessions.c.lifecycle == "pool",
                    sessions.c.state == "stopped",
                    sessions.c.desired_state == "stopped",
                    sessions.c.claim_phase == "active",
                )
                .with_for_update()
            )
        ).mappings().all()
        if len(session_rows) != 1:
            return False
        session_row = session_rows[0]
        if (
            session_row["agent_id"] is None
            or session_row["last_claim_epoch"] != task_row["claim_epoch"]
        ):
            return False
        workspace_rows = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.workspace_path == session_row["work_dir"])
                .with_for_update()
            )
        ).mappings().all()
        if len(workspace_rows) != 1:
            return False
        workspace_row = workspace_rows[0]
        owner_row = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == task_row["repo_id"],
                    integration_branch_owners.c.ref == task_row["branch_name"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        old_session_ownership = (
            await conn.execute(
                select(integration_branch_owners.c.id)
                .where(integration_branch_owners.c.session_id == session_row["id"])
                .limit(1)
            )
        ).first()
        if (
            owner_row is None
            or task_row["created_by_kind"] != "integration_repair"
            or not task_row["created_by_id"]
            or (owner_row["owner_id"], owner_row["owner_role"]) not in {
                (task_id, "repair"), (task_row["created_by_id"], "collector"),
            }
            or owner_row["handoff_state"] not in {"reserved", "released"}
            or owner_row["session_id"] is not None
            or owner_row["workspace_id"] is not None
            or owner_row["confirmed_workspace_id"] != workspace_row["id"]
            or workspace_row["project_id"] != task_row["project_id"]
            or old_session_ownership is not None
        ):
            return False
        claim_release = await db.release_historical_pool_claim(
            conn,
            session_row["id"],
            task_id=task_id,
            claim_epoch=session_row["last_claim_epoch"],
            now=time.time(),
        )
        if not claim_release.released:
            return False
    await db._after_release(claim_release)
    return True


async def mark_integration_pool_handoff_released(
    db,
    owner: dict,
    *,
    workspace,
    task_id: str,
    session_instance_token: str,
) -> bool:
    """Record a **pool** writer's detach proof without unbinding its slot.

    The push-model twin (:func:`mark_integration_handoff_released`) proves a
    writer gone by stopping its session and then unwinding the whole
    attachment -- workspace lock, agent, session state.  A pool session is
    not a writer the daemon may stop: it holds its workspace agent-lock
    across tasks (only ``terminate_pool_session`` drops it) and the loop that
    is mid-``aq task close`` is the same loop that will claim the next task.

    What a pool close *does* provide is the other half of the same proof, in
    the right order: the caller has already detached the checkout off the
    branch (clean, pushed, ``origin`` tip verified) and ``release_claim`` is
    about to erase the task-hold.  Running here -- inside the close, while
    ``sessions.task_id`` and ``workspaces.locked_by_task_id`` still name this
    task -- is what makes that evidence checkable at all; afterwards it is
    gone and the owner row can never be confirmed again (amber-delta).

    So this CAS asserts the same identity as its twin, minus the two clauses
    a live pool session cannot satisfy (``state``/``desired_state`` stopped),
    and writes only the owner row.  The workspace lock, the agent and the
    session are left exactly as they are for ``release_claim`` to unwind on
    its own terms.
    """
    async with db.immediate() as conn:
        # Match release_claim: session before owner/workspace.
        session_row = (await conn.execute(select(sessions).where(
            sessions.c.id == owner.get("session_id")
        ).with_for_update())).mappings().one_or_none()
        owner_row = (
            await conn.execute(
                select(integration_branch_owners)
                .where(integration_branch_owners.c.id == owner.get("id"))
                .with_for_update()
            )
        ).mappings().one_or_none()
        workspace_row = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.id == workspace.id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            owner_row is None
            or session_row is None
            or workspace_row is None
            or owner_row["fence_token"] != owner.get("fence_token")
            or owner_row["owner_id"] != owner.get("owner_id")
            or owner_row["owner_id"] != task_id
            # Every task-owned writer role: a re-queued ``verifier`` reaches
            # here through the same pool detach proof as a worker or repair
            # delegate (fair-willow).  ``collector`` is owned by an operation,
            # so it never has the claim this CAS reads.
            or owner_row["owner_role"] not in REQUEUE_INTEGRATION_OWNER_ROLES
            or owner_row["handoff_state"] != "handoff_pending"
            or owner_row["session_id"] != owner.get("session_id")
            or owner_row["workspace_id"] != workspace.id
            or session_row["lifecycle"] != "pool"
            or session_row["instance_token"] != session_instance_token
            or session_row["task_id"] != task_id
            or session_row["work_dir"] != workspace_row["workspace_path"]
            or session_row["project_id"] != workspace_row["project_id"]
            or workspace_row["locked_by_task_id"] != task_id
            or workspace_row["locked_by_agent_id"] != session_row["agent_id"]
        ):
            return False

        released_owner = await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.id == owner_row["id"],
                integration_branch_owners.c.fence_token == owner_row["fence_token"],
                integration_branch_owners.c.owner_id == owner_row["owner_id"],
                integration_branch_owners.c.handoff_state == "handoff_pending",
                integration_branch_owners.c.session_id == owner_row["session_id"],
                integration_branch_owners.c.workspace_id == workspace.id,
            )
            .values(
                handoff_state="released",
                session_id=None,
                workspace_id=None,
                confirmed_workspace_id=workspace.id,
                updated_at=time.time(),
            )
        )
        if released_owner.rowcount != 1:
            raise RuntimeError("integration pool handoff release lost its compare-and-swap")
    return True


async def mark_stopped_integration_pool_handoff_released(
    db,
    owner: dict,
    *,
    workspace,
    session_instance_token: str,
    confirmed_later_sessions: dict[str, tuple[str, float]] | None = None,
) -> bool:
    """Release an exact, stopped pool claim without replaying its task transition.

    A pool teardown can stop a process after its task lease expired and was
    requeued. In that state ``release_claim`` is deliberately the wrong tool:
    its normal READY transition would need to rediscover the old claim epoch,
    and must never overwrite the successor claim. This is the narrow repair
    for that stranded attachment. It requires the requeued task to still be
    unassigned, the stopped session/retired worker/slot to be unchanged, and
    no later session for that worker. It then releases only those old rows;
    the task row (including its newer claim epoch) is left untouched.
    """
    async with db.immediate() as conn:
        session_row = (await conn.execute(select(sessions).where(
            sessions.c.id == owner.get("session_id")
        ).with_for_update())).mappings().one_or_none()
        owner_row = (
            await conn.execute(
                select(integration_branch_owners)
                .where(integration_branch_owners.c.id == owner.get("id"))
                .with_for_update()
            )
        ).mappings().one_or_none()
        workspace_row = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.id == workspace.id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        task_row = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == owner.get("owner_id"))
                .with_for_update()
            )
        ).mappings().one_or_none()
        agent_id = session_row["agent_id"] if session_row is not None else None
        agent_row = (
            await conn.execute(select(agents).where(agents.c.id == agent_id).with_for_update())
            if agent_id
            else None
        )
        agent_row = agent_row.mappings().one_or_none() if agent_row is not None else None
        newer_sessions = []
        if session_row is not None and agent_id:
            newer_sessions = (
                await conn.execute(
                    select(sessions)
                    .where(
                        or_(sessions.c.agent_id == agent_id,
                            sessions.c.work_dir == session_row["work_dir"]),
                        sessions.c.id != session_row["id"],
                        sessions.c.started_at >= session_row["started_at"],
                    )
                    .with_for_update()
                )
            ).mappings().all()

        unlocked_verifier = (
            owner_row is not None and owner_row["owner_role"] == "verifier"
            and workspace_row is not None
            and workspace_row["locked_by_task_id"] is None
            and workspace_row["locked_by_agent_id"] is None
            and workspace.locked_by_task_id is None
            and workspace.locked_by_agent_id is None
        )
        if (
            owner_row is None
            or session_row is None
            or workspace_row is None
            or task_row is None
            or agent_row is None
            or (bool(newer_sessions) and not (
                unlocked_verifier
                and all(
                    row["state"] == "stopped" and row["desired_state"] == "stopped"
                    and (confirmed_later_sessions or {}).get(row["id"]) == (
                        row["instance_token"], row["started_at"]
                    )
                    for row in newer_sessions
                )
            ))
            or owner_row["fence_token"] != owner.get("fence_token")
            or owner_row["owner_id"] != owner.get("owner_id")
            or owner_row["owner_role"] != owner.get("owner_role")
            or owner_row["handoff_state"] != "handoff_pending"
            or owner_row["session_id"] != session_row["id"]
            or owner_row["workspace_id"] != workspace.id
            or session_row["lifecycle"] != "pool"
            or session_row["instance_token"] != session_instance_token
            or session_row["state"] != "stopped"
            or session_row["desired_state"] != "stopped"
            or session_row["task_id"] != owner_row["owner_id"]
            or session_row["work_dir"] != workspace_row["workspace_path"]
            or (not unlocked_verifier and (
                workspace_row["locked_by_task_id"] != owner_row["owner_id"]
                or workspace_row["locked_by_agent_id"] != agent_id
            ))
            or task_row["status"] != "READY"
            or task_row["assigned_agent_id"] is not None
            or agent_row["state"] != AgentState.RETIRED.value
            or agent_row["current_task_id"] != owner_row["owner_id"]
        ):
            return False

        released_owner = await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.id == owner_row["id"],
                integration_branch_owners.c.fence_token == owner_row["fence_token"],
                integration_branch_owners.c.owner_id == owner_row["owner_id"],
                integration_branch_owners.c.handoff_state == "handoff_pending",
                integration_branch_owners.c.session_id == session_row["id"],
                integration_branch_owners.c.workspace_id == workspace.id,
            )
            .values(
                handoff_state="released",
                session_id=None,
                workspace_id=None,
                confirmed_workspace_id=workspace.id,
                updated_at=time.time(),
            )
        )
        released_workspace = await conn.execute(
            update(workspaces)
            .where(
                workspaces.c.id == workspace.id,
                workspaces.c.locked_by_task_id == workspace_row["locked_by_task_id"],
                workspaces.c.locked_by_agent_id == workspace_row["locked_by_agent_id"],
            )
            .values(
                locked_by_agent_id=None,
                locked_by_task_id=None,
                locked_at=None,
                lock_mode=None,
            )
        )
        released_session = await conn.execute(
            update(sessions)
            .where(
                sessions.c.id == session_row["id"],
                sessions.c.instance_token == session_instance_token,
                sessions.c.task_id == owner_row["owner_id"],
                sessions.c.state == "stopped",
                sessions.c.desired_state == "stopped",
            )
            .values(
                task_id=None,
                claim_phase=None,
                claim_phase_at=None,
                last_claim_result="stale_handoff_recovered",
            )
        )
        released_agent = await conn.execute(
            update(agents)
            .where(
                agents.c.id == agent_id,
                agents.c.state == AgentState.RETIRED.value,
                agents.c.current_task_id == owner_row["owner_id"],
            )
            .values(state=AgentState.IDLE.value, current_task_id=None)
        )
        if any(
            result.rowcount != 1
            for result in (released_owner, released_workspace, released_session, released_agent)
        ):
            raise RuntimeError("stopped pool integration handoff lost its compare-and-swap")
    return True


async def release_never_attached_integration_launch(
    db,
    *,
    repository_id: str,
    branch: str,
    task_id: str,
    agent_id: str,
    workspace_id: str,
) -> bool:
    """Release a losing launch only when no integration writer was attached.

    This is deliberately separate from handoff confirmation: a collector may
    win after workspace preparation but before the starting-session/owner CAS.
    In that case there is no process to stop.  The current owner must itself
    prove that fact by remaining reserved with no session or workspace.
    """
    async with db.immediate() as conn:
        owner_row = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == repository_id,
                    integration_branch_owners.c.ref == branch,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        workspace_row = (
            await conn.execute(
                select(workspaces)
                .where(workspaces.c.id == workspace_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        agent_row = (
            await conn.execute(
                select(agents).where(agents.c.id == agent_id).with_for_update()
            )
        ).mappings().one_or_none()
        live_session = (
            await conn.execute(
                select(sessions.c.id)
                .where(
                    sessions.c.task_id == task_id,
                    sessions.c.state != "stopped",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            owner_row is None
            or (
                owner_row["owner_id"] == task_id
                and owner_row["owner_role"] == "worker"
            )
            or owner_row["handoff_state"] != "reserved"
            or owner_row["session_id"] is not None
            or owner_row["workspace_id"] is not None
            or workspace_row is None
            or workspace_row["locked_by_task_id"] != task_id
            or workspace_row["locked_by_agent_id"] != agent_id
            or agent_row is None
            or agent_row["state"] != AgentState.BUSY.value
            or agent_row["current_task_id"] != task_id
            or live_session is not None
        ):
            return False

        released_workspace = await conn.execute(
            update(workspaces)
            .where(
                workspaces.c.id == workspace_id,
                workspaces.c.locked_by_task_id == task_id,
                workspaces.c.locked_by_agent_id == agent_id,
            )
            .values(
                locked_by_agent_id=None,
                locked_by_task_id=None,
                locked_at=None,
                lock_mode=None,
            )
        )
        released_agent = await conn.execute(
            update(agents)
            .where(
                agents.c.id == agent_id,
                agents.c.state == AgentState.BUSY.value,
                agents.c.current_task_id == task_id,
            )
            .values(state=AgentState.IDLE.value, current_task_id=None)
        )
        if released_workspace.rowcount != 1 or released_agent.rowcount != 1:
            raise RuntimeError("unattached integration launch release lost its compare-and-swap")
    return True


async def detach_slot_for_integration_handoff(
    db,
    git,
    git_mutex: Callable,
    workspace,
    *,
    expected_branch: str,
    allow_published_detached_head: bool = False,
    require_detached: bool = False,
) -> bool:
    """Detach a clean, fully pushed slot without resetting its contents.

    Handoff is not terminal-failure cleanup.  It must never invoke the slot
    restore ladder, which may salvage and reset work.  Instead, hold the
    repository mutex while proving the checkout is clean, its exact current
    branch tip equals the freshly fetched remote tip, and then detach that
    immutable HEAD.  Unknown Git state is a failed proof, never release
    evidence.
    """
    if not workspace.is_slot or not workspace.base_workspace_id:
        return False
    base = await db.get_workspace(workspace.base_workspace_id)
    if base is None or base.project_id != workspace.project_id:
        return False

    return await detach_workspace_for_integration_handoff(
        git,
        git_mutex,
        workspace,
        mutex_path=base.workspace_path,
        expected_branch=expected_branch,
        allow_published_detached_head=allow_published_detached_head,
        require_detached=require_detached,
    )


async def detach_workspace_for_integration_handoff(
    git,
    git_mutex: Callable,
    workspace,
    *,
    mutex_path: str | None = None,
    expected_branch: str,
    allow_published_detached_head: bool = False,
    require_detached: bool = False,
) -> bool:
    """Prove and detach an exact pushed checkout before releasing its lock."""

    checkout = workspace.workspace_path
    mutex_path = mutex_path or checkout
    expected_branch = expected_branch.removeprefix("refs/heads/")
    branch_ref = f"refs/heads/{expected_branch}"
    remote_ref = f"refs/remotes/origin/{expected_branch}"
    async with git_mutex(mutex_path):
        current = await git._arun_unlocked(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout
        )
        if require_detached and current != "HEAD":
            return False
        if current not in {expected_branch, "HEAD"}:
            return False
        status = await git._arun_unlocked(["status", "--porcelain"], cwd=checkout)
        if status:
            return False

        await git._arun_unlocked(["fetch", "origin"], cwd=mutex_path)
        local_tip = await git._arun_unlocked(["rev-parse", branch_ref], cwd=checkout)
        remote_tip = await git._arun_unlocked(["rev-parse", remote_ref], cwd=checkout)
        head = await git._arun_unlocked(["rev-parse", "HEAD"], cwd=checkout)
        if not local_tip or local_tip != remote_tip:
            return False
        if head != local_tip:
            if current != "HEAD" or not allow_published_detached_head:
                return False
            # Failed verifier preparation can leave the published default
            # branch detached. Preserve it and prove publication afresh;
            # a stale remote-tracking ref cannot authorize release.
            remote_head = await git._arun_unlocked(
                ["ls-remote", "--exit-code", "origin", "HEAD"], cwd=checkout
            )
            if remote_head.split() != [head, "HEAD"]:
                return False

        if current == expected_branch:
            await git._arun_unlocked(["switch", "--detach", head], cwd=checkout)
        detached = await git._arun_unlocked(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout
        )
        detached_head = await git._arun_unlocked(["rev-parse", "HEAD"], cwd=checkout)
        return detached == "HEAD" and detached_head == head


async def effective_requirements(db, task: Task) -> list[ResolvedRequirement]:
    """The single load-bearing function that turns a task into requirements.

    See spec §6.1.  Pure relative to DB state — same input → same output.
    Output is sorted by ``(kind_id, position)`` for canonical lock order
    (spec §6.3).

    Behavior:

    - If the task has explicit ``task_workspace_requirements`` rows, those are
      used.  ``preferred_workspace_id`` is *not* applied (the explicit caller
      knew what they wanted).
    - Otherwise, if a ``project-repo`` kind resolves for the project, a
      synthesized requirement is added carrying ``task.preferred_workspace_id``.
    - Auto-attach kinds (e.g. ``vault``) for the project are appended unless
      already requested.
    """
    project_id = task.project_id
    explicit_rows = await db.fetch_task_workspace_requirements(task.id)

    base: list[ResolvedRequirement] = []

    if explicit_rows:
        for row in explicit_rows:
            base.append(
                ResolvedRequirement(
                    kind_id=row.kind_id,
                    alias=row.alias,
                    position=row.position,
                    preferred_workspace_id=None,
                )
            )
    else:
        project_repo = await db.resolve_workspace_kind(project_id, "project-repo")
        if project_repo is not None:
            base.append(
                ResolvedRequirement(
                    kind_id="project-repo",
                    alias=None,
                    position=0,
                    preferred_workspace_id=task.preferred_workspace_id,
                )
            )

    explicit_kind_ids = {r.kind_id for r in base}
    auto_attach = await db.list_auto_attach_kinds_for_project(project_id)
    for idx, kind in enumerate(auto_attach):
        if kind.id in explicit_kind_ids:
            continue
        base.append(
            ResolvedRequirement(
                kind_id=kind.id,
                alias=None,
                position=_AUTO_ATTACH_POSITION_BASE + idx,
                preferred_workspace_id=None,
            )
        )

    base.sort(key=lambda r: (r.kind_id, r.position))
    return base


async def acquire_for_task(
    db,
    task: Task,
    agent_id: str,
    *,
    worktrees_enabled: bool = False,
    worktree_slot_cap: int | None = None,
    preferred_workspaces: dict[str, str] | None = None,
) -> WorkspaceAttachmentSet:
    """Acquire all required workspaces for a task.

    See spec §6.2.  All-or-nothing semantics: if any required lockable kind
    has no available instance, every lock acquired so far is released and
    :class:`AcquisitionFailed` is raised.  Non-lockable and auto-attached
    kinds skip locking but still appear in the resulting attachment set.

    There is no read-only acquisition path.  ``profile.read_only`` used to
    attach a lockable kind's *first* workspace without taking a lock, on the
    theory that a reviewer must never own the repo.  What that actually did
    was hand every read-only agent the kind's **base** row — the one
    ``first_workspace_of_kind`` returns, which under worktree mode is the
    registry root and is frequently a human's own checkout (a ``LINK``
    workspace).  The DB lock was skipped but ``_prepare_workspace_locked``
    still wrote ``.agent-queue-lock`` there, so read-only agents serialized
    on the base's sentinel *and* ran their tools inside it.  Read-only means
    "no write intent", not "no isolation": a read-only task now acquires a
    disposable slot exactly like any other task.

    Lock mode resolution: ``task.workspace_mode`` wins over
    ``kind.default_lock_mode`` for *all* lockable kinds when set.  This
    preserves the legacy behavior where a per-task override applied
    uniformly.

    Note: each per-kind lock is acquired in its own DB transaction (the
    underlying ``acquire_one_unlocked`` opens its own ``begin()``).  The
    explicit rollback loop on failure is what enforces all-or-nothing.

    ``worktrees_enabled`` is the rollout gate (worktree-execution §5): while
    it is False every git kind is treated as ``exclusive-clone`` regardless
    of the kind's declared ``mode``, so acquisition behaves exactly as it
    does today.  Canonical lock order and all-or-nothing rollback are
    untouched either way (§6.3).

    ``worktree_slot_cap`` is the project's ``max_concurrent_agents``.  It
    bounds the candidate slot set to indices below the cap, matching the
    bound ``count_available_workspaces`` and ``_ensure_worktree_slots_for_task``
    already apply — otherwise a shrunk cap leaves capacity reporting 0 while
    acquisition still hands out an out-of-cap slot.

    ``preferred_workspaces`` is a ``{kind_id: workspace_id}`` *soft* hint
    computed by the caller — in practice the slot that already has the task's
    branch checked out (worktree-execution §3.4, §6.3).  A released slot
    stays on its last task's branch, so without the hint a retry lands on a
    different slot and ``git switch`` is refused forever.  It is strictly a
    preference: an explicit ``Task.preferred_workspace_id`` always wins, and
    a hinted workspace that is busy or gone falls through to the ordinary
    first-unlocked pick rather than blocking the task behind it.
    """
    requirements = await effective_requirements(db, task)
    acquired: list[WorkspaceAttachment] = []

    # Per-task lock mode override; applies to every lockable kind.  None
    # means "use the kind's default_lock_mode".
    task_mode_override = task.workspace_mode.value if task.workspace_mode else None

    try:
        for req in requirements:
            kind: WorkspaceKind | None = await db.resolve_workspace_kind(
                task.project_id, req.kind_id
            )
            if kind is None:
                raise AcquisitionFailed(req.kind_id)

            if kind.lockable:
                effective_mode = task_mode_override or kind.default_lock_mode
                ws = await db.acquire_one_unlocked(
                    project_id=task.project_id,
                    kind_id=kind.id,
                    mode=effective_mode,
                    locked_by_task_id=task.id,
                    locked_by_agent_id=agent_id,
                    prefer_workspace_id=(
                        req.preferred_workspace_id
                        or (preferred_workspaces or {}).get(req.kind_id)
                    ),
                    kind_mode=(
                        kind.mode
                        if worktrees_enabled and kind.is_git_repo
                        else None
                    ),
                    worktree_slot_cap=worktree_slot_cap,
                )
                if ws is None:
                    raise AcquisitionFailed(req.kind_id)
            else:
                ws = await db.first_workspace_of_kind(
                    project_id=task.project_id, kind_id=kind.id
                )
                if ws is None:
                    # Auto-attach kinds (e.g. vault) skip silently when no
                    # workspace exists for the project — they're best-effort
                    # by design.  Explicitly requested non-lockable kinds
                    # still fail loudly so the operator notices.
                    if kind.auto_attach:
                        continue
                    raise AcquisitionFailed(req.kind_id)

            acquired.append(
                WorkspaceAttachment(requirement=req, workspace=ws, kind=kind)
            )

        return WorkspaceAttachmentSet(attachments=acquired)
    except Exception:
        # Roll back any locks we managed to take.  Lock release is idempotent.
        for att in acquired:
            if att.lockable:
                await db.release_workspace(att.workspace.id)
        raise


# Re-export for callers that just want the constants/types.
__all__ = [
    "SYSTEM_KIND_SCOPE",
    "AcquisitionFailed",
    "acquire_for_task",
    "effective_requirements",
]
