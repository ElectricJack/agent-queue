"""Human recovery controls over already-frozen integration work."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, select, update

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
    sessions,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
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
            stage = await self._locked_stage_on(conn, operation)
            if stage is None or stage["state"] not in {"failed", "expired", "cancelled"}:
                return self._state_result("invalid_state", operation, project_id)
            live_resolution = await self._safe_live_resolution_resume_on(
                conn, operation, stage, project_id
            )
            blockers = await self._ambiguous_writes_on(
                conn,
                operation,
                allow_reserved_delegate=not live_resolution,
                allowed_writer_id=(live_resolution or {}).get("writer_id"),
                allowed_promotion_intent_id=(live_resolution or {}).get("promotion_intent_id"),
            )
            if blockers:
                return self._ambiguous_result(operation, project_id, blockers)
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
                    # Consumed attempts are durable (revision 3f30b34c7e7c keeps
                    # ``attempts`` monotone on both dialects): a human resume
                    # re-arms the stage's clock, never its attempt budget, so a
                    # resumed stage that fails again escalates or re-blocks
                    # instead of silently earning a fresh ladder.
                    state="active",
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
                    "blockers": [
                        {
                            "code": "cleanup_irreversible",
                            "detail": (
                                "cleanup item has an unresolved irreversible write marker"
                            ),
                            "ref": identity,
                        }
                        for identity in sorted(ambiguous)
                    ],
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
    async def _safe_live_resolution_resume_on(
        conn: Any,
        operation: dict[str, Any],
        stage: dict[str, Any],
        project_id: str,
    ) -> dict[str, str] | None:
        """Recognize one proven, never-started resolution push.

        This is intentionally narrower than ordinary resume.  It admits no
        replacement writer and no general attached-writer exception: the
        original delegate, claim epoch, workspace, branch fence and frozen
        reservation must still describe precisely one live actor.  A durable
        pre-push marker makes even a remote write that *might* have started
        ineligible.
        """
        if (
            operation["target_kind"] != "parent"
            or stage["state"] != "expired"
            or stage["writer_kind"] != "repair_delegate"
            or not stage["repair_task_id"]
        ):
            return None
        repair_task_id = stage["repair_task_id"]
        parent = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        delegate = (
            await conn.execute(select(tasks).where(tasks.c.id == repair_task_id).with_for_update())
        ).mappings().one_or_none()
        if (
            parent is None
            or delegate is None
            or parent["project_id"] != project_id
            or delegate["project_id"] != project_id
            or delegate["repo_id"] != parent["repo_id"]
            or delegate["branch_name"] != parent["branch_name"]
            or delegate["status"] != "IN_PROGRESS"
            or delegate["assigned_agent_id"] is None
        ):
            return None
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints.c.episode_id)
                .where(task_integration_checkpoints.c.task_id == parent["id"])
                .with_for_update()
            )
        ).scalar_one_or_none()
        if checkpoint != operation["episode_id"]:
            return None
        if (
            await conn.execute(
                select(task_metadata.c.task_id).where(
                    task_metadata.c.task_id == repair_task_id,
                    task_metadata.c.key == "manual_pause",
                )
            )
        ).scalar_one_or_none() is not None:
            return None
        owner = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == parent["repo_id"],
                    integration_branch_owners.c.ref == parent["branch_name"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            owner is None
            or owner["owner_id"] != repair_task_id
            or owner["owner_role"] != "repair"
            or owner["handoff_state"] != "attached"
            or not owner["session_id"]
            or not owner["workspace_id"]
        ):
            return None
        session = (
            await conn.execute(select(sessions).where(sessions.c.id == owner["session_id"]).with_for_update())
        ).mappings().one_or_none()
        workspace = (
            await conn.execute(
                select(workspaces).where(workspaces.c.id == owner["workspace_id"]).with_for_update()
            )
        ).mappings().one_or_none()
        if (
            session is None
            or workspace is None
            or session["task_id"] != repair_task_id
            or session["project_id"] != project_id
            or session["state"] not in {"starting", "running", "draining"}
            or session["agent_id"] != delegate["assigned_agent_id"]
            or session["last_claim_epoch"] != delegate["claim_epoch"]
            or workspace["project_id"] != project_id
            or workspace["locked_by_task_id"] != repair_task_id
            or workspace["locked_by_agent_id"] != session["agent_id"]
            or not workspace["enabled"]
            or session["work_dir"] != workspace["workspace_path"]
        ):
            return None
        intents = (
            await conn.execute(
                select(integration_promotion_intents)
                .where(
                    (
                        integration_promotion_intents.c.operation_key == operation["id"]
                    )
                    | (
                        integration_promotion_intents.c.resolution_operation_id
                        == operation["id"]
                    ),
                    integration_promotion_intents.c.state.not_in(
                        ("committed", "conflict", "superseded")
                    ),
                )
                .with_for_update()
            )
        ).mappings().all()
        if len(intents) != 1:
            return None
        intent = intents[0]
        if not (
            intent["state"] == "resolution_reserved"
            and intent["operation_key"] == operation["id"]
            and intent["resolution_operation_id"] == operation["id"]
            and intent["resolution_stage_ordinal"] == stage["ordinal"]
            and intent["resolution_task_id"] == repair_task_id
            and intent["repository_id"] == parent["repo_id"]
            and intent["target_branch"] == parent["branch_name"]
            and intent["resolution_session_id"] == session["id"]
            and intent["resolution_session_instance_token"] == session["instance_token"]
            and intent["resolution_workspace_id"] == workspace["id"]
            and intent["resolution_fence_owner_id"] == repair_task_id
            and intent["resolution_fence_token"] == owner["fence_token"]
            and intent["resolution_push_started_at"] is None
            and intent["resolution_push_evidence"] is None
        ):
            return None
        # The recovery exception is for these exact durable rows only.  A
        # second owner for the same task on another ref is still a writer and
        # a second intent remains a promotion blocker.
        return {"writer_id": str(owner["id"]), "promotion_intent_id": str(intent["id"])}

    @staticmethod
    async def _ambiguous_writes_on(
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_reserved_delegate: bool = False,
        allowed_writer_id: str | None = None,
        allowed_promotion_intent_id: str | None = None,
    ) -> list[str]:
        operation_id = operation["id"]
        writer = select(integration_branch_owners.c.id).where(
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
        )
        if allow_reserved_delegate:
            exact_delegate = select(
                integration_repair_stages.c.repair_task_id
            ).where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == operation["active_stage"],
                integration_repair_stages.c.writer_kind == "repair_delegate",
            )
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id.in_(exact_delegate),
                    integration_branch_owners.c.owner_role == "repair",
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
        if allowed_writer_id is not None:
            writer = writer.where(integration_branch_owners.c.id != allowed_writer_id)
        promotion = select(integration_promotion_intents.c.id).where(
            (
                integration_promotion_intents.c.operation_key == operation_id
            )
            | (
                integration_promotion_intents.c.resolution_operation_id == operation_id
            ),
            integration_promotion_intents.c.state.not_in(
                ("committed", "conflict", "superseded")
            ),
        )
        if allowed_promotion_intent_id is not None:
            promotion = promotion.where(
                integration_promotion_intents.c.id != allowed_promotion_intent_id
            )
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
            "promotion": promotion,
            "writer": writer,
        }
        if operation["batch_id"] is not None:
            statements["candidate_publication"] = select(
                integration_candidate_publications.c.batch_id
            ).where(
                integration_candidate_publications.c.batch_id == operation["batch_id"],
                integration_candidate_publications.c.state != "pr_published",
            )
            if allow_reserved_delegate:
                # pr_reserved follows a confirmed ref write. Audit-PR metadata
                # may remain pending without making that branch write uncertain.
                pub = integration_candidate_publications.c
                mutation = integration_candidate_ref_mutations.c
                applied_ref = select(mutation.id).where(
                    mutation.purpose == "candidate_final",
                    mutation.state == "applied",
                    mutation.operation_id == operation_id,
                    mutation.operation_episode_id == operation["episode_id"],
                    mutation.batch_id == pub.batch_id,
                    mutation.revision == pub.revision,
                    mutation.repository_id == pub.repository_id,
                    mutation.target_branch == "refs/heads/" + pub.head_ref,
                    mutation.branch == mutation.target_branch,
                    mutation.target_branch == select(integration_batches.c.integration_branch)
                        .where(integration_batches.c.id == operation["batch_id"]).scalar_subquery(),
                    mutation.expected_old_sha == pub.expected_old_sha,
                    mutation.desired_sha == pub.head_sha,
                    mutation.remote_sha == pub.head_sha,
                ).exists()
                statements["candidate_publication"] = statements["candidate_publication"].where(
                    ~and_(pub.state == "pr_reserved", applied_ref)
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
            "blockers": [
                {
                    "code": "ambiguous_external_write",
                    "detail": "operation has unresolved external mutation evidence",
                    "ref": blocker,
                }
                for blocker in blockers
            ],
        }
