"""The operator's one-shot close: obsolete, no-change or done, from any state.

``aq task close <id> --obsolete|--no-change|--done --reason "..."`` is the
supported way for an operator or supervisor to finish a task no worker will
close: work that was superseded (a duplicate fix that landed another way, a
plan that changed), work that needed no change, or work already done
elsewhere.  Before the obsolete form existed the only tools were a failing
close plus a status edit.  That left the task in COMPLETED with its branch
still publishable, so the development publisher collected it again, parked the
batch, and the task could never be deleted (solid-cascade and crisp-apex,
2026-09-26).  Before the no-change and done forms existed, closing finished
work in a train project took six refused commands, a resume through READY (a
pool worker claimed it within seconds) and a hand-deleted claim record
(azure-beacon-38, 2026-10-13).

The three dispositions differ only in what they record
(:data:`DISPOSITIONS`): ``obsolete`` is ``work_outcome=abandoned``;
``no_change`` is a passing completion with ``work_outcome=no-op``; ``done`` is
a passing completion with ``work_outcome=shipped``.  All three leave nothing
to publish.

:meth:`ObsoleteClose.close` does two things:

1. **The close.**  In one transaction the task moves to COMPLETED from any
   non-terminal status (a stale hold or terminal BLOCKED included) or from
   FAILED, never through READY.  The claim record, workspace locks and agent
   binding a stopped writer left behind are cleared and the claim epoch is
   bumped, so no stale holder can act on the task again.  The task gets the
   disposition's ``work_outcome`` and the ``obsolete`` marker
   (``blocked_state.OBSOLETE_META_KEY``, which names the disposition).  The
   marker is what the development publisher, the train and delivery admission
   honor: the task is never published, and dependents stop waiting for its
   delivery.  The dependents' projection is recomputed in the same
   transaction.
2. **The cleanup** (:meth:`ObsoleteClose.cleanup`, idempotent):

   * every unreleased branch-owner row the task owns goes through the
     ``release-owner`` safety proof (:class:`ObsoleteOwnerRelease`): the writer
     is gone, anything origin lacks is pushed to ``aq/preserved/<row>``, then a
     fenced compare-and-swap releases the row.  A refused proof stays pending
     with its reason;
   * durable integration Subjects keep their own lifecycle; an obsolete task
     cannot mutate a Subject that still owns its work. Historical train and
     development operation records are retained unchanged.

   Whatever is still pending is recorded in the marker's ``cleanup``, and the
   lifecycle sweep retries it (:meth:`ObsoleteClose.retry_pending`) until
   nothing is left.  At that point the task holds nothing and can be deleted
   or archived.

It refuses a task a live session holds, a task with an agent still assigned,
a container with open children, and, in hierarchy/train projects, a child
(whose branch belongs to its parent's collection episode) or a root that is a
member of an open integration batch.  The command
(:meth:`src.commands.session_commands.SessionCommandsMixin._close_task_obsolete`)
stops a live writer through the operator pause before it calls the close, so
the first two refusals only fire when that stop could not be confirmed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import case, delete, insert, select, update

from src.database.queries.blocked_state import OBSOLETE_META_KEY
from src.database.queries.claim_queries import CLAIMED_BY_SESSION_KEY
from src.database.queries.hierarchy_queries import HIERARCHY_MODES, LIVE_SESSION_STATES
from src.database.queries.task_queries import STALE_OPEN_DETAIL_KEY, TransitionResult
from src.database.tables import (
    agents,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_subjects,
    projects,
    sessions,
    task_completion_records,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.finished_owners import stop_confirmer_for
from src.integration.owner_recovery import (
    COLLECTOR_ROLE,
    NOT_FOUND,
    PRESERVED_AND_RELEASED,
    RELEASED,
    OwnerRecovery,
    RecoveryOutcome,
)
from src.models import AgentState, TaskStatus

logger = logging.getLogger(__name__)

#: Transition context of the close, and the audit event's suffix.
CLOSED_OBSOLETE = "closed_obsolete"

#: ``disposition -> (work_outcome, transition context, label)``.  ``obsolete``
#: abandons the work; ``no_change`` and ``done`` also record a passing
#: completion, so the close reads like any other finished task.
DISPOSITIONS: dict[str, tuple[str, str, str]] = {
    "obsolete": ("abandoned", CLOSED_OBSOLETE, "obsolete"),
    "no_change": ("no-op", "closed_no_change", "no change"),
    "done": ("shipped", "closed_done", "done"),
}
#: Statuses a task can be closed obsolete from.  COMPLETED re-runs the cleanup.
_CLOSEABLE_FROM = frozenset(
    {
        TaskStatus.DEFINED,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
        TaskStatus.WAITING_INPUT,
        TaskStatus.WAITING,
        TaskStatus.HIBERNATED,
        TaskStatus.RESUME_PENDING,
        TaskStatus.ASSIGNED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.COMPLETED,
    }
)
#: Metadata a closed obsolete task no longer needs: holds, attention, and the
#: publisher's skip diagnostics for a source it will never publish.
_CLEARED_KEYS = (
    "manual_pause",
    "manual_pause_withholds_children",
    "needs_attention",
    STALE_OPEN_DETAIL_KEY,
    "development_publisher_skip",
)


#: ``(owner_row_id, principal) -> outcome``: the release-owner proof for one row.
ReleaseOwner = Callable[[str, str], Awaitable[RecoveryOutcome]]


class ObsoleteCloseRefused(Exception):
    """The task cannot be closed as obsolete; ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ObsoleteOwnerRelease(OwnerRecovery):
    """``release-owner``'s proof for every unreleased row of an obsolete task.

    The base service takes only rows naming a writer (``attached`` /
    ``handoff_pending``).  An obsolete task's ``reserved`` row is taken
    through the same proof: no live writer, a safe branch, then the fenced
    compare-and-swap.  Nothing else about the proof changes.
    """

    recoverable_states = ("reserved", "attached", "handoff_pending")


