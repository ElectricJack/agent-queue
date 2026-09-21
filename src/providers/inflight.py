"""In-flight provider failures: requeue instead of pausing into a dead provider.

``docs/specs/provider-failover.md`` D13, the two in-flight rows (task
``bold-rapids.4``).  Everything here answers one question for the launch
path (:meth:`~src.orchestrator.execution.ExecutionMixin._fail_session_launch`)
and the exit path (:class:`~src.sessions.reconciler.SessionReconciler`):
*this failure -- is it the provider's, and if so what happens to the task?*

A failure is **attributed** to its provider when it carries the provider's
own signal -- a typed startup dialog (``auth``/``usage``), a ``RATE_LIMIT``
exit, a pre-launch refusal -- or when it happens while the provider is
unavailable (already, or tripped by this very evidence).  An attributed
failure never consumes ``retry_count`` and never pauses the task into the
dead provider:

* **tripped** -- the provider is in the unavailable half: the task returns
  to ``READY`` and the ``provider-failover`` sweep moves or holds it (D12);
  launch suppression keeps it off the dead provider in the meantime.
* **suspect** -- the first signal, still awaiting corroboration: the task
  pauses ``launch.suspect_backoff_seconds`` with context
  ``provider_suspect`` and ``task_metadata['provider_pause']`` written in the
  same transaction (D17), which is what lets the next launch confirm or
  clear it -- and what lets the sweep resume it at once if the provider
  trips first.

Unattributed failures, and every failure outside ``mode: enforce``, keep
today's behaviour exactly.

A session that dies **mid-task** also owes its successor the work: before the
workspace is released the daemon commits what is uncommitted as
``aq-wip: provider failover checkpoint``, pushes the branch
(:func:`~src.orchestrator.stranded_work.preserve_unpushed_work` -- never
forced) and leaves a hand-off note (a task comment plus
``task_metadata['provider_failover_handoff']``, rendered by ``aq prime``).
When the push fails nothing is discarded to make a re-route possible: the
caller holds the task instead (:attr:`Checkpoint.at_risk`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from src.providers.availability import (
    EXIT_RATE_LIMIT,
    LAUNCH_FAILURE,
    STARTUP_DIALOG,
    dialog_from_startup_death,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AUTHOR_ID",
    "CONTEXT_RECOVERING",
    "CONTEXT_SUSPECT",
    "CONTEXT_UNAVAILABLE",
    "HANDOFF_META",
    "LAUNCH_REFUSED",
    "PROVIDER_PAUSE_META",
    "PUSH_FAILED_ATTENTION",
    "SESSION_EXIT",
    "SUSPECT",
    "TRIPPED",
    "UNATTRIBUTED",
    "WIP_COMMIT_MESSAGE",
    "Checkpoint",
    "ProviderFailure",
    "build_handoff",
    "checkpoint_workspace",
    "decide",
    "handoff_comment",
    "provider_pause_record",
]

#: The commit a failover checkpoint makes of uncommitted work (D13).
WIP_COMMIT_MESSAGE = "aq-wip: provider failover checkpoint"
#: Written with a provider-caused ``PAUSED`` transition, deleted when the
#: task leaves ``PAUSED`` (``_apply_transition``).  The sweep reads it.
PROVIDER_PAUSE_META = "provider_pause"
#: The hand-off note the next worker's ``aq prime`` shows.
HANDOFF_META = "provider_failover_handoff"
#: ``needs_attention`` when the checkpoint could not be pushed.
PUSH_FAILED_ATTENTION = "provider_failover_push_failed"
#: Who the system-authored hand-off comment is from.
AUTHOR_ID = "system:provider-failover"

#: Transition contexts.
CONTEXT_SUSPECT = "provider_suspect"
CONTEXT_UNAVAILABLE = "provider_unavailable"
CONTEXT_RECOVERING = "provider_recovering"

#: Failure kinds beyond the evidence kinds :mod:`availability` defines.
LAUNCH_REFUSED = "launch_refused"
SESSION_EXIT = "session_exit"
#: Kinds that are the provider's own statement, attributed whatever its state.
_SIGNALLED_KINDS = frozenset({STARTUP_DIALOG, EXIT_RATE_LIMIT, LAUNCH_REFUSED})

#: Dispositions.
UNATTRIBUTED = "unattributed"
TRIPPED = "tripped"
SUSPECT = "suspect"


@dataclass(frozen=True)
class ProviderFailure:
    """A failed launch or a dead session, as structured fields (D2).

    The launch path used to format a :class:`SessionDiedDuringStartup` into
    free text and throw the dialog away; this carries what the provider path
    needs to act on it instead.
    """

    kind: str
    provider: str = ""
    harness: str = ""
    profile_id: str | None = None
    dialog: str | None = None
    signal: str | None = None
    detail: str = ""
    session_id: str | None = None

    @classmethod
    def from_startup_death(
        cls,
        exc: BaseException,
        *,
        provider: str,
        harness: str | None,
        profile_id: str | None = None,
        session_id: str | None = None,
    ) -> ProviderFailure:
        dialog, signal = dialog_from_startup_death(exc)
        return cls(
            kind=STARTUP_DIALOG if signal else LAUNCH_FAILURE,
            provider=provider or "",
            harness=harness or "",
            profile_id=profile_id,
            dialog=dialog,
            signal=signal,
            detail=str(getattr(exc, "detail", "") or "")[:500],
            session_id=session_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


def decide(availability: Any, failure: ProviderFailure | None) -> str:
    """``tripped`` | ``suspect`` | ``unattributed`` for *failure* (D13).

    Read **after** the failure's evidence was recorded, so a death that
    tripped the provider is attributed to it.  Outside ``mode: enforce``
    nothing is attributed: observe mode watches, it changes no outcome.
    """
    if failure is None or availability is None or not failure.provider:
        return UNATTRIBUTED
    if not getattr(availability, "enforcing", False):
        return UNATTRIBUTED
    down = availability.is_unavailable(failure.provider)
    if down:
        return TRIPPED
    if failure.kind in _SIGNALLED_KINDS:
        return SUSPECT
    return UNATTRIBUTED


def provider_pause_record(
    availability: Any,
    failure: ProviderFailure,
    *,
    context: str,
    resume_after: float | None,
    now: float | None = None,
) -> dict[str, Any]:
    """``task_metadata['provider_pause']`` for a provider-caused pause (D17)."""
    at = time.time() if now is None else float(now)
    row = availability.row(failure.provider) if availability is not None else None
    record: dict[str, Any] = {
        "provider": failure.provider,
        "state": availability.effective_state(failure.provider, at)
        if availability is not None
        else "",
        "generation": int(getattr(row, "generation", 0) or 0),
        "context": context,
        "kind": failure.kind,
        "at": at,
        "resume_after": resume_after,
    }
    if failure.signal:
        record["signal"] = failure.signal
    if failure.dialog:
        record["dialog"] = failure.dialog
    return record


# -- the mid-task checkpoint -------------------------------------------------------


@dataclass
class Checkpoint:
    """What preserving a dead session's workspace found and did.

    ``status``:

    * ``pushed`` -- commits (the WIP one included) are now on ``origin``;
    * ``clean`` -- nothing uncommitted, every commit already on a remote;
    * ``not_git`` / ``no_workspace`` -- nothing Git could lose;
    * ``integration_managed`` -- a hierarchy/train branch, whose preservation
      its integration owner governs (nothing is pushed around its fence);
    * ``push_failed`` / ``no_remote`` / ``unknown`` / ``dirty`` -- work
      exists that no remote carries (or whose safety could not be proved).
    """

    status: str
    workspace: str | None = None
    branch: str | None = None
    head: str | None = None
    wip_commit: bool = False
    pushed_branch: str | None = None
    commits: int = 0
    error: str | None = None

    @property
    def at_risk(self) -> bool:
        """True when a re-route would strand work: the caller must hold."""
        return self.status in ("push_failed", "no_remote", "unknown", "dirty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def checkpoint_workspace(
    git: Any,
    workspace: str | None,
    task_id: str,
    *,
    event_bus: Any = None,
    project_id: str | None = None,
) -> Checkpoint:
    """Commit uncommitted work as a WIP checkpoint and push the branch (D13).

    Async :class:`~src.git.manager.GitManager` API only.  Never forces and
    never deletes: the push goes through ``preserve_unpushed_work``, which
    picks ``aq/<task>-wip`` rather than overwrite a remote branch this HEAD
    does not descend from.  Never raises.
    """
    import os

    from src.git.manager import GitError
    from src.orchestrator.stranded_work import preserve_unpushed_work

    if not workspace or not os.path.isdir(workspace):
        return Checkpoint(status="no_workspace", workspace=workspace)
    try:
        if not await git.avalidate_checkout(workspace):
            return Checkpoint(status="not_git", workspace=workspace)
    except Exception as exc:  # a probe failure proves nothing either way
        logger.debug("Task %s: checkout probe failed in %s", task_id, workspace, exc_info=True)
        return Checkpoint(status="unknown", workspace=workspace, error=str(exc))

    result = Checkpoint(status="unknown", workspace=workspace)
    try:
        result.branch = (await git.aget_current_branch(workspace)) or None
    except Exception:
        logger.debug("Task %s: branch unreadable in %s", task_id, workspace, exc_info=True)
        result.branch = None
    try:
        dirty = await git.ahas_uncommitted_changes(workspace, strict=True)
    except Exception as exc:
        logger.debug("Task %s: status unreadable in %s", task_id, workspace, exc_info=True)
        dirty = None
        result.error = str(exc)
    if dirty:
        try:
            result.wip_commit = bool(
                await git.acommit_all(
                    workspace,
                    WIP_COMMIT_MESSAGE,
                    no_verify=True,
                    event_bus=event_bus,
                    project_id=project_id,
                )
            )
        except GitError as exc:
            result.error = f"WIP commit failed: {exc}"
            logger.warning("Task %s: failover WIP commit failed in %s: %s", task_id, workspace, exc)

    work = await preserve_unpushed_work(
        git, workspace, task_id, event_bus=event_bus, project_id=project_id
    )
    result.commits = work.count
    result.head = work.commit or await git.arev_parse(workspace, "HEAD")
    if work.status == "pushed":
        result.status = "pushed"
        result.pushed_branch = work.branch
    elif work.status == "clean":
        result.status = "clean"
    else:
        result.status = work.status
        result.error = work.error or result.error
    if not result.at_risk:
        # Work the WIP commit could not take (a conflicted index, a hook
        # that still ran) is still only in this checkout.
        try:
            still_dirty = await git.ahas_uncommitted_changes(workspace, strict=True)
        except Exception:
            logger.debug("Task %s: status unreadable in %s", task_id, workspace, exc_info=True)
            still_dirty = None
        if still_dirty or (dirty is None and still_dirty is None):
            result.status = "dirty" if still_dirty else "unknown"
    return result


# -- the hand-off note --------------------------------------------------------------


def build_handoff(
    *,
    task: Any,
    session: Any,
    failure: ProviderFailure,
    verdict: str,
    reason: str,
    checkpoint: Checkpoint,
    disposition: str,
    subtasks: list[Mapping[str, Any]] | None = None,
    held: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """``task_metadata['provider_failover_handoff']``: where the last worker stopped."""
    items = [
        {
            "ordinal": item.get("ordinal"),
            "title": item.get("title"),
            "status": item.get("status"),
        }
        for item in (subtasks or [])
    ]
    settled = sum(1 for item in items if item["status"] in ("done", "skipped"))
    session_id = getattr(session, "id", None) or failure.session_id
    return {
        "at": time.time() if now is None else float(now),
        "session_id": session_id,
        "session_logs": f"aq session logs {session_id}" if session_id else None,
        "profile_id": getattr(session, "profile_id", None) or failure.profile_id,
        "provider": failure.provider,
        "harness": getattr(session, "harness", None) or failure.harness,
        "model": getattr(session, "model", None),
        "verdict": verdict,
        "reason": reason,
        "branch": checkpoint.pushed_branch or checkpoint.branch or getattr(task, "branch_name", None),
        "head": checkpoint.head,
        "wip_commit": checkpoint.wip_commit,
        "checkpoint": checkpoint.status,
        "push_error": checkpoint.error if checkpoint.at_risk else None,
        "disposition": "held" if held else disposition,
        "subtasks": {"total": len(items), "settled": settled, "items": items},
    }


def handoff_comment(handoff: Mapping[str, Any], task_id: str) -> str:
    """The task comment carrying the same note, for ``aq task comments`` readers."""
    who = handoff.get("profile_id") or "-"
    model = f", model {handoff['model']}" if handoff.get("model") else ""
    lines = [
        (
            f"Provider failover hand-off: session `{handoff.get('session_id') or '-'}` "
            f"(`{who}`, provider {handoff.get('provider') or '-'}{model}) stopped mid-task "
            f"-- {handoff.get('verdict')}: {handoff.get('reason') or 'no detail'}."
        )
    ]
    branch = handoff.get("branch") or "-"
    head = (handoff.get("head") or "")[:12] or "-"
    wip = (
        f"uncommitted work saved as `{WIP_COMMIT_MESSAGE}`"
        if handoff.get("wip_commit")
        else "no uncommitted work to save"
    )
    status = handoff.get("checkpoint")
    if status == "pushed":
        where = f"pushed to origin/{branch}"
    elif status == "clean":
        where = "already on origin"
    elif status == "integration_managed":
        where = "left to the branch's integration owner"
    elif status in ("not_git", "no_workspace"):
        where = "no Git checkout to preserve"
    else:
        where = f"NOT pushed ({handoff.get('push_error') or status})"
    lines.append(f"Branch `{branch}` at `{head}`: {wip}; {where}.")
    subtasks = handoff.get("subtasks") or {}
    if subtasks.get("total"):
        open_items = [
            f"{item['ordinal']}. {item['title']} ({item['status']})"
            for item in subtasks.get("items", [])
            if item.get("status") not in ("done", "skipped")
        ]
        text = f"Subtasks: {subtasks.get('settled', 0)}/{subtasks['total']} settled"
        if open_items:
            text += "; open: " + "; ".join(open_items[:5])
        lines.append(text + ".")
    if handoff.get("disposition") == "held":
        lines.append(
            "The task is HELD (operator pause) with a local Git checkpoint the next "
            "slot restores: nothing was discarded. Fix the push, then "
            f"`aq task resume {task_id}`."
        )
    elif handoff.get("disposition") == TRIPPED:
        lines.append(
            "The provider is unavailable, so the task went back to the queue for "
            "re-routing (no retry was spent)."
        )
    else:
        lines.append(
            "The provider is suspect, so the task pauses briefly before its next "
            "launch (no retry was spent)."
        )
    if handoff.get("session_logs"):
        lines.append(
            f"Next worker: start from the branch tip; `{handoff['session_logs']}` shows "
            "where the previous session stopped."
        )
    return "\n".join(lines)
