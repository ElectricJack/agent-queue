"""Why a workspace was disabled: the ``workspaces.disabled_reason`` vocabulary.

Every writer that sets ``workspaces.enabled = false`` records a reason
``"<code>: <detail>"`` and a ``disabled_at`` timestamp, so a project quietly
running below its agent cap can say which slots are off and why.  The slot
reconciler (:func:`src.orchestrator.slot_restore.restore_disabled_slots`) reads
the code to decide whether a disabled worktree slot may come back.
"""

from __future__ import annotations

import time

#: An operator (``aq workspace edit --enabled false``) turned it off.  Never
#: restored automatically; reported until the operator re-enables it.
OPERATOR = "operator"
#: The pool drained a session after repeated identical claim failures in this
#: workspace.  Restored once clean, after :data:`CLAIM_FAILURE_COOLDOWN`.
CLAIM_FAILURES = "claim_failures"
#: Task deletion removed the worktree directory.  Recreated in place.
WORKTREE_REMOVED = "worktree_removed"
#: The reconciler found uncommitted work in a disabled slot and kept it off
#: for an operator to salvage.  Restored once the checkout is clean.
QUARANTINED_DIRTY = "quarantined_dirty"
#: Disabled before reasons were recorded.
UNRECORDED = "unrecorded"

#: How long a claim-failure quarantine holds before the reconciler retries the
#: slot.  Long enough that a durable refusal cannot turn the slot over every
#: reaper pass; short enough that a transient one does not cap the project.
CLAIM_FAILURE_COOLDOWN = 15 * 60


def disabled_reason(code: str, detail: str | None = None) -> str:
    """Format a ``disabled_reason`` value."""
    detail = " ".join((detail or "").split())
    return f"{code}: {detail[:500]}" if detail else code


def reason_code(reason: str | None) -> str:
    """The code part of a ``disabled_reason`` (``unrecorded`` for NULL)."""
    if not reason:
        return UNRECORDED
    return reason.split(":", 1)[0].strip() or UNRECORDED


def disable_fields(code: str, detail: str | None = None, *, now: float | None = None) -> dict:
    """Column values that disable a workspace with a recorded reason."""
    return {
        "enabled": False,
        "disabled_reason": disabled_reason(code, detail),
        "disabled_at": time.time() if now is None else now,
    }


#: Column values that re-enable a workspace and clear its reason.
ENABLE_FIELDS = {"enabled": True, "disabled_reason": None, "disabled_at": None}


def slot_cap(project) -> int:
    """A project's worktree slot cap — slot count equals its agent cap.

    Mirrors ``WorkspaceMixin._project_slot_cap`` so the capacity report and
    acquisition agree on which slots count.
    """
    if project is None:
        return 1
    return max(1, getattr(project, "max_concurrent_agents", 1) or 1)


def disabled_slots_in_cap(project, workspaces) -> list:
    """Disabled worktree slots inside *project*'s cap, ordered by index.

    Each one is an agent the pool cannot start: acquisition skips disabled
    rows and lazy growth counts them as existing, so they never get replaced.
    """
    cap = slot_cap(project)
    return sorted(
        (
            ws for ws in workspaces
            if ws.is_slot and not ws.enabled and (ws.slot_index or 0) < cap
        ),
        key=lambda ws: ws.slot_index or 0,
    )


def describe_disabled_slot(ws) -> dict:
    """The JSON shape doctor and stall-sweep use for one disabled slot."""
    return {
        "workspace_id": ws.id,
        "slot_index": ws.slot_index,
        "path": ws.workspace_path,
        "reason_code": reason_code(ws.disabled_reason),
        "reason": ws.disabled_reason,
        "disabled_at": ws.disabled_at,
        "locked_by_task_id": ws.locked_by_task_id,
    }
