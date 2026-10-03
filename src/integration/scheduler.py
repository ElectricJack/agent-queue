"""Durable per-project integration sweep scheduling."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import case, delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.integration_schedule_queries import (
    INTEGRATION_LEASE_RENEW_WITHIN_SECONDS,
    INTEGRATION_LEASE_SECONDS,
)
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_promotion_intents,
    integration_repair_operations,
    integration_source_ci,
    messages,
    playbook_artifacts,
    project_integration_leases,
    project_integration_schedules,
    projects,
    repos,
    task_integration_checkpoints,
)
from src.git.manager import GitError, _validate_ref
from src.integration.engine import root_engine_guard

from src.integration.epic_dependencies import dependencies_for, order_members
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.outbox import enqueue_integration_event
from src.integration.repair import RepairService
from src.integration.settling import clear as clear_settling_window, settled
from src.integration.stale_schedule import (
    classify_outstanding_request_on,
    release_outstanding_request_on,
)
from src.models import resolve_integration_mode_with_source
from src.playbooks.artifact_ref import ArtifactRef

ScheduleTrigger = Literal["periodic", "manual"]
logger = logging.getLogger(__name__)

#: An empty frontier keeps no ``integration_batches`` row.  Its seal answers
#: with this id instead, naming the request it consumed, so a caller that
#: hands the seal's ``batch_id`` on -- the root-train playbook releases every
#: empty seal -- is answered from the schedule rather than from a row.
EMPTY_SEAL_PREFIX = "integration-empty:"
_SWEEP_REQUEST = re.compile(r"integration-sweep:(?P<project>.+):(?P<sequence>[1-9][0-9]*)")


def empty_seal_id(request_id: str) -> str:
    """The ``batch_id`` an empty seal of *request_id* answers with."""
    return EMPTY_SEAL_PREFIX + request_id


def empty_seal_request(batch_id: str) -> tuple[str, str] | None:
    """``(project_id, request_id)`` an empty seal's id names, else ``None``."""
    if not batch_id.startswith(EMPTY_SEAL_PREFIX):
        return None
    request_id = batch_id[len(EMPTY_SEAL_PREFIX):]
    match = _SWEEP_REQUEST.fullmatch(request_id)
    return (match["project"], request_id) if match else None


