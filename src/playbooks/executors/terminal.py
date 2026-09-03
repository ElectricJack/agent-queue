"""The terminal executor — Package 4 child plan §4.14 and T-5.

Pure and mode-independent, and shared across the three modes for the same
reason the decision executor is: no I/O, so a per-mode copy could only
diverge.  The engine maps :attr:`ExecutorResult.terminal_outcome` onto the
run lifecycle; there is no ``timed_out`` terminal, because a timeout is
something that *happens to* a run rather than something an artifact declares
(child plan §2.5 item 6).
"""

from __future__ import annotations

from typing import ClassVar

from src.playbooks.definition import TerminalStep
from src.playbooks.executors.base import (
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
)
from src.playbooks.expressions import ValueResolutionError, resolve_value


class TerminalExecutor:
    step_type: ClassVar[str] = "terminal"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: TerminalStep, ctx: StepContext) -> ExecutorResult:
        value = None
        if step.result is not None:
            try:
                value = resolve_value(step.result, ctx.scope)
            except ValueResolutionError as exc:
                return ExecutorResult(
                    control=StepControl.ADVANCE,
                    outcome="input_resolution_failed",
                    operation=f"terminal:{step.outcome}",
                    diagnostics=(exc.reason,),
                )
        return ExecutorResult(
            control=StepControl.TERMINATE,
            outcome=step.outcome,
            terminal_outcome=step.outcome,
            value=value,
            operation=f"terminal:{step.outcome}",
        )


__all__ = ["TerminalExecutor"]
