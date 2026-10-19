"""``ensure_fidelity_review`` — one server-owned reviewer per held ingestion batch.

A held batch waits on a fidelity review before release (spec-ingestion held
batches, P2 L2.1; design §5.2).  Policy reacts to ``batch.held`` by calling
this command; the command owns the reviewer task: its role route, dedup key,
metadata, informational edges to the batch's epics, and the batch's
``reviewer_task_id``, all committed with the task.  The durable
``fidelity_review.dispatch`` operation carries the receipt and the attempt
count across reconciler wakeups.

Nothing here releases a batch.  A reviewer that fails its retries, or a
dispatch that exceeds its bound, leaves the batch ``held`` and raises one ask.
"""

from pydantic import ValidationError
from sqlalchemy import select, update

from src.api.auth import RequestScope
from src.api.scope import fidelity_review_task_for_session, held_task_for_session
from src.asks.batch_scope import scope_ask, scope_source
from src.asks.models import AskError
from src.asks.operations import (
    FIDELITY_REVIEW_DISPATCH,
    get_operation,
    reserve_fidelity_review_dispatch,
    succeed,
)
from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    current_principal,
    principal_context,
)
from src.commands import task_changes
from src.commands.contracts.builtin import TaskBatchReviewRecordArgs
from src.database.queries import ingest_batch_queries as batches
from src.database.queries.ingest_batch_queries import (
    assign_reviewer,
    get_batch,
    members,
    passing_receipt,
)
from src.database.tables import escalation_actions, tasks
from src.models import DepType
from src.integration.outbox import enqueue_batch_event
from src.review_keys import FIDELITY_REVIEWER_PROFILE_ID, fidelity_review_dedup_key

#: ``task_metadata`` key naming the batch a reviewer task reviews.
FIDELITY_REVIEW_META = "fidelity_review"

#: Task-level retries of one reviewer (design table "Reviewer failure").
FIDELITY_REVIEW_MAX_RETRIES = 2

#: Reviewer tasks one batch may be given.  A reviewer that vanished or closed
#: without a receipt is replaced; past this bound the batch waits on a human.
FIDELITY_REVIEW_MAX_DISPATCHES = 3

_REVIEWABLE_STATES = frozenset({"held", "reviewing"})
_TERMINAL = ("COMPLETED", "FAILED")


class _ReviewerExists(Exception):
    def __init__(self, task_id):
        self.task_id = task_id


class _ReviewerExhausted(Exception):
    def __init__(self, reason, task_id=None):
        self.reason = reason
        self.task_id = task_id