async def request_ended_without_batch_on(conn, project_id: str, request_id: str) -> bool:
    """Whether *project_id*'s sweep request *request_id* ended with no batch row.

    An empty seal consumes its request without a row, and ``stale_schedule``
    may release a request no seal ever answered.  Either way the request was
    minted (its sequence is the schedule's or older), is no longer
    outstanding, and never will be again: ``mark_due`` only mints higher
    sequences.  Nothing can be sealed under it any more.
    """
    match = _SWEEP_REQUEST.fullmatch(request_id)
    if match is None or match["project"] != project_id:
        return False
    schedule = (
        await conn.execute(
            select(
                project_integration_schedules.c.outstanding_request_id,
                project_integration_schedules.c.request_sequence,
            ).where(project_integration_schedules.c.project_id == project_id)
        )
    ).mappings().one_or_none()
    if (
        schedule is None
        or schedule["outstanding_request_id"] == request_id
        or int(match["sequence"]) > int(schedule["request_sequence"])
    ):
        return False
    batch_id = (
        await conn.execute(
            select(integration_batches.c.id).where(
                integration_batches.c.project_id == project_id,
                integration_batches.c.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    return batch_id is None


class IntegrationScheduler:
    """Coalesce periodic and manual triggers into one durable sweep request."""

    DEFAULT_INTERVAL_SECONDS = 300

    def __init__(self, db: Any, *, clock: Callable[[], float] = time.time):
        self.db = db
        self._clock = clock

    async def maintain_lease(self, project_id: str) -> None:
        """Refresh only the outstanding batch's exact authority before dispatch."""
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            schedule = (await conn.execute(
                select(project_integration_schedules)
                .where(project_integration_schedules.c.project_id == project_id)
                .with_for_update()
            )).mappings().one_or_none()
            if schedule is not None:
                await self._maintain_batch_lease_on(conn, project_id, schedule, self._clock())

    async def configure(
        self,
        *,
        project_id: str,
        now: float,
        enabled: bool,
        interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Persist scheduling controls while retaining any outstanding request."""
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("integration schedule interval must be positive")
        async with self.db.immediate() as conn:
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=self.DEFAULT_INTERVAL_SECONDS,
            )
            values: dict[str, Any] = {"enabled": enabled, "updated_at": now}
            if (
                interval_seconds is not None
                and interval_seconds != schedule["interval_seconds"]
            ):
                values["interval_seconds"] = interval_seconds
                values["next_due_at"] = now + interval_seconds
            return await self.db.update_integration_schedule_on(
                conn, project_id=project_id, values=values
            )

    async def mark_due(self, project_id: str, now: float, trigger: str) -> dict[str, Any]:
        """Mark one sweep due, or return the durable request already in flight."""
        if trigger not in {"periodic", "manual"}:
            raise ValueError("integration schedule trigger must be periodic or manual")

        async with self.db.immediate() as conn:
            # The mode check belongs at this mutation boundary.  A command-
            # layer check alone leaves timers, durable replay, and direct
            # service callers able to create the first schedule/request while
            # disabled, observing, hierarchy-only, or draining.
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                (
                    await conn.execute(
                        select(
                            projects.c.hierarchical_integration_mode,
                            projects.c.hierarchical_integration_desired_mode,
                            projects.c.hierarchical_integration_draining,
                        ).where(projects.c.id == project_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                project is None
                or project["hierarchical_integration_mode"] != "train"
                or project["hierarchical_integration_draining"]
            ):
                if project is not None:
                    # Disabling new sweeps must not strand the batch being drained.
                    schedule = (await conn.execute(
                        select(project_integration_schedules)
                        .where(project_integration_schedules.c.project_id == project_id)
                        .with_for_update()
                    )).mappings().one_or_none()
                    if schedule is not None:
                        await self._maintain_batch_lease_on(conn, project_id, schedule, now)
                return {
                    "outcome": "disabled",
                    "project_id": project_id,
                    "request_id": None,
                    "trigger": None,
                    "requested_at": None,
                    "request_sequence": 0,
                    "next_due_at": None,
                }
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=self.DEFAULT_INTERVAL_SECONDS,
            )
            await self._maintain_batch_lease_on(conn, project_id, schedule, now)
            # Before the enabled and settling gates: a request that nothing will
            # ever release must not keep coalescing every later trigger, and a
            # periodic tick is what notices it without an operator.
            schedule, catchup_due = await self._release_stale_request_on(
                conn, project_id, schedule, now, trigger
            )
            if catchup_due:
                return self._result("due", project_id, schedule)
            if trigger == "periodic" and not schedule["enabled"]:
                return self._result("disabled", project_id, schedule)
            if trigger == "periodic" and not await settled(
                conn, project_id=project_id, now=now
            ):
                return {"outcome": "not_due", "project_id": project_id, "reason": "settling"}

            periodic_due = trigger == "periodic" and now >= schedule["next_due_at"]
            if periodic_due:
                interval = int(schedule["interval_seconds"])
                elapsed_boundaries = int((now - schedule["next_due_at"]) // interval) + 1
                next_due_at = schedule["next_due_at"] + elapsed_boundaries * interval
                schedule = await self.db.update_integration_schedule_on(
                    conn,
                    project_id=project_id,
                    values={
                        "next_due_at": next_due_at,
                        "last_observed_window": next_due_at - interval,
                        "updated_at": now,
                    },
                )

            if schedule["outstanding_request_id"] is not None:
                active_batch = (
                    await conn.execute(
                        select(integration_batches.c.id).where(
                            integration_batches.c.project_id == project_id,
                            integration_batches.c.request_id == schedule["outstanding_request_id"],
                            integration_batches.c.lifecycle.in_(
                                (
                                    "sealing",
                                    "sealed",
                                    "building",
                                    "testing",
                                    "repairing",
                                    "human_blocked",
                                    "promoting",
                                    "cleanup_pending",
                                    "promoted",
                                )
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if (
                    active_batch is not None
                    and schedule["catchup_trigger"] is None
                    and (trigger == "manual" or periodic_due)
                ):
                    schedule = await self.db.update_integration_schedule_on(
                        conn,
                        project_id=project_id,
                        values={
                            "catchup_trigger": trigger,
                            "catchup_requested_at": now,
                            "catchup_after_sequence": int(schedule["request_sequence"]),
                            "updated_at": now,
                        },
                    )
                return self._result("coalesced", project_id, schedule)
            if trigger == "periodic" and not periodic_due:
                return self._result("not_due", project_id, schedule)

            sequence = int(schedule["request_sequence"]) + 1
            request_id = f"integration-sweep:{project_id}:{sequence}"
            schedule = await self.db.update_integration_schedule_on(
                conn,
                project_id=project_id,
                values={
                    "request_sequence": sequence,
                    "outstanding_request_id": request_id,
                    "outstanding_trigger": trigger,
                    "outstanding_requested_at": now,
                    "updated_at": now,
                },
            )
            await enqueue_integration_event(
                conn,
                event_id=request_id,
                dedup_key=request_id,
                project_id=project_id,
                event_type="integration.sweep_due",
                payload={"project_id": project_id, "operation_id": request_id},
                available_at=now,
            )
            return self._result("due", project_id, schedule)

    async def _release_stale_request_on(self, conn, project_id, schedule, now, trigger):
        """Release an outstanding request nothing will end; see ``stale_schedule``.

        Returns the schedule to continue with, and whether a recorded catch-up
        became the next request (which is then already due).
        """
        if schedule["outstanding_request_id"] is None:
            return schedule, False
        state = await classify_outstanding_request_on(
            conn, project_id, schedule, now=now, lock=True
        )
        if state.verdict != "stale":
            return schedule, False
        released = await release_outstanding_request_on(
            self.db,
            conn,
            state,
            schedule=schedule,
            now=now,
            released_by=f"integration_scheduler:{trigger}",
            reason=state.reason,
        )
        logger.warning(
            "integration: released stale sweep request %s for %s: %s",
            state.request_id,
            project_id,
            state.reason,
        )
        return released.schedule, released.catchup_request_id is not None

    async def _maintain_batch_lease_on(self, conn, project_id, schedule, now):
        """Keep the active request fenced independently of its next sweep interval."""
        if schedule["outstanding_request_id"] is None:
            return
        lease = (
            (
                await conn.execute(
                    select(project_integration_leases)
                    .where(project_integration_leases.c.project_id == project_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        now = self._clock()
        renew_by = now + INTEGRATION_LEASE_RENEW_WITHIN_SECONDS
        if lease is None or float(lease["expires_at"]) > renew_by:
            return
        batch = (
            (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == lease["batch_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            batch is None
            or batch["project_id"] != project_id
            or batch["request_id"] != schedule["outstanding_request_id"]
            or batch["repository_id"] != lease["repository_id"]
            or lease["owner_id"] != f"sealer-{batch['id']}"
            or batch["lifecycle"]
            not in {"sealed", "building", "testing", "repairing", "human_blocked", "promoting"}
        ):
            return
        # A prepared root intent already freezes project-lease authority. Renew
        # that exact owner/fence; replacing it would orphan a possible remote push.
        bound_intent = (await conn.execute(
            select(integration_promotion_intents).where(
                integration_promotion_intents.c.intent_kind == "root",
                integration_promotion_intents.c.root_batch_id == batch["id"],
                integration_promotion_intents.c.root_candidate_revision == batch["current_revision"],
                integration_promotion_intents.c.state.in_(("prepared", "pushed")),
            )
        )).mappings().one_or_none()
        if bound_intent is not None and (
            bound_intent["project_lease_owner_id"] != lease["owner_id"]
            or int(bound_intent["project_lease_fence_token"]) != int(lease["fence_token"])
        ):
            logger.warning(
                "integration lease renewal refused project=%s batch=%s owner=%s fence=%s "
                "intent=%s: bound root authority differs",
                project_id, batch["id"], lease["owner_id"], lease["fence_token"],
                bound_intent["id"],
            )
            return
        # Locks and authority reads may themselves wait. The guarded renewal
        # must buy a full horizon from this operation, not the poll's timestamp.
        now = self._clock()
        expired = float(lease["expires_at"]) <= now
        recovered = expired and bound_intent is None
        fence = int(lease["fence_token"]) + int(recovered)
        await conn.execute(
            update(project_integration_leases)
            .where(project_integration_leases.c.project_id == project_id)
            .values(heartbeat_at=now, expires_at=now + INTEGRATION_LEASE_SECONDS, fence_token=fence)
        )
        logger.info(
            "integration lease renewed project=%s batch=%s owner=%s fence=%s "
            "remaining_before=%.3fs expires_at=%.3f recovered=%s bound_intent=%s",
            project_id, batch["id"], lease["owner_id"], fence,
            float(lease["expires_at"]) - now, now + INTEGRATION_LEASE_SECONDS,
            recovered, bound_intent["id"] if bound_intent else None,
        )
        if recovered:
            operation_id = (
                await conn.execute(
                    select(integration_repair_operations.c.id).where(
                        integration_repair_operations.c.batch_id == batch["id"],
                        integration_repair_operations.c.state.in_(("active", "escalated")),
                    )
                )
            ).scalar_one_or_none()
            if operation_id is not None:
                event_id = f"integration-sealed:{batch['id']}:lease:{fence}"
                await enqueue_integration_event(
                    conn,
                    event_id=event_id,
                    dedup_key=event_id,
                    project_id=project_id,
                    event_type="integration.sealed",
                    payload={
                        "project_id": project_id,
                        "batch_id": batch["id"],
                        "operation_id": operation_id,
                    },
                    available_at=now,
                )

    @staticmethod
    def _result(outcome: str, project_id: str, schedule: dict[str, Any]) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "project_id": project_id,
            "request_id": schedule["outstanding_request_id"],
            "trigger": schedule["outstanding_trigger"],
            "requested_at": schedule["outstanding_requested_at"],
            "request_sequence": int(schedule["request_sequence"]),
            "next_due_at": float(schedule["next_due_at"]),
        }


class TrainService:
    """Inspect reviewed sources, then seal the compatible frontier atomically."""

    DEFAULT_PAGE_SIZE = 64
    LEASE_SECONDS = INTEGRATION_LEASE_SECONDS

    def __init__(
        self,
        db: Any,
        *,
        default_mode: str = "pull_request",
        page_size: int = DEFAULT_PAGE_SIZE,
        migration_inspector=None,
        delivery_observer=None,
    ) -> None:
        if page_size <= 0:
            raise ValueError("integration train page size must be positive")
        self.db = db
        self.default_mode = default_mode
        self.page_size = page_size
        self.migration_inspector = migration_inspector
        self.delivery_observer = delivery_observer or getattr(db, "_delivery_observer", None)

    async def _observe_root_deliveries(self, project_id, request_id):
        """Ask canonical Git truth before taking the hierarchy lock.

        Include prerequisites even when they have no review, are held, or have
        been archived: delivery and permission to enter a train are separate.
        """
        if self.delivery_observer is None:
            return None
        async with self.db._engine.connect() as conn:
            prior = await self._batch_for_request(conn, project_id, request_id)
            if prior is not None and prior["lifecycle"] != "sealing":
                return None
            if prior is None and await request_ended_without_batch_on(conn, project_id, request_id):
                return None
            repository_id = await conn.scalar(select(projects.c.integration_repository_id).where(
                projects.c.id == project_id,
            ))
            if repository_id is None:
                return None
            ids = set()
            after = None
            while True:
                page = await self.db.eligible_root_page_on(
                    conn, project_id=project_id, repository_id=repository_id,
                    after=after, limit=self.page_size,
                )
                if not page:
                    break
                ids.update(row["task_id"] for row in page)
                after = (page[-1]["task_id"], page[-1]["source_head"])
            edges = await dependencies_for(conn, list(ids))
            ids.update(dependency for dependencies in edges.values() for dependency in dependencies)
        return await self.delivery_observer.observe(ids)

    async def _git_delivered_roots_on(self, conn, view, project_id, repository):
        """Recheck generation, repository, target and the train's exact source.

        A contained completion can answer a dependency without a checkpoint.
        When a checkpoint exists, it must name that same source; a previous
        adoption cannot hide newly checkpointed work. Settlements and empty
        artifacts are not code delivery to the default branch.
        """
        from src.integration.delivery_truth import DeliveryState

        if view is None:
            return set()
        verified = await view.verified_on(conn, view.evidence)
        target_ref = "refs/heads/" + repository["default_branch"].removeprefix("refs/heads/")
        sources = {task_id: proof.source_oid for task_id, proof in verified.items() if (
            proof.state == DeliveryState.CONTAINED
            and proof.request.task_status == "COMPLETED"
            and (proof.request.project_id, proof.request.repository_id, proof.request.target_ref)
            == (project_id, repository["id"], target_ref)
            and view.targets[task_id].repository_url == repository["url"]
        )}
        checkpoint = task_integration_checkpoints
        heads = dict((await conn.execute(
            select(checkpoint.c.task_id, case(
                (checkpoint.c.episode_id.is_not(None), checkpoint.c.verified_sha),
                else_=checkpoint.c.checkpoint_sha,
            )).where(
                checkpoint.c.task_id.in_(sources),
                checkpoint.c.repository_id == repository["id"],
            )
        )).all())
        return {task_id for task_id, source in sources.items()
                if task_id not in heads or heads[task_id] == source}

    @root_engine_guard("project", outcome="busy")
    async def seal(self, project_id: str, request_id: str, now: float) -> dict[str, Any]:
        # Keep the public sealed/empty/busy contract. A frontier that moved
        # while Git was being read gets fresh inspection, never unchecked seal.
        for _ in range(3):
            result = await self._seal_once(project_id, request_id, now)
            if result["outcome"] != "stale":
                return result
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            event_id = f"integration-seal-retry:{request_id}:{uuid4().hex}"
            await enqueue_integration_event(
                conn,
                event_id=event_id,
                dedup_key=event_id,
                project_id=project_id,
                event_type="integration.sweep_due",
                available_at=now + 5,
                payload={"operation_id": request_id},
            )
        return self._result("busy", project_id, request_id, None, None)

    async def _seal_once(self, project_id: str, request_id: str, now: float) -> dict[str, Any]:
        if not project_id.strip() or not request_id.strip():
            raise ValueError("integration seal project and request are required")
        delivery_view = await self._observe_root_deliveries(project_id, request_id)
        inspected = None
        if self.migration_inspector is not None:
            # Git I/O must never hold the hierarchy lock. The transaction below
            # reselects exact reviewed identities before using this observation.
            async with self.db._engine.connect() as read_conn:
                prior = await self._batch_for_request(read_conn, project_id, request_id)
                if prior is not None and prior["lifecycle"] != "sealing":
                    return await self._replay_result(read_conn, prior)
                if prior is None and await request_ended_without_batch_on(
                    read_conn, project_id, request_id
                ):
                    return self._empty_result(project_id, request_id)
                project = await self.db.get_project(project_id)
                preview_delivered = set()
                if delivery_view is not None and project is not None:
                    repository = (await read_conn.execute(select(repos).where(
                        repos.c.id == project.integration_repository_id,
                        repos.c.project_id == project_id,
                    ))).mappings().one_or_none()
                    if repository is not None:
                        preview_delivered = await self._git_delivered_roots_on(
                            read_conn, delivery_view, project_id, repository
                        )
                preview = (
                    await self._eligible_members(
                        read_conn,
                        project_id=project_id,
                        repository_id=project.integration_repository_id,
                        project_mode=project.integration_mode,
                        git_delivered=preview_delivered,
                    )
                    if project is not None
                    else []
                )
            inspected = {}
            for member in preview:
                key = (
                    member["task_id"],
                    member["repository_id"],
                    member["source_base"],
                    member["source_head"],
                )
                inspected[key] = await self.migration_inspector(member)
        if delivery_view is not None and not await delivery_view.fresh():
            return self._result("stale", project_id, request_id, None, None)
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)

            request_batch = await self._batch_for_request(conn, project_id, request_id)
            if request_batch is not None and request_batch["lifecycle"] != "sealing":
                return await self._replay_result(conn, request_batch)
            # Before the lease: a later train holding it must not turn the
            # replay of an empty seal into busy.
            if request_batch is None and await request_ended_without_batch_on(
                conn, project_id, request_id
            ):
                return self._empty_result(project_id, request_id)

            lease = (
                (
                    await conn.execute(
                        select(project_integration_leases)
                        .where(project_integration_leases.c.project_id == project_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if lease is not None and float(lease["expires_at"]) > now:
                return self._result("busy", project_id, request_id, lease["batch_id"], None)

            schedule = (
                (
                    await conn.execute(
                        select(project_integration_schedules)
                        .where(project_integration_schedules.c.project_id == project_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if schedule is None or schedule["outstanding_request_id"] != request_id:
                raise ValueError("integration seal request is not outstanding")

            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            if (
                project is None
                or project["hierarchical_integration_mode"] != "train"
                or project["hierarchical_integration_draining"]
                or not project["integration_repository_id"]
            ):
                raise ValueError("project is not configured for integration trains")
            repository_id = project["integration_repository_id"]
            repository = (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == repository_id,
                        repos.c.project_id == project_id,
                    )
                )
            ).mappings().one_or_none()
            if repository is None:
                raise ValueError("designated integration repository does not exist")
            git_delivered = await self._git_delivered_roots_on(
                conn, delivery_view, project_id, repository
            )

            policy = HierarchicalIntegrationPolicy.model_validate(
                project["hierarchical_integration_policy"]
            )
            boundary = policy.root
            artifact = (
                await conn.execute(
                    select(playbook_artifacts).where(
                        playbook_artifacts.c.artifact_sha256
                        == boundary.route.artifact.artifact_sha256
                    )
                )
            ).mappings().one_or_none()
            artifact_snapshot = boundary.route.artifact.model_dump(mode="json")
            if (
                artifact is None
                or ArtifactRef.from_row(artifact).as_dict() != artifact_snapshot
                or artifact["playbook_id"] != boundary.route.playbook_id
                or artifact["scope"] != boundary.route.scope
                or artifact["scope_identifier"] != boundary.route.scope_identifier
            ):
                raise ValueError("root route artifact identity is not stored and exact")

            if lease is not None:
                batch = (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == lease["batch_id"])
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if (
                    batch is None
                    or batch["lifecycle"] != "sealing"
                    or batch["request_id"] != request_id
                ):
                    raise ValueError("expired integration lease has no resumable batch")
                request_batch = dict(batch)
            elif request_batch is not None:
                batch = request_batch
            else:
                active = (
                    await conn.execute(
                        select(integration_batches).where(
                            integration_batches.c.project_id == project_id,
                            integration_batches.c.lifecycle.in_(
                                (
                                    "sealing",
                                    "sealed",
                                    "building",
                                    "testing",
                                    "repairing",
                                    "human_blocked",
                                    "promoting",
                                    "cleanup_pending",
                                )
                            ),
                        )
                    )
                ).mappings().one_or_none()
                if active is not None:
                    return self._result(
                        "busy", project_id, request_id, active["id"], None
                    )

            members = await self._eligible_members(
                conn,
                project_id=project_id,
                repository_id=repository_id,
                project_mode=project["integration_mode"],
                git_delivered=git_delivered,
            )
            if inspected is not None:
                from src.integration.migration_heads import select_members

                if any(
                    (
                        member["task_id"],
                        member["repository_id"],
                        member["source_base"],
                        member["source_head"],
                    )
                    not in inspected
                    for member in members
                ):
                    return self._result("stale", project_id, request_id, None, None)
                # A dependent cannot ride a batch whose prerequisite was just
                # deferred for a collision, even if it adds no migrations.
                edges = await dependencies_for(conn, [member["task_id"] for member in members])
                delivered = await self.db.delivered_root_task_ids_on(
                    conn,
                    project_id=project_id,
                    repository_id=repository_id,
                )
                delivered |= git_delivered
                members, deferred = select_members(
                    members,
                    inspected,
                    dependencies=edges,
                    delivered=delivered,
                )
                for finding in deferred:
                    await self._record_migration_deferral_on(
                        conn,
                        project_id,
                        request_id,
                        finding,
                        now,
                    )
            policy_snapshot = policy.model_dump(mode="json")
            for member in members:
                member["source_ref"] = self._source_ref(member["source_branch"])
                member["source_ref_retention"] = (
                    "retain"
                    if member["source_branch"] == member["default_branch"]
                    else policy.cleanup.successful_source_refs
                )
            if len({member["source_ref"] for member in members}) != len(members):
                raise ValueError("integration source refs must be unique within a batch")

            if not members:
                if request_batch is not None:
                    raise ValueError("expired non-empty sealing batch lost its frontier")
                # An empty frontier is not a train: consume the request and
                # keep no batch row.  A replay answers from the schedule.
                await self._consume_request(conn, project_id, request_id, now)
                await clear_settling_window(conn, project_id=project_id)
                return self._empty_result(project_id, request_id)

            manifest_digest = self._manifest_digest(members)
            batch_id = (
                request_batch["id"]
                if request_batch is not None
                else self._batch_id(project_id, request_id)
            )
            integration_branch = self._integration_branch(project_id, request_id)
            if request_batch is None:
                await conn.execute(
                    insert(integration_batches).values(
                        id=batch_id,
                        project_id=project_id,
                        repository_id=repository_id,
                        request_id=request_id,
                        trigger=schedule["outstanding_trigger"],
                        source_manifest_digest=manifest_digest,
                        base_sha=members[0]["source_base"],
                        lifecycle="sealing",
                        current_revision=0,
                        integration_branch=integration_branch,
                        policy_snapshot=policy_snapshot,
                        artifact_snapshot=artifact_snapshot,
                        cleanup_state="pending",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await conn.execute(
                    insert(project_integration_leases).values(
                        project_id=project_id,
                        repository_id=repository_id,
                        batch_id=batch_id,
                        owner_id=f"sealer-{batch_id}",
                        fence_token=1,
                        heartbeat_at=now,
                        expires_at=now + self.LEASE_SECONDS,
                    )
                )
            else:
                await conn.execute(
                    delete(integration_batch_members).where(
                        integration_batch_members.c.batch_id == batch_id
                    )
                )
                await conn.execute(
                    update(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .values(
                        repository_id=repository_id,
                        trigger=schedule["outstanding_trigger"],
                        source_manifest_digest=manifest_digest,
                        base_sha=members[0]["source_base"],
                        integration_branch=integration_branch,
                        policy_snapshot=policy_snapshot,
                        artifact_snapshot=artifact_snapshot,
                        updated_at=now,
                    )
                )
                if lease is None:
                    await conn.execute(
                        insert(project_integration_leases).values(
                            project_id=project_id,
                            repository_id=repository_id,
                            batch_id=batch_id,
                            owner_id=f"sealer-{batch_id}",
                            fence_token=1,
                            heartbeat_at=now,
                            expires_at=now + self.LEASE_SECONDS,
                        )
                    )
                else:
                    await conn.execute(
                        update(project_integration_leases)
                        .where(project_integration_leases.c.project_id == project_id)
                        .values(
                            repository_id=repository_id,
                            owner_id=f"sealer-{batch_id}",
                            fence_token=int(lease["fence_token"]) + 1,
                            heartbeat_at=now,
                            expires_at=now + self.LEASE_SECONDS,
                        )
                    )

            for ordinal, member in enumerate(members):
                review = member["review"]
                await conn.execute(
                    insert(integration_batch_members).values(
                        batch_id=batch_id,
                        ordinal=ordinal,
                        task_id=member["task_id"],
                        pr_url=member["pr_url"],
                        repository_id=repository_id,
                        source_base_sha=member["source_base"],
                        reviewed_head_sha=member["source_head"],
                        reviewed_tree_sha=review["reviewed_tree_sha"],
                        source_ref=member["source_ref"],
                        source_ref_retention=member["source_ref_retention"],
                        review_evidence_id=review["id"],
                        review_evidence=review,
                    )
                )

            operation = await RepairService(self.db).reserve_batch_operation_on(
                conn, batch_id, now=now
            )
            await enqueue_integration_event(
                conn,
                event_id=f"integration-sealed:{batch_id}",
                dedup_key=f"integration-sealed:{batch_id}",
                project_id=project_id,
                event_type="integration.sealed",
                payload={
                    "project_id": project_id,
                    "batch_id": batch_id,
                    "operation_id": operation["id"],
                },
                available_at=now,
            )
            await conn.execute(
                update(integration_batches)
                .where(
                    integration_batches.c.id == batch_id,
                    integration_batches.c.lifecycle == "sealing",
                )
                .values(lifecycle="sealed", updated_at=now)
            )
            await clear_settling_window(conn, project_id=project_id)
            return self._result("sealed", project_id, request_id, batch_id, operation["id"])

    async def _record_migration_deferral_on(self, conn, project_id, request_id, finding, now):
        identity = hashlib.sha256(
            f"{request_id}:{finding['task_id']}:{finding['source_head']}".encode()
        ).hexdigest()
        body = (
            f"Deferred {finding['task_id']} (branch {finding['source_branch']}, "
            f"head {finding['source_head']}) from sweep {request_id}: Alembic head collision "
            f"with {finding['conflicts_with']} ({finding['path']}, revision "
            f"{finding['revision']}, down_revision {finding['down_revisions']}). "
            "Rechain the deferred branch after the first member delivers and approve its new "
            "head before the next sweep. The current review approval is preserved."
        )
        logger.warning("integration: %s", body)
        await self.db.log_event(
            "integration.migration_deferred",
            project_id=project_id,
            task_id=finding["task_id"],
            payload=json.dumps({"request_id": request_id, **finding}),
            conn=conn,
        )
        statement = pg_insert(messages).values(
            id=f"migration-deferred-{identity}",
            project_id=project_id,
            from_kind="system",
            from_id="integration_train",
            to_kind="session",
            to_id=f"supervisor-{project_id}",
            subject="Integration migration needs rechaining",
            body=body,
            priority=100,
            created_at=now,
        )
        await conn.execute(statement.on_conflict_do_nothing(index_elements=["id"]))

    async def _eligible_members(
        self,
        conn,
        *,
        project_id: str,
        repository_id: str,
        project_mode: str | None,
        git_delivered: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        after: tuple[str, str] | None = None
        while True:
            page = await self.db.eligible_root_page_on(
                conn,
                project_id=project_id,
                repository_id=repository_id,
                after=after,
                limit=self.page_size,
            )
            if not page:
                break
            reviews = await self.db.latest_exact_reviews_on(conn, page)
            for candidate in page:
                mode, _source = resolve_integration_mode_with_source(
                    candidate["task_integration_mode"],
                    parent_task_mode=None,
                    project_mode=project_mode,
                    default_mode=self.default_mode,
                )
                key = (
                    candidate["task_id"],
                    candidate["repository_id"],
                    candidate["source_base"],
                    candidate["source_head"],
                    int(candidate["generation"]),
                )
                review = reviews.get(key)
                if (
                    mode != "pull_request"
                    or review is None
                    or review["verdict"] != "approved"
                    or not review["evidence"]
                    or review["review_kind"] != candidate["source_kind"]
                    or (
                        candidate["source_kind"] == "parent"
                        and review["evidence"].get("verification_id")
                        != candidate["current_verification_id"]
                    )
                ):
                    continue
                members.append({**candidate, "review": review})
            after = (page[-1]["task_id"], page[-1]["source_head"])
        members.sort(key=lambda row: (row["task_id"], row["source_head"]))
        git_delivered = git_delivered or set()
        members = [member for member in members if member["task_id"] not in git_delivered]
        from src.integration.engine import current_admission

        admission = current_admission()
        if admission is not None:
            if not admission.include_authorized:
                members = [m for m in members if
                           m["review"]["evidence"].get("decision_path") != "authorized_task"]
            if admission.task_kinds and members:
                from src.database.tables import tasks

                allowed = set((await conn.execute(select(tasks.c.id).where(
                    tasks.c.id.in_([m["task_id"] for m in members]),
                    tasks.c.task_type.in_(admission.task_kinds),
                ))).scalars())
                members = [m for m in members if m["task_id"] in allowed]
            if admission.max_members is not None:
                members = members[:admission.max_members]
        project = (await conn.execute(select(projects).where(projects.c.id == project_id))).mappings().one()
        policy_data = project["hierarchical_integration_policy"]
        policy = HierarchicalIntegrationPolicy.model_validate(policy_data) if policy_data else None
        if policy is not None:
            members = [member for member in members if (
                member["review"]["evidence"].get("decision_path") != "authorized_task"
                or (policy.root.admission == "authorized" and
                    member["review"]["evidence"].get("policy_generation")
                    == project["hierarchical_integration_generation"]))]
        if policy is not None and policy.root.repair.source_ci:
            member_ids = [member["task_id"] for member in members]
            records = (await conn.execute(select(integration_source_ci).where(
                integration_source_ci.c.repository_id == repository_id,
                or_(integration_source_ci.c.task_id.in_(member_ids),
                    integration_source_ci.c.repair_task_id.in_(member_ids)),
            ))).mappings().all()
            # A repair cannot bypass a hold, rejected review or changed
            # generation on any source in its chain. Require every linked
            # source to remain an exact eligible member at sealing time.
            eligible = {
                (member["task_id"], member["source_base"], member["source_head"], member["generation"])
                for member in members
            }
            while True:
                blocked = {row["repair_task_id"] for row in records if (
                    row["repair_task_id"] is not None and (
                        row["policy_generation"] != project["hierarchical_integration_generation"]
                        or (row["task_id"], row["source_base"], row["source_head"], row["generation"])
                        not in eligible
                    )
                )}
                retained = {key for key in eligible if key[0] not in blocked}
                if retained == eligible:
                    break
                eligible = retained
            members = [member for member in members if (
                member["task_id"], member["source_base"], member["source_head"], member["generation"]
            ) in eligible]
            exact = {(row["task_id"], row["source_base"], row["source_head"], row["generation"]): row
                     for row in records if row["policy_generation"] == project["hierarchical_integration_generation"]}
            # GitHub runs no PR CI on a conflicting head; under batch conflict
            # scope the batch repair resolves it and candidate CI gates it.
            admissible_states = {"green"}
            if policy.root.repair.conflict_scope == "batch":
                admissible_states.add("conflict")
            green = {member["task_id"] for member in members if (
                exact.get((member["task_id"], member["source_base"], member["source_head"], member["generation"]), {}).get("state") in admissible_states)}
            admitted_ids = set(green)
            # Follow the complete repair chain, so repeated CI repair also
            # delivers/cleans every proven ancestor source rather than only
            # the immediate predecessor of the final green repair.
            while True:
                covered = {member["task_id"] for member in members if (
                    exact.get((member["task_id"], member["source_base"], member["source_head"], member["generation"]), {}).get("repair_task_id") in admitted_ids)}
                if covered <= admitted_ids:
                    break
                admitted_ids.update(covered)
            admitted = []
            for member in members:
                record = exact.get((member["task_id"], member["source_base"], member["source_head"], member["generation"]))
                # A green repair is Git-attested to retain the failed source's
                # ancestry before authorization evidence is recorded. Seat
                # both so normal exact coverage receipts/cleanup include the
                # original PR, not merely the repair's PR.
                if record and member["task_id"] in admitted_ids:
                    admitted.append(member)
            members = admitted
        edges = await dependencies_for(conn, [member["task_id"] for member in members])
        delivered = await self.db.delivered_root_task_ids_on(
            conn, project_id=project_id, repository_id=repository_id
        )
        delivered |= git_delivered
        ordered, deferred = order_members(members, edges, delivered)
        for member in deferred:
            logger.info(
                "integration: deferring %s: declared dependencies cannot be satisfied in this batch",
                member["task_id"],
            )
        return ordered

    async def _batch_for_request(self, conn, project_id: str, request_id: str):
        row = (
            await conn.execute(
                select(integration_batches)
                .where(
                    integration_batches.c.project_id == project_id,
                    integration_batches.c.request_id == request_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def _empty_result(self, project_id: str, request_id: str) -> dict[str, Any]:
        return self._result(
            "empty", project_id, request_id, empty_seal_id(request_id), None
        )

    async def _replay_result(self, conn, batch: dict[str, Any]) -> dict[str, Any]:
        # A row-backed empty seal predates rowless empty seals; it replays as is.
        if batch["lifecycle"] == "empty":
            return self._result(
                "empty", batch["project_id"], batch["request_id"], batch["id"], None
            )
        operation_id = (
            await conn.execute(
                select(integration_repair_operations.c.id).where(
                    integration_repair_operations.c.batch_id == batch["id"]
                )
            )
        ).scalar_one()
        return self._result(
            "sealed",
            batch["project_id"],
            batch["request_id"],
            batch["id"],
            operation_id,
        )

    @staticmethod
    async def _consume_request(conn, project_id: str, request_id: str, now: float) -> None:
        result = await conn.execute(
            update(project_integration_schedules)
            .where(
                project_integration_schedules.c.project_id == project_id,
                project_integration_schedules.c.outstanding_request_id == request_id,
            )
            .values(
                outstanding_request_id=None,
                outstanding_trigger=None,
                outstanding_requested_at=None,
                catchup_trigger=None,
                catchup_requested_at=None,
                catchup_after_sequence=None,
                last_completed_sweep_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise ValueError("integration seal request changed during sealing")

    @staticmethod
    def _batch_id(project_id: str, request_id: str) -> str:
        digest = hashlib.sha256(f"{project_id}\0{request_id}".encode()).hexdigest()
        return f"integration-batch-{digest[:32]}"

    @staticmethod
    def _integration_branch(project_id: str, request_id: str) -> str:
        project_digest = hashlib.sha256(project_id.encode()).hexdigest()[:32]
        request_digest = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        return (
            "refs/heads/aq/integration/"
            f"p-{project_digest}/r-{request_digest}"
        )

    @staticmethod
    def _source_ref(branch: str) -> str:
        try:
            _validate_ref(branch, field="integration source branch")
        except GitError as exc:
            raise ValueError("integration source branch identity is invalid") from exc
        return f"refs/heads/{branch}"

    @staticmethod
    def _manifest_digest(members: list[dict[str, Any]]) -> str:
        manifest = [
            {
                "task_id": member["task_id"],
                "repository_id": member["repository_id"],
                "source_base_sha": member["source_base"],
                "reviewed_head_sha": member["source_head"],
                "reviewed_tree_sha": member["review"]["reviewed_tree_sha"],
                "review_evidence_id": member["review"]["id"],
                "source_ref": member["source_ref"],
                "source_ref_retention": member["source_ref_retention"],
            }
            for member in members
        ]
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _result(
        outcome: str,
        project_id: str,
        request_id: str,
        batch_id: str,
        operation_id: str | None,
    ) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "project_id": project_id,
            "request_id": request_id,
            "batch_id": batch_id,
            "operation_id": operation_id,
        }
