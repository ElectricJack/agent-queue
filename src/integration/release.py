"""Hierarchy-fenced release of a terminal root integration train."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update

from src.database.tables import (
    integration_attestation_publications,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_release_results,
    integration_root_intent_members,
    project_integration_leases,
    project_integration_schedules,
    projects,
    task_delivery_receipts,
)
from src.integration.outbox import enqueue_integration_event


class IntegrationReleaseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "released", "already_released", "empty", "wait", "stale", "invariant_error"
    ]
    project_id: str | None = None
    batch_id: str
    request_id: str | None = None
    catchup_request_id: str | None = None
    operation_id: str | None = None


class _CASLost(RuntimeError):
    pass


class IntegrationReleaseService:
    """Release one shipped train without coupling release to cleanup progress."""

    def __init__(self, db: Any):
        self.db = db

    async def release(self, batch_id: str, now: float) -> IntegrationReleaseResult:
        async with self.db._engine.connect() as conn:
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
        if project_id is None:
            return IntegrationReleaseResult(outcome="stale", batch_id=batch_id)
        try:
            return await self._release_locked(str(project_id), batch_id, now)
        except _CASLost:
            return await self._canonical_replay(str(project_id), batch_id)

    async def _release_locked(
        self, project_id: str, batch_id: str, now: float
    ) -> IntegrationReleaseResult:
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if project is None or batch is None or batch["project_id"] != project_id:
                return IntegrationReleaseResult(
                    outcome="stale", project_id=project_id, batch_id=batch_id
                )
            release_result = await self._one_for_update(
                conn,
                select(integration_release_results).where(
                    integration_release_results.c.batch_id == batch_id
                ),
            )
            if release_result is not None:
                return self._persisted_result(batch, release_result)
            if batch["lifecycle"] == "empty":
                return IntegrationReleaseResult(
                    outcome="empty",
                    project_id=project_id,
                    batch_id=batch_id,
                    request_id=batch["request_id"],
                )

            revision = int(batch["current_revision"])
            candidate = await self._one_for_update(
                conn,
                select(integration_candidate_revisions).where(
                    integration_candidate_revisions.c.batch_id == batch_id,
                    integration_candidate_revisions.c.revision == revision,
                ),
            )
            publication = await self._one_for_update(
                conn,
                select(integration_candidate_publications).where(
                    integration_candidate_publications.c.batch_id == batch_id,
                    integration_candidate_publications.c.revision == revision,
                ),
            )
            lease = await self._one_for_update(
                conn,
                select(project_integration_leases).where(
                    project_integration_leases.c.project_id == project_id
                ),
            )
            operation = await self._one_for_update(
                conn,
                select(integration_repair_operations).where(
                    integration_repair_operations.c.batch_id == batch_id
                ),
            )
            stage = None
            if operation is not None:
                stage = await self._one_for_update(
                    conn,
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                    ),
                )
            intent = await self._one_for_update(
                conn,
                select(integration_promotion_intents).where(
                    integration_promotion_intents.c.intent_kind == "root",
                    integration_promotion_intents.c.root_batch_id == batch_id,
                    integration_promotion_intents.c.root_candidate_revision == revision,
                ),
            )
            root_mutations = await self._all_for_update(
                conn,
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.batch_id == batch_id,
                    integration_candidate_ref_mutations.c.revision == revision,
                ),
            )
            members = await self._all_for_update(
                conn,
                select(integration_batch_members)
                .where(integration_batch_members.c.batch_id == batch_id)
                .order_by(integration_batch_members.c.ordinal),
            )
            results = await self._all_for_update(
                conn,
                select(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == batch_id,
                    integration_candidate_member_results.c.revision == revision,
                )
                .order_by(integration_candidate_member_results.c.member_ordinal),
            )
            reservations = []
            if intent is not None:
                reservations = await self._all_for_update(
                    conn,
                    select(integration_root_intent_members)
                    .where(integration_root_intent_members.c.intent_id == intent["id"])
                    .order_by(integration_root_intent_members.c.member_ordinal),
                )
            receipts = await self._all_for_update(
                conn,
                select(task_delivery_receipts)
                .where(
                    task_delivery_receipts.c.batch_id == batch_id,
                    task_delivery_receipts.c.candidate_revision == revision,
                )
                .order_by(task_delivery_receipts.c.member_ordinal),
            )
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=300,
            )

            operation_id = operation["id"] if operation is not None else None
            if lease is None:
                if schedule["outstanding_request_id"] == batch["request_id"]:
                    return self._result("invariant_error", batch, operation_id)
                return self._result(
                    "already_released",
                    batch,
                    operation_id,
                    self._replay_catchup(schedule, batch["request_id"]),
                )

            branch_owner = await self._one_for_update(
                conn,
                select(integration_branch_owners).where(
                    integration_branch_owners.c.repository_id == batch["repository_id"],
                    integration_branch_owners.c.ref == batch["integration_branch"],
                ),
            )
            if await self._has_unresolved_on(conn, batch_id, revision) or (
                branch_owner is not None
                and branch_owner["handoff_state"] == "handoff_pending"
            ):
                return self._result("wait", batch, operation_id)
            if not self._complete_shipping(
                batch,
                candidate,
                publication,
                lease,
                operation,
                stage,
                intent,
                root_mutations,
                members,
                results,
                reservations,
                receipts,
            ):
                return self._result("invariant_error", batch, operation_id)
            if schedule["outstanding_request_id"] != batch["request_id"]:
                return self._result("stale", batch, operation_id)

            catchup_request_id = None
            sequence = int(schedule["request_sequence"])
            values: dict[str, Any] = {
                "catchup_trigger": None,
                "catchup_requested_at": None,
                "catchup_after_sequence": None,
                "last_completed_sweep_at": now,
                "updated_at": now,
            }
            if schedule["catchup_trigger"] is not None:
                if int(schedule["catchup_after_sequence"]) != sequence:
                    return self._result("invariant_error", batch, operation_id)
                sequence += 1
                catchup_request_id = f"integration-sweep:{project_id}:{sequence}"
                values.update(
                    request_sequence=sequence,
                    outstanding_request_id=catchup_request_id,
                    outstanding_trigger=schedule["catchup_trigger"],
                    outstanding_requested_at=schedule["catchup_requested_at"],
                )
            else:
                values.update(
                    outstanding_request_id=None,
                    outstanding_trigger=None,
                    outstanding_requested_at=None,
                )
            schedule_cas = await conn.execute(
                update(project_integration_schedules)
                .where(
                    project_integration_schedules.c.project_id == project_id,
                    project_integration_schedules.c.outstanding_request_id
                    == batch["request_id"],
                    project_integration_schedules.c.request_sequence
                    == schedule["request_sequence"],
                )
                .values(**values)
            )
            lease_cas = await conn.execute(
                delete(project_integration_leases).where(
                    project_integration_leases.c.project_id == project_id,
                    project_integration_leases.c.batch_id == batch_id,
                    project_integration_leases.c.owner_id == lease["owner_id"],
                    project_integration_leases.c.fence_token == lease["fence_token"],
                )
            )
            if schedule_cas.rowcount != 1 or lease_cas.rowcount != 1:
                raise _CASLost
            await conn.execute(
                insert(integration_release_results).values(
                    batch_id=batch_id,
                    project_id=project_id,
                    request_id=batch["request_id"],
                    operation_id=operation_id,
                    catchup_request_id=catchup_request_id,
                    released_at=now,
                )
            )
            if catchup_request_id is not None:
                await enqueue_integration_event(
                    conn,
                    event_id=catchup_request_id,
                    dedup_key=catchup_request_id,
                    project_id=project_id,
                    event_type="integration.sweep_due",
                    payload={"project_id": project_id, "operation_id": catchup_request_id},
                    available_at=now,
                )
            return self._result(
                "released", batch, operation_id, catchup_request_id
            )

    async def _canonical_replay(
        self, project_id: str, batch_id: str
    ) -> IntegrationReleaseResult:
        async with self.db._engine.connect() as conn:
            batch = (
                await conn.execute(
                    select(integration_batches).where(integration_batches.c.id == batch_id)
                )
            ).mappings().one_or_none()
            operation_id = (
                await conn.execute(
                    select(integration_repair_operations.c.id).where(
                        integration_repair_operations.c.batch_id == batch_id
                    )
                )
            ).scalar_one_or_none()
            release_result = (
                await conn.execute(
                    select(integration_release_results).where(
                        integration_release_results.c.batch_id == batch_id
                    )
                )
            ).mappings().one_or_none()
            schedule = (
                await conn.execute(
                    select(project_integration_schedules).where(
                        project_integration_schedules.c.project_id == project_id
                    )
                )
            ).mappings().one_or_none()
        if batch is None:
            return IntegrationReleaseResult(outcome="stale", batch_id=batch_id)
        if batch["lifecycle"] == "empty":
            return self._result("empty", batch, None)
        if release_result is not None:
            return self._persisted_result(batch, release_result)
        if schedule is None or schedule["outstanding_request_id"] == batch["request_id"]:
            return self._result("invariant_error", batch, operation_id)
        return self._result(
            "already_released",
            batch,
            operation_id,
            self._replay_catchup(schedule, batch["request_id"]),
        )

    @staticmethod
    async def _one_for_update(conn: Any, statement: Any) -> dict[str, Any] | None:
        row = (await conn.execute(statement.with_for_update())).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _all_for_update(conn: Any, statement: Any) -> list[dict[str, Any]]:
        rows = (await conn.execute(statement.with_for_update())).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    async def _has_unresolved_on(conn: Any, batch_id: str, revision: int) -> bool:
        checks = (
            select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.batch_id == batch_id,
                integration_candidate_ref_mutations.c.revision == revision,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
            select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.batch_id == batch_id,
                integration_candidate_resolutions.c.revision == revision,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
            select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.batch_id == batch_id,
                integration_attestation_publications.c.revision == revision,
                integration_attestation_publications.c.state == "reserved",
            ),
        )
        for statement in checks:
            if (await conn.execute(statement.with_for_update())).scalar_one_or_none() is not None:
                return True
        return False

    @staticmethod
    def _complete_shipping(
        batch: Any,
        candidate: dict[str, Any] | None,
        publication: dict[str, Any] | None,
        lease: dict[str, Any],
        operation: dict[str, Any] | None,
        stage: dict[str, Any] | None,
        intent: dict[str, Any] | None,
        mutations: list[dict[str, Any]],
        members: list[dict[str, Any]],
        results: list[dict[str, Any]],
        reservations: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
    ) -> bool:
        root_main = [row for row in mutations if row["purpose"] == "root_main"]
        count = len(members)
        expected_ordinals = list(range(count))
        return bool(
            batch["lifecycle"] == "promoted"
            and batch["final_main_sha"] is not None
            and candidate is not None
            and candidate["state"] == "promoted"
            and candidate["head_sha"] == batch["tested_candidate_sha"]
            and publication is not None
            and publication["state"] == "pr_published"
            and publication["head_sha"] == candidate["head_sha"]
            and operation is not None
            and operation["target_kind"] == "batch"
            and operation["batch_id"] == batch["id"]
            and operation["episode_id"] == batch["id"]
            and operation["state"] == "completed"
            and stage is not None
            and stage["state"] == "passed"
            and intent is not None
            and intent["state"] == "committed"
            and intent["operation_key"] == operation["id"]
            and intent["prepared_sha"] == candidate["head_sha"]
            and intent["remote_evidence"]
            == {"kind": "authenticated_main", "remote_sha": batch["final_main_sha"]}
            and lease["batch_id"] == batch["id"]
            and lease["repository_id"] == batch["repository_id"]
            and lease["owner_id"] == intent["project_lease_owner_id"]
            and int(lease["fence_token"]) == int(intent["project_lease_fence_token"])
            and len(root_main) == 1
            and root_main[0]["state"] == "applied"
            and root_main[0]["desired_sha"] == candidate["head_sha"]
            and root_main[0]["remote_sha"] == candidate["head_sha"]
            and count > 0
            and [int(row["ordinal"]) for row in members] == expected_ordinals
            and [int(row["member_ordinal"]) for row in results] == expected_ordinals
            and [int(row["member_ordinal"]) for row in reservations] == expected_ordinals
            and [int(row["member_ordinal"]) for row in receipts] == expected_ordinals
            and all(row["result"] == "applied" for row in results)
            and [row["receipt_id"] for row in reservations] == [row["id"] for row in receipts]
        )

    @staticmethod
    def _replay_catchup(schedule: Any, released_request_id: str) -> str | None:
        current = schedule["outstanding_request_id"]
        return str(current) if current is not None and current != released_request_id else None

    @staticmethod
    def _result(
        outcome: str,
        batch: Any,
        operation_id: str | None,
        catchup_request_id: str | None = None,
    ) -> IntegrationReleaseResult:
        return IntegrationReleaseResult(
            outcome=outcome,
            project_id=batch["project_id"],
            batch_id=batch["id"],
            request_id=batch["request_id"],
            catchup_request_id=catchup_request_id,
            operation_id=operation_id,
        )

    @staticmethod
    def _persisted_result(batch: Any, result: Any) -> IntegrationReleaseResult:
        if (
            result["project_id"] != batch["project_id"]
            or result["request_id"] != batch["request_id"]
        ):
            return IntegrationReleaseResult(
                outcome="invariant_error",
                project_id=batch["project_id"],
                batch_id=batch["id"],
                request_id=batch["request_id"],
            )
        return IntegrationReleaseResult(
            outcome="already_released",
            project_id=result["project_id"],
            batch_id=result["batch_id"],
            request_id=result["request_id"],
            catchup_request_id=result["catchup_request_id"],
            operation_id=result["operation_id"],
        )


__all__ = ["IntegrationReleaseResult", "IntegrationReleaseService"]
