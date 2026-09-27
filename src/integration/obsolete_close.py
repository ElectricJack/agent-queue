"""Close a task as obsolete and release everything it still holds.

``aq task close <id> --obsolete --reason "..."`` is the supported way to retire
work that was superseded (a duplicate fix that landed another way, a plan that
changed).  Before it existed the only tools were a failing close plus a status
edit.  That left the task in COMPLETED with its branch still publishable, so the
development publisher collected it again, parked the batch, and the task could
never be deleted (solid-cascade and crisp-apex, 2026-09-26).

:meth:`ObsoleteClose.close` does two things:

1. **The close.**  In one transaction the task moves to COMPLETED from any
   non-terminal status (a stale hold or terminal BLOCKED included) or from
   FAILED.  It gets ``work_outcome=abandoned`` and the ``obsolete`` marker
   (``blocked_state.OBSOLETE_META_KEY``).  The marker is what the development
   publisher and ``_development_delivery_pending`` honor: the task is never
   published, and dependents stop waiting for its delivery.  The dependents'
   projection is recomputed in the same transaction.
2. **The cleanup** (:meth:`ObsoleteClose.cleanup`, idempotent):

   * every unreleased branch-owner row the task owns goes through the
     ``release-owner`` safety proof (:class:`ObsoleteOwnerRelease`): the writer
     is gone, anything origin lacks is pushed to ``aq/preserved/<row>``, then a
     fenced compare-and-swap releases the row.  A refused proof stays pending
     with its reason;
   * a *parked* development batch that lists the task is cancelled under the
     publisher's lock.  Its other members return to the publisher and are
     batched again without the obsolete source.  A batch still being published
     (``prepared``/``publishing``), an open development repair naming the task,
     and an active train batch are left alone and stay pending.

   Whatever is still pending is recorded in the marker's ``cleanup``, and the
   lifecycle sweep retries it (:meth:`ObsoleteClose.retry_pending`) until
   nothing is left.  At that point the task holds nothing and can be deleted
   or archived.

It refuses a task a live session holds, a task with an agent still assigned,
a container with open children, and hierarchy/train projects, where a child's
branch belongs to its parent's collection episode and the train owns
abandonment.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete, select, update

from src.database.queries.archive_queries import (
    DEVELOPMENT_REPAIR_SOURCES_KEY,
    SETTLED_DEVELOPMENT_DELIVERY_STATES,
)
from src.database.queries.blocked_state import OBSOLETE_META_KEY
from src.database.queries.hierarchy_queries import HIERARCHY_MODES, LIVE_SESSION_STATES
from src.database.queries.task_queries import STALE_OPEN_DETAIL_KEY, TransitionResult
from src.database.tables import (
    development_deliveries,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    projects,
    sessions,
    task_metadata,
    tasks,
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
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Transition context of the close, and the audit event's suffix.
CLOSED_OBSOLETE = "closed_obsolete"
#: Statuses a task can be closed obsolete from.  COMPLETED re-runs the cleanup.
_CLOSEABLE_FROM = frozenset(
    {
        TaskStatus.DEFINED,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
        TaskStatus.WAITING_INPUT,
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
_ACTIVE_TRAIN_LIFECYCLES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
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
        db, git, getattr(orchestrator, "_git_mutex", None), confirm_stopped=confirm_stopped
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.release_owner = release_owner
        self.clock = clock

    # -- the close --------------------------------------------------------------

    async def close(self, task_id: str, *, reason: str, principal: str) -> dict[str, Any]:
        """Close *task_id* as obsolete, then run its cleanup.

        Raises :class:`ObsoleteCloseRefused` when the task cannot be closed.
        """
        reason = (reason or "").strip()
        if not reason:
            raise ObsoleteCloseRefused("obsolete.reason_required", "--obsolete requires --reason")
        task = await self.db.get_task(task_id)
        if task is None:
            raise ObsoleteCloseRefused("obsolete.not_found", f"no task {task_id!r}")
        if task.status not in _CLOSEABLE_FROM:
            raise ObsoleteCloseRefused(
                "obsolete.invalid_status", f"task {task_id} is {task.status.value}"
            )
        async with self.db.immediate() as conn:
            row = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id).with_for_update()))
                .mappings()
                .one()
            )
            await self._refuse_unsafe(conn, row)
            previous = row["status"]
            existing = await self._marker(conn, task_id)
            marker = existing or {
                "reason": reason,
                "closed_by": principal,
                "closed_at": self.clock(),
                "previous_status": previous,
                "cleanup": {"state": "pending"},
            }
            await self.db._upsert_meta(task_id, OBSOLETE_META_KEY, marker, conn=conn)
            await self.db._upsert_meta(task_id, "work_outcome", "abandoned", conn=conn)
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id, task_metadata.c.key.in_(_CLEARED_KEYS)
                )
            )
            if previous != TaskStatus.COMPLETED.value:
                result = await self.db._apply_transition(
                    conn,
                    task_id,
                    TaskStatus.COMPLETED,
                    context=CLOSED_OBSOLETE,
                    force=True,
                    _manual_pause_control=True,
                    assigned_agent_id=None,
                    resume_after=None,
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
                await self.db.log_event(
                    f"task.{CLOSED_OBSOLETE}",
                    project_id=row["project_id"],
                    task_id=task_id,
                    payload=json.dumps(
                        {"reason": reason, "closed_by": principal, "previous_status": previous}
                    ),
                    conn=conn,
                )
        await self.db.log_blocked_flips(result.flipped)
        await self.db._notify_settled(result.settled)
        await self.db._notify_ready(result.ready)
        cleanup = await self.cleanup(task_id, principal=principal)
        return {
            "outcome": "already_obsolete" if existing else "closed",
            "task_id": task_id,
            "previous_status": (existing or marker)["previous_status"],
            "reason": (existing or marker)["reason"],
            "settled": list(result.settled),
            "cleanup": cleanup,
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
            raise ObsoleteCloseRefused(
                "obsolete.unsupported_mode",
                f"project {row['project_id']} integrates in {mode} mode, where a task's branch "
                "belongs to its parent's collection; abandon it through the container close "
                "or `aq task archive --abandon-undelivered` instead",
            )
        live = (
            await conn.execute(
                select(sessions.c.id).where(
                    sessions.c.task_id == task_id, sessions.c.state.in_(LIVE_SESSION_STATES)
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
            return {"state": "gone", "released_owners": [], "dropped_batches": [], "pending": []}

        for row in await self._owner_rows(task_id):
            if self.release_owner is None:
                pending.append(
                    _pending_owner(row, "owner_recovery_unavailable", "no stop probe in this process")
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
                    _pending_owner(row, outcome.reason, outcome.evidence.get("detail", ""))
                )

        repair = await self._open_repair_naming(task_id, task.project_id)
        for batch in await self._unsettled_batches(task_id, task.project_id):
            if batch["state"] != "parked":
                pending.append(
                    _pending_batch(batch, "publishing", "retried after the batch finishes")
                )
            elif repair is not None:
                pending.append(
                    _pending_batch(
                        batch,
                        "repair_in_flight",
                        f"open development repair {repair} lists this task as a source",
                    )
                )
            else:
                verdict = await self._cancel_parked(batch, task_id, principal)
                if verdict is None:
                    dropped.append({"batch_id": batch["id"], "state": "parked"})
                else:
                    pending.append(_pending_batch(batch, *verdict))
        if repair is not None and not any(p.get("reason") == "repair_in_flight" for p in pending):
            pending.append(
                {
                    "kind": "development_repair",
                    "repair_task_id": repair,
                    "reason": "repair_in_flight",
                    "detail": "an open development repair lists this task as a source",
                }
            )
        for batch_id, lifecycle in await self._active_train_batches(task_id):
            pending.append(
                {
                    "kind": "integration_batch",
                    "batch_id": batch_id,
                    "state": lifecycle,
                    "reason": "sealed_batch",
                    "detail": "an active integration batch lists this task",
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
            if not isinstance(marker, dict) or (marker.get("cleanup") or {}).get("state") == "done":
                continue
            try:
                results.append(
                    {"task_id": task_id, **await self.cleanup(task_id, principal=principal)}
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

    async def _unsettled_batches(self, task_id: str, project_id: str) -> list[dict]:
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(development_deliveries)
                        .where(
                            development_deliveries.c.project_id == project_id,
                            development_deliveries.c.state.notin_(
                                SETTLED_DEVELOPMENT_DELIVERY_STATES
                            ),
                        )
                        .order_by(development_deliveries.c.created_at, development_deliveries.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [
            dict(row) for row in rows if self.db._named_task_ids(row["manifest"], {task_id})
        ]

    async def _open_repair_naming(self, task_id: str, project_id: str) -> str | None:
        repair = tasks.alias("obsolete_repair")
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(task_metadata.c.task_id, task_metadata.c.value)
                    .join(repair, repair.c.id == task_metadata.c.task_id)
                    .where(
                        task_metadata.c.key == DEVELOPMENT_REPAIR_SOURCES_KEY,
                        repair.c.project_id == project_id,
                        repair.c.status.notin_(
                            (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
                        ),
                    )
                    .order_by(task_metadata.c.task_id)
                )
            ).all()
        for repair_id, raw in rows:
            try:
                manifest = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if self.db._named_task_ids(manifest, {task_id}):
                return repair_id
        return None

    async def _active_train_batches(self, task_id: str) -> list[tuple[str, str]]:
        async with self.db._engine.connect() as conn:
            return [
                (row[0], row[1])
                for row in await conn.execute(
                    select(integration_batches.c.id, integration_batches.c.lifecycle)
                    .join(
                        integration_batch_members,
                        integration_batch_members.c.batch_id == integration_batches.c.id,
                    )
                    .where(
                        integration_batch_members.c.task_id == task_id,
                        integration_batches.c.lifecycle.in_(_ACTIVE_TRAIN_LIFECYCLES),
                    )
                    .order_by(integration_batches.c.id)
                )
            ]

    async def _cancel_parked(
        self, batch: dict, task_id: str, principal: str
    ) -> tuple[str, str] | None:
        """Cancel one parked batch under the publisher's lock; ``None`` on success."""
        from src.integration.development import DevelopmentBusy, publisher_exclusion

        try:
            async with publisher_exclusion(self.db, batch["repository_id"]):
                now = self.clock()
                async with self.db._engine.begin() as conn:
                    current = (
                        (
                            await conn.execute(
                                select(development_deliveries)
                                .where(development_deliveries.c.id == batch["id"])
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if current is None or current["state"] != "parked":
                        return None  # settled meanwhile: nothing left to drop
                    evidence = {
                        **(current["evidence"] or {}),
                        "released": {
                            "at": now,
                            "conclusion": "obsolete_member",
                            "task_id": task_id,
                            "by": principal,
                        },
                    }
                    await conn.execute(
                        update(development_deliveries)
                        .where(development_deliveries.c.id == batch["id"])
                        .values(state="cancelled", evidence=evidence, updated_at=now)
                    )
                    members = {
                        member["task_id"]
                        for member in current["manifest"] or []
                        if isinstance(member, dict) and member.get("task_id")
                    }
                    flipped = await self.db.recompute_blocked(members, conn=conn)
                    ready = await self.db._note_frontier_entry(conn, flipped, reason="unblocked")
                    await self.db.log_event(
                        "integration.development_batch_released",
                        project_id=current["project_id"],
                        task_id=task_id,
                        payload=json.dumps(
                            {
                                "batch_id": batch["id"],
                                "reason": "obsolete_member",
                                "members": sorted(members),
                            }
                        ),
                        conn=conn,
                    )
        except DevelopmentBusy:
            return "publisher_busy", "the development publisher holds the repository lock"
        await self.db.log_blocked_flips(flipped)
        await self.db._notify_ready([(tid, "unblocked") for tid in ready])
        return None

    async def _record_cleanup(self, task_id: str, project_id: str, summary: dict) -> None:
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
