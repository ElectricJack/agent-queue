"""Immutable approval reads and decision-keyed dispatch; never creates tasks itself."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import select, update

from src.asks.models import AskError, digest
from src.asks.operations import get_operation
from src.database.tables import escalation_actions, task_metadata

INGEST_META = "spec_ingest"


async def task_stamp(conn, task_id):
    value = (await conn.execute(select(task_metadata.c.value).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == INGEST_META,
    ))).scalar_one_or_none()
    return json.loads(value) if value else None


async def approved_packet(db, decision_id, packet_sha256, project_id, *, conn, lock=True,
                          require_current=True):
    """Lock the source before ask/operations and validate the domain apply receipt.

    Mirrors and current frontmatter are deliberately absent from this boundary.
    Withdrawn or superseded approvals cannot commission new work. Historical
    validation is only for replaying an already committed ingestion receipt.
    """
    decision = await db.get_ask_decision(decision_id, conn=conn)
    if (not decision or decision["packet_sha256"] != packet_sha256
            or decision["packet"]["source"]["project_id"] != project_id):
        raise AskError("ask.not_found", "Approved packet was not found")
    if digest(decision["packet"]) != packet_sha256:
        raise AskError("ask.application_unknown", "Decision packet hash differs")
    domain = decision["packet"]["domain"]
    if decision["review_id"]:
        review = await db.lock_review_for_mutation(decision["review_id"], conn)
        if (not review or review["project_id"] != project_id
                or review["kind"] not in {"spec", "plan"}
                or (require_current and (
                    review["state"] != "approved"
                    or review["current_revision"] != decision["review_revision"]
                    or review["pending_decision_id"]))):
            raise AskError("ask.stale_source", "Review approval is no longer current")
        revision = await db.get_review_revision(
            review["id"], decision["review_revision"], conn=conn,
        )
        body = revision["content"].encode("utf-8")
        spec_path = domain["review"]["vault_path"]
    elif decision["snapshot_id"]:
        snapshot = await db.get_approved_document_snapshot(decision["snapshot_id"], conn=conn)
        if not snapshot or snapshot["project_id"] != project_id:
            raise AskError("ask.not_found", "Approved snapshot was not found")
        body, spec_path = snapshot["body"], snapshot["source_path"]
    else:
        raise AskError("ask.unsupported_source", "Decision has no immutable document")
    ask = await db.get_ask(decision["ask_id"], conn=conn, lock=lock)
    if (not ask or ask["project_id"] != project_id or (require_current and (
            ask["context_state"] != "complete"
            or ask["decision_generation"] != decision["decision_generation"]))):
        raise AskError("ask.stale_source", "Approved source generation changed")
    applied = await get_operation(conn, decision_id, "apply")
    if (domain.get("effective_option") != "approve" or not applied
            or applied["status"] != "succeeded"
            or (applied["receipt"] or {}).get("decision_id") != decision_id
            or (applied["receipt"] or {}).get("state") != "approved"):
        raise AskError("ask.application_unknown", "Approval lacks a successful application receipt")
    if hashlib.sha256(body).hexdigest() != decision["content_sha256"]:
        raise AskError("ask.application_unknown", "Approved document bytes differ from their hash")
    feedback = await db.list_review_feedback_obligations(decision_id, conn=conn)
    frozen = {item["feedback_key"]: item for item in domain.get("feedback", [])}
    if {item["feedback_key"] for item in feedback} != set(frozen):
        raise AskError("ask.application_unknown", "Approved feedback is missing")
    filing = await get_operation(conn, decision_id, "file_task")
    fields = ("comment_id", "original_text", "original_author", "original_revision",
              "original_anchor", "acceptance_text", "disposition", "required_before_release")
    for item in feedback:
        if any(item[key] != frozen[item["feedback_key"]][key] for key in fields):
            raise AskError("ask.application_unknown", "Approved feedback changed: " + ", ".join(
                key for key in fields if item[key] != frozen[item["feedback_key"]][key]
            ))
        if item["accepted_by"] != {
            **frozen[item["feedback_key"]]["accepted_by"],
            "evidence_entry_id": decision["evidence_entry_id"],
        }:
            raise AskError("ask.application_unknown", "Approved feedback acceptance changed")
        if item["disposition"] == "follow_up" and (
            not item["task_id"] or not filing or filing["status"] != "succeeded"
            or filing["receipt"].get("task_id") != item["task_id"]
            or item["id"] not in filing["receipt"].get("obligation_ids", [])
        ):
            raise AskError("ask.application_unknown", "Follow-up lacks its work receipt")
    # Progress/evidence may evolve; approval and work identity may not. Stamps
    # therefore preserve stable obligations, never a mutable progress snapshot.
    feedback = [{key: item[key] for key in (
        "id", "feedback_key", "task_id", "operation_id", "accepted_by", *fields,
    )} for item in feedback]
    return {
        "decision_id": decision_id, "packet_sha256": packet_sha256,
        "project_id": project_id, "review_id": decision["review_id"],
        "revision": decision["review_revision"], "snapshot_id": decision["snapshot_id"],
        "spec_kind": domain.get("spec_kind") or "design", "spec_path": spec_path,
        "content_sha256": decision["content_sha256"], "body": body.decode("utf-8"),
        "packet": decision["packet"], "feedback": feedback,
    }


def stamp_for(packet):
    return {key: packet[key] for key in (
        "decision_id", "packet_sha256", "project_id", "review_id", "revision",
        "snapshot_id", "spec_kind", "spec_path", "content_sha256",
    )}


async def dispatch(db, decision, emit, vault_root):
    """Replay the event until ensure_spec_ingest records its task in this operation.

    Bus acknowledgement is not a task receipt. Emission has no domain effect and
    is safe to repeat, including after a lost connection or process restart.
    """
    async with db.immediate() as conn:
        if not await get_operation(conn, decision["id"], "ingestion"):
            return None
        packet = await approved_packet(
            db, decision["id"], decision["packet_sha256"],
            decision["packet"]["source"]["project_id"], conn=conn,
        )
        op = await get_operation(conn, decision["id"], "ingestion", lock=True)
        if not op or op["status"] == "succeeded":
            return (op or {}).get("receipt")
        if op["retry_at"] and op["retry_at"] > time.time():
            return op["receipt"]
        payload = {key: packet[key] for key in (
            "project_id", "decision_id", "packet_sha256", "review_id", "revision", "spec_kind",
        )}
        payload["spec_path"] = str((Path(vault_root) / packet["spec_path"]).resolve())
        payload["operation_key"] = op["operation_key"]
        receipt = op["receipt"] or {}
        # A failed playbook run is terminal under its event identity. Give a
        # retry a fresh event while keeping the decision/operation keys fixed.
        receipt["attempts"] = receipt.get("attempts", 0) + 1
        receipt["seq"] = await db.log_event(
            "spec.approved", project_id=packet["project_id"],
            payload=json.dumps(payload), conn=conn,
        )
        await conn.execute(update(escalation_actions).where(
            escalation_actions.c.id == op["id"],
        ).values(status="pending", receipt=receipt, retry_at=time.time() + 30))
    await emit("spec.approved", {**payload, "seq": receipt["seq"]})
    return receipt
