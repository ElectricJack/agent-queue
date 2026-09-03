"""Durable waits — Package 4 child plan §4.6 and T-6.

The race this closes is the one the design spec names: an event that arrives
between "decide to wait" and "the pause is persisted" must not be lost.  It
is closed by *construction*, not by retry, and the construction has three
parts, only the first of which lives here:

1. the executor computes the exact typed correlation key from the run's own
   scope and returns ``SUSPEND`` carrying an inert :class:`WaitSpec`;
2. the engine writes the paused snapshot, the receipt and the registration in
   **one** ``commit_boundary`` transaction;
3. Package 3's ``register`` scans the durable pending-event inbox inside that
   same transaction and reports ``matched_immediately`` when ingestion got
   there first, so the engine resumes instead of sleeping.

The rule Package 4 owns is the ordering of the other side: **event ingestion
writes the inbox before it matches waits**, never the reverse.  Whichever
side wins the per-playbook delivery lock, the loser sees what the winner
persisted, so all three interleavings end in exactly one resume.

Deadlines are *not* ``TimerService`` entries.  ``src/timer_service.py`` is a
playbook-*trigger* scheduler whose entries are cron-like and operator-visible;
a per-run wait is neither.  :class:`~src.playbooks.engine.WaitScheduler` owns
``deadline_at`` instead, and the earlier of the wait deadline and the run
deadline wins.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from src.playbooks.definition import WAIT_BUSINESS_OUTCOMES, WaitStep
from src.playbooks.executors.base import (
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
    project_step_receipt,
)
from src.playbooks.expressions import ValueResolutionError, resolve_value
from src.playbooks.waits import WaitSpec

#: ``WaitStep.wait_kind`` is the author's vocabulary; ``WaitSpec.kind`` is the
#: storage vocabulary.  They agree on three of four names, and the fourth is
#: mapped here rather than at each call site so a new kind cannot be added on
#: one side only.
WAIT_KIND_STORAGE: Mapping[str, str] = {
    "event": "event",
    "human": "human",
    "task": "agent_task",
    "timer": "timer",
}

#: The outcome a suspension receipt records.  It is deliberately *not* one of
#: the step's declared outcomes: nothing was decided, no edge was selected,
#: and the transition is chosen at the resume boundary instead.
SUSPENDED_OUTCOME = "waiting"

#: The dry-run / shadow reason, quoted by the tree node.
UNRESOLVED_REASON = "wait_not_persisted"


def wait_id_for(run_id: str, step_id: str, iteration: int, attempt: int) -> str:
    """A wait id that is a *function* of the attempt that opens it.

    Restart safety depends on this being deterministic.  A crash between the
    executor returning and the boundary committing leaves nothing registered
    and nothing recorded, so the replay is the same attempt of the same step
    and computes the same id — which means a registration that *did* land
    collides on the primary key instead of opening a second wait for one
    suspension.
    """
    digest = hashlib.sha256(f"{run_id}|{step_id}|{iteration}|{attempt}".encode()).hexdigest()
    return f"w-{digest[:30]}"


def correlation_match(correlation: Any) -> dict[str, Any]:
    """The inert match predicate for a resolved correlation key.

    A mapping is already ``{dotted field path: required literal}`` and is used
    verbatim, which is what makes an author's ``ObjectValue`` correlation key
    an exact field match.  Anything else correlates on a single synthetic
    field, so a scalar key still produces a *predicate* rather than a value
    the matcher would have to interpret.
    """
    if isinstance(correlation, Mapping):
        return {str(name): value for name, value in correlation.items()}
    if correlation is None:
        return {}
    return {"correlation_key": correlation}


@dataclass(frozen=True, slots=True)
class WaitResumption:
    """What ended one suspension.

    One shape for all four ways a wait ends — an inbox match consumed at
    registration, an event claimed later, a human answer, a child task, a
    deadline — so the outcome vocabulary below has exactly one input and a
    new resume path cannot quietly grow a fifth branch.
    """

    expired: bool = False
    event_type: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    at: float = 0.0

    @property
    def decision(self) -> str | None:
        value = self.payload.get("resolution") or self.payload.get("decision")
        return str(value) if value is not None else None

    @property
    def task_id(self) -> str | None:
        value = self.payload.get("task_id")
        return str(value) if value is not None else None

    @property
    def task_status(self) -> str:
        return str(self.payload.get("status") or "")


def resolve_wait_result(
    step: WaitStep, resumption: WaitResumption
) -> tuple[str, dict[str, Any] | None]:
    """The outcome and bound value a resumption produces, per wait kind.

    Kept beside the executor rather than in the engine because it is the
    other half of one vocabulary: the executor decides what a wait *is* and
    this decides what ending it *means*.  Splitting them across two modules is
    how the two drift.

    A timer wait is the one kind whose deadline is not a timeout — firing is
    its declared success — so the expiry branch is per kind, not global.
    """
    kind = step.wait_kind
    if resumption.expired and kind != "timer":
        return "timed_out", None
    if kind == "timer":
        fired_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(resumption.at))
        return "fired", {"fired_at": fired_at}
    if kind == "event":
        return "matched", {
            "event_type": resumption.event_type,
            "payload": dict(resumption.payload),
        }
    if kind == "human":
        decision = resumption.decision
        if decision not in step.outcomes:
            # The gate's vocabulary is closed by the artifact.  An answer
            # outside it is a contract fault, never a new edge: routing on an
            # unlisted string is how an operator invents control flow the
            # graph never displayed.
            return "contract_violation", None
        return decision, {
            "resolution": decision,
            "note": resumption.payload.get("note"),
            "resolved_by": resumption.payload.get("resolved_by"),
        }
    status = resumption.task_status
    outcome = status if status in WAIT_BUSINESS_OUTCOMES["task"] else "failed"
    return outcome, {
        "task_id": resumption.task_id or "",
        "status": status,
        "outcome": resumption.payload.get("outcome"),
    }


class LiveWaitExecutor:
    """Computes the pause; the engine and Package 3 persist it atomically."""

    step_type: ClassVar[str] = "wait"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = False

    async def execute(self, step: WaitStep, ctx: StepContext) -> ExecutorResult:
        operation = f"wait:{step.wait_kind}"
        try:
            awaited = (
                resolve_value(step.awaited, ctx.scope) if step.awaited is not None else None
            )
            correlation = (
                resolve_value(step.correlation_key, ctx.scope)
                if step.correlation_key is not None
                else None
            )
        except ValueResolutionError as exc:
            # Resolved here rather than by the engine's step 4 because a wait
            # declares its correlation as a first-class field, not as
            # ``inputs``; the failure mode is the same reserved outcome.
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="input_resolution_failed",
                operation=operation,
                diagnostics=(exc.reason,),
            )

        now = ctx.services.clock()
        iteration = -1 if ctx.iteration_index is None else ctx.iteration_index
        spec = WaitSpec(
            wait_id=wait_id_for(ctx.run_id, ctx.step_id, iteration, ctx.attempt),
            run_id=ctx.run_id,
            step_id=ctx.step_id,
            iteration=iteration,
            kind=WAIT_KIND_STORAGE[step.wait_kind],
            event_type=str(awaited) if step.wait_kind == "event" and awaited else "",
            match=correlation_match(correlation),
            deadline_at=_deadline(step, ctx, now),
            created_at=now,
        )
        receipt_inputs, receipt_result = project_step_receipt(
            {"wait_kind": step.wait_kind, "awaited": awaited, "correlation_key": correlation},
            {},
            run_id=ctx.run_id,
        )
        return ExecutorResult(
            control=StepControl.SUSPEND,
            outcome=SUSPENDED_OUTCOME,
            wait=spec,
            operation=operation,
            receipt_inputs=receipt_inputs,
            receipt_result=receipt_result,
            # The digest, never the key: an operator needs to see that two
            # runs wait on the same thing without the correlation's contents
            # appearing in a surface Package 5 renders.
            diagnostics=(f"correlation:{spec.correlation_key[:16]}",),
        )


class ReportingWaitExecutor:
    """Dry-run and shadow: report the pause, never persist it.

    A wait's result is by definition external, so guessing an outcome would
    make everything downstream of it fiction.  The node forks symbolically
    across the declared outcomes instead, and nothing is registered.
    """

    step_type: ClassVar[str] = "wait"
    mode: ClassVar[ExecutionMode] = ExecutionMode.DRY_RUN
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: WaitStep, ctx: StepContext) -> ExecutorResult:
        return ExecutorResult(
            control=StepControl.UNRESOLVED,
            outcome="unresolved",
            possible_outcomes=tuple(sorted(step.transitions)),
            operation=f"wait:{step.wait_kind}",
            diagnostics=(UNRESOLVED_REASON,),
        )


def _deadline(step: WaitStep, ctx: StepContext, now: float) -> float | None:
    """The earlier of this wait's timeout and the run's own deadline."""
    candidates = [
        value
        for value in (
            now + step.timeout_seconds if step.timeout_seconds is not None else None,
            ctx.run_deadline_at,
        )
        if value is not None
    ]
    return min(candidates) if candidates else None


__all__ = [
    "SUSPENDED_OUTCOME",
    "UNRESOLVED_REASON",
    "WAIT_KIND_STORAGE",
    "LiveWaitExecutor",
    "ReportingWaitExecutor",
    "WaitResumption",
    "correlation_match",
    "resolve_wait_result",
    "wait_id_for",
]
