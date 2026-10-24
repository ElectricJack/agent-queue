"""Reserve missing root origins and cut pending hierarchical task branches.

The outbox reaches no shipped branch consumer. ``materialize_origin`` cuts the
ref required by the claim frontier and checks ownership on every retry. See
``2026-09-08-task-deletion-with-materialized-branches-design`` §2.1."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select, tuple_

from src.database.tables import (
    integration_branch_owners, projects, task_branch_origins, task_integration_checkpoints,
    task_session_attempts, tasks,
)

logger = logging.getLogger(__name__)

#: Bound pushes per tick; successive ticks drain the backlog.
DEFAULT_LIMIT = 10


class BranchMaterializationService:
    """Cut the reserved-but-missing branches for hierarchy/train projects."""

    def __init__(
        self, db: Any, *, hierarchy_service_factory: Any,
        legacy_container_collection: bool = True, clock=time.time,
    ) -> None:
        self.db = db
        #: Lazily builds ``HierarchyIntegration`` from the handler's repository closures.
        self.hierarchy_service_factory = hierarchy_service_factory
        #: Only the legacy runtime consumes episodes; Git-first collects directly.
        #: An episode there strands the container (grand-lantern-78, quick-current-13).
        self.legacy_container_collection = legacy_container_collection
        self.clock = clock
        #: Both drivers skip concurrent passes; the next cycle/tick retries.
        self._drain_lock = asyncio.Lock()
        self._after_origin = None
        self._after_root = None

    async def _reserve_missing_roots(self, service, *, limit: int) -> None:
        """Backfill a bounded page, advancing past failures before retrying them."""
        from src.integration.hierarchy import ready_root_origin_tasks

        async with self.db._engine.connect() as conn:
            statement = ready_root_origin_tasks().order_by(tasks.c.created_at, tasks.c.id)
            if self._after_root is not None:
                statement = statement.where(tuple_(tasks.c.created_at, tasks.c.id) > self._after_root)
            rows = (await conn.execute(statement.limit(limit))).mappings().all()
            if not rows and self._after_root is not None:
                self._after_root = None
                rows = (await conn.execute(ready_root_origin_tasks().order_by(
                    tasks.c.created_at, tasks.c.id).limit(limit))).mappings().all()
        if rows:
            self._after_root = (rows[-1]["created_at"], rows[-1]["id"])
        for row in rows:
            try:
                async with self.db.immediate() as conn:
                    await service.reserve_ready_root_origin_on(conn, row["id"])
            except Exception:
                logger.warning("Origin reservation for %s failed; will retry", row["id"],
                               exc_info=True)

    async def pending_origins(self, *, limit: int = DEFAULT_LIMIT, after=None) -> list[dict]:
        """Live reservations with no branch, oldest first, in enabled projects."""
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(
                task_branch_origins.c.id, task_branch_origins.c.task_id,
                task_branch_origins.c.repository_id, task_branch_origins.c.created_at,
            ).select_from(task_branch_origins.join(
                tasks, tasks.c.id == task_branch_origins.c.task_id,
            ).join(projects, projects.c.id == tasks.c.project_id)).where(
                task_branch_origins.c.retired_at.is_(None), task_branch_origins.c.reserved.is_(True),
                task_branch_origins.c.materialized.is_(False),
                projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                *([tuple_(task_branch_origins.c.created_at, task_branch_origins.c.id) > after]
                  if after else []),
            ).order_by(task_branch_origins.c.created_at, task_branch_origins.c.id)
             .limit(limit))).mappings().all()
        return [dict(row) for row in rows]

    async def drain_due(self, *, now: float | None = None, limit: int = DEFAULT_LIMIT,
    ) -> list[dict]:
        """Drain reservations, isolating failures for retry on the next tick.

        Re-entrant calls return ``[]`` immediately; another driver is already working."""
        _ = now
        if self._drain_lock.locked():
            return []
        async with self._drain_lock:
            return await self._drain(limit=limit)

    async def _drain(self, *, limit: int) -> list[dict]:
        service = self.hierarchy_service_factory()
        if service is not None:
            await self._reserve_missing_roots(service, limit=limit)
        pending = await self.pending_origins(limit=limit, after=self._after_origin)
        if not pending and self._after_origin is not None:
            self._after_origin = None
            pending = await self.pending_origins(limit=limit, after=None)
        if pending:
            self._after_origin = (pending[-1]["created_at"], pending[-1]["id"])
        if service is None:
            logger.debug("branch materialization: no hierarchy service available")
            return []
        results = []
        for row in pending:
            try:
                outcome = await service.materialize_origin(row["id"])
                results.append({"origin_id": row["id"], "task_id": row["task_id"], **outcome})
                logger.info("Materialized branch aq/%s for reservation %s",
                            row["task_id"], row["id"])
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - reported, retried next tick
                results.append({
                    "origin_id": row["id"], "task_id": row["task_id"], "outcome": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                logger.warning("Branch materialization for aq/%s failed: %s", row["task_id"], exc)
        # An untouched origin checkpoints a container only for the legacy collector.
        # The Git-first train collects it directly; reserve no legacy episode there.
        if not self.legacy_container_collection:
            logger.debug("Legacy container collection is off; the train collects containers")
            return results
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
                       # A claim epoch alone does not prove this task incarnation had
                       # a writer; an attempt predating its creation cannot prove it either.
                       ~select(task_session_attempts.c.id).where(
                           task_session_attempts.c.task_id == tasks.c.id,
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
