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

from sqlalchemy import select, update

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
from src.database.queries.ingest_batch_queries import (
    assign_reviewer,
    get_batch,
    live_digest,
    store_live_digest,
    members,
    passing_receipt,
)
from src.database.tables import escalation_actions, tasks
from src.models import DepType
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
            batch = await get_batch(conn, batch_id)
            if await live_digest(conn, batch_id) != batch["structure_digest"]:
                raise AskError("batch.stale_revision", "Live batch changed before reviewer dispatch")
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
            # The canonical snapshot includes these server-owned provenance
            # links. Establish the review snapshot atomically with dispatch,
            # so its first edit does not see its own assignment as drift.
            await store_live_digest(conn, batch_id)
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
