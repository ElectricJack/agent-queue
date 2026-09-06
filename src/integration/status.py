"""Read-only, snapshot-consistent integration rollout status."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_cleanup_items,
    integration_legacy_suppression,
    integration_promotion_intents,
    integration_release_results,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    project_integration_leases,
    project_integration_schedules,
    projects,
    task_integration_checkpoints,
    tasks,
)
from src.integration.parent_completion import ParentCompletion
from src.integration.models import RepairPolicy


ACTIVE_BATCH_STATES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)
TERMINAL_TASK_STATES = ("COMPLETED", "FAILED", "CANCELLED")


def _blocker(code: str, detail: str, ref: str | None = None, **facts: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "detail": detail, "ref": ref}
    item.update(facts)
    return item


def _sorted_blockers(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for value in values:
        key = (value["code"], str(value.get("ref") or ""), value["detail"])
        unique[key] = value
    return [unique[key] for key in sorted(unique)]


class IntegrationStatusService:
    """Project/task integration projections with no provider I/O or writes."""

    def __init__(self, db, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock

    @asynccontextmanager
    async def _consistent_snapshot(self) -> AsyncIterator[AsyncConnection]:
        engine = self.db._engine
        if engine is None:
            raise RuntimeError("database is not initialized")
        conn = await engine.connect()
        try:
            if conn.dialect.name == "sqlite":
                # The sqlite3 driver's legacy transaction mode does not emit
                # BEGIN for SELECTs.  Issue it ourselves so the first read
                # establishes the WAL snapshot used by the whole projection.
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.exec_driver_sql("BEGIN")
                try:
                    yield conn
                finally:
                    await conn.exec_driver_sql("ROLLBACK")
            else:
                if conn.dialect.name == "postgresql":
                    await conn.execution_options(isolation_level="REPEATABLE READ")
                transaction = await conn.begin()
                try:
                    yield conn
                finally:
                    await transaction.rollback()
        finally:
            await conn.close()

    async def status(self, project_id: str) -> dict[str, Any] | None:
        """Return one complete project projection from one database snapshot."""
        async with self._consistent_snapshot() as conn:
            project = (
                await conn.execute(select(projects).where(projects.c.id == project_id))
            ).mappings().one_or_none()
            if project is None:
                return None

            schedule = await self._one(
                conn,
                select(project_integration_schedules).where(
                    project_integration_schedules.c.project_id == project_id
                ),
            )
            batch = await self._one(
                conn,
                select(integration_batches)
                .where(
                    integration_batches.c.project_id == project_id,
                    integration_batches.c.lifecycle.in_(ACTIVE_BATCH_STATES),
                )
                .order_by(integration_batches.c.updated_at.desc(), integration_batches.c.id),
            )
            revision = None
            members: list[dict[str, Any]] = []
            repair: list[dict[str, Any]] = []
            evidence: list[dict[str, Any]] = []
            promotion: list[dict[str, Any]] = []
            release = None
            if batch is not None:
                revision = await self._one(
                    conn,
                    select(integration_candidate_revisions).where(
                        integration_candidate_revisions.c.batch_id == batch["id"],
                        integration_candidate_revisions.c.revision
                        == batch["current_revision"],
                    ),
                )
                member_rows = await self._all(
                    conn,
                    select(integration_batch_members)
                    .where(integration_batch_members.c.batch_id == batch["id"])
                    .order_by(integration_batch_members.c.ordinal),
                )
                members = [
                    {
                        "ordinal": row["ordinal"],
                        "task_id": row["task_id"],
                        "repository_id": row["repository_id"],
                        "reviewed_head_sha": row["reviewed_head_sha"],
                        "review_evidence_id": row["review_evidence_id"],
                    }
                    for row in member_rows
                ]
                operation_rows = await self._all(
                    conn,
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.batch_id == batch["id"]
                    ),
                )
                repair = await self._repair_projection(conn, operation_rows)
                evidence_rows = await self._all(
                    conn,
                    select(integration_check_evidence).where(
                        integration_check_evidence.c.batch_id == batch["id"],
                        integration_check_evidence.c.candidate_revision
                        == batch["current_revision"],
                    ),
                )
                evidence = [
                    {
                        "id": row["id"],
                        "producer_id": row["producer_id"],
                        "workflow_id": row["workflow_id"],
                        "run_id": row["run_id"],
                        "attempt": row["attempt"],
                        "required_check_version": row["required_check_version"],
                        "conclusion": row["conclusion"],
                        "classification": row["classification"],
                        "observed_at": row["observed_at"],
                    }
                    for row in evidence_rows
                ]
                promotion_rows = await self._all(
                    conn,
                    select(integration_promotion_intents).where(
                        integration_promotion_intents.c.root_batch_id == batch["id"],
                        integration_promotion_intents.c.root_candidate_revision
                        == batch["current_revision"],
                    ),
                )
                promotion = [
                    {
                        "id": row["id"],
                        "state": row["state"],
                        "source_head": row["source_head"],
                        "expected_target": row["expected_target"],
                        "prepared_sha": row["prepared_sha"],
                    }
                    for row in promotion_rows
                ]
            release = await self._one(
                conn,
                select(integration_release_results)
                .where(integration_release_results.c.project_id == project_id)
                .order_by(
                    integration_release_results.c.released_at.desc(),
                    integration_release_results.c.batch_id,
                ),
            )

            ownership_rows = await self._all(
                conn,
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id
                    == project["integration_repository_id"],
                    integration_branch_owners.c.handoff_state != "released",
                )
                .order_by(integration_branch_owners.c.ref),
            ) if project["integration_repository_id"] else []
            ownership = [
                {
                    "ref": row["ref"],
                    "owner_id": row["owner_id"],
                    "owner_role": row["owner_role"],
                    "fence_token": row["fence_token"],
                    "handoff_state": row["handoff_state"],
                    "expires_at": row["expires_at"],
                }
                for row in ownership_rows
            ]
            lease = await self._one(
                conn,
                select(project_integration_leases).where(
                    project_integration_leases.c.project_id == project_id
                ),
            )
            cleanup_rows = await self._all(
                conn,
                select(integration_cleanup_items)
                .where(
                    integration_cleanup_items.c.project_id == project_id,
                    integration_cleanup_items.c.state != "complete",
                )
                .order_by(
                    integration_cleanup_items.c.batch_id,
                    integration_cleanup_items.c.domain_key,
                ),
            )
            cleanup = [
                {
                    "batch_id": row["batch_id"],
                    "kind": row["kind"],
                    "identity": row["identity"],
                    "state": row["state"],
                    "attempts": row["attempts"],
                    "next_attempt_at": row["next_attempt_at"],
                    "irreversible": row["irreversible_prewrite_at"] is not None,
                }
                for row in cleanup_rows
            ]
            suppression = await self._one(
                conn,
                select(integration_legacy_suppression).where(
                    integration_legacy_suppression.c.project_id == project_id
                ),
            )
            task_rows = await self._all(
                conn,
                select(tasks.c.id).where(tasks.c.project_id == project_id).order_by(tasks.c.id),
            )
            parent_readiness = []
            project_blockers = self._project_blockers(
                project, batch, revision, repair, cleanup, now=self.clock()
            )
            for task_row in task_rows:
                task_projection = await self._task_blockers_on(
                    conn, task_row["id"], expected_project_id=project_id
                )
                if task_projection and task_projection["blockers"]:
                    parent_readiness.append(task_projection)

            blockers = list(project_blockers)
            if ownership:
                blockers.append(
                    _blocker(
                        "active_owner",
                        "one or more integration branches retain an active owner",
                        ownership[0]["ref"],
                    )
                )
            blockers.extend(
                blocker
                for task_projection in parent_readiness
                for blocker in task_projection["blockers"]
                if blocker["code"] != "preflight_evidence_unavailable"
            )
            blockers = _sorted_blockers(blockers)
            rollout_ready = not blockers
            return {
                "project_id": project_id,
                "effective_mode": project["hierarchical_integration_mode"],
                "desired_mode": project["hierarchical_integration_desired_mode"],
                "draining": bool(project["hierarchical_integration_draining"]),
                "generation": project["hierarchical_integration_generation"],
                "repository_id": project["integration_repository_id"],
                "schedule": schedule,
                "active_batch": self._batch_projection(batch, revision),
                "members": members,
                "parent_readiness": parent_readiness,
                "ownership": ownership,
                "lease": lease,
                "repair": repair,
                "ci_evidence": evidence,
                "promotion": promotion,
                "reconciliation": {
                    "pending": any(item["state"] not in {"committed", "superseded"} for item in promotion)
                },
                "cleanup_pending": cleanup,
                "release": release,
                "legacy_suppression": suppression,
                "blockers": blockers,
                # ``ready`` is rollout/preflight eligibility, not ordinary
                # task schedulability.  Keep the explicit alias so later CLI
                # work can name that distinction without changing this wire
                # contract.
                "ready": rollout_ready,
                "rollout_ready": rollout_ready,
            }

    async def task_blockers(self, task_id: str) -> dict[str, Any] | None:
        """Return integration blockers after resolving task/project server-side."""
        async with self._consistent_snapshot() as conn:
            return await self._task_blockers_on(conn, task_id)

    async def _task_blockers_on(
        self,
        conn: AsyncConnection,
        task_id: str,
        *,
        expected_project_id: str | None = None,
    ) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(tasks, projects)
                .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                .where(tasks.c.id == task_id)
            )
        ).mappings().one_or_none()
        if row is None or (
            expected_project_id is not None and row["project_id"] != expected_project_id
        ):
            return None
        blockers: list[dict[str, Any]] = []
        designated = row["integration_repository_id"]
        integration_active = (
            row["hierarchical_integration_mode"] != "disabled"
            or row["hierarchical_integration_desired_mode"] != "disabled"
        )
        if integration_active and (designated is None or row["repo_id"] != designated):
            blockers.append(
                _blocker(
                    "repository_not_designated",
                    "task repository is not the project's designated integration repository",
                    row["repo_id"],
                )
            )
        if integration_active:
            blockers.append(
                _blocker(
                    "preflight_evidence_unavailable",
                    "transport, protection, and probe evidence is not yet persisted",
                    row["project_id"],
                )
            )
        checkpoint = await self._one(
            conn,
            select(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == task_id
            ),
        )
        if checkpoint is not None:
            if checkpoint["verified_generation"] is not None and (
                checkpoint["verified_generation"] != checkpoint["generation"]
            ):
                blockers.append(
                    _blocker(
                        "stale_generation",
                        "verified generation does not match the current task generation",
                        task_id,
                    )
                )
            if checkpoint["verified_sha"] is not None and (
                checkpoint["verified_sha"] != checkpoint["checkpoint_sha"]
            ):
                blockers.append(
                    _blocker(
                        "stale_head",
                        "verified head does not match the current checkpoint",
                        task_id,
                    )
                )
            review = await self._one(
                conn,
                select(integration_review_evidence)
                .where(integration_review_evidence.c.source_task_id == task_id)
                .order_by(
                    integration_review_evidence.c.created_at.desc(),
                    integration_review_evidence.c.id.desc(),
                ),
            )
            if review is not None and (
                review["generation"] != checkpoint["generation"]
                or review["reviewed_head_sha"] != checkpoint["checkpoint_sha"]
                or review["verdict"] != "approved"
            ):
                blockers.append(
                    _blocker("stale_review", "review does not bind the current task head", review["id"])
                )
        owners = await self._all(
            conn,
            select(integration_branch_owners.c.id).where(
                integration_branch_owners.c.repository_id == designated,
                integration_branch_owners.c.ref == row["branch_name"],
                integration_branch_owners.c.handoff_state != "released",
            ),
        ) if designated and row["branch_name"] else []
        if owners:
            blockers.append(_blocker("active_owner", "task branch has an active owner", owners[0]["id"]))

        readiness_operation = None
        if checkpoint is not None and checkpoint["episode_id"] is not None:
            readiness_operation = await self._one(
                conn,
                select(integration_repair_operations).where(
                    integration_repair_operations.c.parent_task_id == task_id,
                    integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                ),
            )
        operation_rows = await self._all(
            conn,
            select(integration_repair_operations).where(
                integration_repair_operations.c.parent_task_id == task_id,
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                ),
            ),
        )
        child_rows = await self._all(
            conn,
            select(tasks.c.id, tasks.c.status).where(tasks.c.parent_task_id == task_id),
        )
        child_status = {child["id"]: child["status"] for child in child_rows}
        parent_readiness = None
        if checkpoint is not None and readiness_operation is not None:
            parent_readiness = await ParentCompletion(self.db).readiness_on(
                conn,
                parent=dict(row),
                project=dict(row),
                checkpoint=checkpoint,
                operation=readiness_operation,
            )
            for item in parent_readiness["blockers"]:
                child_id = item["task_id"]
                cause = item["reason"]
                if child_status.get(child_id) not in TERMINAL_TASK_STATES:
                    blockers.append(
                        _blocker("open_child", "child task is not terminal", child_id)
                    )
                elif cause == "origin_mismatch":
                    blockers.append(
                        _blocker(
                            "repository_not_designated",
                            "child origin does not match the current parent target",
                            child_id,
                            cause=cause,
                        )
                    )
                elif cause == "receipt_chain":
                    blockers.append(
                        _blocker(
                            "stale_head",
                            "delivery receipt chain does not bind the current parent head",
                            child_id,
                            cause=cause,
                        )
                    )
                else:
                    blockers.append(
                        _blocker(
                            "missing_receipt",
                            "child has no applicable current delivery receipt",
                            child_id,
                            cause=cause,
                        )
                    )
        else:
            for child in child_rows:
                code = (
                    "open_child"
                    if child["status"] not in TERMINAL_TASK_STATES
                    else "missing_receipt"
                )
                detail = (
                    "child task is not terminal"
                    if code == "open_child"
                    else "no current parent collection exists for the terminal child"
                )
                blockers.append(_blocker(code, detail, child["id"]))
        repair = await self._repair_projection(conn, operation_rows)
        for item in repair:
            if item["state"] == "human_required":
                blockers.append(_blocker("human_hold", "repair requires a human decision", item["id"]))
        blockers.extend(self._repair_blockers(repair, now=self.clock()))
        return {
            "task_id": task_id,
            "project_id": row["project_id"],
            "integration_active": integration_active,
            "checkpoint": checkpoint,
            "parent_readiness": parent_readiness,
            "repair": repair,
            "blockers": _sorted_blockers(blockers),
        }

    @staticmethod
    async def _one(conn: AsyncConnection, statement) -> dict[str, Any] | None:
        row = (await conn.execute(statement.limit(1))).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _all(conn: AsyncConnection, statement) -> list[dict[str, Any]]:
        rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def _repair_projection(
        self, conn: AsyncConnection, operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result = []
        for operation in sorted(operations, key=lambda item: item["id"]):
            stage_rows = await self._all(
                conn,
                select(integration_repair_stages)
                .where(integration_repair_stages.c.operation_id == operation["id"])
                .order_by(integration_repair_stages.c.ordinal),
            )
            stages = [
                {
                    "ordinal": stage["ordinal"],
                    "state": stage["state"],
                    "attempts": stage["attempts"],
                    "deadline_at": stage["deadline_at"],
                    "started_at": stage["started_at"],
                    "intelligence_class": stage["intelligence_class"],
                    "profile_id": stage["profile_id"],
                    "policy": stage["policy"],
                }
                for stage in stage_rows
            ]
            result.append(
                {
                    "id": operation["id"],
                    "target_kind": operation["target_kind"],
                    "batch_id": operation["batch_id"],
                    "parent_task_id": operation["parent_task_id"],
                    "active_stage": operation["active_stage"],
                    "state": operation["state"],
                    "stages": stages,
                }
            )
        return result

    @staticmethod
    def _batch_projection(
        batch: dict[str, Any] | None, revision: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if batch is None:
            return None
        return {
            "id": batch["id"],
            "repository_id": batch["repository_id"],
            "request_id": batch["request_id"],
            "trigger": batch["trigger"],
            "state": batch["lifecycle"],
            "revision": batch["current_revision"],
            "base_sha": batch["base_sha"],
            "integration_branch": batch["integration_branch"],
            "tested_candidate_sha": batch["tested_candidate_sha"],
            "ci_evidence_id": batch["ci_evidence_id"],
            "final_main_sha": batch["final_main_sha"],
            "cleanup_state": batch["cleanup_state"],
            "revision_state": revision["state"] if revision else None,
            "candidate_sha": revision["head_sha"] if revision else None,
        }

    @staticmethod
    def _project_blockers(
        project,
        batch: dict[str, Any] | None,
        revision: dict[str, Any] | None,
        repair: list[dict[str, Any]],
        cleanup: list[dict[str, Any]],
        *,
        now: float,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if project["integration_repository_id"] is None:
            blockers.append(
                _blocker(
                    "repository_not_designated",
                    "project has no designated integration repository",
                    project["id"],
                )
            )
        blockers.append(
            _blocker(
                "preflight_evidence_unavailable",
                "transport, protection, and probe evidence is not yet persisted",
                project["id"],
            )
        )
        if batch is not None and (
            batch["lifecycle"] == "testing"
            or revision is None
            or revision["state"] not in {"green", "promoted"}
        ):
            blockers.append(_blocker("pending_ci", "active candidate is not proven green", batch["id"]))
        for operation in repair:
            if operation["state"] == "human_required" or (
                batch is not None and batch["lifecycle"] == "human_blocked"
            ):
                blockers.append(
                    _blocker("human_hold", "integration repair awaits a human", operation["id"])
                )
        blockers.extend(IntegrationStatusService._repair_blockers(repair, now=now))
        conflict = next((item for item in cleanup if item["state"] == "conflict"), None)
        if conflict is not None:
            blockers.append(
                _blocker("cleanup_conflict", "cleanup requires operator reconciliation", conflict["identity"])
            )
        return blockers

    @staticmethod
    def _repair_blockers(
        repair: list[dict[str, Any]], *, now: float
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for operation in repair:
            current = next(
                (
                    stage
                    for stage in operation["stages"]
                    if int(stage["ordinal"]) == int(operation["active_stage"])
                ),
                None,
            )
            if current is None or current["state"] not in {"active", "awaiting_completion"}:
                continue
            policy = RepairPolicy.model_validate(current["policy"])
            ordinal = int(current["ordinal"])
            limit = policy.primary_attempts if ordinal == 0 else policy.debug_attempts
            if current["state"] == "active" and int(current["attempts"]) >= limit:
                blockers.append(
                    _blocker(
                        "budget_exhausted",
                        "current repair stage has exhausted its attempt budget",
                        operation["id"],
                        cause="attempts",
                        stage=ordinal,
                        attempts=int(current["attempts"]),
                        limit=limit,
                    )
                )
            deadline_at = current["deadline_at"]
            deadline_bound = (
                current["state"] == "active" or operation["target_kind"] == "parent"
            )
            if (
                deadline_bound
                and deadline_at is not None
                and now >= float(deadline_at)
            ):
                blockers.append(
                    _blocker(
                        "budget_exhausted",
                        "current repair stage deadline is exhausted",
                        operation["id"],
                        cause="deadline",
                        stage=ordinal,
                        deadline_at=float(deadline_at),
                    )
                )
        return blockers


__all__ = ["IntegrationStatusService"]
