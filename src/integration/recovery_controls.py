"""Human recovery controls over already-frozen integration work."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from src.database.tables import (
    integration_attestation_publications,
    integration_batches,
    integration_branch_owners,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_cleanup_items,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    tasks,
)
from src.integration.models import RepairPolicy
from src.integration.outbox import enqueue_integration_event


class IntegrationRecoveryControls:
    """Resume, abort, and retry only when no external mutation is ambiguous."""

    def __init__(self, db: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock

    async def resume(self, operation_id: str) -> dict[str, Any]:
        now = self.clock()
        async with self.db.immediate() as conn:
            operation = await self._locked_operation_on(conn, operation_id)
            if operation is None:
                return {"outcome": "not_found", "operation_id": operation_id}
            project_id = await self._project_id_on(conn, operation)
            if operation["state"] != "human_required":
                return self._state_result("invalid_state", operation, project_id)
            blockers = await self._ambiguous_writes_on(conn, operation)
            if blockers:
                return self._ambiguous_result(operation, project_id, blockers)
            stage = await self._locked_stage_on(conn, operation)
            if stage is None or stage["state"] not in {"failed", "expired", "cancelled"}:
                return self._state_result("invalid_state", operation, project_id)
            policy = RepairPolicy.model_validate(stage["policy"])
            timeout = policy.primary_seconds if int(stage["ordinal"]) == 0 else policy.debug_seconds
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                    integration_repair_stages.c.state == stage["state"],
                )
                .values(
                    state="active",
                    attempts=0,
                    started_at=now,
                    deadline_at=now + timeout,
                    deadline_event_id=f"repair-deadline-{operation_id}-resume-{uuid4().hex}",
                    completed_at=None,
                )
            )
            resumed_state = "active" if int(stage["ordinal"]) == 0 else "escalated"
            await conn.execute(
                update(integration_repair_operations)
                .where(
                    integration_repair_operations.c.id == operation_id,
                    integration_repair_operations.c.state == "human_required",
                )
                .values(state=resumed_state, updated_at=now)
            )
            if operation["target_kind"] == "batch":
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == operation["batch_id"],
                        integration_batches.c.lifecycle == "human_blocked",
                    )
                    .values(lifecycle="repairing", human_abort_reason=None, updated_at=now)
                )
            await self._event_on(
                conn, operation, project_id, "integration.repair_exhausted", now
            )
        return {
            "outcome": "resumed",
            "operation_id": operation_id,
            "project_id": project_id,
            "state": resumed_state,
            "stage": int(stage["ordinal"]),
            "deadline_at": now + timeout,
        }

    async def abort(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("abort reason is required")
        now = self.clock()
        async with self.db.immediate() as conn:
            operation = await self._locked_operation_on(conn, operation_id)
            if operation is None:
                return {"outcome": "not_found", "operation_id": operation_id}
            project_id = await self._project_id_on(conn, operation)
            if operation["state"] != "human_required":
                return self._state_result("invalid_state", operation, project_id)
            blockers = await self._ambiguous_writes_on(conn, operation)
            if blockers:
                return self._ambiguous_result(operation, project_id, blockers)
            stage = await self._locked_stage_on(conn, operation)
            await conn.execute(
                update(integration_repair_operations)
                .where(
                    integration_repair_operations.c.id == operation_id,
                    integration_repair_operations.c.state == "human_required",
                )
                .values(state="cancelled", updated_at=now)
            )
            if stage is not None and stage["state"] not in {"passed", "cancelled"}:
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(state="cancelled", completed_at=now)
                )
            if operation["target_kind"] == "batch":
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == operation["batch_id"],
                        integration_batches.c.lifecycle == "human_blocked",
                    )
                    .values(lifecycle="aborted", human_abort_reason=reason, updated_at=now)
                )
        return {
            "outcome": "aborted",
            "operation_id": operation_id,
            "project_id": project_id,
            "reason": reason,
        }

    async def retry_cleanup(self, batch_id: str) -> dict[str, Any]:
        """Requeue existing safe identities without changing any irreversible marker."""
        now = self.clock()
        async with self.db.immediate() as conn:
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if batch is None:
                return {"outcome": "not_found", "batch_id": batch_id}
            rows = (
                await conn.execute(
                    select(integration_cleanup_items)
                    .where(
                        integration_cleanup_items.c.batch_id == batch_id,
                        integration_cleanup_items.c.state.in_(("retryable", "failed")),
                    )
                    .with_for_update()
                )
            ).mappings().all()
            ambiguous = [
                row["domain_key"]
                for row in rows
                if row["irreversible_prewrite_at"] is not None
                or row["execution_nonce"] is not None
            ]
            if ambiguous:
                return {
                    "outcome": "ambiguous",
                    "batch_id": batch_id,
                    "project_id": batch["project_id"],
                    "blockers": sorted(ambiguous),
                }
            if not rows:
                return {
                    "outcome": "nothing_to_retry",
                    "batch_id": batch_id,
                    "project_id": batch["project_id"],
                }
            identities = [row["domain_key"] for row in rows]
            await conn.execute(
                update(integration_cleanup_items)
                .where(integration_cleanup_items.c.domain_key.in_(identities))
                .values(
                    state="retryable",
                    attempts=0,
                    next_attempt_at=now,
                    terminal_at=None,
                    updated_at=now,
                )
            )
        return {
            "outcome": "requeued",
            "batch_id": batch_id,
            "project_id": batch["project_id"],
            "count": len(identities),
        }

    @staticmethod
    async def _locked_operation_on(conn: Any, operation_id: str) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _locked_stage_on(conn: Any, operation: dict[str, Any]) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _project_id_on(conn: Any, operation: dict[str, Any]) -> str:
        if operation["target_kind"] == "batch":
            statement = select(integration_batches.c.project_id).where(
                integration_batches.c.id == operation["batch_id"]
            )
        else:
            statement = select(tasks.c.project_id).where(
                tasks.c.id == operation["parent_task_id"]
            )
        project_id = (await conn.execute(statement)).scalar_one_or_none()
        if project_id is None:
            raise ValueError("operation target has no owning project")
        return str(project_id)

    @staticmethod
    async def _ambiguous_writes_on(
        conn: Any, operation: dict[str, Any]
    ) -> list[str]:
        operation_id = operation["id"]
        statements = {
            "ref_mutation": select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.operation_id == operation_id,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
            "resolution": select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.operation_id == operation_id,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
            "attestation": select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.operation_id == operation_id,
                integration_attestation_publications.c.state == "reserved",
            ),
            "promotion": select(integration_promotion_intents.c.id).where(
                (
                    integration_promotion_intents.c.operation_key == operation_id
                )
                | (
                    integration_promotion_intents.c.resolution_operation_id
                    == operation_id
                ),
                integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")
                ),
            ),
            "writer": select(integration_branch_owners.c.id).where(
                (
                    integration_branch_owners.c.owner_id.in_(
                        select(integration_repair_stages.c.repair_task_id).where(
                            integration_repair_stages.c.operation_id == operation_id,
                            integration_repair_stages.c.repair_task_id.is_not(None),
                        )
                    )
                )
                | (
                    integration_branch_owners.c.owner_id.in_(
                        select(integration_candidate_ref_mutations.c.branch_owner_id).where(
                            integration_candidate_ref_mutations.c.operation_id
                            == operation_id
                        )
                    )
                ),
                integration_branch_owners.c.handoff_state != "released",
            ),
        }
        if operation["batch_id"] is not None:
            statements["candidate_publication"] = select(
                integration_candidate_publications.c.batch_id
            ).where(
                integration_candidate_publications.c.batch_id == operation["batch_id"],
                integration_candidate_publications.c.state != "pr_published",
            )
            statements["cleanup_prewrite"] = select(
                integration_cleanup_items.c.domain_key
            ).where(
                integration_cleanup_items.c.batch_id == operation["batch_id"],
                integration_cleanup_items.c.irreversible_prewrite_at.is_not(None),
                integration_cleanup_items.c.state.in_(("pending", "retryable")),
            )
        blockers = []
        for kind, statement in statements.items():
            if (await conn.execute(statement.limit(1))).scalar_one_or_none() is not None:
                blockers.append(kind)
        return sorted(blockers)

    @staticmethod
    async def _event_on(
        conn: Any,
        operation: dict[str, Any],
        project_id: str,
        event_type: str,
        now: float,
    ) -> None:
        action = event_type.rsplit(".", 1)[-1]
        await enqueue_integration_event(
            conn,
            event_id=f"repair-{action}-{operation['id']}-{uuid4().hex}",
            dedup_key=f"repair-{action}:{operation['id']}:{now}",
            project_id=project_id,
            event_type=event_type,
            payload={"operation_id": operation["id"]},
            available_at=now,
        )

    @staticmethod
    def _state_result(
        outcome: str, operation: dict[str, Any], project_id: str
    ) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "operation_id": operation["id"],
            "project_id": project_id,
            "state": operation["state"],
        }

    @staticmethod
    def _ambiguous_result(
        operation: dict[str, Any], project_id: str, blockers: list[str]
    ) -> dict[str, Any]:
        return {
            "outcome": "ambiguous",
            "operation_id": operation["id"],
            "project_id": project_id,
            "state": operation["state"],
            "blockers": blockers,
        }
