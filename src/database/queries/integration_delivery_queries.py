"""Durable review evidence, promotion intents, and delivery receipts."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    tasks,
)
from src.integration.outbox import enqueue_integration_event

_FROZEN_INTENT_FIELDS = (
    "domain_key",
    "operation_key",
    "project_id",
    "source_task_id",
    "target_task_id",
    "source_head",
    "source_base",
    "repository_id",
    "origin_url",
    "target_branch",
    "expected_target",
    "fence_owner_id",
    "fence_token",
    "review_evidence",
    "authors",
    "provenance",
    "commit_metadata",
)
_REQUEST_IDENTITY_FIELDS = (
    "domain_key",
    "operation_key",
    "project_id",
    "source_task_id",
    "target_task_id",
    "source_head",
    "source_base",
    "repository_id",
    "origin_url",
    "target_branch",
    "expected_target",
    "review_evidence",
    "authors",
)
_RESOLUTION_IDENTITY_FIELDS = (
    "resolution_head_sha",
    "resolution_tree_sha",
    "resolution_commit_shas",
    "resolution_operation_id",
    "resolution_stage_ordinal",
    "resolution_task_id",
    "resolution_session_id",
    "resolution_session_instance_token",
    "resolution_workspace_id",
    "resolution_fence_owner_id",
    "resolution_fence_token",
)


class IntegrationDeliveryQueriesMixin:
    """Backend-neutral promotion operations with transactional idempotency."""

    async def append_integration_review_evidence(self, evidence: dict[str, Any]) -> dict:
        required = {
            "id",
            "source_task_id",
            "repository_id",
            "source_base",
            "reviewed_head_sha",
            "reviewed_tree_sha",
            "reviewer_task_id",
            "review_kind",
            "generation",
            "verdict",
            "evidence",
            "created_at",
        }
        missing = required - evidence.keys()
        if missing:
            raise ValueError("review evidence missing: " + ", ".join(sorted(missing)))
        async with self.immediate() as conn:
            source_task_id = evidence["source_task_id"]
            project_id = (
                await conn.execute(
                    select(tasks.c.project_id)
                    .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                    .where(tasks.c.id == source_task_id)
                )
            ).scalar_one_or_none()
            if project_id is None:
                raise ValueError("review evidence source task project does not exist")
            await self.lock_hierarchy_project(conn, project_id)
            current_project_id = (
                await conn.execute(
                    select(tasks.c.project_id)
                    .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                    .where(tasks.c.id == source_task_id)
                )
            ).scalar_one_or_none()
            if current_project_id != project_id:
                raise ValueError("review evidence source task project does not exist")
            await conn.execute(insert(integration_review_evidence).values(**evidence))
        return dict(evidence)

    async def get_active_integration_verifier_for_task(
        self, verifier_task_id: str
    ) -> dict | None:
        statement = (
            select(integration_repair_operations)
            .where(integration_repair_operations.c.verifier_task_id == verifier_task_id)
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                )
            )
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_verifier_operation(
        self, verifier_task_id: str
    ) -> dict | None:
        """Return the verifier binding even after its parent operation completes.

        A branchless verifier closes its own task only after completing the
        parent operation.  Keeping this lookup independent of operation state
        prevents that final close from falling through to the legacy Git
        integration pipeline.
        """
        statement = (
            select(integration_repair_operations)
            .where(integration_repair_operations.c.verifier_task_id == verifier_task_id)
            .order_by(integration_repair_operations.c.created_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_active_parent_integration_operation(self, parent_task_id: str) -> dict | None:
        statement = (
            select(integration_repair_operations)
            .where(integration_repair_operations.c.parent_task_id == parent_task_id)
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                )
            )
            .order_by(integration_repair_operations.c.created_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_applicable_integration_review_evidence(
        self,
        *,
        source_task_id: str,
        repository_id: str,
        source_base: str,
        reviewed_head_sha: str,
        current_generation: int,
    ) -> dict | None:
        """Return the one current approved exact tuple, never an older approval."""
        statement = (
            select(integration_review_evidence)
            .where(integration_review_evidence.c.source_task_id == source_task_id)
            .where(integration_review_evidence.c.repository_id == repository_id)
            .where(integration_review_evidence.c.source_base == source_base)
            .where(integration_review_evidence.c.reviewed_head_sha == reviewed_head_sha)
            .where(integration_review_evidence.c.generation == current_generation)
            .order_by(
                integration_review_evidence.c.generation.desc(),
                integration_review_evidence.c.created_at.desc(),
                integration_review_evidence.c.id.desc(),
            )
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        result = dict(row)
        if result["verdict"] != "approved":
            return None
        return result

    async def get_integration_review_evidence(self, evidence_id: str) -> dict | None:
        statement = select(integration_review_evidence).where(
            integration_review_evidence.c.id == evidence_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_task_branch_origin_for_promotion(
        self, task_id: str, repository_id: str
    ) -> dict | None:
        statement = (
            select(task_branch_origins)
            .where(task_branch_origins.c.task_id == task_id)
            .where(task_branch_origins.c.repository_id == repository_id)
            .where(task_branch_origins.c.retired_at.is_(None))
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def reserve_integration_promotion_intent(self, values: dict[str, Any]) -> dict:
        """Reserve a domain identity and receipt before Git construction."""
        required = set(_FROZEN_INTENT_FIELDS) | {"receipt_id", "created_at"}
        missing = required - values.keys()
        if missing:
            raise ValueError("promotion intent missing: " + ", ".join(sorted(missing)))

        async with self.immediate() as conn:
            # Cancellation holds this row through its pending-intent check
            # and fence release. Recheck here because prepare validates its
            # authority before entering this reservation transaction.
            operation_state = await conn.scalar(
                select(integration_repair_operations.c.state).where(
                    integration_repair_operations.c.id == values["operation_key"],
                    integration_repair_operations.c.target_kind == "parent",
                ).with_for_update()
            )
            existing = await self._promotion_intent_by_domain(conn, values["domain_key"])
            if existing is not None:
                self._assert_same_intent(existing, values)
                return existing

            if operation_state in {"cancelled", "completed"}:
                raise ValueError("parent collection is no longer active")

            target = await self._unresolved_target_intent(
                conn, values["repository_id"], values["target_branch"]
            )
            if target is not None:
                raise ValueError("target has an unresolved promotion")

            row_values = dict(values)
            row_values.setdefault("id", str(uuid.uuid4()))
            row_values.update(state="reserved", updated_at=values["created_at"])
            try:
                async with conn.begin_nested():
                    await conn.execute(insert(integration_promotion_intents).values(**row_values))
            except IntegrityError:
                existing = await self._promotion_intent_by_domain(conn, values["domain_key"])
                if existing is not None:
                    self._assert_same_intent(existing, values)
                    return existing
                raise ValueError("target has an unresolved promotion") from None
            created = await self._promotion_intent_by_domain(conn, values["domain_key"])
            if created is None:  # pragma: no cover - insert/read invariant
                raise RuntimeError("reserved promotion intent was not persisted")
            return created

    async def get_integration_promotion_intent(self, intent_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_promotion_intents).where(
                            integration_promotion_intents.c.id == intent_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    async def reserve_integration_conflict_resolution(
        self, conn, intent_id: str, values: dict[str, Any]
    ) -> dict:
        """Freeze one conflicted intent's agent-authored resolution identity."""
        required = {
            "resolved_head_sha",
            "resolved_tree_sha",
            "repair_commit_shas",
            "operation_id",
            "stage_ordinal",
            "repair_task_id",
            "repair_session_id",
            "repair_session_instance_token",
            "repair_workspace_id",
            "fence_owner_id",
            "fence_token",
        }
        missing = required - values.keys()
        if missing:
            raise ValueError("conflict resolution missing: " + ", ".join(sorted(missing)))
        frozen = {
            "resolution_head_sha": values["resolved_head_sha"],
            "resolution_tree_sha": values["resolved_tree_sha"],
            "resolution_commit_shas": list(values["repair_commit_shas"]),
            "resolution_operation_id": values["operation_id"],
            "resolution_stage_ordinal": values["stage_ordinal"],
            "resolution_task_id": values["repair_task_id"],
            "resolution_session_id": values["repair_session_id"],
            "resolution_session_instance_token": values[
                "repair_session_instance_token"
            ],
            "resolution_workspace_id": values["repair_workspace_id"],
            "resolution_fence_owner_id": values["fence_owner_id"],
            "resolution_fence_token": values["fence_token"],
        }
        intent = await self._locked_intent(conn, intent_id)
        if intent["state"] in {"resolution_reserved", "committed"}:
            changed = [
                field
                for field in _RESOLUTION_IDENTITY_FIELDS
                if intent.get(field) != frozen[field]
            ]
            if changed:
                raise ValueError("resolution identity changed: " + ", ".join(changed))
            return intent | {"_resolution_replayed": True}
        if intent["state"] != "conflict":
            raise ValueError("only a conflicted promotion can reserve a resolution")
        try:
            async with conn.begin_nested():
                result = await conn.execute(
                    update(integration_promotion_intents)
                    .where(integration_promotion_intents.c.id == intent_id)
                    .where(integration_promotion_intents.c.state == "conflict")
                    .values(**frozen, state="resolution_reserved", updated_at=time.time())
                )
        except IntegrityError:
            raise ValueError(
                "target has another unresolved promotion"
            ) from None
        if result.rowcount != 1:
            raise ValueError("promotion conflict changed during resolution reservation")
        return intent | frozen | {"state": "resolution_reserved"}

    async def recover_unwritten_conflict_resolution_on(
        self, conn, intent_id: str, *, successor_id: str, successor_domain_key: str
    ) -> dict:
        """Supersede one never-written resolution and create its fresh successor.

        The caller has already proved the live remote still equals the old
        target while holding its current ownership fence.  This helper only
        performs the durable, append-only-shaped transition under that lock.
        """
        intent = await self._locked_intent(conn, intent_id)
        if intent["state"] == "superseded":
            successor = (
                await conn.execute(
                    select(integration_promotion_intents).where(
                        integration_promotion_intents.c.supersedes_intent_id == intent_id
                    )
                )
            ).mappings().one_or_none()
            if successor is None:
                raise ValueError("superseded resolution has no recovery successor")
            return dict(successor)
        if intent["state"] != "resolution_reserved":
            raise ValueError("only a reserved conflict resolution can be recovered")
        if intent["resolution_push_started_at"] is not None:
            raise ValueError("resolution push may have started; recovery is ambiguous")
        if intent["resolution_push_evidence"] is not None:
            raise ValueError("resolution push already has durable evidence")

        successor_values = dict(intent)
        successor_values.update(
            id=successor_id,
            domain_key=successor_domain_key,
            state="conflict",
            supersedes_intent_id=intent_id,
            superseded_by_intent_id=None,
            resolution_head_sha=None,
            resolution_tree_sha=None,
            resolution_operation_id=None,
            resolution_stage_ordinal=None,
            resolution_task_id=None,
            resolution_session_id=None,
            resolution_session_instance_token=None,
            resolution_workspace_id=None,
            resolution_fence_owner_id=None,
            resolution_fence_token=None,
            resolution_push_started_at=None,
            updated_at=time.time(),
        )
        # SQLAlchemy JSON serializes an explicit ``None`` as JSON ``null``;
        # the binding guard intentionally requires database NULL for absent
        # resolution fields, so omit the two JSON columns entirely.
        successor_values.pop("resolution_commit_shas", None)
        successor_values.pop("resolution_push_evidence", None)
        successor_values.pop("resolution_recovery_evidence", None)
        await conn.execute(insert(integration_promotion_intents).values(**successor_values))
        changed = await conn.execute(
            update(integration_promotion_intents)
            .where(integration_promotion_intents.c.id == intent_id)
            .where(integration_promotion_intents.c.state == "resolution_reserved")
            .where(integration_promotion_intents.c.resolution_push_started_at.is_(None))
            .where(integration_promotion_intents.c.resolution_push_evidence.is_(None))
            .values(
                state="superseded",
                superseded_by_intent_id=successor_id,
                updated_at=time.time(),
            )
        )
        if changed.rowcount != 1:  # pragma: no cover - locked row guards this
            raise ValueError("resolution changed during recovery")
        # Keep the stage's human-facing dossier in step with its authority.
        # A trigger-only update strands a resumed delegate on the old intent.
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == intent["resolution_operation_id"],
                    integration_repair_stages.c.ordinal == intent["resolution_stage_ordinal"],
                ).with_for_update()
            )
        ).mappings().one_or_none()
        if stage is None or stage["trigger_id"] != intent_id:
            raise ValueError("resolution repair stage changed during recovery")
        dossier = dict(stage["dossier"] or {})
        conflict = dict(dossier.get("current_conflict") or {})
        if conflict.get("intent_id") not in {None, intent_id}:
            raise ValueError("resolution repair dossier names another conflict")
        conflict["intent_id"] = successor_id
        dossier["current_conflict"] = conflict
        dossier["trigger_id"] = successor_id
        dossier["superseded_conflict_intent_id"] = intent_id
        changed = await conn.execute(
            update(integration_repair_stages).where(
                integration_repair_stages.c.operation_id == stage["operation_id"],
                integration_repair_stages.c.ordinal == stage["ordinal"],
                integration_repair_stages.c.trigger_id == intent_id,
            ).values(trigger_id=successor_id, dossier=dossier)
        )
        if changed.rowcount != 1:
            raise ValueError("resolution repair stage changed during recovery")
        if stage["repair_task_id"]:
            delegate = (
                await conn.execute(select(tasks).where(
                    tasks.c.id == stage["repair_task_id"]
                ).with_for_update())
            ).mappings().one_or_none()
            if delegate is None or delegate["created_by_id"] != stage["operation_id"]:
                raise ValueError("resolution repair delegate changed during recovery")
            notice = (
                f"Current conflict intent: {successor_id} (supersedes {intent_id}). "
                "Use the current attached repair fence.\n\n"
            )
            await conn.execute(
                update(tasks).where(tasks.c.id == delegate["id"])
                .values(description=notice + delegate["description"])
            )
        return successor_values

    async def record_integration_resolution_push_on(
        self, conn, intent_id: str, evidence: dict[str, Any]
    ) -> dict:
        """Persist one stable post-push observation and its lifecycle fact."""
        intent = await self._locked_intent(conn, intent_id)
        if intent["state"] not in {"resolution_reserved", "committed"}:
            raise ValueError("promotion has no reserved conflict resolution")
        if intent["resolution_push_evidence"] is None:
            await conn.execute(
                update(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == intent_id)
                .where(integration_promotion_intents.c.resolution_push_evidence.is_(None))
                .values(resolution_push_evidence=evidence, updated_at=time.time())
            )
            intent = intent | {"resolution_push_evidence": evidence}
        await enqueue_integration_event(
            conn,
            event_id=f"resolution-pushed-{intent_id}",
            dedup_key=f"integration.resolution_push_observed:{intent_id}",
            project_id=intent["project_id"],
            event_type="integration.resolution_push_observed",
            payload={
                "project_id": intent["project_id"],
                "operation_id": intent["resolution_operation_id"],
                "promotion_intent_id": intent_id,
            },
            available_at=time.time(),
        )
        return intent

    async def mark_integration_resolution_push_started_on(
        self, conn, intent_id: str, *, started_at: float
    ) -> dict:
        """Persist the pre-write fence for one resolution push attempt.

        This is intentionally not post-push evidence.  Once set, a crash may
        have occurred during the external write, so human recovery must
        reconcile rather than repeat that write.
        """
        intent = await self._locked_intent(conn, intent_id)
        if intent["state"] not in {"resolution_reserved", "committed"}:
            raise ValueError("promotion has no reserved conflict resolution")
        if (
            intent["resolution_push_evidence"] is not None
            or intent["resolution_push_started_at"] is not None
        ):
            return intent
        result = await conn.execute(
            update(integration_promotion_intents)
            .where(
                integration_promotion_intents.c.id == intent_id,
                integration_promotion_intents.c.state == "resolution_reserved",
                integration_promotion_intents.c.resolution_push_evidence.is_(None),
                integration_promotion_intents.c.resolution_push_started_at.is_(None),
            )
            .values(resolution_push_started_at=started_at, updated_at=time.time())
        )
        if result.rowcount != 1:
            raise ValueError("resolution push marker changed during preflight")
        return intent | {"resolution_push_started_at": started_at}

    async def authorize_legacy_resolution_recovery_on(
        self, conn, intent_id: str, evidence: dict[str, Any]
    ) -> dict:
        """Turn one legacy unknown-start reservation into a newly fenced attempt.

        The caller has already observed the immutable ``expected_target`` on
        the remote while holding the repair's exact ownership proof.  The
        sentinel is cleared only once and the observation remains durable, so
        a restart never mistakes an uninstrumented reservation for a normal
        post-marker NULL.
        """
        intent = await self._locked_intent(conn, intent_id)
        if (
            intent["state"] != "resolution_reserved"
            or intent["resolution_push_started_at"] != 0.0
            or intent["resolution_push_evidence"] is not None
            or intent["resolution_recovery_evidence"] is not None
        ):
            return intent
        result = await conn.execute(
            update(integration_promotion_intents)
            .where(
                integration_promotion_intents.c.id == intent_id,
                integration_promotion_intents.c.state == "resolution_reserved",
                integration_promotion_intents.c.resolution_push_started_at == 0.0,
                integration_promotion_intents.c.resolution_push_evidence.is_(None),
                integration_promotion_intents.c.resolution_recovery_evidence.is_(None),
            )
            .values(
                resolution_push_started_at=None,
                resolution_recovery_evidence=evidence,
                updated_at=time.time(),
            )
        )
        if result.rowcount != 1:
            raise ValueError("legacy resolution recovery changed during authorization")
        return intent | {
            "resolution_push_started_at": None,
            "resolution_recovery_evidence": evidence,
        }

    async def mark_integration_promotion_prepared(
        self, intent_id: str, *, prepared_sha: str, recovery_ref: str
    ) -> dict:
        async with self.immediate() as conn:
            row = await self._locked_intent(conn, intent_id)
            if row["state"] == "conflict":
                raise ValueError("conflicted promotion cannot become prepared")
            if row["state"] == "superseded":
                raise ValueError("superseded promotion cannot become prepared")
            if row["prepared_sha"] is not None and (
                row["prepared_sha"] != prepared_sha or row["recovery_ref"] != recovery_ref
            ):
                raise ValueError("prepared promotion identity changed")
            if row["state"] == "committed":
                return row
            await conn.execute(
                update(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == intent_id)
                .values(
                    prepared_sha=prepared_sha,
                    recovery_ref=recovery_ref,
                    state="prepared",
                    updated_at=time.time(),
                )
            )
            return dict(row) | {
                "prepared_sha": prepared_sha,
                "recovery_ref": recovery_ref,
                "state": "prepared",
            }

    async def mark_integration_promotion_conflict(
        self, intent_id: str, diagnostics: dict[str, Any]
    ) -> dict:
        async with self.immediate() as conn:
            row = await self._locked_intent(conn, intent_id)
            if row["state"] == "superseded":
                raise ValueError("superseded promotion cannot become a conflict")
            if row["prepared_sha"] is not None or row["state"] == "committed":
                raise ValueError("prepared promotion cannot become a conflict")
            await conn.execute(
                update(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == intent_id)
                .values(
                    state="conflict",
                    conflict_diagnostics=diagnostics,
                    updated_at=time.time(),
                )
            )
            return dict(row) | {"state": "conflict", "conflict_diagnostics": diagnostics}

    async def mark_integration_promotion_pushed(
        self, intent_id: str, evidence: dict[str, Any]
    ) -> None:
        async with self.immediate() as conn:
            row = await self._locked_intent(conn, intent_id)
            if row["state"] == "superseded":
                raise ValueError("superseded promotion cannot be pushed")
            if row["state"] == "committed":
                return
            if row["prepared_sha"] is None:
                raise ValueError("unprepared promotion cannot be pushed")
            await conn.execute(
                update(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == intent_id)
                .values(state="pushed", remote_evidence=evidence, updated_at=time.time())
            )

    async def list_integration_delivery_receipts(
        self,
        *,
        source_task_id: str,
        repository_id: str,
        target_branch: str,
    ) -> list[dict]:
        statement = (
            select(task_delivery_receipts)
            .where(task_delivery_receipts.c.source_task_id == source_task_id)
            .where(task_delivery_receipts.c.repository_id == repository_id)
            .where(task_delivery_receipts.c.target_branch == target_branch)
            .order_by(task_delivery_receipts.c.created_at, task_delivery_receipts.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    async def _promotion_intent_by_domain(conn, domain_key: str) -> dict | None:
        row = (
            (
                await conn.execute(
                    select(integration_promotion_intents).where(
                        integration_promotion_intents.c.domain_key == domain_key
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def _unresolved_target_intent(conn, repository_id: str, branch: str) -> dict | None:
        row = (
            (
                await conn.execute(
                    select(integration_promotion_intents)
                    .where(integration_promotion_intents.c.repository_id == repository_id)
                    .where(integration_promotion_intents.c.target_branch == branch)
                    .where(
                        integration_promotion_intents.c.state.not_in(
                            ("committed", "conflict", "superseded")
                        )
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def _locked_intent(conn, intent_id: str) -> dict:
        row = (
            (
                await conn.execute(
                    select(integration_promotion_intents)
                    .where(integration_promotion_intents.c.id == intent_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("promotion intent does not exist")
        return dict(row)

    @staticmethod
    def _assert_same_intent(existing: dict, values: dict[str, Any]) -> None:
        changed = [
            field for field in _REQUEST_IDENTITY_FIELDS if existing.get(field) != values.get(field)
        ]
        if changed:
            raise ValueError("promotion intent identity changed: " + ", ".join(changed))