def obsolete_owner_release_for(orchestrator: Any) -> ReleaseOwner | None:
    """The daemon's owner-release callable, or ``None`` when it cannot be built."""
    db = getattr(orchestrator, "db", None)
    git = getattr(orchestrator, "git", None)
    confirm_stopped = stop_confirmer_for(orchestrator)
    if db is None or git is None or confirm_stopped is None:
        return None
    service = ObsoleteOwnerRelease(
        db,
        git,
        getattr(orchestrator, "_git_mutex", None),
        confirm_stopped=confirm_stopped,
    )

    async def release(owner_row_id: str, principal: str) -> RecoveryOutcome:
        return await service.recover(owner_row_id, principal=principal)

    return release


class ObsoleteClose:
    """Close superseded work and release its branch owners and batch membership."""

    def __init__(
        self,
        db,
        *,
        release_owner: ReleaseOwner | None,
        git_manager=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.release_owner = release_owner
        self.git = git_manager
        self.clock = clock

    # -- the close --------------------------------------------------------------

    async def close(
        self,
        task_id: str,
        *,
        reason: str,
        principal: str,
        disposition: str = "obsolete",
        _prepare_on: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        """Close *task_id* with *disposition*, then run its cleanup.

        Raises :class:`ObsoleteCloseRefused` when the task cannot be closed.
        """
        if disposition not in DISPOSITIONS:
            raise ObsoleteCloseRefused(
                "obsolete.invalid_disposition",
                f"disposition must be one of {sorted(DISPOSITIONS)}",
            )
        work_outcome, context, label = DISPOSITIONS[disposition]
        flag = "--" + disposition.replace("_", "-")
        reason = (reason or "").strip()
        if not reason:
            raise ObsoleteCloseRefused(
                "obsolete.reason_required", f"{flag} requires --reason"
            )
        task = await self.db.get_task(task_id)
        if task is None:
            raise ObsoleteCloseRefused("obsolete.not_found", f"no task {task_id!r}")
        if task.status not in _CLOSEABLE_FROM:
            raise ObsoleteCloseRefused(
                "obsolete.invalid_status", f"task {task_id} is {task.status.value}"
            )
        async with self.db.immediate() as conn:
            # Internal recovery seam: acquire exclusion and settle an exactly
            # proven Subject in this same transaction. A later refusal rolls
            # back that settlement along with the obsolete close.
            settlement = await _prepare_on(conn) if _prepare_on is not None else None
            row = (
                (
                    await conn.execute(
                        select(tasks).where(tasks.c.id == task_id).with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            await self._refuse_unsafe(conn, row)
            previous = row["status"]
            existing = await self._marker(conn, task_id)
            closed_at = self.clock()
            marker = existing or {
                "reason": reason,
                "disposition": disposition,
                "closed_by": principal,
                "closed_at": closed_at,
                "previous_status": previous,
                "cleanup": {"state": "pending"},
            }
            await self.db._upsert_meta(task_id, OBSOLETE_META_KEY, marker, conn=conn)
            if existing is None:
                await self.db._upsert_meta(task_id, "work_outcome", work_outcome, conn=conn)
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key.in_(_CLEARED_KEYS),
                )
            )
            released = await self._release_stopped_claim(conn, row)
            if existing is None and disposition != "obsolete":
                await conn.execute(
                    insert(task_completion_records).values(
                        id=f"{context}:{task_id}:{closed_at}",
                        task_id=task_id,
                        outcome="pass",
                        work_outcome=work_outcome,
                        branch=row["branch_name"],
                        commits="[]",
                        summary=reason,
                        notes=json.dumps(
                            {"authority": principal, "disposition": disposition}
                        ),
                        completed_at=closed_at,
                    )
                )
            if previous != TaskStatus.COMPLETED.value:
                result = await self.db._apply_transition(
                    conn,
                    task_id,
                    TaskStatus.COMPLETED,
                    context=context,
                    force=True,
                    _manual_pause_control=True,
                    _obsolete_supersession_token=settlement if disposition == "obsolete" else None,
                    assigned_agent_id=None,
                    resume_after=None,
                    extra_values={"claim_epoch": tasks.c.claim_epoch + 1},
                )
            else:
                # Already COMPLETED: the marker alone changes what its
                # dependents wait for, so recompute them here.
                affected = await self.db._collect_affected({task_id}, conn)
                result = TransitionResult(
                    flipped=await self.db.recompute_blocked(affected, conn=conn)
                )
                result.ready = [
                    (tid, "unblocked")
                    for tid in await self.db._note_frontier_entry(
                        conn, result.flipped, reason="unblocked"
                    )
                ]
            if existing is None:
                from src.integration.branch_retirement import request_task_retirement_on

                await request_task_retirement_on(
                    conn, task_id, request_id=f"{disposition}:{task_id}:{closed_at}",
                    reason=f"closed {label}: {reason}",
                )
                await self.db.log_event(
                    f"task.{context}",
                    project_id=row["project_id"],
                    task_id=task_id,
                    payload=json.dumps(
                        {
                            "reason": reason,
                            "disposition": disposition,
                            "closed_by": principal,
                            "previous_status": previous,
                            "released": released,
                        }
                    ),
                    conn=conn,
                )
        await self.db.log_blocked_flips(result.flipped)
        await self.db._notify_settled(result.settled)
        await self.db._notify_ready(result.ready)
        cleanup = await self.cleanup(task_id, principal=principal)
        recorded = existing or marker
        return {
            "outcome": "already_obsolete" if existing else "closed",
            "task_id": task_id,
            "disposition": recorded.get("disposition", "obsolete"),
            "previous_status": recorded["previous_status"],
            "reason": recorded["reason"],
            "released": released,
            "settled": list(result.settled),
            "cleanup": cleanup,
        }

    async def _release_stopped_claim(self, conn, row) -> dict[str, Any]:
        """Clear what a stopped writer left on the task, in the close's transaction.

        :meth:`_refuse_unsafe` has already proved no live session works on the
        task, so the claim record (``claimed_by_session``), workspace locks
        and the agent binding are history: a dead session's record is exactly
        what made azure-beacon-38 unclosable.  The claim-epoch bump in the
        transition fences any holder that still believes it owns the claim.
        """
        task_id = row["id"]
        claim = await conn.execute(
            delete(task_metadata)
            .where(
                task_metadata.c.task_id == task_id,
                task_metadata.c.key == CLAIMED_BY_SESSION_KEY,
            )
            .returning(task_metadata.c.value)
        )
        holder = claim.scalar_one_or_none()
        unlocked = (
            (
                await conn.execute(
                    update(workspaces)
                    .where(workspaces.c.locked_by_task_id == task_id)
                    .values(locked_by_task_id=None, locked_by_agent_id=None, locked_at=None)
                    .returning(workspaces.c.id)
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            update(agents)
            .where(agents.c.current_task_id == task_id)
            .values(
                current_task_id=None,
                state=case(
                    (agents.c.state == AgentState.BUSY.value, AgentState.IDLE.value),
                    else_=agents.c.state,
                ),
            )
        )
        return {
            "claimed_by_session": json.loads(holder) if holder else None,
            "workspaces": sorted(unlocked),
        }

    async def _refuse_unsafe(self, conn, row) -> None:
        task_id = row["id"]
        mode = (
            await conn.execute(
                select(projects.c.hierarchical_integration_mode).where(
                    projects.c.id == row["project_id"]
                )
            )
        ).scalar_one_or_none()
        if mode in HIERARCHY_MODES:
            if row["parent_task_id"] is not None:
                raise ObsoleteCloseRefused(
                    "obsolete.unsupported_mode",
                    f"project {row['project_id']} integrates in {mode} mode, where a child's "
                    "branch belongs to its parent's collection; abandon it through the "
                    "container close or `aq task archive --disposition obsolete --reason ...` "
                    "instead",
                )
            # A root is the train's own candidate; the marker keeps it out of
            # every later batch, but one already cut must settle first.
            batch_id = await conn.scalar(
                select(integration_batches.c.id)
                .where(
                    integration_batches.c.id.in_(
                        select(integration_batch_members.c.batch_id).where(
                            integration_batch_members.c.task_id == task_id
                        )
                    ),
                    integration_batches.c.intent != "aborted",
                    integration_batches.c.lifecycle != "promoted",
                )
                .limit(1)
            )
            if batch_id is not None:
                raise ObsoleteCloseRefused(
                    "obsolete.open_batch",
                    f"{task_id} is a member of open integration batch {batch_id}; let the "
                    "batch settle (or abort it) first",
                )
        subject_id = await conn.scalar(
            select(integration_subjects.c.id)
            .where(
                integration_subjects.c.task_id == task_id,
                integration_subjects.c.phase != "done",
            )
            .limit(1)
        )
        if subject_id is not None:
            raise ObsoleteCloseRefused(
                "obsolete.live_subject",
                f"integration subject {subject_id} still owns this work",
            )
        live = (
            await conn.execute(
                select(sessions.c.id).where(
                    sessions.c.task_id == task_id,
                    sessions.c.state.in_(LIVE_SESSION_STATES),
                )
            )
        ).scalar_one_or_none()
        if live is not None:
            raise ObsoleteCloseRefused(
                "obsolete.live_session",
                f"session {live} is still working on {task_id}; stop it first, or let its "
                "worker close the task",
            )
        if row["assigned_agent_id"] and row["status"] in (
            TaskStatus.ASSIGNED.value,
            TaskStatus.IN_PROGRESS.value,
        ):
            raise ObsoleteCloseRefused(
                "obsolete.in_flight",
                f"{task_id} is {row['status']} with agent {row['assigned_agent_id']} assigned; "
                "stop it first",
            )
        child = tasks.alias("obsolete_child")
        open_children = (
            (
                await conn.execute(
                    select(child.c.id)
                    .where(
                        child.c.parent_task_id == task_id,
                        child.c.status.notin_(
                            (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
                        ),
                    )
                    .order_by(child.c.id)
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        if open_children:
            raise ObsoleteCloseRefused(
                "obsolete.open_children",
                f"{task_id} has open child(ren): {', '.join(open_children)}; close or "
                "obsolete each first",
            )

    @staticmethod
    async def _marker(conn, task_id: str) -> dict | None:
        raw = (
            await conn.execute(
                select(task_metadata.c.value).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key == OBSOLETE_META_KEY,
                )
            )
        ).scalar_one_or_none()
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    # -- the cleanup ------------------------------------------------------------

    async def cleanup(self, task_id: str, *, principal: str) -> dict[str, Any]:
        """Release what *task_id* still holds; record and return what remains."""
        released: list[dict] = []
        dropped: list[dict] = []
        pending: list[dict] = []
        task = await self.db.get_task(task_id)
        if task is None:
            return {
                "state": "gone",
                "released_owners": [],
                "dropped_batches": [],
                "pending": [],
            }

        for row in await self._owner_rows(task_id):
            if self.release_owner is None:
                pending.append(
                    _pending_owner(
                        row,
                        "owner_recovery_unavailable",
                        "no stop probe in this process",
                    )
                )
                continue
            try:
                outcome = await self.release_owner(row["id"], principal)
            except Exception as exc:
                logger.warning("Owner release of %s failed", row["id"], exc_info=True)
                pending.append(_pending_owner(row, "release_failed", str(exc)))
                continue
            if outcome.outcome in (RELEASED, PRESERVED_AND_RELEASED):
                released.append(
                    {
                        "owner_row_id": row["id"],
                        "ref": row["ref"],
                        "outcome": outcome.outcome,
                        "preserved_ref": outcome.evidence.get("preserved_ref"),
                    }
                )
            elif outcome.reason != NOT_FOUND:
                pending.append(
                    _pending_owner(
                        row, outcome.reason, outcome.evidence.get("detail", "")
                    )
                )

        pr_cleanup = None
        if task.pr_url:
            from src.integration.pr_cleanup import SettledTaskPullRequestClosure

            async with self.db._engine.connect() as conn:
                marker = await self._marker(conn, task_id)
            if self.git is None:
                pr_cleanup = {
                    "outcome": "blocked",
                    "reason": "GitHub cleanup unavailable",
                }
            else:
                pr_cleanup = await SettledTaskPullRequestClosure(self.db, self.git).run(
                    task_id,
                    reason=_pull_request_reason(marker),
                    principal=principal,
                )
            if pr_cleanup["outcome"] == "blocked":
                pending.append(
                    {
                        "kind": "pull_request",
                        "pr_url": task.pr_url,
                        "reason": "pr_cleanup_failed",
                        "detail": pr_cleanup["reason"],
                    }
                )

        state = "pending" if pending else "done"
        summary = {
            "state": state,
            "checked_at": self.clock(),
            "released_owners": released,
            "dropped_batches": dropped,
            "pending": pending,
        }
        if pr_cleanup is not None:
            summary["pr_cleanup"] = pr_cleanup
        await self._record_cleanup(task_id, task.project_id, summary)
        return summary

    async def retry_pending(self, *, principal: str = "lifecycle-sweep") -> list[dict]:
        """Re-run :meth:`cleanup` for every obsolete task whose cleanup is pending."""
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(task_metadata.c.task_id, task_metadata.c.value)
                    .join(tasks, tasks.c.id == task_metadata.c.task_id)
                    .where(task_metadata.c.key == OBSOLETE_META_KEY)
                    .order_by(task_metadata.c.task_id)
                )
            ).all()
        results = []
        for task_id, raw in rows:
            try:
                marker = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(marker, dict)
                or (marker.get("cleanup") or {}).get("state") == "done"
            ):
                continue
            try:
                results.append(
                    {
                        "task_id": task_id,
                        **await self.cleanup(task_id, principal=principal),
                    }
                )
            except Exception:
                logger.exception("Obsolete cleanup retry for %s failed", task_id)
        return results

    async def _owner_rows(self, task_id: str) -> list[dict]:
        owners = integration_branch_owners
        async with self.db._engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(owners)
                        .where(
                            owners.c.owner_id == task_id,
                            owners.c.handoff_state != "released",
                            owners.c.owner_role != COLLECTOR_ROLE,
                        )
                        .order_by(owners.c.repository_id, owners.c.ref)
                    )
                ).mappings()
            ]

    async def _record_cleanup(
        self, task_id: str, project_id: str, summary: dict
    ) -> None:
        async with self.db.immediate() as conn:
            marker = await self._marker(conn, task_id)
            if marker is None:
                return
            before = (marker.get("cleanup") or {}).get("state")
            marker["cleanup"] = summary
            await self.db._upsert_meta(task_id, OBSOLETE_META_KEY, marker, conn=conn)
            if summary["state"] == "done" and before != "done":
                await self.db.log_event(
                    "task.obsolete_cleanup_done",
                    project_id=project_id,
                    task_id=task_id,
                    payload=json.dumps(summary, sort_keys=True, default=str),
                    conn=conn,
                )


def _pull_request_reason(marker: dict) -> str:
    """The note an open pull request is closed with, by disposition."""
    if marker.get("disposition", "obsolete") == "obsolete":
        return f"Superseded/obsolete: {marker['reason']}"
    label = DISPOSITIONS[marker["disposition"]][2]
    return f"Closed as {label}, nothing to publish: {marker['reason']}"


def _pending_owner(row: dict, reason: str, detail: str) -> dict:
    return {
        "kind": "branch_owner",
        "owner_row_id": row["id"],
        "ref": row["ref"],
        "handoff_state": row["handoff_state"],
        "reason": reason,
        "detail": detail,
    }


def _pending_batch(batch: dict, reason: str, detail: str) -> dict:
    return {
        "kind": "development_batch",
        "batch_id": batch["id"],
        "state": batch["state"],
        "reason": reason,
        "detail": detail,
    }
