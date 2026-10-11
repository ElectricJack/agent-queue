"""Connection-aware ask ledger primitives; callers own source-first transactions.

No method commits or acquires a source lock. Lock the source, then ask, then
its decision/operation/feedback rows; domain gate/waiter locks come last.
Decision/snapshot inserts are append-only. Unknown operations need an explicit
source reconciliation receipt before retry, never an expired-lease takeover.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.asks.models import AskError, digest
from src.database.tables import (
    approved_document_snapshots,
    ask_decision_previews,
    ask_decisions,
    ask_reconciliation_cases,
    ask_source_aliases,
    doc_review_comments,
    doc_review_revisions,
    doc_reviews,
    escalation_actions,
    escalation_messages,
    human_asks,
    review_feedback_obligations,
)


def _dict(row):
    return dict(row) if row is not None else None


class AskQueriesMixin:
    async def get_ask(self, ask_id: str, *, conn, lock: bool = False) -> dict | None:
        stmt = select(human_asks).where(human_asks.c.id == ask_id)
        if lock:
            stmt = stmt.with_for_update()
        return _dict((await conn.execute(stmt)).mappings().first())

    async def get_ask_by_source(
        self, project_id: str, source_kind: str, source_key: str, *, conn, lock: bool = False
    ) -> dict | None:
        stmt = select(human_asks).where(
            human_asks.c.project_id == project_id,
            human_asks.c.source_kind == source_kind,
            human_asks.c.source_key == source_key,
        )
        if lock:
            stmt = stmt.with_for_update()
        return _dict((await conn.execute(stmt)).mappings().first())

    async def insert_ask(self, values: dict, *, conn) -> tuple[dict, bool]:
        row = (
            (
                await conn.execute(
                    pg_insert(human_asks)
                    .values(**values)
                    .on_conflict_do_nothing(
                        constraint="uq_human_asks_source",
                    )
                    .returning(human_asks)
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row), True
        return await self.get_ask_by_source(
            values["project_id"], values["source_kind"], values["source_key"], conn=conn, lock=True
        ), False

    async def update_ask_projection(self, ask_id: str, values: dict, *, conn) -> dict:
        allowed = {
            "source_fingerprint",
            "generation_fingerprint",
            "feedback_version",
            "decision_generation",
            "display_version",
            "context",
            "audience",
            "logical_owner",
            "decider",
            "next_actor",
            "decision_state",
            "application_state",
            "context_state",
            "severity",
            "updated_at",
        }
        if values.keys() - allowed:
            raise ValueError("Ask identity cannot be updated")
        return dict(
            (
                await conn.execute(
                    update(human_asks)
                    .where(human_asks.c.id == ask_id)
                    .values(**values)
                    .returning(human_asks)
                )
            )
            .mappings()
            .one()
        )

    async def get_ask_alias(self, project_id: str, kind: str, key: str, *, conn) -> dict | None:
        return _dict(
            (
                await conn.execute(
                    select(ask_source_aliases).where(
                        ask_source_aliases.c.project_id == project_id,
                        ask_source_aliases.c.legacy_kind == kind,
                        ask_source_aliases.c.legacy_id == key,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def insert_ask_alias(self, values: dict, *, conn) -> tuple[dict, bool]:
        row = await conn.execute(
            pg_insert(ask_source_aliases)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ask_source_aliases)
        )
        inserted = row.mappings().first()
        existing = (
            dict(inserted)
            if inserted
            else await self.get_ask_alias(
                values["project_id"],
                values["legacy_kind"],
                values["legacy_id"],
                conn=conn,
            )
        )
        matches = all(existing[k] == values[k] for k in ("ask_id", "source_reference"))
        return existing, matches

    async def insert_ask_reconciliation_case(self, values: dict, *, conn) -> dict:
        result = (
            (
                await conn.execute(
                    pg_insert(ask_reconciliation_cases)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_ask_reconciliation_cases_conflict")
                    .returning(ask_reconciliation_cases)
                )
            )
            .mappings()
            .first()
        )
        if result:
            return dict(result)
        return dict(
            (
                await conn.execute(
                    select(ask_reconciliation_cases).where(
                        ask_reconciliation_cases.c.project_id == values["project_id"],
                        ask_reconciliation_cases.c.conflict_key == values["conflict_key"],
                    )
                )
            )
            .mappings()
            .one()
        )

    async def resolve_ask_reconciliation_case(
        self, case_id: str, receipt: dict, *, now: float, conn
    ) -> bool:
        if not receipt:
            raise ValueError("A repair receipt is required")
        return bool(
            (
                await conn.execute(
                    update(ask_reconciliation_cases)
                    .where(
                        ask_reconciliation_cases.c.id == case_id,
                        ask_reconciliation_cases.c.status == "open",
                    )
                    .values(status="resolved", repair_receipt=receipt, updated_at=now)
                )
            ).rowcount
        )

    async def insert_ask_preview(self, values: dict, *, conn) -> dict:
        if digest(values["packet"]) != values["packet_sha256"]:
            raise ValueError("Preview packet hash does not match")
        return dict(
            (
                await conn.execute(
                    insert(ask_decision_previews).values(**values).returning(ask_decision_previews)
                )
            )
            .mappings()
            .one()
        )

    async def get_ask_preview(self, preview_id: str, *, conn) -> dict | None:
        return _dict(
            (
                await conn.execute(
                    select(ask_decision_previews)
                    .where(
                        ask_decision_previews.c.id == preview_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )

    async def consume_ask_preview(self, preview_id: str, *, now: float, conn) -> bool:
        return bool(
            (
                await conn.execute(
                    update(ask_decision_previews)
                    .where(
                        ask_decision_previews.c.id == preview_id,
                        ask_decision_previews.c.consumed_at.is_(None),
                        ask_decision_previews.c.expires_at > now,
                    )
                    .values(consumed_at=now)
                )
            ).rowcount
        )

    async def insert_approved_document_snapshot(self, values: dict, *, conn) -> dict:
        if hashlib.sha256(values["body"]).hexdigest() != values["body_sha256"]:
            raise ValueError("Document bytes do not match their hash")
        return dict(
            (
                await conn.execute(
                    insert(approved_document_snapshots)
                    .values(**values)
                    .returning(approved_document_snapshots)
                )
            )
            .mappings()
            .one()
        )

    async def get_approved_document_snapshot(self, snapshot_id: str, *, conn) -> dict | None:
        return _dict(
            (
                await conn.execute(
                    select(approved_document_snapshots).where(
                        approved_document_snapshots.c.id == snapshot_id,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def get_ask_decision(self, decision_id: str, *, conn) -> dict | None:
        return _dict(
            (
                await conn.execute(
                    select(ask_decisions).where(
                        ask_decisions.c.id == decision_id,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def get_ask_decision_by_generation(
        self, ask_id: str, generation: int, *, conn
    ) -> dict | None:
        return _dict(
            (
                await conn.execute(
                    select(ask_decisions).where(
                        ask_decisions.c.ask_id == ask_id,
                        ask_decisions.c.decision_generation == generation,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def get_ask_decision_by_request(
        self, ask_id: str, actor_id: str, key: str, *, conn
    ) -> dict | None:
        return _dict(
            (
                await conn.execute(
                    select(ask_decisions).where(
                        ask_decisions.c.ask_id == ask_id,
                        ask_decisions.c.actor_id == actor_id,
                        ask_decisions.c.idempotency_key == key,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def insert_ask_decision(self, values: dict, *, conn) -> tuple[dict, bool]:
        if digest(values["packet"]) != values["packet_sha256"]:
            raise ValueError("Decision packet hash does not match")
        evidence = (
            (
                await conn.execute(
                    select(escalation_messages).where(
                        escalation_messages.c.id == values["evidence_entry_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            evidence["ask_id"] != values["ask_id"]
            or evidence["verified_actor"] != values["actor_id"]
        ):
            raise AskError("ask.not_decider", "Decision evidence does not match ask/principal")
        ask = await self.get_ask(values["ask_id"], conn=conn)
        if values.get("review_id") is not None:
            revision = (
                await conn.execute(
                    select(doc_review_revisions.c.content_sha256)
                    .join(doc_reviews, doc_reviews.c.id == doc_review_revisions.c.review_id)
                    .where(
                        doc_reviews.c.project_id == ask["project_id"],
                        doc_review_revisions.c.review_id == values["review_id"],
                        doc_review_revisions.c.revision == values.get("review_revision"),
                    )
                )
            ).first()
            if revision is None or revision[0] != values.get("content_sha256"):
                raise AskError("ask.stale_source", "Review project/revision/hash does not match")
        if values.get("snapshot_id") is not None:
            snapshot = await self.get_approved_document_snapshot(values["snapshot_id"], conn=conn)
            if (
                snapshot is None
                or snapshot["project_id"] != ask["project_id"]
                or snapshot["body_sha256"] != values.get("content_sha256")
            ):
                raise AskError(
                    "ask.stale_source", "Standalone project/snapshot/hash does not match"
                )
        inserted = (
            (
                await conn.execute(
                    pg_insert(ask_decisions)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(ask_decisions)
                )
            )
            .mappings()
            .first()
        )
        if inserted:
            return dict(inserted), True
        existing = await self.get_ask_decision_by_request(
            values["ask_id"],
            values["actor_id"],
            values["idempotency_key"],
            conn=conn,
        )
        if existing is None:
            raise AskError("ask.stale_source", "This decision generation already has a decision")
        if existing["request_sha256"] != values["request_sha256"]:
            raise AskError(
                "ask.idempotency_conflict", "Request key reused for a different decision"
            )
        return existing, False

    async def insert_ask_correspondence(self, values: dict, *, conn) -> tuple[dict, bool]:
        inserted = (
            (
                await conn.execute(
                    pg_insert(escalation_messages)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(escalation_messages)
                )
            )
            .mappings()
            .first()
        )
        if inserted:
            return dict(inserted), True
        existing = (
            (
                await conn.execute(
                    select(escalation_messages).where(
                        or_(
                            escalation_messages.c.id == values["id"],
                            and_(
                                escalation_messages.c.transport == values["transport"],
                                escalation_messages.c.external_message_id
                                == values.get("external_message_id"),
                                escalation_messages.c.external_message_id.is_not(None),
                            ),
                        )
                    )
                )
            )
            .mappings()
            .one()
        )
        if any(existing[k] != values.get(k) for k in ("ask_id", "verified_actor", "text")):
            raise AskError(
                "ask.idempotency_conflict", "Correspondence replay has different evidence"
            )
        return dict(existing), False

    async def insert_ask_operation(self, values: dict, *, conn) -> tuple[dict, bool]:
        if not values.get("ask_id") or not values.get("operation_key"):
            raise ValueError("Human operations require an ask and stable operation key")
        if values.get("decision_id"):
            decision = await self.get_ask_decision(values["decision_id"], conn=conn)
            if decision is None or decision["ask_id"] != values["ask_id"]:
                raise AskError("ask.stale_source", "Operation decision belongs to another ask")
        inserted = (
            (
                await conn.execute(
                    pg_insert(escalation_actions)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_escalation_actions_operation")
                    .returning(escalation_actions)
                )
            )
            .mappings()
            .first()
        )
        if inserted:
            return dict(inserted), True
        existing = (
            (
                await conn.execute(
                    select(escalation_actions).where(
                        escalation_actions.c.ask_id == values["ask_id"],
                        escalation_actions.c.operation_key == values["operation_key"],
                    )
                )
            )
            .mappings()
            .one()
        )
        if any(
            existing[k] != values[k]
            for k in ("decision_id", "operation_kind", "parameters", "dependency_keys")
        ):
            raise AskError("ask.idempotency_conflict", "Operation key has different work")
        return dict(existing), False

    async def claim_ask_operation(
        self, operation_id: str, owner: str, *, now: float, lease_seconds: float, conn
    ) -> dict | None:
        if not owner or lease_seconds <= 0:
            raise ValueError("A lease needs an owner and positive duration")
        operation = (
            (
                await conn.execute(
                    select(escalation_actions)
                    .where(
                        escalation_actions.c.id == operation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            not operation
            or not operation["ask_id"]
            or operation["status"] != "pending"
            or (operation["retry_at"] is not None and operation["retry_at"] > now)
        ):
            return None
        dependencies = set(operation["dependency_keys"])
        if dependencies:
            satisfied = set(
                (
                    await conn.execute(
                        select(escalation_actions.c.operation_key).where(
                            escalation_actions.c.ask_id == operation["ask_id"],
                            escalation_actions.c.operation_key.in_(dependencies),
                            escalation_actions.c.status == "succeeded",
                        )
                    )
                ).scalars()
            )
            if satisfied != dependencies:
                return None
        return dict(
            (
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == operation_id,
                    )
                    .values(
                        status="processing",
                        lease_owner=owner,
                        lease_expires_at=now + lease_seconds,
                        lease_generation=escalation_actions.c.lease_generation + 1,
                    )
                    .returning(escalation_actions)
                )
            )
            .mappings()
            .one()
        )

    async def complete_ask_operation(
        self,
        operation_id: str,
        owner: str,
        generation: int,
        *,
        state: str,
        receipt: dict,
        now: float,
        conn,
    ) -> bool:
        if state not in {"succeeded", "failed", "unknown", "cancelled"} or not receipt:
            raise ValueError("An operation completion needs its actual state and receipt")
        return bool(
            (
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == operation_id,
                        escalation_actions.c.status == "processing",
                        escalation_actions.c.lease_owner == owner,
                        escalation_actions.c.lease_generation == generation,
                        escalation_actions.c.lease_expires_at > now,
                    )
                    .values(
                        status=state,
                        outcome=state,
                        receipt=receipt,
                        completed_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                )
            ).rowcount
        )

    async def expire_ask_operation(self, operation_id: str, *, now: float, conn) -> bool:
        """Ambiguous abandoned execution is UNKNOWN, never pending/retryable."""
        return bool(
            (
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == operation_id,
                        escalation_actions.c.ask_id.is_not(None),
                        escalation_actions.c.status == "processing",
                        escalation_actions.c.lease_expires_at <= now,
                    )
                    .values(
                        status="unknown",
                        outcome="lease_expired",
                        completed_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                )
            ).rowcount
        )

    async def reconcile_ask_operation(
        self,
        operation_id: str,
        generation: int,
        *,
        state: str,
        receipt: dict,
        retry_safe: bool = False,
        conn,
    ) -> bool:
        if not receipt or state not in {"pending", "succeeded", "failed", "cancelled"}:
            raise ValueError("Source reconciliation requires an explicit receipt and outcome")
        if state == "pending" and not retry_safe:
            raise AskError("ask.application_unknown", "Source has not proved retry safe")
        values: dict[str, Any] = {
            "status": state,
            "receipt": receipt,
            "outcome": None if state == "pending" else state,
        }
        if state == "pending":
            values["completed_at"] = None
        return bool(
            (
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == operation_id,
                        escalation_actions.c.status == "unknown",
                        escalation_actions.c.lease_generation == generation,
                    )
                    .values(**values)
                )
            ).rowcount
        )

    async def insert_review_feedback_obligation(self, values: dict, *, conn) -> tuple[dict, bool]:
        if values.get("comment_id"):
            decision = await self.get_ask_decision(values["decision_id"], conn=conn)
            comment = (
                (
                    await conn.execute(
                        select(doc_review_comments).where(
                            doc_review_comments.c.id == values["comment_id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                decision is None
                or comment is None
                or comment["review_id"] != decision["review_id"]
                or comment["body"] != values["original_text"]
                or comment["author"] != values["original_author"]
                or comment["revision"] != values["original_revision"]
            ):
                raise AskError(
                    "ask.stale_feedback", "Feedback origin differs from the reviewed comment"
                )
        inserted = (
            (
                await conn.execute(
                    pg_insert(review_feedback_obligations)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_review_feedback_obligations_item")
                    .returning(review_feedback_obligations)
                )
            )
            .mappings()
            .first()
        )
        if inserted:
            return dict(inserted), True
        existing = (
            (
                await conn.execute(
                    select(review_feedback_obligations).where(
                        review_feedback_obligations.c.decision_id == values["decision_id"],
                        review_feedback_obligations.c.feedback_key == values["feedback_key"],
                    )
                )
            )
            .mappings()
            .one()
        )
        mutable = {"id", "task_id", "operation_id", "progress", "completion_evidence", "updated_at"}
        if any(existing[k] != v for k, v in values.items() if k not in mutable):
            raise AskError(
                "ask.idempotency_conflict", "Feedback obligation differs from frozen packet"
            )
        return dict(existing), False

    async def list_review_feedback_obligations(self, decision_id: str, *, conn) -> list[dict]:
        rows = await conn.execute(
            select(review_feedback_obligations)
            .where(
                review_feedback_obligations.c.decision_id == decision_id,
            )
            .order_by(review_feedback_obligations.c.feedback_key)
        )
        return [dict(row) for row in rows.mappings()]

    async def update_review_feedback_progress(
        self,
        obligation_id: str,
        *,
        progress: str,
        evidence: dict | None,
        now: float,
        conn,
        task_id: str | None = None,
        operation_id: str | None = None,
    ) -> bool:
        if progress == "completed" and not evidence:
            raise ValueError("Each feedback item needs its own completion evidence")
        values = {"progress": progress, "completion_evidence": evidence, "updated_at": now}
        if task_id is not None:
            values["task_id"] = task_id
        if operation_id is not None:
            values["operation_id"] = operation_id
        return bool(
            (
                await conn.execute(
                    update(review_feedback_obligations)
                    .where(
                        review_feedback_obligations.c.id == obligation_id,
                        review_feedback_obligations.c.progress != "completed",
                    )
                    .values(**values)
                )
            ).rowcount
        )
