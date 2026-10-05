"""Closing the gate of a review that will never be approved.

A review gate (type ``review``, ``await_id`` = review id) is the durable
barrier that keeps work filed with ``--after-review`` from starting on a
document nobody approved.  Only an approval resolves it; a withdrawal used to
leave it ``open`` forever, which orphaned three things at once:

* the gate itself — nothing could ever resolve it (``aq gate resolve`` refuses
  review gates), so its escalation stayed live and its Discord card kept asking
  the human a question about a review that no longer existed;
* its waiters, blocked with nothing to wait *for*;
* the dashboards and the layout endpoints, which read an open gate as a live
  dependency.

So a withdrawal now closes the gate in the same transaction that moves the
review to ``withdrawn``, and this module owns that write.  Two properties make
it safe:

* **The resolution names itself.**  ``resolution="withdrawn"`` is what tells
  every reader that this gate resolved *without* an approval, so nothing
  downstream mistakes a withdrawal for consent.
* **The waiters fail closed.**  Resolving a gate releases its waiters, so
  releasing them onto an unapproved design would trade an orphan for a worse
  bug.  Every waiter is therefore labelled ``hold:review_withdrawn`` on the
  same transaction: a held task is visible and unblocked but never scheduled
  (design §6), ``aq task explain`` names the hold as the reason, and
  ``needs_attention`` keeps its close honest.  One command releases a waiter
  when the human decides what replaces the review:

      aq task edit --task-id <id> --labels-remove hold:review_withdrawn

The hold is written *before* the gate is resolved so the post-resolution
frontier check (``_note_frontier_entry``, which excludes held tasks) never
announces a released waiter as ready work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.database.queries.escalation_queries import (
    OPEN_ESCALATION_STATES,
    TERMINAL_ESCALATION_STATES,
    EscalationStateError,
)
from src.database.queries.gate_queries import GateResolution
from src.models import HOLD_LABEL_PREFIX

logger = logging.getLogger(__name__)

__all__ = [
    "REVIEW_GATE_WITHDRAWN_RESOLUTION",
    "REVIEW_WITHDRAWN_ATTENTION",
    "REVIEW_WITHDRAWN_HOLD",
    "WithdrawnGateClosure",
    "close_withdrawn_gate",
    "retire_gate_escalation",
    "review_gate_resolution_is_terminal",
]

#: The ``gates.resolution`` a withdrawal writes.  Not a magic string: it is
#: the fact every reader of the gate uses to tell "approved" from "closed".
REVIEW_GATE_WITHDRAWN_RESOLUTION = "withdrawn"

#: The hold every waiter of a withdrawn review's gate is put on (design §6).
REVIEW_WITHDRAWN_HOLD = f"{HOLD_LABEL_PREFIX}review_withdrawn"

#: The ``needs_attention`` code, unchanged from the flag-only era: it is what
#: refuses a passing close and what the doctor check reports.
REVIEW_WITHDRAWN_ATTENTION = "review_withdrawn"

#: Statuses a waiter can no longer leave ``is_blocked`` from; holding one of
#: these says nothing, because nothing will ever schedule it again.
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "ARCHIVED"})


@dataclass(frozen=True)
class WithdrawnGateClosure:
    """What closing a withdrawn review's gate wrote."""

    gate_id: str
    #: Who closed it — the decider label on a withdrawal, or the caller of
    #: ``aq gate resolve``.  Carried so the post-commit announcement can name
    #: the same actor the ``gates`` row does.
    resolved_by: str = ""
    #: ``False`` when the gate was absent or already resolved: nothing was
    #: written and there is nothing to announce.
    resolved: bool = False
    #: Waiters that were left unblocked by the resolution but held instead.
    held_task_ids: tuple[str, ...] = ()
    #: The write itself, to be announced after the transaction commits.
    resolution: GateResolution | None = field(default=None, repr=False)


async def review_gate_resolution_is_terminal(db, gate_id: str) -> tuple[bool, str]:
    """``(terminal, review_id)`` for a ``review`` gate whose review can no longer decide.

    *Terminal* means the review will never approve: ``withdrawn`` (closed for
    good) or ``approved`` (whose gate should already be resolved, and whose
    open gate is the ``reviews.consistency`` repair case).  ``rejected`` is
    deliberately **not** terminal — the author resubmits into ``in_review``
    against this same gate, and resolving it early would release the waiters
    onto a design the human turned down.

    A missing gate, or one whose review row is gone, is not terminal: the
    caller keeps refusing.
    """
    gate = await db.get_gate(gate_id)
    if gate is None or str(gate.get("gate_type") or "") != "review":
        return False, ""
    review_id = str(gate.get("await_id") or "")
    review = await db.get_review(review_id) if review_id else None
    if review is None:
        return False, review_id
    return str(review.get("state") or "") in {"approved", "withdrawn"}, review_id


