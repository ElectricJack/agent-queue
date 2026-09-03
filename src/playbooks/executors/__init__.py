"""Executor selection — Package 4 child plan §3.1.2.

Per the spec, "mode selects executor implementations; it does not select a
different graph or validator".  That is why this is a table rather than a
branch inside each executor: a mode cannot leak a live invocation into
dry-run, because the dry-run table simply does not hold the live class.

The deterministic three — decision, foreach and terminal — are registered as
the **same object** in all three modes.  They contain no I/O, so a per-mode
copy could only diverge, and object identity is what makes a future refactor
that clones them fail a test rather than pass silently.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.playbooks.executors.base import (
    Cancellable,
    EngineServices,
    ExecutionMode,
    Executor,
    ExecutorResult,
    StepContext,
    StepControl,
    TokenUsage,
    UnknownStepType,
)
from src.playbooks.executors.command import (
    LiveCommandExecutor,
    PreviewCommandExecutor,
    ShadowCommandExecutor,
)
from src.playbooks.executors.decision import DecisionExecutor
from src.playbooks.executors.llm import LiveLlmExecutor, SymbolicLlmExecutor
from src.playbooks.executors.terminal import TerminalExecutor

#: One shared instance per deterministic step kind (§3.1.2).
DECISION_EXECUTOR = DecisionExecutor()
TERMINAL_EXECUTOR = TerminalExecutor()

EXECUTORS: Mapping[ExecutionMode, Mapping[str, Executor]] = {
    ExecutionMode.LIVE: {
        "command": LiveCommandExecutor(),
        "llm": LiveLlmExecutor(),
        "decision": DECISION_EXECUTOR,
        "terminal": TERMINAL_EXECUTOR,
    },
    ExecutionMode.DRY_RUN: {
        "command": PreviewCommandExecutor(),
        "llm": SymbolicLlmExecutor(),
        "decision": DECISION_EXECUTOR,
        "terminal": TERMINAL_EXECUTOR,
    },
    ExecutionMode.SHADOW: {
        "command": ShadowCommandExecutor(),
        "llm": SymbolicLlmExecutor(),
        "decision": DECISION_EXECUTOR,
        "terminal": TERMINAL_EXECUTOR,
    },
}


def executor_for(step_type: str, mode: ExecutionMode) -> Executor:
    """The executor for *step_type* in *mode*.

    Raises rather than returning ``None``: an unregistered step kind must
    stop the walk loudly, not end it quietly, which is the behaviour V1's
    dry-run got wrong (§2.2 item 3).
    """
    table = EXECUTORS[ExecutionMode(mode)]
    try:
        return table[step_type]
    except KeyError:
        raise UnknownStepType(
            f"no {mode} executor for step type {step_type!r}"
        ) from None


__all__ = [
    "EXECUTORS",
    "Cancellable",
    "DecisionExecutor",
    "EngineServices",
    "ExecutionMode",
    "Executor",
    "ExecutorResult",
    "LiveCommandExecutor",
    "LiveLlmExecutor",
    "PreviewCommandExecutor",
    "ShadowCommandExecutor",
    "StepContext",
    "StepControl",
    "SymbolicLlmExecutor",
    "TerminalExecutor",
    "TokenUsage",
    "UnknownStepType",
    "executor_for",
]
