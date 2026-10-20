"""Bring disabled worktree slots back into service — the slot reconciler.

A disabled slot inside a project's cap is an agent the pool can never start:
acquisition skips disabled rows, and lazy growth (``_ensure_worktree_slots``)
counts them as existing, so nothing ever replaces them.  Left alone, a few
disabled slots silently hold a project below ``max_concurrent_agents`` and the
scheduler reports only "no workspace capacity".

Runs from the rate-limited worktree reaper (``_reap_worktree_slots``).  For each
disabled, unlocked slot within the cap:

* **operator** — an operator turned it off; reported, never touched.
* **claim_failures** — left alone for :data:`CLAIM_FAILURE_COOLDOWN` so a
  durable refusal cannot turn the slot over every pass.
* a live process still uses the slot (or liveness is unknowable) — skipped.
* the worktree directory is **missing** — re-created in place, keeping the
  row id, and re-enabled.
* the checkout is **clean** — re-enabled.
* the checkout is **dirty**, or cleanliness cannot be proved — kept disabled
  with a ``quarantined_dirty`` reason; on the transition into quarantine the
  project supervisor is messaged so the work is salvaged, not lost.  A
  quarantined slot is re-enabled on a later pass once it is clean.

Mechanism only: no LLM, no judgment beyond "is anything uncommitted here".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.workspace_disable import (
    CLAIM_FAILURE_COOLDOWN,
    CLAIM_FAILURES,
    ENABLE_FIELDS,
    OPERATOR,
    QUARANTINED_DIRTY,
    disable_fields,
    disabled_slots_in_cap,
    reason_code,
)

logger = logging.getLogger(__name__)

_DIRTY_PATHS_SHOWN = 10


@dataclass(frozen=True)
class SlotRestoreAction:
    """What the reconciler did with one disabled slot."""

    workspace_id: str
    slot_index: int
    action: str  # recreated | reenabled | quarantined | skipped
    detail: str = ""


def _slot_label(ws) -> str:
    return f"slot-{ws.slot_index or 0}"


def _worktree_missing(path: Path) -> bool:
    """True when there is no checkout to inspect: no directory, or an empty one."""
    try:
        return not path.exists() or not any(path.iterdir())
    except OSError:
        return False


async def restore_disabled_slots(
    db,
    mgr,
    project,
    *,
    command_handler: Any = None,
    now: float | None = None,
) -> list[SlotRestoreAction]:
    """Reconcile *project*'s disabled in-cap worktree slots.  See module doc.

    Per-slot failures are logged and reported as ``skipped``; they never
    abort the pass for the remaining slots.
    """
    now = time.time() if now is None else now
    try:
        workspaces = await db.list_workspaces(project_id=project.id)
    except Exception as e:  # noqa: BLE001 — one slot never stops the pass
        logger.warning("slot restore: list_workspaces(%s) failed: %s", project.id, e)
        return []

    from src.workspace_disable import slot_cap

    actions: list[SlotRestoreAction] = []
    for ws in disabled_slots_in_cap(project, workspaces):
        try:
            kind = await db.resolve_workspace_kind(project.id, ws.kind_id or "project-repo")
            if (ws.slot_index or 0) >= slot_cap(project, kind):
                continue
            base = next((row for row in workspaces if row.id == ws.base_workspace_id), None)
            if base is not None and not base.enabled:
                continue
            action = await _reconcile_slot(db, mgr, project, ws, command_handler, now)
        except Exception as e:  # noqa: BLE001 — one slot never stops the pass
            logger.warning("slot restore: %s (%s) failed: %s", ws.id, _slot_label(ws), e)
            action = SlotRestoreAction(ws.id, ws.slot_index or 0, "skipped", str(e))
        actions.append(action)
    return actions


async def _reconcile_slot(db, mgr, project, ws, command_handler, now) -> SlotRestoreAction:
    index = ws.slot_index or 0

    def skipped(detail: str) -> SlotRestoreAction:
        return SlotRestoreAction(ws.id, index, "skipped", detail)

    if ws.locked_by_task_id or ws.locked_by_agent_id:
        return skipped("locked")
    code = reason_code(ws.disabled_reason)
    if code == OPERATOR:
        return skipped("disabled by an operator")
    if code == CLAIM_FAILURES and ws.disabled_at and now - ws.disabled_at < CLAIM_FAILURE_COOLDOWN:
        return skipped("claim-failure cooldown")
    try:
        if await mgr._slot_is_live(ws):
            return skipped("a live process is using it")
    except Exception as e:  # noqa: BLE001 — one slot never stops the pass
        return skipped(f"liveness unknown: {e}")

    path = Path(ws.workspace_path)
    if _worktree_missing(path):
        kind = await db.resolve_workspace_kind(project.id, ws.kind_id) if ws.kind_id else None
        if kind is None:
            return skipped("worktree missing and its workspace kind is unknown")
        await mgr.reprovision_slot(ws, kind)
        await db.update_workspace(ws.id, **ENABLE_FIELDS)
        await _emit_restored(mgr, ws, "recreated")
        logger.info("Re-created missing worktree slot %s at %s", ws.id, path)
        return SlotRestoreAction(ws.id, index, "recreated")

    dirty = await mgr.git.aget_dirty_paths(str(path))
    if dirty == []:
        await db.update_workspace(ws.id, **ENABLE_FIELDS)
        await _emit_restored(mgr, ws, "reenabled")
        logger.info("Re-enabled clean worktree slot %s at %s", ws.id, path)
        return SlotRestoreAction(ws.id, index, "reenabled")

    detail = (
        f"{len(dirty)} uncommitted path(s)" if dirty is not None
        else "could not prove the checkout clean (git status failed)"
    )
    if code == QUARANTINED_DIRTY:
        return skipped(f"still quarantined: {detail}")
    fields = disable_fields(QUARANTINED_DIRTY, detail, now=now)
    await db.update_workspace(ws.id, **fields)
    await mgr._emit(
        "worktree.quarantined",
        {
            "project_id": ws.project_id,
            "workspace_id": ws.id,
            "slot": _slot_label(ws),
            "path": str(path),
            "reason": fields["disabled_reason"],
            "dirty_paths": list(dirty or [])[:_DIRTY_PATHS_SHOWN],
        },
    )
    logger.warning("Quarantined dirty worktree slot %s at %s: %s", ws.id, path, detail)
    await _alert_supervisor(command_handler, project, ws, detail, dirty, ws.disabled_reason)
    return SlotRestoreAction(ws.id, index, "quarantined", detail)


async def _emit_restored(mgr, ws, action: str) -> None:
    await mgr._emit(
        "worktree.restored",
        {
            "project_id": ws.project_id,
            "workspace_id": ws.id,
            "slot": _slot_label(ws),
            "path": ws.workspace_path,
            "action": action,
            "previous_reason": ws.disabled_reason,
        },
    )


async def _alert_supervisor(command_handler, project, ws, detail, dirty, previous) -> None:
    """Tell the project supervisor a slot is held out of the pool for salvage."""
    if command_handler is None:
        return
    shown = list(dirty or [])[:_DIRTY_PATHS_SHOWN]
    lines = [
        f"Worktree {_slot_label(ws)} ({ws.id}) of {project.id} is quarantined: {detail}.",
        f"Path: {ws.workspace_path}",
        f"It was disabled for: {previous or 'no recorded reason'}.",
        (
            "The pool runs one agent short until it is clean. Salvage or discard the "
            "changes; the reconciler re-enables it on its next pass once `git status` "
            "is clean."
        ),
    ]
    if shown:
        lines.append("Uncommitted: " + ", ".join(shown))
        if len(dirty) > len(shown):
            lines.append(f"... and {len(dirty) - len(shown)} more")
    try:
        result = await command_handler.execute("message_send", {
            "project_id": project.id, "from_kind": "system",
            "from_id": "slot-reconciler", "to_kind": "session",
            "to_id": f"supervisor-{project.id}",
            "subject": f"Worktree {_slot_label(ws)} quarantined: {project.id}",
            "body": "\n".join(lines), "archive_after_inject": True,
        })
        if result.get("error") or result.get("success") is False:
            logger.warning(
                "slot quarantine alert for %s not accepted: %s",
                ws.id, result.get("error") or result,
            )
    except Exception as e:  # noqa: BLE001 — one slot never stops the pass
        logger.warning("slot quarantine alert for %s failed: %s", ws.id, e)
