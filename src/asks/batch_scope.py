"""Scope answers are evidence for another fidelity review, never release authority.

This is a static source adapter on the generic AskService boundary. The source
key is the batch/revision idempotency key; the first scope receipt owns its
question. Later receipts and human answers cannot silently change that question.
"""

from sqlalchemy import select

from src.asks.models import MAX_BLOCKED_WORK, AskChoice, AskError, OperationResult, SourceIdentity
from src.asks.sources import SourceAdapter, context_for, source_snapshot
from src.database.queries import ingest_batch_queries as batches
from src.database.tables import ingest_batch_receipts, tasks


def scope_source(batch, revision=None):
    revision = batch["structure_revision"] if revision is None else revision
    return SourceIdentity(
        batch["project_id"], "batch_scope", f"batch-scope:{batch['batch_id']}:{revision}",
        {"batch_id": batch["batch_id"], "structure_revision": revision, "ask_kind": "batch_scope"},
    )


async def scope_ask(db, conn, batch, revision=None):
    source = scope_source(batch, revision)
    return await db.get_ask_by_source(source.project_id, source.kind, source.key, conn=conn)


class BatchScopeAdapter(SourceAdapter):
    source_kind = "batch_scope"

    async def read(self, source, conn):
        batch = await batches.get_batch(conn, source.reference.get("batch_id"), lock=True)
        revision = source.reference.get("structure_revision")
        if not batch or source != scope_source(batch, revision):
            raise AskError("ask.not_found", "Batch scope request was not found")
        receipt = (await conn.execute(select(ingest_batch_receipts).where(
            ingest_batch_receipts.c.batch_id == batch["batch_id"],
            ingest_batch_receipts.c.structure_revision == revision,
            ingest_batch_receipts.c.passing.is_(False),
        ).order_by(ingest_batch_receipts.c.created_at, ingest_batch_receipts.c.receipt_id)
          .limit(1))).mappings().first()
        if not receipt:
            raise AskError("ask.not_found", "Batch has no scope-question receipt")
        answer = (batch["check_results"] or {}).get("scope_answers", {}).get(str(revision))
        return batch, dict(receipt), answer

    async def snapshot(self, source, *, conn, lock=True):
        batch, receipt, answer = await self.read(source, conn)
        active = await batches.members(conn, batch["batch_id"], state="active")
        questions = [section for section, verdict in receipt["section_verdicts"].items()
                     if verdict == "scope_question"]
        findings = receipt["structural_findings"].get("findings", [])
        choices = [f["question"] for f in findings if isinstance(f.get("question"), str)]
        affected = {m["task_id"] for m in active if not questions
                    or m["section_anchor"] in questions
                    or receipt["reverse_coverage_verdict"] == "scope_question"
                    or receipt["walkthrough_verdict"] == "scope_question"}
        parents = dict((await conn.execute(select(tasks.c.id, tasks.c.parent_task_id).where(
            tasks.c.id.in_([m["task_id"] for m in active]),
        ))).all())
        while descendants := {tid for tid, parent in parents.items()
                              if parent in affected and tid not in affected}:
            affected.update(descendants)
        leaves = [m["task_id"] for m in active
                  if m["role"] != "container" and m["task_id"] in affected]
        why = ("Unresolved scope choices: " + "\n".join(choices) if choices else
               "The fidelity reviewer found an unresolved scope choice. Inspect the section "
               "verdicts, affected leaves and findings before supplying an answer.")
        context = context_for(
            source, f"Scope answer for batch {batch['batch_id']}",
            why,
            "An answer permits another edit and attestation round. Every member stays held; "
            "material scope changes still need source-document revision and approval.",
            blocked=leaves[:MAX_BLOCKED_WORK],
        )
        context.update({
            "ask_kind": "batch_scope", "receipt_id": receipt["receipt_id"],
            "section_verdicts": receipt["section_verdicts"],
            "reverse_coverage_verdict": receipt["reverse_coverage_verdict"],
            "walkthrough_verdict": receipt["walkthrough_verdict"],
            "structural_findings": receipt["structural_findings"],
            "affected_leaves": leaves,
            "links": [{"label": "Held batch and review receipt",
                       "url": f"/proposals/{batch['batch_id']}"}],
        })
        pending = (not answer and batch["state"] == "needs_scope"
                   and batch["structure_revision"] == source.reference["structure_revision"])
        return source_snapshot(
            source, meaning={"receipt": receipt},
            state="pending" if pending else "answered" if answer else "stale", context=context,
            choices=[AskChoice("answer", "Record scope answer",
                               "Save evidence and return the batch to held for a fresh review.")],
        )

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        packet = await super().validate(snapshot, actor, intent, conn=conn,
                                        accepted_decision=accepted_decision)
        if not accepted_decision and not intent.overall_note.strip():
            raise AskError("ask.context_missing", "A scope answer is required in the note")
        return packet

    async def apply_on(self, decision, *, conn):
        source = SourceIdentity(**decision["packet"]["source"])
        batch, scope_receipt, answer = await self.read(source, conn)
        if answer:
            if answer["decision_id"] == decision["id"]:
                return OperationResult("succeeded", answer)
            raise AskError("ask.stale_source", "Scope question was already answered")
        if (batch["state"] != "needs_scope"
                or batch["structure_revision"] != source.reference["structure_revision"]):
            raise AskError("ask.stale_source", "Batch no longer awaits this scope answer")
        receipt = {
            "batch_id": batch["batch_id"], "structure_revision": batch["structure_revision"],
            "scope_receipt_id": scope_receipt["receipt_id"], "decision_id": decision["id"],
            "evidence_entry_id": decision["evidence_entry_id"], "answer": decision["overall_note"],
        }
        checks = batch["check_results"] or {}
        checks = {**checks, "scope_answers": {
            **checks.get("scope_answers", {}), str(batch["structure_revision"]): receipt,
        }}
        await batches.set_check_results(conn, batch["batch_id"], checks)
        await batches.set_state(conn, batch["batch_id"], "held", from_states=["needs_scope"])
        return OperationResult("succeeded", receipt)
