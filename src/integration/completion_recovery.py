"""Recover missing delivery links without manufacturing review evidence."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from sqlalchemy import or_, select, update

from src.database.tables import (
    integration_branch_owners,
    projects,
    sessions,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState
from src.models import TaskStatus
from src.review_keys import is_review_completion
from src.sessions.provider import SessionHandle

logger = logging.getLogger(__name__)


def schedule_ready_owner_recovery(orch) -> None:
    """Keep network probes off the scheduler and permit only one recovery pass."""
    pending = getattr(orch, "_integration_owner_recovery_task", None)
    if pending is not None and not pending.done():
        return
    if time.monotonic() < getattr(orch, "_integration_owner_reconcile_after", 0):
        return

    async def run():
        try:
            await reconcile_ready_integration_owners(orch)
        except Exception:
            logger.warning("Integration owner recovery pass failed", exc_info=True)
        finally:
            orch._integration_owner_reconcile_after = time.monotonic() + 30

    orch._integration_owner_recovery_task = asyncio.create_task(run())


async def stop_ready_owner_recovery(orch) -> None:
    pending = getattr(orch, "_integration_owner_recovery_task", None)
    if pending is not None:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def reconcile_ready_integration_owners(orch) -> None:
    """Repair ended attachments before reopened tasks retry their slot setup."""
    now = time.monotonic()
    if now < getattr(orch, "_integration_owner_reconcile_after", 0):
        return
    orch._integration_owner_reconcile_after = now + 30
    for project in await orch.db.list_projects():
        if project.hierarchical_integration_mode not in {"hierarchy", "train"}:
            continue
        try:
            released = await reconcile_closed_integration_owners(
                orch, project.id, ready_only=True
            )
            if released:
                logger.info("Recovered ended integration owners for retries: %s", released)
        except Exception:
            logger.warning("Integration owner recovery failed for %s", project.id, exc_info=True)


async def reconcile_closed_integration_owners(
    orch, project_id: str, *, ready_only: bool = False
) -> list[str]:
    """Fence out stopped writers whose ordinary close already freed their slot.

    The provider must confirm termination. SQL locks exclude new assignments
    while the repository mutex protects local detach proof; no network I/O
    runs inside that transaction. Reopened tasks require an ended attempt.
    Reused slots are read-only evidence: never touch their checkout or lock.
    """
    db, git = orch.db, orch.git
    project = await db.get_project(project_id)
    if (
        project is None
        or project.hierarchical_integration_mode not in {"hierarchy", "train"}
        or project.hierarchical_integration_draining
    ):
        return []
    recovered = []
    if not ready_only:
        recovered = await recover_completed_pool_claims(orch, project_id)
    eligible_statuses = ["READY"] if ready_only else ["READY", "COMPLETED", "BLOCKED", "FAILED", "PAUSED"]
    owners = integration_branch_owners
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(owners)
                    .join(tasks, tasks.c.id == owners.c.owner_id)
                    .where(
                        tasks.c.project_id == project_id,
                        owners.c.repository_id == project.integration_repository_id,
                        owners.c.owner_role.in_(["worker", "repair"]),
                        or_(tasks.c.status != "PAUSED", owners.c.owner_role == "repair"),
                        owners.c.handoff_state.in_(["attached", "handoff_pending"]),
                        tasks.c.status.in_(eligible_statuses),
                        tasks.c.assigned_agent_id.is_(None),
                    )
                )
            )
            .mappings()
            .all()
        )
    released = list(recovered)
    for owner in rows:
        session = await db.get_session(owner["session_id"]) if owner["session_id"] else None
        workspace = await db.get_workspace(owner["workspace_id"]) if owner["workspace_id"] else None
        if (
            session is None
            or workspace is None
            or session.state != "stopped"
            or session.desired_state != "stopped"
            or (
                session.task_id != owner["owner_id"]
                and not (session.task_id is None and session.lifecycle == "pool")
            )
            or session.project_id != project_id
            or workspace.project_id != project_id
            or os.path.realpath(session.work_dir) != os.path.realpath(workspace.workspace_path)
        ):
            continue
        # An expired pool claim can still have its original task/workspace
        # attachment. Let the same public confirmer recover that exact
        # stopped writer; it has the stronger CAS that refuses a reused
        # worker, workspace, or successor claim. The older reconciliation
        # below remains for already-released pool claims (task_id is None).
        if session.lifecycle == "pool" and session.task_id == owner["owner_id"]:
            confirmer = getattr(orch, "aconfirm_integration_owner_handoff", None)
            if confirmer is not None:
                try:
                    from src.integration.models import BranchKey, Fence
                    from src.integration.ownership import BranchOwnership

                    ownership = BranchOwnership(db, confirm_handoff=confirmer)
                    confirmed = await ownership.confirm_transfer(Fence(
                        target=BranchKey(repository_id=owner["repository_id"], branch=owner["ref"]),
                        owner_id=owner["owner_id"], token=int(owner["fence_token"]),
                    ))
                    if confirmed["handoff_state"] == "released":
                        released.append(owner["owner_id"])
                        continue
                except Exception:
                    logger.warning(
                        "Could not confirm stopped pool integration owner %s",
                        owner["owner_id"],
                        exc_info=True,
                    )
                    continue
        try:
            provider = orch.session_providers.create(session.provider, orch.config)
            if not await provider.confirm_stopped(
                SessionHandle(
                    name=session.name,
                    provider=session.provider,
                    instance_token=session.instance_token,
                )
            ):
                continue
            path = workspace.workspace_path
            branch = owner["ref"].removeprefix("refs/heads/")
            remote = await git.als_remote_ref(path, branch)
            if remote.state is not RemoteRefState.PRESENT:
                continue
            base = (
                await db.get_workspace(workspace.base_workspace_id)
                if workspace.base_workspace_id
                else None
            )
            async with db.immediate() as conn:
                await db.lock_hierarchy_project(conn, project_id)
                current = (
                    (
                        await conn.execute(
                            select(owners).where(owners.c.id == owner["id"]).with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                ws = (
                    (
                        await conn.execute(
                            select(workspaces)
                            .where(workspaces.c.id == workspace.id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                stopped = (
                    (
                        await conn.execute(
                            select(sessions).where(sessions.c.id == session.id).with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                task = (
                    (
                        await conn.execute(
                            select(tasks).where(tasks.c.id == owner["owner_id"]).with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                ended_attempt = (
                    await conn.execute(
                        select(task_session_attempts.c.id)
                        .where(
                            task_session_attempts.c.session_id == session.id,
                            task_session_attempts.c.task_id == owner["owner_id"],
                            task_session_attempts.c.project_id == project_id,
                            task_session_attempts.c.ended_at.is_not(None),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                current_project = (
                    (await conn.execute(select(projects).where(projects.c.id == project_id)))
                    .mappings()
                    .one()
                )
                if (
                    current is None
                    or ws is None
                    or stopped is None
                    or task is None
                    or current_project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
                    or current_project["hierarchical_integration_draining"]
                    or current_project["integration_repository_id"]
                    != project.integration_repository_id
                    or any(
                        current[k] != owner[k]
                        for k in (
                            "fence_token",
                            "owner_id",
                            "handoff_state",
                            "session_id",
                            "workspace_id",
                            "owner_role",
                            "ref",
                            "repository_id",
                        )
                    )
                    or ws["workspace_path"] != path
                    or stopped["state"] != "stopped"
                    or stopped["desired_state"] != "stopped"
                    or stopped["instance_token"] != session.instance_token
                    or stopped["task_id"] != session.task_id
                    or (session.task_id is None and ended_attempt is None)
                    or task["status"] not in eligible_statuses
                    or (task["status"] == "PAUSED" and current["owner_role"] != "repair")
                    or (task["status"] in {"READY", "PAUSED"} and ended_attempt is None)
                    or task["assigned_agent_id"] is not None
                ):
                    continue
                async with orch._git_mutex(base.workspace_path if base else path):
                    ref = f"refs/heads/{branch}"
                    local_refs = await git._arun_unlocked(
                        ["for-each-ref", "--format=%(refname) %(objectname)", ref], cwd=path
                    )
                    tip = next(
                        (
                            line.partition(" ")[2]
                            for line in local_refs.splitlines()
                            if line.partition(" ")[0] == ref
                        ),
                        None,
                    )
                    # Slot reuse may already have deleted the old local ref.
                    # Absence is safe only with the worktree exclusion below.
                    if tip is not None and tip != remote.oid:
                        continue
                    listing = await git._arun_unlocked(
                        ["worktree", "list", "--porcelain"], cwd=path
                    )
                    attached_paths = []
                    for block in listing.split("\n\n"):
                        lines = block.splitlines()
                        if f"branch {ref}" in lines:
                            attached_paths.extend(
                                line[9:] for line in lines if line.startswith("worktree ")
                            )
                    if any(os.path.realpath(p) != os.path.realpath(path) for p in attached_paths):
                        continue
                    if attached_paths:
                        # A reused slot may prove the old branch absent, but
                        # its current holder must never be detached or unlocked.
                        if ws["locked_by_task_id"] or ws["locked_by_agent_id"]:
                            continue
                        if await git._arun_unlocked(["status", "--porcelain"], cwd=path):
                            continue
                        await git._arun_unlocked(["switch", "--detach", remote.oid], cwd=path)
                        if (
                            await git._arun_unlocked(
                                ["rev-parse", "--abbrev-ref", "HEAD"], cwd=path
                            )
                            != "HEAD"
                        ):
                            continue
                    await conn.execute(
                        update(owners)
                        .where(owners.c.id == owner["id"])
                        .values(
                            handoff_state="released",
                            session_id=None,
                            workspace_id=None,
                            confirmed_workspace_id=workspace.id,
                            updated_at=time.time(),
                        )
                    )
                    released.append(owner["owner_id"])
        except Exception:
            logger.warning(
                "Could not reconcile stopped integration owner %s", owner["owner_id"], exc_info=True
            )
    return released


async def recover_completed_pool_claims(orch, project_id: str) -> list[str]:
    """Public recovery for a terminal pool close interrupted before cleanup.

    This deliberately discovers only attached or handoff-pending owners. A released owner,
    a successor claim, or an ordinary completed task is not evidence that an
    operator may touch a pool session.  The orchestrator method repeats all
    identity, liveness, Git, and release fences before making any change.
    """
    db = orch.db
    project = await db.get_project(project_id)
    if (
        project is None
        or project.hierarchical_integration_mode not in {"hierarchy", "train"}
        or project.hierarchical_integration_draining
        or not project.integration_repository_id
    ):
        return []
    recover = getattr(orch, "arecover_completed_integration_pool_claim", None)
    if recover is None:
        return []
    async with db._engine.connect() as conn:
        candidates = (
            await conn.execute(
                select(tasks.c.id.label("task_id"), sessions.c.id.label("session_id"))
                .join(integration_branch_owners, integration_branch_owners.c.owner_id == tasks.c.id)
                .join(sessions, sessions.c.id == integration_branch_owners.c.session_id)
                .where(
                    tasks.c.project_id == project_id,
                    tasks.c.status == TaskStatus.COMPLETED.value,
                    tasks.c.repo_id == project.integration_repository_id,
                    integration_branch_owners.c.repository_id == project.integration_repository_id,
                    integration_branch_owners.c.owner_role.in_(["worker", "repair"]),
                    integration_branch_owners.c.handoff_state.in_(["attached", "handoff_pending"]),
                    sessions.c.lifecycle == "pool",
                )
            )
        ).mappings().all()
    recovered = []
    for candidate in candidates:
        task = await db.get_task(candidate["task_id"])
        session = await db.get_session(candidate["session_id"])
        if await recover(task, session):
            recovered.append(task.id)
    return recovered


async def recover_completed_pr_links(db, promotion, project_id: str) -> list[str]:
    """An explicit flush repairs exact root PR links lost by older close paths.

    Git reads precede the transaction; compare-and-swap protects against a
    task being reopened or its completion head changing during observation.
    Reviews, gates, branch ownership, and completion status are untouched.
    """
    project = await db.get_project(project_id)
    if (
        project is None
        or project.hierarchical_integration_mode != "train"
        or project.hierarchical_integration_draining
        or not project.integration_repository_id
    ):
        return []
    cp = task_integration_checkpoints
    missing = or_(tasks.c.pr_url.is_(None), tasks.c.pr_url == "")
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        tasks.c.id,
                        tasks.c.branch_name,
                        tasks.c.dedup_key,
                        tasks.c.profile_id,
                        cp.c.checkpoint_sha,
                        cp.c.generation,
                        cp.c.version,
                    )
                    .join(cp, cp.c.task_id == tasks.c.id)
                    .where(
                        tasks.c.project_id == project_id,
                        tasks.c.repo_id == project.integration_repository_id,
                        cp.c.repository_id == project.integration_repository_id,
                        tasks.c.parent_task_id.is_(None),
                        tasks.c.status == TaskStatus.COMPLETED.value,
                        missing,
                    )
                    .order_by(tasks.c.id)
                )
            )
            .mappings()
            .all()
        )
    rows = [
        r
        for r in rows
        if r["branch_name"] and not is_review_completion(r["dedup_key"], r["profile_id"])
    ]
    if not rows:
        return []
    resolved = await promotion._resolve_repository(project.integration_repository_id)
    await promotion._ensure_retained_repository(resolved)
    recovered = []
    # Git observations precede SQL locking: origin materialization takes the
    # project lock before the repository lock, so retaining the latter while
    # waiting for SQL here would deadlock filing and the integration sweep.
    async with promotion.git.arepository_transaction(str(resolved.retained_git_dir)):
        await promotion._fetch_all_heads(resolved.retained_git_dir)
    for row in rows:
        try:
            async with promotion.git.arepository_transaction(str(resolved.retained_git_dir)):
                checkout = str(resolved.retained_git_dir)
                remote = await promotion.git.als_remote_ref(checkout, row["branch_name"])
                if (
                    remote.state is not RemoteRefState.PRESENT
                    or remote.oid != row["checkpoint_sha"]
                ):
                    continue
                url = await promotion.git.afind_open_pr(
                    checkout, row["branch_name"], head_ref=remote.oid, include_workspace_head=False
                )
                if not url:
                    continue
                identity = await promotion.git.aget_pr_identity(checkout, url)
                if (
                    identity.head_oid != remote.oid
                    or identity.head_ref != row["branch_name"]
                    or identity.base_ref != resolved.repo.default_branch
                ):
                    continue
            async with db.immediate() as conn:
                await db.lock_hierarchy_project(conn, project_id)
                current_project = (
                    (await conn.execute(select(projects).where(projects.c.id == project_id)))
                    .mappings()
                    .one()
                )
                if (
                    current_project["hierarchical_integration_mode"] != "train"
                    or current_project["hierarchical_integration_draining"]
                    or current_project["integration_repository_id"]
                    != project.integration_repository_id
                ):
                    break
                current_head = (
                    select(cp.c.task_id)
                    .where(
                        cp.c.task_id == row["id"],
                        cp.c.checkpoint_sha == remote.oid,
                        cp.c.generation == row["generation"],
                        cp.c.version == row["version"],
                    )
                    .exists()
                )
                changed = await conn.execute(
                    update(tasks)
                    .where(
                        tasks.c.id == row["id"],
                        tasks.c.project_id == project_id,
                        tasks.c.repo_id == project.integration_repository_id,
                        tasks.c.branch_name == row["branch_name"],
                        tasks.c.parent_task_id.is_(None),
                        tasks.c.status == TaskStatus.COMPLETED.value,
                        missing,
                        current_head,
                    )
                    .values(pr_url=url)
                )
                if changed.rowcount:
                    recovered.append(row["id"])
        except Exception:
            logger.warning("Could not recover integration PR for %s", row["id"], exc_info=True)
    return recovered
