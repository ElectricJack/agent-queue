"""Durable review evidence, promotion intents, and delivery receipts."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    integration_promotion_intents,
    integration_review_evidence,
    integration_repair_operations,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
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
            existing = await self._promotion_intent_by_domain(conn, values["domain_key"])
            if existing is not None:
                self._assert_same_intent(existing, values)
                return existing

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

    async def mark_integration_promotion_prepared(
        self, intent_id: str, *, prepared_sha: str, recovery_ref: str
    ) -> dict:
        async with self.immediate() as conn:
            row = await self._locked_intent(conn, intent_id)
            if row["state"] == "conflict":
                raise ValueError("conflicted promotion cannot become prepared")
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
            if row["state"] == "committed":
                return
            if row["prepared_sha"] is None:
                raise ValueError("unprepared promotion cannot be pushed")
            await conn.execute(
                update(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == intent_id)
                .values(state="pushed", remote_evidence=evidence, updated_at=time.time())
            )

    async def finalize_integration_promotion(
        self, intent_id: str, remote_evidence: dict[str, Any]
    ) -> dict:
        """Insert receipt plus delivery/cleanup events in one transaction."""
        async with self.immediate() as conn:
            intent = await self._locked_intent(conn, intent_id)
            resolution = intent["resolution_head_sha"] is not None
            if not resolution and intent["prepared_sha"] is None:
                raise ValueError("unprepared promotion cannot be finalized")
            if resolution and intent["state"] not in {"resolution_reserved", "committed"}:
                raise ValueError("unreserved conflict resolution cannot be finalized")
            existing = (
                (
                    await conn.execute(
                        select(task_delivery_receipts).where(
                            task_delivery_receipts.c.id == intent["receipt_id"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            committed_at = intent["committed_at"] or time.time()
            hierarchy_mode = (
                await conn.execute(
                    select(projects.c.hierarchical_integration_mode).where(
                        projects.c.id == intent["project_id"]
                    )
                )
            ).scalar_one_or_none()
            parent_episode = None
            parent_operation_id = None
            if intent["target_task_id"]:
                parent_episode = (
                    await conn.execute(
                        select(task_integration_checkpoints.c.episode_id).where(
                            task_integration_checkpoints.c.task_id == intent["target_task_id"]
                        )
                    )
                ).scalar_one_or_none()
            if (
                existing is None
                and intent["target_task_id"]
                and hierarchy_mode in {"hierarchy", "train"}
            ):
                if parent_episode is None:
                    raise ValueError("hierarchical parent has no current collection episode")
                parent_operation_id = (
                    await conn.execute(
                        select(integration_repair_operations.c.id).where(
                            integration_repair_operations.c.parent_task_id
                            == intent["target_task_id"],
                            integration_repair_operations.c.episode_id == parent_episode,
                            integration_repair_operations.c.state.in_(
                                ("active", "escalated", "human_required")
                            ),
                        )
                    )
                ).scalar_one_or_none()
                expected_operation_id = (
                    intent["resolution_operation_id"]
                    if resolution
                    else intent["fence_owner_id"]
                )
                if parent_operation_id != expected_operation_id:
                    raise ValueError(
                        "promotion collector is not the current parent operation"
                    )
            elif existing is not None:
                parent_episode = existing["parent_episode_id"]
                parent_operation_id = existing["parent_operation_id"]
            review_snapshot = {
                "review": intent["review_evidence"],
                "authors": intent["authors"],
                "provenance": intent["provenance"],
                "commit": intent["commit_metadata"],
            }
            resolution_evidence = None
            if resolution:
                resolution_evidence = {
                    "kind": "conflict_resolution",
                    "original_source_base": intent["source_base"],
                    "original_source_head": intent["source_head"],
                    "original_source_tree": intent["review_evidence"]["reviewed_tree_sha"],
                    "original_expected_target": intent["expected_target"],
                    "resolved_head_sha": intent["resolution_head_sha"],
                    "resolved_tree_sha": intent["resolution_tree_sha"],
                    "repair_commit_shas": intent["resolution_commit_shas"],
                    "authoring": {
                        "operation_id": intent["resolution_operation_id"],
                        "stage_ordinal": intent["resolution_stage_ordinal"],
                        "repair_task_id": intent["resolution_task_id"],
                        "repair_session_id": intent["resolution_session_id"],
                        "repair_session_instance_token": intent[
                            "resolution_session_instance_token"
                        ],
                        "repair_workspace_id": intent["resolution_workspace_id"],
                        "fence": {
                            "repository_id": intent["repository_id"],
                            "branch": intent["target_branch"],
                            "owner_id": intent["resolution_fence_owner_id"],
                            "token": intent["resolution_fence_token"],
                        },
                    },
                    "push_authority": intent["resolution_push_evidence"],
                    "remote_proof": remote_evidence,
                }
            receipt = {
                "id": intent["receipt_id"],
                "domain_key": intent["domain_key"],
                "source_task_id": intent["source_task_id"],
                "target_task_id": intent["target_task_id"],
                "repository_id": intent["repository_id"],
                "target_branch": intent["target_branch"],
                "reviewed_head_sha": intent["source_head"],
                "reviewed_tree_sha": intent["review_evidence"]["reviewed_tree_sha"],
                "before_sha": intent["expected_target"],
                "squash_sha": None if resolution else intent["prepared_sha"],
                "after_sha": (
                    intent["resolution_head_sha"] if resolution else intent["prepared_sha"]
                ),
                "review_evidence": review_snapshot,
                "resolution_evidence": resolution_evidence,
                "parent_operation_id": parent_operation_id,
                "parent_episode_id": parent_episode if parent_operation_id else None,
                "disposition": "code",
                "created_at": committed_at,
            }
            if existing is None:
                await conn.execute(insert(task_delivery_receipts).values(**receipt))
            elif {key: existing[key] for key in receipt} != receipt:
                raise ValueError("delivery receipt identity changed")

            if (
                intent["target_task_id"]
                and parent_episode is not None
                and hierarchy_mode in {"hierarchy", "train"}
            ):
                from src.integration.parent_completion import ParentCompletion

                try:
                    await ParentCompletion(self).mark_ready_on(
                        conn, intent["target_task_id"]
                    )
                except Exception as exc:
                    # Parent targets must have a coherent active episode.
                    # Do not commit a receipt that cannot update its owning
                    # readiness projection.
                    raise ValueError(f"parent readiness projection failed: {exc}") from exc

            payload = {
                "project_id": intent["project_id"],
                "operation_id": (
                    intent["resolution_operation_id"]
                    if resolution
                    else intent["fence_owner_id"]
                ),
                "promotion_intent_id": intent["id"],
                "receipt_id": intent["receipt_id"],
                "source_task_id": intent["source_task_id"],
                "target_task_id": intent["target_task_id"],
                "repository_id": intent["repository_id"],
                "target_branch": intent["target_branch"],
            }
            await enqueue_integration_event(
                conn,
                event_id=f"delivery-{intent['receipt_id']}",
                dedup_key=f"delivery.applied:{intent['domain_key']}",
                project_id=intent["project_id"],
                event_type="delivery.applied",
                payload=payload,
                available_at=committed_at,
            )
            await enqueue_integration_event(
                conn,
                event_id=f"cleanup-{intent['receipt_id']}",
                dedup_key=f"integration.cleanup_pending:{intent['domain_key']}",
                project_id=intent["project_id"],
                event_type="integration.cleanup_pending",
                payload=payload,
                available_at=committed_at,
            )
            await conn.execute(
                update(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == intent_id)
                .values(
                    state="committed",
                    remote_evidence=remote_evidence,
                    committed_at=committed_at,
                    updated_at=committed_at,
                )
            )
            return receipt

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
                    .where(integration_promotion_intents.c.state.not_in(("committed", "conflict")))
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
