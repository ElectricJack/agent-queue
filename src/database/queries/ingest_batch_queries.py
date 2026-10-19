"""Held ingestion batches: rows, membership, revision fence, receipts, release.

Module-level helpers over a caller's transaction (ingestion design §4, §6;
implementation spec P1 L1.1). Commands own authority, checks, events and
readiness recomputation; these helpers own the row writes and the fences that
must hold inside one transaction:

- ``structure_revision`` advances only through :func:`bump_revision`, which
  refuses a caller that read an older revision (``stale_revision``).
- The structure digest is always recomputed from live member, task, metadata
  and dependency rows (:func:`hydrate_structure`), never from the proposal
  payload or a previously stored digest.
- A batch's hold is the ``tasks.held_batch_id`` column; :func:`add_member`
  acquires it in the same transaction and only :func:`release` clears it.

Design lineage (:func:`resolve_design_lineage`) follows the implementation
document's author-task provenance to the approved design revision; no column
on ``doc_reviews`` records it.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    ask_decisions,
    doc_review_revisions,
    doc_reviews,
    ingest_batch_members,
    ingest_batch_receipts,
    ingest_batches,
    task_dependencies,
    task_metadata,
    task_proposals,
    tasks,
)
from src.task_graph.batch_digest import compute_structure_digest
from src.review_keys import FIDELITY_REVIEWER_PROFILE_ID, fidelity_review_dedup_key

HELD_STATES = frozenset({"held", "reviewing", "needs_scope"})
SETTABLE_STATES = frozenset({"held", "reviewing", "needs_scope", "retired"})
MEMBER_ROLES = frozenset({"container", "leaf", "operational_root"})

# ``tasks.task_metadata`` key carrying a member's fidelity fields that have no
# task column. Hydration reads it; absent fields hydrate as ``None``.
MEMBER_FIDELITY_META = "ingest_fidelity"
FIDELITY_FIELDS = ("acceptance", "constraints", "size")

# The author task's stamp written by design materialization
# (``src/commands/proposal_commands.py`` ``SPEC_INGEST_META``).
SPEC_INGEST_META = "spec_ingest"


def _error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message, **fields}


def _row(row) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --- Design lineage -----------------------------------------------------------


def _no_lineage(reason: str) -> dict[str, Any]:
    return {
        "lineage_kind": "none",
        "reason": reason,
        "design_review_id": None,
        "design_revision": None,
        "design_content_sha256": None,
    }


async def resolve_design_lineage(conn, source_review_id: str) -> dict[str, Any]:
    """Resolve an implementation document's approved design revision.

    ``doc_reviews.author_task_id`` → that task's ``spec_ingest`` stamp
    ``decision_id`` → ``ask_decisions`` → the design review id, revision and
    ``content_sha256`` of its ``doc_review_revisions`` row. A document whose
    author task carries no stamp, or whose stamped decision did not approve a
    ``spec_kind: design`` revision, records ``lineage_kind: none`` with the
    reason; that gap is shown in the receipt and never refuses the batch
    (design §4, scenario 13).
    """
    author_task_id = (await conn.execute(
        select(doc_reviews.c.author_task_id).where(doc_reviews.c.id == source_review_id)
    )).scalar_one_or_none()
    if author_task_id is None:
        return _no_lineage("no_author_task")
    raw = (await conn.execute(select(task_metadata.c.value).where(
        task_metadata.c.task_id == author_task_id, task_metadata.c.key == SPEC_INGEST_META,
    ))).scalar_one_or_none()
    stamp = json.loads(raw) if raw else None
    decision_id = stamp.get("decision_id") if isinstance(stamp, dict) else None
    if not decision_id:
        return _no_lineage("no_spec_ingest")
    decision = (await conn.execute(
        select(ask_decisions.c.review_id, ask_decisions.c.review_revision)
        .where(ask_decisions.c.id == decision_id)
    )).mappings().first()
    if decision is None or decision["review_id"] is None:
        return _no_lineage("decision_without_review")
    if decision["review_id"] == source_review_id:
        return _no_lineage("self_lineage")
    revision = (await conn.execute(
        select(doc_review_revisions.c.spec_kind, doc_review_revisions.c.content_sha256).where(
            doc_review_revisions.c.review_id == decision["review_id"],
            doc_review_revisions.c.revision == decision["review_revision"],
        )
    )).mappings().first()
    if revision is None or revision["spec_kind"] != "design":
        return _no_lineage("not_design")
    return {
        "lineage_kind": "resolved",
        "decision_id": decision_id,
        "design_review_id": decision["review_id"],
        "design_revision": decision["review_revision"],
        "design_content_sha256": revision["content_sha256"],
    }


# --- Batch rows -----------------------------------------------------------------


async def get_batch(conn, batch_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    stmt = select(ingest_batches).where(ingest_batches.c.batch_id == batch_id)
    if lock:
        stmt = stmt.with_for_update()
    return _row((await conn.execute(stmt)).mappings().first())


async def find_batch_by_source(
    conn,
    project_id: str,
    source_review_id: str,
    source_revision: int,
    source_content_sha256: str,
    *,
    lock: bool = False,
) -> dict[str, Any] | None:
    """The batch already created for this approved source revision, if any."""
    stmt = select(ingest_batches).where(
        ingest_batches.c.project_id == project_id,
        ingest_batches.c.source_review_id == source_review_id,
        ingest_batches.c.source_revision == source_revision,
        ingest_batches.c.source_content_sha256 == source_content_sha256,
    )
    if lock:
        stmt = stmt.with_for_update()
    return _row((await conn.execute(stmt)).mappings().first())


async def create_batch(
    conn,
    *,
    batch_id: str,
    project_id: str,
    source_review_id: str,
    source_revision: int,
    source_content_sha256: str,
    decision_id: str,
    lineage: Mapping[str, Any],
    idempotency_key: str,
    structure_digest: str | None = None,
    state: str = "held",
    check_results: Any = None,
    release_actor: str | None = None,
    release_receipt: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Insert the batch keyed by its proposal id; replay one for the same source.

    *lineage* is :func:`resolve_design_lineage`'s result. Without a digest the
    batch starts with the empty graph's; call :func:`store_live_digest` once the
    members exist. A design-kind batch is created ``released`` with its
    *release_receipt*; every other batch starts ``held``.
    """
    if state not in {"held", "released"}:
        raise ValueError(f"a batch is created held or released, not {state!r}")
    if state == "released" and (release_actor is None or release_receipt is None):
        raise ValueError("a batch created released needs its release actor and receipt")
    now = time.time() if now is None else now
    digest = structure_digest or compute_structure_digest([], [])
    resolved = lineage["lineage_kind"] == "resolved"
    values = {
        "batch_id": batch_id,
        "project_id": project_id,
        "source_review_id": source_review_id,
        "source_revision": source_revision,
        "source_content_sha256": source_content_sha256,
        "decision_id": decision_id,
        "design_review_id": lineage["design_review_id"] if resolved else None,
        "design_revision": lineage["design_revision"] if resolved else None,
        "design_content_sha256": lineage["design_content_sha256"] if resolved else None,
        "lineage_kind": lineage["lineage_kind"],
        "idempotency_key": idempotency_key,
        "structure_revision": 1,
        "structure_digest": digest,
        "state": state,
        "check_results": check_results,
        "created_at": now,
        "updated_at": now,
    }
    if state == "released":
        values.update(
            release_actor=release_actor, release_revision=1, release_digest=digest,
            released_at=now, release_receipt=release_receipt,
        )
    inserted = (await conn.execute(
        pg_insert(ingest_batches).values(**values)
        .on_conflict_do_nothing(constraint="uq_ingest_batches_source")
        .returning(ingest_batches.c.batch_id)
    )).scalar_one_or_none()
    batch = (
        await get_batch(conn, batch_id) if inserted is not None
        else await find_batch_by_source(
            conn, project_id, source_review_id, source_revision, source_content_sha256,
        )
    )
    return {"success": True, "replayed": inserted is None, "batch": batch}


