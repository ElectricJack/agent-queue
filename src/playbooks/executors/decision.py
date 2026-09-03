"""The decision executor — Package 4 child plan §4.14 and T-5.

Pure and mode-independent: the *same instance* serves live, dry-run and
shadow, because a decision contains no I/O and a separate dry-run copy could
only diverge from the live one.  There is no code path from here to
``ctx.services.llm``; the artifact chooses the edge, not a model.
"""

from __future__ import annotations

from typing import ClassVar

from src.playbooks.definition import DecisionStep
from src.playbooks.executors.base import (
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
)
from src.playbooks.expressions import ValueResolutionError, evaluate_condition


class DecisionExecutor:
    """First true case wins; otherwise the declared default.

    ``default`` is required by Package 2's model, so there is no
    fall-through that is neither a displayed edge nor an error.
    """

    step_type: ClassVar[str] = "decision"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: DecisionStep, ctx: StepContext) -> ExecutorResult:
        for index, case in enumerate(step.cases):
            try:
                matched = evaluate_condition(case.when, ctx.scope)
            except ValueResolutionError as exc:
                # A type error the compiler could not see.  It is an outcome,
                # not an exception, so the artifact can route it.
                return ExecutorResult(
                    control=StepControl.ADVANCE,
                    outcome="input_resolution_failed",
                    operation=f"decision:{ctx.step_id}",
                    diagnostics=(f"case {index}: {exc.reason}",),
                )
            if matched:
                return ExecutorResult(
                    control=StepControl.GOTO,
                    outcome="matched",
                    goto_step_id=case.goto,
                    operation=f"decision:{ctx.step_id}",
                    diagnostics=(case.label or f"case {index}",),
                )
        return ExecutorResult(
            control=StepControl.GOTO,
            outcome="default",
            goto_step_id=step.default,
            operation=f"decision:{ctx.step_id}",
            diagnostics=("default",),
        )


__all__ = ["DecisionExecutor"]
