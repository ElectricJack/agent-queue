"""Recoverable review application. Database effects commit with their source receipts.

Reservation never releases a gate. Filing is serialized on the source in the task
service's transaction; its post-create hook owns mappings and the application gate.
Finalization verifies those receipts before transitioning the review and gates.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import insert, select, update

from src.asks.models import AskError, digest
from src.asks.review import FINAL_DISPOSITIONS, authorize_application
from src.asks.service import new_id
from src.database.tables import (
    doc_reviews,
    escalation_actions,
    gates,
    task_criteria,
    task_gates,
    tasks,
)


async def operation(db, decision, kind, *, conn, dependencies=(), parameters=None):
    key = f"{kind}:{decision['id']}"
    return (
        await db.insert_ask_operation(
            {
                "id": new_id("ask-op"),
                "ask_id": decision["ask_id"],
                "decision_id": decision["id"],
                "reply_id": decision["evidence_entry_id"],
                "idempotency_key": key,
                "action_kind": "ask_operation",
                "target_id": decision["review_id"] or decision["snapshot_id"],
                "operation_kind": kind,
                "operation_key": key,
                "dependency_keys": list(dependencies),
                "parameters": {"packet_sha256": decision["packet_sha256"], **(parameters or {})},
                "executor": f"supervisor:{decision['packet']['source']['project_id']}",
                "started_revision": decision["decision_generation"],
                "status": "pending",
                "created_at": decision["created_at"],
            },
            conn=conn,
        )
    )[0]


async def get_operation(conn, decision_id, kind, *, lock=False):
    stmt = select(escalation_actions).where(
        escalation_actions.c.decision_id == decision_id,
        escalation_actions.c.operation_kind == kind,
    )
    row = (await conn.execute(stmt.with_for_update() if lock else stmt)).mappings().first()
    return dict(row) if row else None


async def succeed(conn, op, receipt):
    """For database effects only: caller holds source and operation, effect shares conn."""
    await conn.execute(
        update(escalation_actions)
        .where(escalation_actions.c.id == op["id"])
        .values(
            status="succeeded",
            outcome="succeeded",
            receipt=receipt,
            completed_at=time.time(),
            lease_owner=None,
            lease_expires_at=None,
            error=None,
        )
    )


async def reserve_review_operations(db, decision, *, conn):
    domain = decision["packet"]["domain"]
    filing = await operation(db, decision, "file_task", conn=conn) if domain["task_kind"] else None
    for item in domain["feedback"]:
        await db.insert_review_feedback_obligation(
            {
                "id": f"feedback-{digest([decision['id'], item['feedback_key']])}",
                "decision_id": decision["id"],
                **item,
                "accepted_by": {
                    **item["accepted_by"],
                    "evidence_entry_id": decision["evidence_entry_id"],
                },
                "logical_owner": f"supervisor:{decision['packet']['source']['project_id']}",
                "operation_id": filing["id"]
                if filing and item["disposition"] not in FINAL_DISPOSITIONS
                else None,
                "progress": "completed" if item["disposition"] in FINAL_DISPOSITIONS else "pending",
                "completion_evidence": item["accepted_by"]
                if item["disposition"] in FINAL_DISPOSITIONS
                else None,
                "created_at": decision["created_at"],
                "updated_at": decision["created_at"],
            },
            conn=conn,
        )
    if domain["playbook"] and domain["effective_option"] == "approve":
        await operation(db, decision, "playbook", conn=conn)
    await conn.execute(
        update(doc_reviews)
        .where(doc_reviews.c.id == decision["review_id"])
        .values(
            pending_decision_id=decision["id"],
        )
    )


class FilingExists(Exception):
    """The pre-create transaction found the original filing's durable receipt."""