class FidelityReviewCommandsMixin:
    async def _cmd_task_batch_review_record(self, args):
        """Attest the current graph; scope answers never substitute for review."""
        try:
            request = TaskBatchReviewRecordArgs.model_validate(args)
        except ValidationError as exc:
            return {"success": False, "error_code": "invalid_receipt", "error": str(exc)}
        principal = current_principal() or TRUSTED_LOCAL
        async with self.db._engine.connect() as conn:
            batch = await get_batch(conn, request.batch_id)
        if batch is None:
            return {"success": False, "error_code": "batch_not_found", "error": "Batch not found"}
        if (request.project_id not in (None, batch["project_id"])
                or principal.project_id not in (None, batch["project_id"])):
            return {"success": False, "error_code": "not_authorized",
                    "error": "out of scope: project mismatch"}
        # Even a trusted caller must name a live, server-assigned reviewer. No
        # caller-supplied reviewer/session id is accepted as receipt authority.
        session_id = (principal.session_id if principal.kind is PrincipalKind.SESSION
                      else batch["reviewer_session_id"])
        scope = RequestScope(
            kind="session", session_id=session_id, project_id=batch["project_id"],
            session_instance_token=principal.session_instance_token,
            task_id=principal.task_id or batch["reviewer_task_id"],
        )
        reviewer = await fidelity_review_task_for_session(self.db, scope, request.batch_id)
        if reviewer is None:
            return {"success": False, "error_code": "not_authorized",
                    "error": "Only the live assigned fidelity reviewer may attest this batch"}
        try:
            async with task_changes.boundary(self.db, batch["project_id"]) as conn:
                batch = await get_batch(conn, request.batch_id, lock=True)
                current = await held_task_for_session(self.db, scope)
                if (current is None or current.id != reviewer.id
                        or batch["state"] not in batches.HELD_STATES
                        or batch["reviewer_task_id"] != current.id
                        or batch["reviewer_session_id"] != session_id
                        or batch["reviewer_claim_epoch"] != current.claim_epoch):
                    raise AskError("not_authorized", "Fidelity reviewer assignment changed")
                stale = batches._stale(
                    batch, request.expected_revision, request.expected_digest,
                    await batches.live_digest(conn, request.batch_id, lock=True),
                )
                if stale:
                    return stale
                active = await members(conn, request.batch_id, state="active", lock=True)
                required = {m["section_anchor"] for m in active}
                if not required or required - request.section_verdicts.keys():
                    return {"success": False, "error_code": "incomplete_review",
                            "error": "Every active section requires a verdict",
                            "missing_sections": sorted(required - request.section_verdicts.keys())}
                passing = "scope_question" not in (
                    *request.section_verdicts.values(), request.reverse_coverage_verdict,
                    request.walkthrough_verdict,
                )
                ask = await scope_ask(self.db, conn, batch)
                if passing and (batch["state"] == "needs_scope" or ask and (
                        ask["decision_state"] != "answered" or ask["application_state"] != "applied"
                )):
                    return {"success": False, "error_code": "needs_scope",
                            "error": "A scope answer is required before a fresh attestation"}
                if not passing:
                    if await passing_receipt(conn, request.batch_id, request.expected_revision):
                        raise AskError("receipt_exists", "This revision already has a passing receipt")
                    if ask and ask["decision_state"] == "answered":
                        raise AskError("scope_answered", "Revise the graph for a new scope question")
                # Freeze lineage in the existing JSONB envelope: these values
                # remain evidence even if the batch or document later changes.
                findings = {
                    "findings": request.structural_findings,
                    "lineage": {key: batch[key] for key in (
                        "source_review_id", "source_revision", "source_content_sha256",
                        "design_review_id", "design_revision", "design_content_sha256",
                        "lineage_kind", "decision_id",
                    )},
                    "last_diff": (batch["check_results"] or {}).get("last_diff"),
                }
                recorded = await batches.record_receipt(
                    conn, request.batch_id, structure_revision=request.expected_revision,
                    structure_digest=request.expected_digest, reviewer_task_id=current.id,
                    reviewer_session_id=session_id, passing=passing,
                    section_verdicts=request.section_verdicts,
                    reverse_coverage_verdict=request.reverse_coverage_verdict,
                    walkthrough_verdict=request.walkthrough_verdict, structural_findings=findings,
                )
                if not recorded["success"]:
                    return recorded
                if not passing and ask and not recorded["replayed"]:
                    raise AskError("scope_question_exists",
                                   "This revision already has a different scope question")
                state = "reviewing" if passing else "needs_scope"
                await batches.set_state(conn, request.batch_id, state)
                batch = {**batch, "state": state}
                if not passing:
                    publication = await self._ask_service().publish(scope_source(batch), conn=conn)
                    if publication.reconciliation_case_ids:
                        # Commit the quarantine created by AskService so the
                        # supervisor can reconcile it; the batch stays held.
                        return {"success": False, "error_code": "ask.idempotency_conflict",
                                "error": "Scope ask identity conflicts",
                                "ask_id": publication.ask["id"]}
                    ask = publication.ask
                await enqueue_batch_event(
                    conn, batch, "batch.receipt_recorded" if passing else "batch.needs_scope",
                )
                return {"success": True, "batch_id": request.batch_id, "state": state,
                        "passing": passing, "receipt": recorded["receipt"],
                        "replayed": recorded["replayed"], "ask_id": ask["id"] if ask else None}
        except AskError as exc:
            return {"success": False, "error_code": exc.code, "error": str(exc)}

    async def _cmd_ensure_fidelity_review(self, args):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION:
            return {"success": False, "error": "Only ingestion policy may dispatch fidelity review"}
        if set(args) - {"project_id", "batch_id", "structure_revision"}:
            return {"success": False, "error": "Reviewer role and metadata are server-owned"}
        project_id, batch_id = args.get("project_id"), args.get("batch_id")
        revision = args.get("structure_revision")
        if not (project_id and batch_id) or isinstance(revision, bool) or not isinstance(
            revision, int,
        ):
            return {"success": False,
                    "error": "project_id, batch_id and integer structure_revision required"}
        if principal.project_id and principal.project_id != project_id:
            return {"success": False, "error": "out of scope: project mismatch"}
        key = fidelity_review_dedup_key(batch_id)

        async def before(conn):
            batch = await get_batch(conn, batch_id, lock=True)
            if batch is None or batch["project_id"] != project_id:
                raise AskError("ask.not_found", f"Batch '{batch_id}' not found in project")
            await reserve_fidelity_review_dispatch(self.db, batch, conn=conn)
            op = await get_operation(conn, batch["decision_id"], FIDELITY_REVIEW_DISPATCH,
                                     lock=True)
            reviewer = None
            if batch["reviewer_task_id"]:
                reviewer = (await conn.execute(select(tasks.c.id, tasks.c.status).where(
                    tasks.c.id == batch["reviewer_task_id"],
                    tasks.c.project_id == project_id, tasks.c.dedup_key == key,
                ))).mappings().first()
            if reviewer is not None and reviewer["status"] not in _TERMINAL:
                if op["status"] != "succeeded":
                    await succeed(conn, op, {**(op["receipt"] or {}), "task_id": reviewer["id"]})
                raise _ReviewerExists(reviewer["id"])
            if reviewer is not None and reviewer["status"] == "COMPLETED":
                receipt = await passing_receipt(conn, batch_id, batch["structure_revision"])
                if receipt and receipt["reviewer_task_id"] == reviewer["id"]:
                    raise _ReviewerExists(reviewer["id"])
            if batch["state"] not in _REVIEWABLE_STATES:
                raise AskError("batch.not_reviewable",
                               f"Batch '{batch_id}' is {batch['state']}, not awaiting review")
            if op["status"] in {"failed", "cancelled"}:
                raise _ReviewerExhausted((op["receipt"] or {}).get("exhausted") or op["status"],
                                         batch["reviewer_task_id"])
            if reviewer is not None and reviewer["status"] == "FAILED":
                raise _ReviewerExhausted("reviewer_failed", reviewer["id"])
            if (op["receipt"] or {}).get("attempts", 0) >= FIDELITY_REVIEW_MAX_DISPATCHES:
                raise _ReviewerExhausted("dispatch_bound", batch["reviewer_task_id"])
            if revision != batch["structure_revision"]:
                raise AskError("batch.stale_revision", (
                    f"Batch '{batch_id}' is at revision {batch['structure_revision']}, "
                    f"not {revision}"
                ))
            # A dedup string alone never proves that this command assigned it.
            if (await conn.execute(select(tasks.c.id).where(
                tasks.c.project_id == project_id, tasks.c.dedup_key == key,
                tasks.c.status.not_in(_TERMINAL),
            ))).first():
                raise AskError("ask.idempotency_conflict", "Reviewer key has no batch assignment")
            return batch

        async def record(conn, task_id, parent_id):
            await self.db._upsert_meta(task_id, FIDELITY_REVIEW_META, {
                "batch_id": batch_id, "structure_revision": revision,
            }, conn=conn)
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                max_retries=FIDELITY_REVIEW_MAX_RETRIES,
            ))
            # Rewriting the reviewer clears the claim binding: the previous
            # holder loses batch authority in this transaction (design §5.2).
            await assign_reviewer(conn, batch_id, reviewer_task_id=task_id)
            active = await members(conn, batch_id, state="active")
            epics = [m for m in active if m["role"] == "container"] or active
            for member in epics:
                await self.db.add_dependency(
                    task_id, member["task_id"], DepType.RELATED.value,
                    description="fidelity review of this batch", conn=conn,
                )
            batch = await get_batch(conn, batch_id)
            op = await get_operation(conn, batch["decision_id"], FIDELITY_REVIEW_DISPATCH,
                                     lock=True)
            prior = op["receipt"] or {}
            await succeed(conn, op, {
                **prior, "task_id": task_id, "dedup_key": key, "batch_id": batch_id,
                "structure_revision": revision, "attempts": prior.get("attempts", 0) + 1,
            })

        try:
            async with self.db.immediate() as conn:
                batch = await before(conn)
            # Domain command composition after checking the batch and its
            # dispatch receipt; no caller chooses the role or task metadata.
            with principal_context(ExecutionPrincipal.service("fidelity-review-dispatch")):
                created = await self._cmd_create_task({
                    "project_id": project_id, "profile_id": FIDELITY_REVIEWER_PROFILE_ID,
                    "title": f"Fidelity review of held batch {batch_id}", "dedup_key": key,
                    "root": True, "_suppress_created_event": True,
                    "description": (
                        f"Held ingestion batch: {batch_id}\n"
                        f"Structure revision: {revision}\n"
                        f"Approved decision: {batch['decision_id']}\n"
                        "Review the batch against its approved spec: fidelity first (every "
                        "section and goal is covered by a member, and every member traces to "
                        "the spec), structure second. Read approved_spec_packet, inspect with "
                        "task_batch_validate, correct with task_batch_update, and record the "
                        "verdict with task_batch_review_record. Raise scope questions through "
                        "the receipt; never release the batch."
                    ),
                    "_before_create_on": before, "_after_create_on": record,
                })
            if not created.get("success"):
                return created
            return {"success": True, "task_id": created["created"], "created": True}
        except _ReviewerExists as existing:
            return {"success": True, "task_id": existing.task_id, "created": False}
        except _ReviewerExhausted as exhausted:
            return await self._fidelity_review_exhausted(project_id, batch_id, exhausted)
        except AskError as exc:
            return {"success": False, "error": str(exc), "code": exc.code}

    async def _fidelity_review_exhausted(self, project_id, batch_id, exhausted):
        """Raise the one ask, then park the dispatch; the batch stays ``held``.

        The escalation is idempotent on its incident key, so a crash between
        the two writes, or any later wakeup, raises the same ask again.
        """
        fields = {"task_id": exhausted.task_id} if exhausted.task_id else {
            "task_title": f"Fidelity review of held batch {batch_id}",
        }
        with principal_context(ExecutionPrincipal.service("fidelity-review-dispatch")):
            raised = await self._cmd_escalation_create({
                "project_id": project_id, "source_kind": "fidelity_review",
                "source_identity": batch_id,
                "incident_key": f"fidelity-review-exhausted:{batch_id}",
                "summary": f"Fidelity review of held batch {batch_id} could not complete",
                "investigation": (
                    f"Reviewer dispatch stopped ({exhausted.reason}) after "
                    f"{FIDELITY_REVIEW_MAX_RETRIES} task retries or "
                    f"{FIDELITY_REVIEW_MAX_DISPATCHES} reviewer assignments. The batch "
                    "remains held: none of its members can be claimed."
                ),
                "decision_requested": (
                    "Fix the reviewer and re-dispatch it, review the batch by hand, or "
                    "discard the batch. Nothing is released until a passing receipt exists."
                ),
                "severity": "high", **fields,
            })
        if not raised.get("success"):
            return raised
        async with self.db.immediate() as conn:
            batch = await get_batch(conn, batch_id, lock=True)
            op = await get_operation(conn, batch["decision_id"], FIDELITY_REVIEW_DISPATCH,
                                     lock=True)
            if op["status"] not in {"failed", "cancelled"}:
                await conn.execute(update(escalation_actions).where(
                    escalation_actions.c.id == op["id"],
                ).values(
                    status="failed", outcome="exhausted", retry_at=None,
                    lease_owner=None, lease_expires_at=None,
                    error=f"fidelity review exhausted: {exhausted.reason}",
                    receipt={**(op["receipt"] or {}), "exhausted": exhausted.reason,
                             "ask_id": raised.get("ask_id")},
                ))
        return {
            "success": False, "error_code": "fidelity_review_exhausted",
            "error": f"Fidelity review of batch {batch_id} exhausted ({exhausted.reason}); "
                     "the batch stays held",
            "batch_id": batch_id, "ask_id": raised.get("ask_id"),
            "escalation_id": raised["escalation"]["id"],
        }
