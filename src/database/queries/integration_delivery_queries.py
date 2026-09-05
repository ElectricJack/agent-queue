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
    task_branch_origins,
    task_delivery_receipts,
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
        if (
            result["source_base"] != source_base
            or result["reviewed_head_sha"] != reviewed_head_sha
            or result["verdict"] != "approved"
        ):
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
            if intent["prepared_sha"] is None:
                raise ValueError("unprepared promotion cannot be finalized")
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
            review_snapshot = {
                "review": intent["review_evidence"],
                "authors": intent["authors"],
                "provenance": intent["provenance"],
                "commit": intent["commit_metadata"],
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
                "squash_sha": intent["prepared_sha"],
                "after_sha": intent["prepared_sha"],
                "review_evidence": review_snapshot,
                "disposition": "code",
                "created_at": committed_at,
            }
            if existing is None:
                await conn.execute(insert(task_delivery_receipts).values(**receipt))
            elif {key: existing[key] for key in receipt} != receipt:
                raise ValueError("delivery receipt identity changed")

            payload = {
                "project_id": intent["project_id"],
                "operation_id": intent["id"],
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
