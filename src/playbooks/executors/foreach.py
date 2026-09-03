"""Sequential loops — Package 4 child plan §4.7 and T-7.

One active iteration, nesting rejected at compile time, so the loop frame is
finite and inspectable.  The executor is a **pure state transition over the
frame**: it reads the frame off the context, computes the next one, and hands
it back for the engine to persist.  It writes nothing, which is what keeps
"the frame is committed on both sides of every body transition" a property of
the engine's single boundary rather than a convention two modules share.

Two things the live ``pipeline_runner._run_for_each`` gets wrong and this
does not:

* **The loop item is not a binding.**  V1 wrote the item into the same dict
  as step outputs and popped it in a ``finally``, so a step output named
  ``task`` and a loop item named ``task`` silently collided and a failure
  branch could read a stale item.  Here the item lives in ``scope.loop``,
  a namespace a ``BindingRef`` cannot reach at all.
* **The iteration's outcome is not read off a name.**  A body ends at whatever
  step transitions back into the loop node, and in the golden fixture that is
  a ``DecisionStep`` whose outcome is a case label — not a success or failure
  word.  The engine classifies the returning edge against the *producing
  step's contract* and records the verdict on the frame; this module reads
  ``frame.last_failed`` and never re-derives it.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, ClassVar

from src.playbooks.definition import ForEachStep
from src.playbooks.executors.base import (
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
)
from src.playbooks.expressions import ValueResolutionError, resolve_value
from src.playbooks.run_state import LoopFrame, canonical_json

#: The outcome of a boundary that enters or re-enters the body.  It is not a
#: declared business outcome — ``business_outcomes`` for a foreach is
#: ``{completed, failed}`` — because it selects no edge: the body entry is a
#: statically declared target the step exposes, so the control is ``GOTO``.
ITERATING_OUTCOME = "iterating"


def collection_digest(collection: list[Any]) -> str:
    """Pins the collection a frame started on.

    A loop resumed against a different collection would silently re-index:
    item 3 of the new list is not item 3 of the old one, and the aggregate
    would claim it was.  The digest turns that into a contract violation.
    """
    return "sha256:" + hashlib.sha256(canonical_json(collection)).hexdigest()


def aggregate(items: tuple[Any, ...], total: int) -> dict[str, Any]:
    """The bound result, in Package 2's ``FOREACH_RESULT_SCHEMA`` shape.

    The child plan's §4.7 prose gives ``{items, outcomes, errors}``, but the
    binding type-checker walks ``FOREACH_RESULT_SCHEMA``
    (``{total, succeeded, failed, items}``), so an artifact binding a loop
    result type-checks against *that* and this must produce it.
    """
    entries = [dict(entry) for entry in items]
    failed = sum(1 for entry in entries if entry.get("error") is not None)
    return {
        "total": total,
        "succeeded": len(entries) - failed,
        "failed": failed,
        "items": entries,
    }


class ForEachExecutor:
    """Pure, and therefore the *same instance* in live, dry-run and shadow."""

    step_type: ClassVar[str] = "foreach"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: ForEachStep, ctx: StepContext) -> ExecutorResult:
        operation = f"foreach:{ctx.step_id}"
        frame = ctx.loop_frame
        if frame is not None and frame.step_id != ctx.step_id:
            # Package 2 rejects a *nested* loop statically.  A second loop
            # reached while another frame is live is the dynamic form of the
            # same thing — two frames, one snapshot field — and V2 has no
            # parallel loops either, so it stops here rather than overwriting
            # the frame it cannot see.
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="contract_violation",
                operation=operation,
                diagnostics=(f"loop {frame.step_id} is already active",),
            )

        try:
            collection = resolve_value(step.collection, ctx.scope)
        except ValueResolutionError as exc:
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="input_resolution_failed",
                operation=operation,
                diagnostics=(exc.reason,),
            )
        if not isinstance(collection, list):
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="input_resolution_failed",
                operation=operation,
                diagnostics=(
                    f"collection resolved to {type(collection).__name__}, not a list",
                ),
            )

        digest = collection_digest(collection)
        total = len(collection)
        if frame is None:
            return self._enter(step, ctx, total, digest, operation)
        if frame.collection_digest != digest:
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="contract_violation",
                operation=operation,
                diagnostics=("the collection changed under an active loop",),
            )
        return self._advance(step, frame, total, operation)

    # ------------------------------------------------------------------

    def _enter(
        self, step: ForEachStep, ctx: StepContext, total: int, digest: str, operation: str
    ) -> ExecutorResult:
        if total > step.max_iterations:
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="state_limit_exceeded",
                operation=operation,
                diagnostics=(f"{total} items exceeds max_iterations={step.max_iterations}",),
            )
        if total == 0:
            # Straight to the continuation with an empty aggregate: an empty
            # collection is a completed loop, not a skipped step, so the
            # binding downstream steps read exists and is well-formed.
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="completed",
                value=aggregate((), 0),
                clear_loop=True,
                operation=operation,
                diagnostics=("empty collection",),
            )
        return ExecutorResult(
            control=StepControl.GOTO,
            outcome=ITERATING_OUTCOME,
            goto_step_id=step.body_entry,
            loop_frame=LoopFrame(
                step_id=ctx.step_id,
                item_binding=step.item_binding,
                collection_digest=digest,
                index=0,
                total=total,
            ),
            operation=operation,
        )

    def _advance(
        self, step: ForEachStep, frame: LoopFrame, total: int, operation: str
    ) -> ExecutorResult:
        failed = bool(frame.last_failed)
        entry: dict[str, Any] = {
            "index": frame.index,
            "outcome": frame.last_outcome or "",
            "value": None,
            "error": None,
        }
        if failed:
            if step.failure_policy == "halt":
                # The whole loop fails, and the aggregate still reports every
                # iteration that did run — a halted loop that reported nothing
                # would make the partial side effects invisible.
                entry["error"] = frame.last_outcome
                return ExecutorResult(
                    control=StepControl.ADVANCE,
                    outcome="failed",
                    value=aggregate(frame.partial + (entry,), total),
                    clear_loop=True,
                    operation=operation,
                    diagnostics=(f"iteration {frame.index} failed: {frame.last_outcome}",),
                )
            if step.failure_policy == "collect":
                entry["error"] = frame.last_outcome

        partial = frame.partial + (entry,)
        next_index = frame.index + 1
        if next_index < total:
            return ExecutorResult(
                control=StepControl.GOTO,
                outcome=ITERATING_OUTCOME,
                goto_step_id=step.body_entry,
                loop_frame=replace(
                    frame,
                    index=next_index,
                    partial=partial,
                    last_step_id=None,
                    last_outcome=None,
                    last_failed=None,
                ),
                operation=operation,
            )
        # ``collect`` ends ``completed`` with a populated error list; only
        # ``halt`` routes a per-item failure onto the loop's failed edge.
        return ExecutorResult(
            control=StepControl.ADVANCE,
            outcome="completed",
            value=aggregate(partial, total),
            clear_loop=True,
            operation=operation,
        )


__all__ = ["ITERATING_OUTCOME", "ForEachExecutor", "aggregate", "collection_digest"]
