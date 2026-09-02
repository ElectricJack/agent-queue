"""Which workspace rows are *base* checkouts, and who may run in one.

Under worktree mode a project's ``project-repo`` kind is one **base** clone
plus N **slot** worktrees (worktree-execution §2.2).  The base exists for
fetch and ``git worktree`` registry bookkeeping — ``WorktreeSlotManager``
owns it.  It is routinely a ``LINK`` row pointing at a human's own working
tree, so it is the one directory in the install that is neither disposable
nor exclusively ours.

Nothing else may run there.  A session launched with the base as its
``work_dir`` gets the harness's full tool surface pointed at the operator's
checkout: on 2026-09-01 that cost a developer every gitignored directory in
``agent-queue2`` (``.venv``, ``node_modules``, generated TS client) and, via
the base's ``.agent-queue-lock`` sentinel, serialized 19 review tasks behind
one checkout while 18 slots sat free.

A base is identified structurally, not by config: **a workspace row that is
not itself a slot and that at least one slot names as its base.**  That is a
fact about the rows, so it stays correct with ``worktrees.enabled: false``
(no slots exist, so no row is a base and nothing is refused) and it cannot
misfire on a plain single-clone install.
"""

from __future__ import annotations

import os

from src.models import Workspace

#: Profile config key that opts a profile into running in a base checkout.
ALLOW_BASE_CHECKOUT = "allow_base_checkout"


def normalize_workspace_path(path: str) -> str:
    """Comparable form of a workspace path — absolute, symlinks resolved."""
    return os.path.realpath(os.path.expanduser(path or ""))


def base_workspace_ids(workspaces: list[Workspace]) -> set[str]:
    """Ids of the rows in *workspaces* that host slot worktrees."""
    return {ws.base_workspace_id for ws in workspaces if ws.is_slot and ws.base_workspace_id}


def base_workspaces(workspaces: list[Workspace]) -> list[Workspace]:
    """The base rows in *workspaces*, in input order."""
    ids = base_workspace_ids(workspaces)
    return [ws for ws in workspaces if ws.id in ids and not ws.is_slot]


async def list_base_workspaces(db, project_id: str | None = None) -> list[Workspace]:
    """Every base workspace row, optionally narrowed to one project.

    One ``list_workspaces`` round trip: the slot rows carry the pointers, so
    the answer is derivable from a single snapshot rather than a query per
    candidate.
    """
    rows = await db.list_workspaces()
    bases = base_workspaces(rows)
    if project_id is not None:
        bases = [ws for ws in bases if ws.project_id == project_id]
    return bases


async def base_workspace_paths(db, project_id: str | None = None) -> dict[str, Workspace]:
    """``{normalized path: base workspace row}``."""
    return {
        normalize_workspace_path(ws.workspace_path): ws
        for ws in await list_base_workspaces(db, project_id)
    }


def profile_allows_base_checkout(profile) -> bool:
    """Whether *profile* has explicitly opted into running in a base."""
    return bool(getattr(profile, ALLOW_BASE_CHECKOUT, False))


async def base_checkout_refusal(db, work_dir: str, profile, *, project_id: str | None = None):
    """The reason to refuse this launch, or ``None`` when it is allowed.

    Fail *open* on a DB error: this guard exists to stop a specific
    misrouting, and an unreadable workspace table must not become a reason
    no session can start at all.
    """
    if not work_dir or profile_allows_base_checkout(profile):
        return None
    try:
        bases = await base_workspace_paths(db, project_id)
    except Exception:  # noqa: BLE001 - pragma: no cover; fail open, see docstring
        return None
    base = bases.get(normalize_workspace_path(work_dir))
    if base is None:
        return None
    return (
        f"refusing to launch in base checkout {base.workspace_path!r} "
        f"(workspace {base.id}): the base is reserved for fetch and "
        f"`git worktree` bookkeeping, and is often the operator's own working "
        f"tree. Give the task a slot worktree, or set "
        f"`{ALLOW_BASE_CHECKOUT}: true` in profile "
        f"'{getattr(profile, 'id', '?')}' if this is deliberate."
    )