class ReviewOperations:
    def __init__(self, service):
        self.service, self.db = service, service.db

    async def reconcile_review(self, review_id):
        from src.database.tables import ask_decisions

        async with self.db.immediate() as conn:
            review = await self.db.lock_review_for_mutation(review_id, conn)
            decision = (
                (
                    await conn.execute(
                        select(ask_decisions)
                        .where(
                            ask_decisions.c.review_id == review_id,
                            ask_decisions.c.review_revision == review["current_revision"],
                        )
                        .order_by(ask_decisions.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if (
                decision is None
                and review["state"] == "in_review"
                and not review["pending_decision_id"]
            ):
                current = await self.db.get_review_revision(
                    review_id, review["current_revision"], conn=conn
                )
                if self.service.vault_state(review, current["content_sha256"]) == "missing":
                    await self.service._rewrite_status(review, current)
                    if self.service.vault_state(review, current["content_sha256"]) == "ok":
                        return {"application_state": "applied", "mirror_only": True}
            if decision is None:
                await self.db.insert_ask_reconciliation_case(
                    {
                        "id": new_id("ask-repair"),
                        "project_id": review["project_id"],
                        "conflict_key": f"legacy-review:{review_id}:{review['current_revision']}",
                        "source_evidence": {"review_id": review_id, "state": review["state"]},
                        "logical_owner": f"supervisor:{review['project_id']}",
                        "reason": "Review projection has no immutable decision and verified feedback receipts",
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    },
                    conn=conn,
                )
                return {
                    "application_state": "unknown",
                    "error": "Review requires legacy reconciliation",
                }
            decision = dict(decision)
        result = await self.apply(decision["id"])
        # Verify the mirror even when its earlier operation succeeded but the
        # file was subsequently removed. Divergence remains a repair case.
        if result["application_state"] == "applied":
            try:
                await self.mirror(decision)
            except AskError as exc:
                return {**result, "application_state": "failed", "error": str(exc)}
        return result

    async def locked(self, decision, conn):
        review = await self.db.lock_review_for_mutation(decision["review_id"], conn)
        ask = await self.db.get_ask(decision["ask_id"], conn=conn, lock=True)
        if not review or not ask:
            raise AskError("ask.application_unknown", "Decision source is missing")
        return review

    async def project(self, decision, values, *, conn):
        # A replay of an old accepted decision must not overwrite a newer ask.
        ask = await self.db.get_ask(decision["ask_id"], conn=conn)
        if ask["decision_generation"] == decision["decision_generation"]:
            await self.db.update_ask_projection(decision["ask_id"], values, conn=conn)

    async def verified_obligations(self, decision, *, conn):
        items = await self.db.list_review_feedback_obligations(decision["id"], conn=conn)
        expected = {i["feedback_key"]: i for i in decision["packet"]["domain"]["feedback"]}
        if {i["feedback_key"] for i in items} != set(expected):
            raise AskError("ask.application_unknown", "Frozen feedback obligation is missing")
        for item in items:
            frozen = expected[item["feedback_key"]]
            if any(
                item[key] != frozen[key]
                for key in (
                    "comment_id",
                    "original_text",
                    "original_author",
                    "original_revision",
                    "original_anchor",
                    "disposition",
                    "required_before_release",
                    "acceptance_text",
                )
            ):
                raise AskError(
                    "ask.application_unknown", "Feedback differs from the accepted packet"
                )
        return items

    async def before_filing(self, decision, conn):
        review = await self.locked(decision, conn)
        await authorize_application(review, decision, conn=conn, service=self.service)
        op = await get_operation(conn, decision["id"], "file_task", lock=True)
        if op["status"] == "succeeded":
            raise FilingExists()
        if review["pending_decision_id"] != decision["id"]:
            raise AskError("ask.stale_source", "Review is no longer held by this decision")
        if op["status"] != "pending":
            raise AskError("ask.application_unknown", "Task filing requires reconciliation")
        # An independently reused key cannot be adopted merely because its title matches.
        key = task_key(decision)
        existing = (
            await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.project_id == review["project_id"],
                    tasks.c.dedup_key == key,
                )
            )
        ).first()
        if existing:
            raise AskError("ask.idempotency_conflict", "Task key exists without the packet receipt")

    async def record_filing(self, decision, conn, task_id, parent_id):
        """Called inside _after_create_on after before_filing locked the source."""
        op = await get_operation(conn, decision["id"], "file_task", lock=True)
        domain = decision["packet"]["domain"]
        application_gate, _ = await self.db.create_gate(
            domain["review"]["project_id"],
            "event",
            "Review decision application",
            await_id=f"ask-application:{decision['id']}",
            waiter_task_ids=[task_id],
            conn=conn,
        )
        obligations = await self.verified_obligations(decision, conn=conn)
        actionable = [o for o in obligations if o["disposition"] not in FINAL_DISPOSITIONS]
        # Acceptance rows have no checklist-size cap; preserve every item in large reviews.
        for index, item in enumerate(actionable):
            await conn.execute(
                insert(task_criteria).values(
                    id=item["id"],
                    task_id=task_id,
                    type="acceptance",
                    content=f"{item['feedback_key']}: {item['acceptance_text']}",
                    sort_order=index,
                )
            )
            await self.db.update_review_feedback_progress(
                item["id"],
                progress="filed",
                evidence=None,
                task_id=task_id,
                operation_id=op["id"],
                now=time.time(),
                conn=conn,
            )
        receipt = {
            "task_id": task_id,
            "packet_sha256": decision["packet_sha256"],
            "application_gate_id": application_gate,
            "obligation_ids": [o["id"] for o in actionable],
            "parent_id": parent_id,
            "dedup_key": task_key(decision),
        }
        await self.db._upsert_meta(
            task_id,
            "review_response",
            {
                "review_id": decision["review_id"],
                "revision": decision["review_revision"],
                "decision_id": decision["id"],
                "packet_sha256": decision["packet_sha256"],
                "profile_source": "router",
                "task_kind": domain["task_kind"],
            },
            conn=conn,
        )
        await succeed(conn, op, receipt)

    async def apply(self, decision_id):
        async with self.db.immediate() as conn:
            decision = await self.db.get_ask_decision(decision_id, conn=conn)
        if not decision:
            raise AskError("ask.not_found", "Decision was not found")
        domain = decision["packet"]["domain"]
        try:
            async with self.db._engine.connect() as conn:
                filing = await get_operation(conn, decision["id"], "file_task")
            if domain["task_kind"] and filing["status"] != "succeeded":
                if self.service.hooks.file_feedback is None:
                    raise AskError(
                        "ask.application_unknown", "Feedback task service is unavailable"
                    )
                try:
                    await self.service.hooks.file_feedback(
                        decision,
                        lambda conn: self.before_filing(decision, conn),
                        lambda conn, tid, parent: self.record_filing(decision, conn, tid, parent),
                    )
                except FilingExists:
                    pass
            if domain["playbook"] and domain["effective_option"] == "approve":
                await self._external(decision, "playbook")
            result = await self.finalize(decision)
            for kind in ("mirror", "review_event", "ingestion", "notification"):
                await self._external(decision, kind)
            for gate_id in result.get("resolved_gate_ids", []):
                await self._external(decision, f"gate_event:{gate_id}")
            for task_id in result.get("blocked_flip_task_ids", []):
                await self._external(decision, f"task_event:{task_id}")
            async with self.db.immediate() as conn:
                await self.locked(decision, conn)
                remaining = (
                    await conn.execute(
                        select(escalation_actions.c.id).where(
                            escalation_actions.c.decision_id == decision["id"],
                            escalation_actions.c.status != "succeeded",
                            escalation_actions.c.operation_kind != "ingestion",
                        )
                    )
                ).first()
                if remaining:
                    raise AskError("ask.application_unknown", "Decision has unfinished operations")
                ingestion = await get_operation(conn, decision["id"], "ingestion")
                dispatch_pending = ingestion and ingestion["status"] != "succeeded"
                await self.project(
                    decision,
                    {
                        "application_state": "applying" if dispatch_pending else "applied",
                        "next_actor": "agent" if dispatch_pending else "none",
                        "updated_at": time.time(),
                    },
                    conn=conn,
                )
            async with self.db._engine.connect() as conn:
                playbook = await get_operation(conn, decision["id"], "playbook")
            return {
                **result,
                "application_state": "applied",
                "ingestion_state": "pending" if dispatch_pending else "acknowledged",
                **({"playbook": playbook["receipt"]} if playbook else {}),
            }
        except Exception as exc:
            # A concurrent finisher may already have completed every receipt. Do
            # not let its losing peer overwrite that applied projection with failed.
            async with self.db.immediate() as conn:
                review = await self.locked(decision, conn)
                failed = (
                    (
                        await conn.execute(
                            select(escalation_actions)
                            .where(
                                escalation_actions.c.decision_id == decision["id"],
                                escalation_actions.c.status != "succeeded",
                            )
                            .order_by(escalation_actions.c.created_at, escalation_actions.c.id)
                        )
                    )
                    .mappings()
                    .first()
                )
                playbook = await get_operation(conn, decision["id"], "playbook")
                applied = await get_operation(conn, decision["id"], "apply")
                if (
                    failed is None
                    and applied["status"] == "succeeded"
                    and getattr(exc, "code", "") == "ask.operation_in_progress"
                ):
                    await self.project(
                        decision,
                        {
                            "application_state": "applied",
                            "next_actor": "none",
                            "updated_at": time.time(),
                        },
                        conn=conn,
                    )
                    return {
                        **applied["receipt"],
                        "application_state": "applied",
                        **({"playbook": playbook["receipt"]} if playbook else {}),
                    }
                await self.project(
                    decision,
                    {
                        "application_state": "failed",
                        "next_actor": "agent",
                        "updated_at": time.time(),
                    },
                    conn=conn,
                )
                return {
                    "operation_id": failed["id"] if failed else None,
                    **({"playbook": playbook["receipt"]} if playbook else {}),
                    "review_id": decision["review_id"],
                    "decision_id": decision_id,
                    "state": review["state"],
                    "application_state": "failed",
                    "error": str(exc),
                    "error_code": getattr(exc, "code", "ask.application_unknown"),
                }

    async def finalize(self, decision):
        domain = decision["packet"]["domain"]
        async with self.db.immediate() as conn:
            review = await self.locked(decision, conn)
            await authorize_application(review, decision, conn=conn, service=self.service)
            apply = await get_operation(conn, decision["id"], "apply", lock=True)
            already_applied = apply["status"] == "succeeded"
            if already_applied and (
                review["current_revision"] != decision["review_revision"]
                or review["decision_generation"] != domain["review"]["decision_generation"]
            ):
                return apply["receipt"]
            if not already_applied and review["pending_decision_id"] != decision["id"]:
                raise AskError("ask.stale_source", "Review pending decision changed")
            obligations = await self.verified_obligations(decision, conn=conn)
            task_gate = None
            if domain["task_kind"]:
                filing = await get_operation(conn, decision["id"], "file_task", lock=True)
                if filing["status"] != "succeeded":
                    raise AskError("ask.application_unknown", "Feedback task has no filing receipt")
                receipt = filing["receipt"]
                task = (
                    (
                        await conn.execute(
                            select(tasks).where(
                                tasks.c.id == receipt["task_id"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not task
                    or task["status"] == "FAILED"
                    or task["dedup_key"] != task_key(decision)
                    or receipt["packet_sha256"] != decision["packet_sha256"]
                ):
                    raise AskError(
                        "ask.application_unknown", "Feedback task or packet receipt is missing"
                    )
                obligations = await self.verified_obligations(decision, conn=conn)
                actionable = {
                    i["id"] for i in obligations if i["disposition"] not in FINAL_DISPOSITIONS
                }
                if actionable != set(receipt["obligation_ids"]):
                    raise AskError(
                        "ask.application_unknown", "Filing receipt omits feedback obligations"
                    )
                for item in obligations:
                    if item["disposition"] in FINAL_DISPOSITIONS:
                        continue
                    criterion = (
                        await conn.execute(
                            select(task_criteria.c.id).where(
                                task_criteria.c.id == item["id"],
                                task_criteria.c.task_id == task["id"],
                                task_criteria.c.type == "acceptance",
                                task_criteria.c.content
                                == f"{item['feedback_key']}: {item['acceptance_text']}",
                            )
                        )
                    ).first()
                    if item["task_id"] != task["id"] or not criterion:
                        raise AskError(
                            "ask.application_unknown", "Feedback lacks a verified acceptance row"
                        )
                if domain["effective_option"] == "approve":
                    from src.asks.coverage import cover_review_waiters

                    await cover_review_waiters(self.db, decision, obligations, conn=conn)
                task_gate = receipt["application_gate_id"]
            if domain["playbook"] and domain["effective_option"] == "approve":
                pin = await get_operation(conn, decision["id"], "playbook", lock=True)
                if pin["status"] != "succeeded":
                    raise AskError(
                        "ask.application_unknown", "Pinned playbook application is pending"
                    )
            option = domain["effective_option"]
            state = {
                "approve": "approved",
                "decline": "rejected",
                "reject": "rejected",
                "fold_in": "changes_requested",
                "request_changes": "changes_requested",
            }[option]
            gate_ids = sorted(g for g in (review["gate_id"], task_gate) if g)
            locked_gates = list(
                (
                    await conn.execute(
                        select(gates)
                        .where(
                            gates.c.id.in_(gate_ids),
                        )
                        .order_by(gates.c.id)
                        .with_for_update()
                    )
                ).mappings()
            )
            if len(locked_gates) != len(gate_ids):
                raise AskError("ask.application_unknown", "Review/application gate is missing")
            review_gate = next((g for g in locked_gates if g["id"] == review["gate_id"]), None)
            if (
                not review_gate
                or review_gate["await_id"] != review["id"]
                or review_gate["gate_type"] != "review"
            ):
                raise AskError("ask.application_unknown", "Review gate identity changed")
            if task_gate:
                application_gate = next(g for g in locked_gates if g["id"] == task_gate)
                linked = (
                    await conn.execute(
                        select(task_gates.c.task_id).where(
                            task_gates.c.task_id == receipt["task_id"],
                            task_gates.c.gate_id == task_gate,
                        )
                    )
                ).first()
                if (
                    application_gate["gate_type"] != "event"
                    or application_gate["await_id"] != f"ask-application:{decision['id']}"
                    or not linked
                ):
                    raise AskError(
                        "ask.application_unknown", "Feedback application gate identity changed"
                    )
            if not already_applied:
                moved = await self.db.transition_review(
                    review["id"],
                    from_states={"in_review"},
                    expected_revision=decision["review_revision"],
                    values={
                        "state": state,
                        "decided_by": (
                            f"supervisor (delegated): {decision['actor_id']}"
                            if decision["actor_id"].startswith("session:")
                            else decision["actor_id"]
                        ),
                        "decided_at": decision["created_at"],
                        "decision_note": decision["overall_note"],
                        "pending_decision_id": None,
                        "updated_at": time.time(),
                    },
                    conn=conn,
                )
                if not moved:
                    raise AskError("ask.stale_source", "Review changed before finalization")
                if domain["task_kind"] == "revision":
                    await self.db.set_review_revision_responder(
                        review["id"],
                        decision["review_revision"],
                        responder={
                            "responder_class": domain["responder_class"],
                            "responder_profile": None,
                            "responder_profile_source": "router",
                        },
                        conn=conn,
                    )
            for gate in locked_gates:
                status = (
                    "resolved"
                    if gate["id"] == task_gate or option == "approve"
                    else ("cancelled" if option == "decline" else "open")
                )
                await conn.execute(
                    update(gates)
                    .where(gates.c.id == gate["id"])
                    .values(
                        status=status,
                        resolved_by=decision["actor_id"],
                        resolution="approved" if option == "approve" else option,
                    )
                )
            waiters = set(
                (
                    await conn.execute(
                        select(task_gates.c.task_id)
                        .where(
                            task_gates.c.gate_id.in_(gate_ids),
                        )
                        .order_by(task_gates.c.task_id)
                    )
                ).scalars()
            )
            flipped = await self.db.recompute_blocked(waiters, conn=conn) if waiters else set()
            await self.db._note_frontier_entry(conn, set(flipped), reason="unblocked")
            result = {
                "review_id": review["id"],
                "state": state,
                "decision_id": decision["id"],
                "decision": option,
                "unblocked_task_ids": sorted(
                    set(flipped) - ({receipt["task_id"]} if domain["task_kind"] else set())
                ),
                "blocked_flip_task_ids": sorted(flipped),
                "resolved_gate_ids": [
                    g["id"] for g in locked_gates if g["id"] == task_gate or option == "approve"
                ],
            }
            if already_applied:
                return apply["receipt"]
            await succeed(conn, apply, result)
            await self.project(
                decision,
                {
                    "decision_state": "declined"
                    if option == "decline"
                    else ("changes_requested" if option == "reject" else state),
                    "application_state": "applying",
                    "next_actor": "agent",
                    "updated_at": time.time(),
                },
                conn=conn,
            )
            for kind in (
                "mirror",
                "review_event",
                "notification",
                *(f"gate_event:{gid}" for gid in result["resolved_gate_ids"]),
            ):
                await operation(
                    self.db, decision, kind, conn=conn, dependencies=[apply["operation_key"]]
                )
            for task_id in sorted(flipped):
                task = (
                    (await conn.execute(select(tasks).where(tasks.c.id == task_id)))
                    .mappings()
                    .one()
                )
                await operation(
                    self.db,
                    decision,
                    f"task_event:{task_id}",
                    conn=conn,
                    dependencies=[apply["operation_key"]],
                    parameters={
                        "task_id": task_id,
                        "title": task["title"],
                        "is_blocked": task["is_blocked"],
                    },
                )
            if option == "approve" and review["kind"] in {"spec", "plan"}:
                await operation(
                    self.db, decision, "ingestion", conn=conn, dependencies=[apply["operation_key"]]
                )
            return result

    async def _external(self, decision, kind):
        """Bounded stage execution with durable leases and explicit retry semantics.

        Event consumers receive a stable decision/operation identity; replay is
        at-least-once. Pinned artifact, mirror and notification writes reconcile
        their source receipts. Unknown stages are never generically reset.
        """
        if kind == "ingestion":
            from src.asks.ingestion import dispatch

            return await dispatch(self.db, decision, self.service.hooks.emit,
                                  self.service.vault_root)
        async with self.db.immediate() as conn:
            review = await self.locked(decision, conn)
            await authorize_application(review, decision, conn=conn, service=self.service)
            op = await get_operation(conn, decision["id"], kind, lock=True)
            if op is None or op["status"] == "succeeded":
                return
            if op["status"] == "processing":
                await self.db.expire_ask_operation(op["id"], now=time.time(), conn=conn)
                op = await get_operation(conn, decision["id"], kind)
            if op["status"] == "unknown":
                # Every supported stage has a stable receipt/key and a specific
                # idempotent implementation below; no arbitrary adapter is retried.
                if kind not in {
                    "mirror",
                    "review_event",
                    "notification",
                    "playbook",
                } and not kind.startswith(("gate_event:", "task_event:")):
                    raise AskError("ask.application_unknown", "No source reconciler for this stage")
                await self.db.reconcile_ask_operation(
                    op["id"],
                    op["lease_generation"],
                    state="pending",
                    receipt={**(op["receipt"] or {}), "replay_key": op["operation_key"]},
                    retry_safe=True,
                    conn=conn,
                )
            claimed = await self.db.claim_ask_operation(
                op["id"],
                new_id("review-application"),
                now=time.time(),
                lease_seconds=60,
                conn=conn,
            )
            if not claimed:
                raise AskError("ask.operation_in_progress", f"{kind} is already being applied")
        try:
            receipt = await self.run_stage(decision, claimed)
        except Exception as exc:
            async with self.db.immediate() as conn:
                await self.locked(decision, conn)
                saved = await get_operation(conn, decision["id"], kind, lock=True)
                await self.db.complete_ask_operation(
                    claimed["id"],
                    claimed["lease_owner"],
                    claimed["lease_generation"],
                    state="unknown",
                    receipt={**(saved["receipt"] or {}), "error": str(exc)},
                    now=time.time(),
                    conn=conn,
                )
            raise
        async with self.db.immediate() as conn:
            await self.locked(decision, conn)
            if not await self.db.complete_ask_operation(
                claimed["id"],
                claimed["lease_owner"],
                claimed["lease_generation"],
                state="succeeded",
                receipt=receipt,
                now=time.time(),
                conn=conn,
            ):
                raise AskError("ask.operation_in_progress", f"{kind} completion lost its lease")
            remaining = (
                await conn.execute(
                    select(escalation_actions.c.id).where(
                        escalation_actions.c.decision_id == decision["id"],
                        escalation_actions.c.status != "succeeded",
                    )
                )
            ).first()
            if remaining is None:
                await self.project(
                    decision,
                    {
                        "application_state": "applied",
                        "next_actor": "none",
                        "updated_at": time.time(),
                    },
                    conn=conn,
                )

    async def run_stage(self, decision, op):
        domain, kind = decision["packet"]["domain"], op["operation_kind"]
        if kind == "mirror":
            return await self.mirror(decision)
        if kind == "playbook":
            if self.service.hooks.record_playbook is None:
                raise AskError("ask.application_unknown", "Playbook recorder is unavailable")
            result = await self.service.hooks.record_playbook(decision)
            async with self.db.immediate() as conn:
                await self.locked(decision, conn)
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == op["id"],
                        escalation_actions.c.lease_generation == op["lease_generation"],
                        escalation_actions.c.lease_owner == op["lease_owner"],
                    )
                    .values(receipt=result)
                )
            if (
                not result
                or not result.get("stored")
                or (result.get("activate_on_approval") and not result.get("activated"))
            ):
                raise AskError("ask.application_unknown", f"Playbook application failed: {result}")
            return result
        if kind == "notification":
            async with self.db.immediate() as conn:
                await self.locked(decision, conn)
                stored = await get_operation(conn, decision["id"], kind, lock=True)
                if stored.get("receipt", {}) and stored["receipt"].get("message_id"):
                    return stored["receipt"]
                playbook = await get_operation(conn, decision["id"], "playbook")
                pin_note = ""
                if playbook:
                    outcome = playbook["receipt"]
                    pin_note = f"\nPlaybook {outcome['artifact_sha256']}: " + (
                        "stored and activated" if outcome.get("activated") else "stored"
                    )
                    if outcome.get("next_step"):
                        pin_note += f"\n{outcome['next_step']}"
                msg = await self.db.create_message(
                    project_id=domain["review"]["project_id"],
                    from_kind="system",
                    from_id=f"review:{decision['review_id']}",
                    to_kind="session",
                    to_id=f"supervisor-{domain['review']['project_id']}",
                    subject=f"Review {decision['review_id']}: {domain['effective_option']}",
                    body=f"Review {decision['review_id']} ({domain['review']['kind']}): "
                    f"{domain['review']['vault_path']}\n"
                    f"Decision {decision['id']} applied to revision {decision['review_revision']}.\n"
                    f"{decision['overall_note']}{pin_note}",
                    conn=conn,
                )
                receipt = {"message_id": msg.id}
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == op["id"],
                    )
                    .values(receipt=receipt)
                )
                return receipt
        payload = {
            "review_id": decision["review_id"],
            "revision": decision["review_revision"],
            "project_id": domain["review"]["project_id"],
            "title": domain["review"]["title"],
            "kind": domain["review"]["kind"],
            "author_task_id": domain["review"]["author_task_id"],
            "decider": domain["decider"],
            "decision": domain["effective_option"],
            "decided_by": decision["actor_id"],
            "note": decision["overall_note"],
            "decision_id": decision["id"],
            "packet_sha256": decision["packet_sha256"],
            "operation_key": op["operation_key"],
        }
        event = "review.decided"
        if kind.startswith("gate_event:"):
            event = "gate.resolved"
            payload.update(
                gate_id=kind.removeprefix("gate_event:"),
                resolved_by=decision["actor_id"],
                resolution=domain["effective_option"],
                gate_type="review"
                if kind == f"gate_event:{domain['review']['gate_id']}"
                else "event",
                await_id=decision["review_id"]
                if kind == f"gate_event:{domain['review']['gate_id']}"
                else f"ask-application:{decision['id']}",
            )
        if kind.startswith("task_event:"):
            fields = op["parameters"]
            event = "task.blocked" if fields["is_blocked"] else "task.unblocked"
            payload.update(
                task_id=fields["task_id"], title=fields["title"], reason="review_decision"
            )
        async with self.db._engine.connect() as conn:
            applied = await get_operation(conn, decision["id"], "apply")
        payload["unblocked_task_ids"] = applied["receipt"]["unblocked_task_ids"]
        async with self.db.immediate() as conn:
            await self.locked(decision, conn)
            stored = await get_operation(conn, decision["id"], kind, lock=True)
            receipt = stored["receipt"] or {}
            seq = receipt.get("seq")
            if not seq:
                seq = await self.db.log_event(
                    event, project_id=payload["project_id"], payload=json.dumps(payload), conn=conn
                )
                await conn.execute(
                    update(escalation_actions)
                    .where(
                        escalation_actions.c.id == op["id"],
                    )
                    .values(receipt={"seq": seq})
                )
        await self.service.hooks.emit(event, {**payload, "seq": seq})
        return {"seq": seq, "operation_key": op["operation_key"]}

    async def mirror(self, decision):
        from src.reviews.vault import frontmatter_for, render, write_atomic

        domain = decision["packet"]["domain"]
        async with self.db.immediate() as conn:
            review = await self.locked(decision, conn)
            current = await self.db.get_review_revision(
                review["id"],
                decision["review_revision"],
                conn=conn,
            )
            if review["current_revision"] != decision["review_revision"]:
                return {"superseded_by_revision": review["current_revision"]}
            if self.service.vault_state(review, current["content_sha256"]) == "diverged":
                await self.db.insert_ask_reconciliation_case(
                    {
                        "id": new_id("ask-repair"),
                        "project_id": review["project_id"],
                        "conflict_key": f"mirror:{decision['id']}",
                        "source_evidence": {
                            "decision_id": decision["id"],
                            "vault_path": review["vault_path"],
                        },
                        "logical_owner": f"supervisor:{review['project_id']}",
                        "reason": "Post-acceptance vault divergence; immutable packet remains authoritative",
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    },
                    conn=conn,
                )
                diverged = True
            else:
                diverged = False
                path = self.service._path(review)
                body = render(
                    frontmatter_for(review, domain["playbook"], domain["spec_kind"]),
                    current["content"],
                )
                expected_bytes = path.read_bytes() if path.exists() else None
                if expected_bytes is not None and expected_bytes != body.encode():
                    if self.service._backup(review, time.time()) is None:
                        raise AskError("ask.application_unknown", "Cannot back up review mirror")
                observed = path.read_bytes() if path.exists() else None
                if observed != expected_bytes:
                    raise AskError(
                        "ask.application_unknown", "Vault changed while backing up the mirror"
                    )
                write_atomic(path, body)
        if diverged:
            raise AskError(
                "ask.application_unknown", "Vault mirror requires repair; packet is immutable"
            )
        return {
            "content_sha256": current["content_sha256"],
            "revision": decision["review_revision"],
        }


def task_key(decision):
    return f"review-{decision['packet']['domain']['task_kind']}:{decision['id']}"
