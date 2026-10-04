"""Shared predicates for the lifecycle of a pool-held task claim."""

from __future__ import annotations

from src.models import TaskStatus


LIVE_POOL_CLAIM_TASK_STATUSES = frozenset((TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED))


def is_live_pool_claim_task_status(status: TaskStatus | None) -> bool:
    """Whether ``status`` may continue to occupy a pool worker's claim slot."""
    return status in LIVE_POOL_CLAIM_TASK_STATUSES


def pool_claim_loop_stall_seconds(swarm) -> float:
    """How long an idle pool worker may show no sign of its claim loop.

    A healthy worker may sit in one server-side long poll for
    ``claim_wait_max`` seconds, then need a scheduler tick to issue the next
    one.  Two complete windows avoid confusing that normal silence with
    abandonment.  ``prepare_timeout`` is also a lower bound: both values are
    existing operator-facing bounds on the same preparation and retry path,
    so this introduces no unbounded idle supply state.
    """
    return max(1.0, float(swarm.prepare_timeout), 2.0 * float(swarm.claim_wait_max))


def idle_pool_claim_loop_stalled(session, *, now: float, stall_seconds: float) -> bool:
    """Whether an idle pool session has stopped entering ``task_claim``.

    ``task_claim`` stamps ``last_activity`` at entry, and pane or transcript
    activity only ever advances it, so a stamp older than *stall_seconds* on a
    session holding no task and no claim phase means nothing has claimed from
    it for two long-poll windows.  That is what a worker parked on a
    provider's usage-limit or login screen looks like — it never reaches its
    loop — and such a session is not supply, whatever its row's state says.
    The caller checks the idle shape; this reads only the clock.
    """
    last = session.last_activity or session.started_at
    return last is not None and last <= now - stall_seconds


def pool_claim_cap(config, profile) -> int | None:
    """The claim budget ``take_claim_slot`` will enforce for this pool session.

    One definition, two callers: ``ClaimCommandsMixin._pool_context_claim_cap``
    answers it at claim time, and the reconciler's idle-stop grace answers it
    to recognise a worker whose context is spent.  A recycled session must
    never be told its budget is spent when ``take_claim_slot`` would still hand
    it work.

    ``None`` means unbounded (no profile limit and ``fresh_context_per_task``
    off), and ``0`` is not a budget: a session that has claimed nothing is not
    exhausted.
    """
    swarm = getattr(config, "swarm", None)
    if swarm is not None and swarm.fresh_context_per_task:
        # A reused global worker must not carry a previous task's conversation.
        return 1
    cap = getattr(profile, "max_claims_per_session", None)
    return None if cap is None else int(cap)


def pool_claim_budget_exhausted(session, cap: int | None) -> bool:
    """Whether this session has spent the budget :func:`pool_claim_cap` returns.

    ``take_claim_slot`` answers ``session_exhausted`` from exactly this
    comparison, which is the worker protocol's "leave" signal.
    """
    return cap is not None and cap > 0 and int(session.claims or 0) >= cap