async def set_state(
    conn,
    batch_id: str,
    state: str,
    *,
    from_states: Iterable[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Move a held batch between review states; only :func:`release` releases.

    *from_states* fences the transition against a concurrent writer.
    """
    if state not in SETTABLE_STATES:
        raise ValueError(f"set_state cannot move a batch to {state!r}")
    batch = await get_batch(conn, batch_id, lock=True)
    if batch is None:
        return _error("batch_not_found", f"Batch {batch_id} does not exist", batch_id=batch_id)
    allowed = set(from_states) if from_states is not None else HELD_STATES
    if batch["state"] not in allowed:
        return _error(
            "batch_state_conflict",
            f"Batch {batch_id} is {batch['state']}, not one of {sorted(allowed)}",
            batch_id=batch_id, state=batch["state"],
        )
    await conn.execute(
        update(ingest_batches).where(ingest_batches.c.batch_id == batch_id)
        .values(state=state, updated_at=time.time() if now is None else now)
    )
    return {"success": True, "batch_id": batch_id, "previous_state": batch["state"],
            "state": state}


async def assign_reviewer(
    conn,
    batch_id: str,
    *,
    reviewer_task_id: str | None,
    reviewer_session_id: str | None = None,
    reviewer_claim_epoch: int | None = None,
) -> None:
    """Record the reviewer; rewriting it revokes the previous holder (design §5.2)."""
    await conn.execute(
        update(ingest_batches).where(ingest_batches.c.batch_id == batch_id).values(
            reviewer_task_id=reviewer_task_id,
            reviewer_session_id=reviewer_session_id,
            reviewer_claim_epoch=reviewer_claim_epoch,
            updated_at=time.time(),
        )
    )


async def set_check_results(conn, batch_id: str, check_results: Any) -> None:
    await conn.execute(
        update(ingest_batches).where(ingest_batches.c.batch_id == batch_id)
        .values(check_results=check_results, updated_at=time.time())
    )


# --- Membership -----------------------------------------------------------------


async def members(
    conn, batch_id: str, *, state: str | None = None, lock: bool = False,
) -> list[dict[str, Any]]:
    """Membership rows ordered by member key; *state* narrows to active/retired."""
    stmt = select(ingest_batch_members).where(ingest_batch_members.c.batch_id == batch_id)
    if state is not None:
        stmt = stmt.where(ingest_batch_members.c.state == state)
    stmt = stmt.order_by(ingest_batch_members.c.member_key)
    if lock:
        stmt = stmt.with_for_update()
    return [dict(row) for row in (await conn.execute(stmt)).mappings()]


async def member_map(conn, batch_id: str) -> dict[str, str]:
    """The stable ``{member_key: task_id}`` map of the batch's active members."""
    return {row["member_key"]: row["task_id"] for row in await members(conn, batch_id,
                                                                         state="active")}


async def batch_of_task(conn, task_id: str) -> dict[str, Any] | None:
    """The active membership of *task_id*, including its batch id."""
    return _row((await conn.execute(select(ingest_batch_members).where(
        ingest_batch_members.c.task_id == task_id, ingest_batch_members.c.state == "active",
    ))).mappings().first())


async def set_member_fidelity(conn, task_id: str, **fields: Any) -> None:
    """Merge acceptance/constraints/size into the task's fidelity metadata."""
    unknown = set(fields) - set(FIDELITY_FIELDS)
    if unknown:
        raise ValueError(f"unknown member fidelity fields: {sorted(unknown)}")
    raw = (await conn.execute(select(task_metadata.c.value).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == MEMBER_FIDELITY_META,
    ))).scalar_one_or_none()
    encoded = json.dumps({**(json.loads(raw) if raw else {}), **fields}, sort_keys=True)
    stmt = pg_insert(task_metadata).values(
        task_id=task_id, key=MEMBER_FIDELITY_META, value=encoded,
    )
    await conn.execute(stmt.on_conflict_do_update(
        index_elements=["task_id", "key"], set_={"value": encoded},
    ))


async def add_member(
    conn,
    batch_id: str,
    *,
    task_id: str,
    member_key: str,
    role: str,
    section_anchor: str,
    primary_outcome: str,
    created_revision: int,
    acceptance: Any = None,
    constraints: Any = None,
    size: str | None = None,
) -> dict[str, Any]:
    """Join *task_id* to the batch and, while it is held, acquire the hold.

    Membership is authoritative, not inferred from parents (design §4): the
    hold travels with the task wherever it is reparented until release.
    """
    if role not in MEMBER_ROLES:
        raise ValueError(f"unknown member role {role!r}")
    batch = await get_batch(conn, batch_id, lock=True)
    if batch is None:
        return _error("batch_not_found", f"Batch {batch_id} does not exist", batch_id=batch_id)
    if batch["state"] not in HELD_STATES and batch["state"] != "released":
        return _error("batch_not_held", f"Batch {batch_id} is {batch['state']}",
                      batch_id=batch_id, state=batch["state"])
    await conn.execute(ingest_batch_members.insert().values(
        batch_id=batch_id, task_id=task_id, member_key=member_key, role=role,
        section_anchor=section_anchor, primary_outcome=primary_outcome, state="active",
        created_revision=created_revision,
    ))
    if batch["state"] in HELD_STATES:
        await conn.execute(
            update(tasks).where(tasks.c.id == task_id).values(held_batch_id=batch_id)
        )
    fidelity = {"acceptance": acceptance, "constraints": constraints, "size": size}
    if any(value is not None for value in fidelity.values()):
        await set_member_fidelity(conn, task_id, **fidelity)
    return {"success": True, "batch_id": batch_id, "task_id": task_id,
            "member_key": member_key, "held": batch["state"] in HELD_STATES}


async def retire_member(
    conn, batch_id: str, task_id: str, *, reason: str, retired_revision: int,
) -> dict[str, Any]:
    """Retire a member in the audit; its row and history stay (design §4).

    The task keeps ``held_batch_id`` so an archived-obsolete member can never
    be admitted by a stale path; release clears it with the rest.
    """
    result = await conn.execute(
        update(ingest_batch_members).where(
            ingest_batch_members.c.batch_id == batch_id,
            ingest_batch_members.c.task_id == task_id,
            ingest_batch_members.c.state == "active",
        ).values(state="retired", retired_reason=reason, retired_revision=retired_revision)
    )
    if result.rowcount != 1:
        return _error("membership", f"{task_id} is not an active member of {batch_id}",
                      batch_id=batch_id, task_id=task_id)
    return {"success": True, "batch_id": batch_id, "task_id": task_id}


# --- Live structure and revision fence ----------------------------------------


async def hydrate_structure(
    conn, batch_id: str, *, lock: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Active members joined to their current task rows, plus incident edges.

    The result is :func:`src.task_graph.batch_digest.compute_structure_digest`
    input. *lock* takes ``FOR UPDATE`` on the membership and task rows, as
    release requires (spec P3 L3.1 step 2).
    Informational links from the server-assigned fidelity reviewer are excluded:
    dispatching review does not change the graph being reviewed.
    """
    stmt = (
        select(
            ingest_batch_members.c.task_id,
            ingest_batch_members.c.member_key,
            ingest_batch_members.c.role,
            ingest_batch_members.c.state,
            ingest_batch_members.c.section_anchor,
            ingest_batch_members.c.primary_outcome,
            tasks.c.parent_task_id,
            tasks.c.title,
            tasks.c.description,
        )
        .select_from(ingest_batch_members.join(tasks, tasks.c.id == ingest_batch_members.c.task_id))
        .where(
            ingest_batch_members.c.batch_id == batch_id,
            ingest_batch_members.c.state == "active",
        )
        .order_by(ingest_batch_members.c.member_key)
    )
    if lock:
        stmt = stmt.with_for_update(of=[ingest_batch_members, tasks])
    rows = [dict(row) for row in (await conn.execute(stmt)).mappings()]
    expected = len(await members(conn, batch_id, state="active"))
    if len(rows) != expected:
        raise ValueError(f"Batch {batch_id} has active members without a live task row")
    ids = [row["task_id"] for row in rows]
    if not ids:
        return [], []
    fidelity = {
        task_id: json.loads(value)
        for task_id, value in (await conn.execute(select(
            task_metadata.c.task_id, task_metadata.c.value,
        ).where(
            task_metadata.c.task_id.in_(ids), task_metadata.c.key == MEMBER_FIDELITY_META,
        ))).all()
    }
    ownership = {
        task_id: json.loads(value)
        for task_id, value in (await conn.execute(select(
            task_metadata.c.task_id, task_metadata.c.value,
        ).where(
            task_metadata.c.task_id.in_(ids), task_metadata.c.key == "owned_modules",
        ))).all()
    }
    for row in rows:
        stored = fidelity.get(row["task_id"], {})
        row.update({field: stored.get(field) for field in FIDELITY_FIELDS})
        if row["task_id"] in ownership:
            row["owned_modules"] = ownership[row["task_id"]]
    edges = [dict(row) for row in (await conn.execute(
        select(
            task_dependencies.c.task_id,
            task_dependencies.c.depends_on_task_id,
            task_dependencies.c.dep_type,
            task_dependencies.c.description,
        ).where(or_(
            task_dependencies.c.task_id.in_(ids),
            task_dependencies.c.depends_on_task_id.in_(ids),
        ), ~and_(
            # Reviewer assignment is control-plane bookkeeping, not a work
            # graph edit. Include execution edges even from these tasks; omit
            # only informational links from this batch's server-routed role.
            task_dependencies.c.dep_type == "related",
            task_dependencies.c.task_id.in_(select(tasks.c.id).where(
                tasks.c.project_id == select(ingest_batches.c.project_id).where(
                    ingest_batches.c.batch_id == batch_id,
                ).scalar_subquery(),
                tasks.c.route_source == "role",
                tasks.c.profile_id == FIDELITY_REVIEWER_PROFILE_ID,
                tasks.c.dedup_key == fidelity_review_dedup_key(batch_id),
            )),
        ))
    )).mappings()]
    return rows, edges


async def live_digest(conn, batch_id: str, *, lock: bool = False) -> str:
    """The structure digest of the batch's current live rows."""
    return compute_structure_digest(*await hydrate_structure(conn, batch_id, lock=lock))


async def store_live_digest(conn, batch_id: str) -> str:
    """Store the live digest at the current revision (initial materialization)."""
    digest = await live_digest(conn, batch_id)
    await conn.execute(
        update(ingest_batches).where(ingest_batches.c.batch_id == batch_id)
        .values(structure_digest=digest, updated_at=time.time())
    )
    return digest


async def bump_revision(conn, batch_id: str, expected_revision: int) -> dict[str, Any]:
    """Advance the structure revision after a structural edit, or refuse stale.

    Recomputes the digest from live rows. Receipts stay bound to the revision
    and digest they attested, so every receipt for an older revision is
    superseded; a ``reviewing`` batch (passing receipt awaiting release)
    therefore returns to ``held`` until a receipt on the new revision.
    """
    batch = await get_batch(conn, batch_id, lock=True)
    if batch is None:
        return _error("batch_not_found", f"Batch {batch_id} does not exist", batch_id=batch_id)
    if batch["state"] not in HELD_STATES:
        return _error("batch_not_held", f"Batch {batch_id} is {batch['state']}",
                      batch_id=batch_id, state=batch["state"])
    if batch["structure_revision"] != expected_revision:
        return _error(
            "stale_revision",
            f"Batch {batch_id} is at revision {batch['structure_revision']}, "
            f"not {expected_revision}",
            batch_id=batch_id, retryable=True,
            expected_revision=expected_revision,
            structure_revision=batch["structure_revision"],
            structure_digest=batch["structure_digest"],
        )
    revision = expected_revision + 1
    digest = await live_digest(conn, batch_id)
    state = "held" if batch["state"] == "reviewing" else batch["state"]
    await conn.execute(
        update(ingest_batches).where(ingest_batches.c.batch_id == batch_id).values(
            structure_revision=revision, structure_digest=digest, state=state,
            updated_at=time.time(),
        )
    )
    return {"success": True, "batch_id": batch_id, "previous_revision": expected_revision,
            "structure_revision": revision, "structure_digest": digest, "state": state}


def _stale(batch: Mapping[str, Any], expected_revision: int, expected_digest: str,
           live: str | None = None) -> dict[str, Any] | None:
    current = live if live is not None else batch["structure_digest"]
    if batch["structure_revision"] == expected_revision and current == expected_digest:
        return None
    return _error(
        "stale_revision",
        f"Batch {batch['batch_id']} is at revision {batch['structure_revision']} "
        f"({current}), not {expected_revision} ({expected_digest})",
        batch_id=batch["batch_id"], retryable=True,
        expected_revision=expected_revision, expected_digest=expected_digest,
        structure_revision=batch["structure_revision"], structure_digest=current,
    )


# --- Receipts and release -----------------------------------------------------


_RECEIPT_IDENTITY = (
    "reviewer_task_id", "reviewer_session_id", "passing", "section_verdicts",
    "reverse_coverage_verdict", "walkthrough_verdict", "structural_findings",
)


async def passing_receipt(
    conn, batch_id: str, structure_revision: int,
) -> dict[str, Any] | None:
    return _row((await conn.execute(select(ingest_batch_receipts).where(
        ingest_batch_receipts.c.batch_id == batch_id,
        ingest_batch_receipts.c.structure_revision == structure_revision,
        ingest_batch_receipts.c.passing.is_(True),
    ))).mappings().first())


async def record_receipt(
    conn,
    batch_id: str,
    *,
    structure_revision: int,
    structure_digest: str,
    reviewer_task_id: str,
    reviewer_session_id: str,
    passing: bool,
    section_verdicts: Mapping[str, Any],
    reverse_coverage_verdict: str,
    walkthrough_verdict: str,
    structural_findings: Any,
    now: float | None = None,
) -> dict[str, Any]:
    """Store a reviewer's receipt bound to the batch's current revision and digest.

    The command validates authority and verdict coverage and derives
    *passing*. A batch holds one passing receipt per revision: an identical
    passing receipt replays, a different one is refused ``receipt_exists``.
    Identical nonpassing receipts also replay without creating duplicate evidence.
    """
    batch = await get_batch(conn, batch_id, lock=True)
    if batch is None:
        return _error("batch_not_found", f"Batch {batch_id} does not exist", batch_id=batch_id)
    if batch["state"] not in HELD_STATES:
        return _error("batch_not_held", f"Batch {batch_id} is {batch['state']}",
                      batch_id=batch_id, state=batch["state"])
    stale = _stale(batch, structure_revision, structure_digest)
    if stale:
        return stale
    values = {
        "reviewer_task_id": reviewer_task_id,
        "reviewer_session_id": reviewer_session_id,
        "passing": bool(passing),
        "section_verdicts": dict(section_verdicts),
        "reverse_coverage_verdict": reverse_coverage_verdict,
        "walkthrough_verdict": walkthrough_verdict,
        "structural_findings": structural_findings,
    }
    if passing:
        existing = await passing_receipt(conn, batch_id, structure_revision)
        if existing is not None:
            if all(existing[key] == values[key] for key in _RECEIPT_IDENTITY):
                return {"success": True, "replayed": True, "receipt": existing}
            return _error(
                "receipt_exists",
                f"Batch {batch_id} already has a passing receipt for revision "
                f"{structure_revision}",
                batch_id=batch_id, receipt_id=existing["receipt_id"],
            )
    else:
        existing_rows = (await conn.execute(select(ingest_batch_receipts).where(
            ingest_batch_receipts.c.batch_id == batch_id,
            ingest_batch_receipts.c.structure_revision == structure_revision,
            ingest_batch_receipts.c.structure_digest == structure_digest,
            ingest_batch_receipts.c.passing.is_(False),
        ))).mappings()
        for existing in existing_rows:
            if all(existing[key] == values[key] for key in _RECEIPT_IDENTITY):
                return {"success": True, "replayed": True, "receipt": dict(existing)}
    receipt_id = "rcpt-" + uuid.uuid4().hex[:16]
    await conn.execute(ingest_batch_receipts.insert().values(
        receipt_id=receipt_id, batch_id=batch_id, structure_revision=structure_revision,
        structure_digest=structure_digest, created_at=time.time() if now is None else now,
        **values,
    ))
    receipt = _row((await conn.execute(select(ingest_batch_receipts).where(
        ingest_batch_receipts.c.receipt_id == receipt_id,
    ))).mappings().first())
    return {"success": True, "replayed": False, "receipt": receipt}


async def release(
    conn,
    batch_id: str,
    *,
    expected_revision: int,
    expected_digest: str,
    actor: str,
    release_receipt: Any,
    now: float | None = None,
) -> dict[str, Any]:
    """Clear every member's hold and record the release, fenced by revision/digest.

    The digest is recomputed from locked live rows, so an edit the caller did
    not see refuses ``stale_revision``. A replay of the same release returns
    ``already_released`` with the stored receipt. Authority, source approval,
    checks, receipt and claim verification, readiness recomputation and events
    belong to the release command (spec P3 L3.1).
    """
    batch = await get_batch(conn, batch_id, lock=True)
    if batch is None:
        return _error("batch_not_found", f"Batch {batch_id} does not exist", batch_id=batch_id)
    if batch["state"] == "released":
        if (batch["release_revision"], batch["release_digest"]) != (
            expected_revision, expected_digest,
        ):
            return _stale({**batch, "structure_revision": batch["release_revision"]},
                          expected_revision, expected_digest, batch["release_digest"])
        return {"success": True, "already_released": True, "batch_id": batch_id,
                "release_receipt": batch["release_receipt"]}
    if batch["state"] not in HELD_STATES:
        return _error("batch_not_held", f"Batch {batch_id} is {batch['state']}",
                      batch_id=batch_id, state=batch["state"])
    stale = _stale(batch, expected_revision, expected_digest,
                   await live_digest(conn, batch_id, lock=True))
    if stale:
        return stale
    now = time.time() if now is None else now
    released = sorted((await conn.execute(
        update(tasks).where(tasks.c.held_batch_id == batch_id)
        .values(held_batch_id=None).returning(tasks.c.id)
    )).scalars())
    await conn.execute(
        update(ingest_batches).where(ingest_batches.c.batch_id == batch_id).values(
            state="released", release_actor=actor, release_revision=expected_revision,
            release_digest=expected_digest, released_at=now, release_receipt=release_receipt,
            updated_at=now,
        )
    )
    await conn.execute(
        update(task_proposals).where(task_proposals.c.id == batch_id)
        .values(status="committed", updated_at=now)
    )
    return {"success": True, "already_released": False, "batch_id": batch_id,
            "released_task_ids": released, "release_receipt": release_receipt}
