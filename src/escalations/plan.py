"""Decide which deliveries an incident *should* have, from durable state only.

The planner is a pure function of (incident, its message history, the
delivery rows that already exist).  That is what makes §7's "routine gateway
reconnects, duplicate events and multiple workers must not create duplicate
threads/posts" true by construction: replaying an event re-derives the same
dedup keys, and :meth:`~src.database.queries.escalation_queries
.EscalationQueriesMixin.enqueue_escalation_delivery` turns a repeat into a
no-op through the unique ``dedup_key``.

Nothing here sends anything or reads a clock; the dispatcher supplies ``now``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.escalations.facts import (
    KIND_ACK,
    KIND_RELAY,
    KIND_RESOLUTION,
    KIND_ROOT,
    PRIORITY_FOLLOWUP,
    PRIORITY_RESOLUTION,
    PRIORITY_ROOT,
    EscalationFacts,
    TransportBinding,
)

#: A delivery is abandoned to ``unknown`` after this many attempts rather than
#: retrying an external send forever.
MAX_ATTEMPTS = 6
#: Bounded exponential backoff, in seconds, indexed by attempt count.
BACKOFF_SECONDS: tuple[float, ...] = (15.0, 60.0, 300.0, 900.0, 1800.0)
#: How long a delivery whose thread does not exist yet waits before re-checking.
DEFER_SECONDS = 10.0

#: Statuses that still owe an external send; a delivery in one of these is
#: "in flight" for the purpose of the one-pending-replacement rule.
LIVE_DELIVERY_STATUSES = frozenset({"pending", "sending", "retry"})


@dataclass(frozen=True)
class PlannedDelivery:
    """One row :func:`plan_deliveries` wants to exist."""

    dedup_key: str
    kind: str
    payload: dict[str, Any]
    priority: int
    generation: int = 0
    escalation_message_id: str | None = None


@dataclass(frozen=True)
class DeliveryPlan:
    """What the incident needs next, plus why nothing is wanted when it is not."""

    deliveries: tuple[PlannedDelivery, ...] = ()
    skipped: tuple[str, ...] = field(default_factory=tuple)


def root_dedup_key(escalation_id: str, generation: int) -> str:
    return f"{escalation_id}:{KIND_ROOT}:{generation}"


def ack_dedup_key(escalation_id: str, message_id: str) -> str:
    return f"{escalation_id}:{KIND_ACK}:{message_id}"


def relay_dedup_key(escalation_id: str, message_id: str) -> str:
    return f"{escalation_id}:{KIND_RELAY}:{message_id}"


def resolution_dedup_key(escalation_id: str, generation: int) -> str:
    return f"{escalation_id}:{KIND_RESOLUTION}:{generation}"


def backoff_for(attempt_count: int) -> float:
    """Bounded backoff for the ``attempt_count``-th failure."""
    index = max(0, attempt_count - 1)
    return BACKOFF_SECONDS[min(index, len(BACKOFF_SECONDS) - 1)]


def current_generation(deliveries: Iterable[Mapping[str, Any]]) -> int:
    """Highest root generation any delivery row mentions."""
    return max(
        (int(row["generation"]) for row in deliveries if row["kind"] == KIND_ROOT),
        default=0,
    )


def binding_from_deliveries(deliveries: Iterable[Mapping[str, Any]]) -> TransportBinding:
    """Recover where the incident lives from confirmed delivery receipts.

    Only a ``sent`` root counts as a binding, and the newest generation wins;
    a retried or abandoned attempt of an older generation can never resurrect
    a stale message ID.  A root that got as far as the channel post but not
    the thread still binds (its IDs are recorded on the row) so the retry
    creates only the missing half.
    """
    best: TransportBinding | None = None
    for row in deliveries:
        if row["kind"] != KIND_ROOT or not row.get("root_message_id"):
            continue
        generation = int(row["generation"])
        if best is not None and generation < best.generation:
            continue
        candidate = TransportBinding(
            channel_id=(str(row["channel_id"]) if row.get("channel_id") else None),
            root_message_id=str(row["root_message_id"]),
            thread_id=(str(row["thread_id"]) if row.get("thread_id") else None),
            generation=generation,
        )
        if (
            best is None
            or generation > best.generation
            or (candidate.has_thread and not best.has_thread)
        ):
            best = candidate
    return best or TransportBinding(generation=current_generation(deliveries))


def has_pending_root(
    deliveries: Iterable[Mapping[str, Any]], *, generation: int | None = None
) -> bool:
    """True when a root send is still owed (the one-replacement-at-a-time fence)."""
    for row in deliveries:
        if row["kind"] != KIND_ROOT or row["status"] not in LIVE_DELIVERY_STATUSES:
            continue
        if generation is None or int(row["generation"]) >= generation:
            return True
    return False


def plan_replacement(
    facts: EscalationFacts,
    deliveries: Sequence[Mapping[str, Any]],
) -> PlannedDelivery | None:
    """A new root generation after the previous post or thread was deleted.

    Refused for a terminal incident — §7 is explicit that a replacement must
    not reopen resolved work — and refused while another root is still owed,
    which is the "at most one pending replacement at a time" rule.
    """
    if facts.is_terminal:
        return None
    generation = current_generation(deliveries)
    if has_pending_root(deliveries):
        return None
    next_generation = generation + 1
    return PlannedDelivery(
        dedup_key=root_dedup_key(facts.id, next_generation),
        kind=KIND_ROOT,
        payload={"replacement": True, "replaces_generation": generation},
        priority=PRIORITY_ROOT,
        generation=next_generation,
    )


def plan_deliveries(
    facts: EscalationFacts,
    *,
    deliveries: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    relay_authors: Sequence[str] = (),
) -> DeliveryPlan:
    """The full set of deliveries the incident's current state implies.

    Returned deliveries are *desired*, not new: the caller enqueues each one
    and the unique ``dedup_key`` makes a repeat a no-op.  ``skipped`` carries
    the reason a plausible delivery was withheld so an operator surface can
    say why nothing was posted.
    """
    planned: list[PlannedDelivery] = []
    skipped: list[str] = []
    generation = current_generation(deliveries)
    binding = binding_from_deliveries(deliveries)
    known = {row["dedup_key"] for row in deliveries}

    root_key = root_dedup_key(facts.id, generation)
    if root_key not in known:
        if facts.is_terminal and not binding.has_root:
            # Closed before it was ever posted: nothing to announce, and a
            # post now would ask a human for a decision nobody still needs.
            skipped.append("terminal incident was never posted")
        else:
            planned.append(
                PlannedDelivery(
                    dedup_key=root_key,
                    kind=KIND_ROOT,
                    payload={"replacement": generation > 0},
                    priority=PRIORITY_ROOT,
                    generation=generation,
                )
            )

    posted = binding.has_root or root_key in known
    if posted:
        for message in messages:
            message_id = str(message["id"])
            if message.get("direction") == "inbound":
                key = ack_dedup_key(facts.id, message_id)
                if key not in known:
                    planned.append(
                        PlannedDelivery(
                            dedup_key=key,
                            kind=KIND_ACK,
                            payload={},
                            priority=PRIORITY_FOLLOWUP,
                            generation=generation,
                            escalation_message_id=message_id,
                        )
                    )
                continue
            # Outbound history is supervisor-authored.  Only messages the
            # supervisor addressed to this incident are relayed; unrelated
            # supervisor chat stays in the dashboard (§7).
            actor = str(message.get("verified_actor") or "")
            if relay_authors and actor not in relay_authors:
                continue
            key = relay_dedup_key(facts.id, message_id)
            if key not in known:
                planned.append(
                    PlannedDelivery(
                        dedup_key=key,
                        kind=KIND_RELAY,
                        payload={"text": str(message.get("text") or "")},
                        priority=PRIORITY_FOLLOWUP,
                        generation=generation,
                        escalation_message_id=message_id,
                    )
                )

    if facts.is_terminal and posted:
        key = resolution_dedup_key(facts.id, generation)
        if key not in known:
            planned.append(
                PlannedDelivery(
                    dedup_key=key,
                    kind=KIND_RESOLUTION,
                    payload={"revision": facts.revision},
                    priority=PRIORITY_RESOLUTION,
                    generation=generation,
                )
            )

    return DeliveryPlan(deliveries=tuple(planned), skipped=tuple(skipped))
