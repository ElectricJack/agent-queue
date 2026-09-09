"""Create the child branches a hierarchical project reserved but never cut.

``HierarchyIntegration._reserve_origin`` records a branch reservation and
enqueues ``integration.branch_materialization_pending``; something is then
meant to call :meth:`HierarchyIntegration.materialize_origin`, which actually
cuts the ref and flips ``task_branch_origins.materialized``.

Nothing did.  The outbox dispatches only to playbooks
(``Orchestrator.accept_integration_event``) and no shipped playbook triggers on
that event, so every such event retried forever with "no enabled matching
playbook durably accepted the event" and every reservation stayed pending.
Because ``materialized_origin_when_hierarchical`` gates claiming on that flag,
the affected tasks could never be claimed and never started — on the box where
this was found, 30 tasks across two epics sat READY with idle pool workers
polling them once a minute and failing ``prepare`` with "exact branch origin is
not materialized".

This drain is that missing consumer.  It is a reconciliation pass rather than a
playbook for the reason recorded in
``2026-09-08-task-deletion-with-materialized-branches-design`` §2.1: the outbox
cannot reach anything but playbooks, so branch-level work needs a consumer that
actually runs.  ``materialize_origin`` is idempotent and does its own fence and
ownership checks, so re-entering it after a crash is safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select

from src.database.tables import (
    integration_branch_owners,
    projects,
    task_branch_origins,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
)

logger = logging.getLogger(__name__)

#: Bound the work per tick.  A backlog drains over successive ticks rather than
#: holding the reconciliation loop for one long batch of pushes.
DEFAULT_LIMIT = 10


class BranchMaterializationService:
    """Cut the reserved-but-missing branches for hierarchy/train projects."""

    def __init__(
        self,
        db: Any,
        *,
        hierarchy_service_factory: Any,
        clock=time.time,
    ) -> None:
        self.db = db
        #: Called with no arguments; returns a ``HierarchyIntegration``.  A
        #: factory rather than an instance because the daemon builds that
        #: service lazily, from the command handler's repository closures.
        self.hierarchy_service_factory = hierarchy_service_factory
        self.clock = clock

    async def pending_origins(self, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Live reservations with no branch, oldest first, in enabled projects."""
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            task_branch_origins.c.id,
                            task_branch_origins.c.task_id,
                            task_branch_origins.c.repository_id,
                        )
                        .select_from(
                            task_branch_origins.join(
                                tasks, tasks.c.id == task_branch_origins.c.task_id
                            ).join(projects, projects.c.id == tasks.c.project_id)
                        )
                        .where(
                            task_branch_origins.c.retired_at.is_(None),
                            task_branch_origins.c.reserved.is_(True),
                            task_branch_origins.c.materialized.is_(False),
                            projects.c.hierarchical_integration_mode.in_(
                                ("hierarchy", "train")
                            ),
                        )
                        .order_by(task_branch_origins.c.created_at)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def drain_due(self, *, now: float | None = None, limit: int = DEFAULT_LIMIT) -> list[
        dict
    ]:
        """Materialize each pending reservation; never raise into the loop.

        One reservation's failure (a repository that will not resolve, a lost
        branch ownership) must not stop the rest: the tasks behind them are
        unrelated, and the next tick retries this one anyway.
        """
        _ = now
        pending = await self.pending_origins(limit=limit)
        service = self.hierarchy_service_factory()
        if service is None:
            logger.debug("branch materialization: no hierarchy service available")
            return []
        results = []
        for row in pending:
            try:
                outcome = await service.materialize_origin(row["id"])
                results.append(
                    {"origin_id": row["id"], "task_id": row["task_id"], **outcome}
                )
                logger.info(
                    "Materialized branch aq/%s for reservation %s",
                    row["task_id"], row["id"],
                )
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - reported, retried next tick
                results.append(
                    {
                        "origin_id": row["id"],
                        "task_id": row["task_id"],
                        "outcome": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                logger.warning(
                    "Branch materialization for aq/%s failed: %s", row["task_id"], exc
                )
        # A code-bearing epic can be a container with no producer session.
        # Its untouched materialized origin is its initial checkpoint.
        async with self.db._engine.connect() as conn:
            containers = (await conn.execute(
                select(tasks.c.id).join(task_integration_checkpoints,
                    task_integration_checkpoints.c.task_id == tasks.c.id)
                .join(integration_branch_owners,
                    (integration_branch_owners.c.owner_id == tasks.c.id)
                    & (integration_branch_owners.c.ref == tasks.c.branch_name))
                .where(tasks.c.status.in_(("IN_PROGRESS", "PAUSED")),
                       integration_branch_owners.c.owner_role == "worker",
                       integration_branch_owners.c.handoff_state == "reserved",
                       # ``claim_epoch`` advances when a graph container is
                       # released without a worker.  It is a fence, not proof
                       # that this incarnation ever had a writer.  Conversely,
                       # an attempt from a deleted older task with this id is
                       # not evidence about the current task row.
                       ~select(task_session_attempts.c.id).where(
                           task_session_attempts.c.task_id == tasks.c.id,
                           task_session_attempts.c.project_id == tasks.c.project_id,
                           task_session_attempts.c.started_at >= tasks.c.created_at,
                       ).exists())
                .order_by(tasks.c.created_at, tasks.c.id).limit(limit)
            )).scalars().all()
        for task_id in containers:
            try:
                outcome = await service.bootstrap_container_collection(task_id)
                if outcome["outcome"] == "checkpointed":
                    logger.info("Started collection for container %s", task_id)
            except Exception:
                logger.warning("Container collection startup failed for %s", task_id, exc_info=True)
        return results


__all__ = ["DEFAULT_LIMIT", "BranchMaterializationService"]
