"""Compute the workspace requirement set for a task and acquire its workspaces.

See spec §6.  ``effective_requirements`` materializes defaults and auto-attach
kinds so downstream code reads a single, canonically-sorted list.
``acquire_for_task`` (added in a follow-on commit) takes that list and atomically
acquires every required lock.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import select, update

from src.database.tables import integration_branch_owners, workspaces
from src.models import (
    ResolvedRequirement,
    SYSTEM_KIND_SCOPE,
    Task,
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
    """Atomically record detach proof and release the exact old DB lock."""
    async with db.immediate() as conn:
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
            or workspace_row is None
            or owner_row["fence_token"] != owner.get("fence_token")
            or owner_row["owner_id"] != owner.get("owner_id")
            or owner_row["handoff_state"] != "handoff_pending"
            or owner_row["session_id"] != owner.get("session_id")
            or owner_row["workspace_id"] != workspace.id
            or workspace_row["locked_by_task_id"] != task_id
        ):
            return False

        released_owner = await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.id == owner_row["id"],
                integration_branch_owners.c.fence_token == owner_row["fence_token"],
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
        released_workspace = await conn.execute(
            update(workspaces)
            .where(
                workspaces.c.id == workspace.id,
                workspaces.c.locked_by_task_id == task_id,
            )
            .values(
                locked_by_agent_id=None,
                locked_by_task_id=None,
                locked_at=None,
                lock_mode=None,
            )
        )
        if released_owner.rowcount != 1 or released_workspace.rowcount != 1:
            raise RuntimeError("integration handoff release lost its compare-and-swap")
    return True


async def detach_slot_for_integration_handoff(
    db,
    git,
    git_mutex: Callable,
    workspace,
    *,
    expected_branch: str,
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
    )


async def detach_workspace_for_integration_handoff(
    git,
    git_mutex: Callable,
    workspace,
    *,
    mutex_path: str | None = None,
    expected_branch: str,
) -> bool:
    """Prove and detach an exact pushed checkout before releasing its lock."""

    checkout = workspace.workspace_path
    mutex_path = mutex_path or checkout
    branch_ref = f"refs/heads/{expected_branch}"
    remote_ref = f"refs/remotes/origin/{expected_branch}"
    async with git_mutex(mutex_path):
        current = await git._arun_unlocked(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout
        )
        if current not in {expected_branch, "HEAD"}:
            return False
        status = await git._arun_unlocked(["status", "--porcelain"], cwd=checkout)
        if status:
            return False

        await git._arun_unlocked(["fetch", "origin"], cwd=mutex_path)
        local_tip = await git._arun_unlocked(["rev-parse", branch_ref], cwd=checkout)
        remote_tip = await git._arun_unlocked(["rev-parse", remote_ref], cwd=checkout)
        head = await git._arun_unlocked(["rev-parse", "HEAD"], cwd=checkout)
        if not local_tip or local_tip != remote_tip or head != local_tip:
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
    "AcquisitionFailed",
    "SYSTEM_KIND_SCOPE",
    "acquire_for_task",
    "effective_requirements",
]