async def close_withdrawn_gate(
    db,
    gate_id: str,
    *,
    resolved_by: str,
    conn=None,
) -> WithdrawnGateClosure:
    """Resolve *gate_id* ``withdrawn``, holding every waiter on the same transaction.

    ``conn`` joins a transaction the caller already owns — ``aq review
    withdraw`` passes the transaction that moved the review to ``withdrawn``,
    so the two cannot disagree.  Without one this opens its own.

    The post-commit announcements are the caller's in both cases: run
    ``db.announce_gate_resolution(closure.resolution)`` once the transaction
    has committed, and emit the gate's ``gate.resolved`` event.
    """
    if conn is not None:
        return await _close_on(db, conn, gate_id, resolved_by=resolved_by)
    async with db.immediate() as owned:
        return await _close_on(db, owned, gate_id, resolved_by=resolved_by)


async def _close_on(db, conn, gate_id: str, *, resolved_by: str) -> WithdrawnGateClosure:
    waiters = sorted(await db.get_gate_waiters(gate_id))
    live = [tid for tid in waiters if not await _is_terminal_task(db, tid)]
    # Hold first: a held waiter is excluded from the frontier check that
    # ``resolve_gate_on`` runs, so no released waiter is announced as ready.
    for task_id in live:
        await db.add_task_label(task_id, REVIEW_WITHDRAWN_HOLD, conn=conn)
        await db.set_task_meta(task_id, "needs_attention", REVIEW_WITHDRAWN_ATTENTION, conn=conn)
    resolution = await db.resolve_gate_on(
        gate_id,
        resolved_by=resolved_by,
        resolution=REVIEW_GATE_WITHDRAWN_RESOLUTION,
        conn=conn,
    )
    return WithdrawnGateClosure(
        gate_id=gate_id,
        resolved_by=resolved_by,
        resolved=resolution.resolved,
        held_task_ids=tuple(live if resolution.resolved else ()),
        resolution=resolution,
    )


async def _is_terminal_task(db, task_id: str) -> bool:
    task = await db.get_task(task_id)
    status = getattr(getattr(task, "status", None), "value", None)
    return str(status or "") in _TERMINAL_STATUSES


async def retire_gate_escalation(
    db,
    gate_id: str,
    *,
    now: float,
    review_id: str = "",
    reason: str = "",
) -> list[dict]:
    """Close any live escalation bound to *gate_id*, and return the closed rows.

    A gate escalation exists to ask a human to decide about that gate.  Once
    the gate is terminal there is nobody left to decide, so the question is
    retired here rather than waiting for the §5.5 ``gate_resolved`` tick — that
    tick is gated on ``discord.escalations.stateful`` and only runs on its
    interval, which is exactly how a withdrawn review's card stayed live for a
    day with an unanswered reply on it.

    ``stale`` is the terminal state: §5.2 renders it as *obsolete*, which is
    what "the question is no longer being asked" means, and it is reachable by
    a legal transition from every open state (``needs_human``,
    ``reply_received``, ``resolving``).  The ``outcome`` still records
    ``gate_resolved``, because the gate resolving is the fact behind it.

    *reason* is the free text a withdrawal carries; it is trimmed into the one
    line a reader of the collapsed post sees, and never replaces the fact
    (``review withdrawn``) that says why.

    Idempotent and never raises: a row that moved underneath (a human reply
    landed first) is left for the next tick rather than forced.
    """
    rows = await db.list_escalations(
        source_kind="gate",
        source_identity=gate_id,
        states=tuple(sorted(OPEN_ESCALATION_STATES)),
    )
    closed: list[dict] = []
    for row in rows:
        if str(row.get("state") or "") in TERMINAL_ESCALATION_STATES:
            continue
        note = " ".join((reason or "").split())[:200]
        terminal_outcome = (
            f"Gate {gate_id} resolved: review withdrawn"
            + (f" ({note})" if note else "")
        )
        try:
            updated = await db.transition_escalation(
                str(row["id"]),
                expected_revision=int(row["revision"]),
                new_state="stale",
                terminal_outcome=terminal_outcome,
                terminal_evidence={
                    "gate_id": gate_id,
                    "gate_type": "review",
                    "review_id": review_id,
                    "resolution": REVIEW_GATE_WITHDRAWN_RESOLUTION,
                },
                outcome="gate_resolved",
                now=now,
            )
        except EscalationStateError:
            logger.debug("retiring escalation %s refused", row.get("id"), exc_info=True)
            continue
        if updated is not None:
            closed.append(updated)
    return closed
