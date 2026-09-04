"""Contract doubles for the Package 4 engine and executor suites (§6.3).

The engine tests must not depend on Package 1's *real* ``ensure_task``
behaviour: a Package 1 change would then break Package 4's suite for reasons
that have nothing to do with the engine.  These are real
:class:`CommandContract` objects over toy models, registered into a fresh
:class:`ContractRegistry` per test — Package 1's ``register`` refuses
replacement, so the module singleton is never mutated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration, ContractRegistry

# --------------------------------------------------------------------------
# ensure_task — the keyed, retry-safe create
# --------------------------------------------------------------------------


class EnsureTaskArgs(CommandArgs):
    project_id: str
    title: str
    dedup_key: str | None = None


class EnsureTaskResult(CommandValue):
    task_id: str
    created: bool


ENSURE_TASK = CommandContract(
    execution=ExecutionContract(
        name="ensure_task",
        args_model=EnsureTaskArgs,
        result_model=EnsureTaskResult,
        outcomes=(
            OutcomeSpec(name="created", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="reused", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
        ),
        capability="ensure_task",
        side_effect=SideEffectClass.CREATE,
        idempotency=IdempotencySpec(mode="keyed", key_field="dedup_key"),
        retry_safe=True,
        receipt_projection=("task_id", "created"),
    ),
    presentation=CommandPresentation(
        title="Ensure a review task exists",
        summary="Create or reuse the matching task",
    ),
)


# --------------------------------------------------------------------------
# list_tasks — naturally idempotent, one projected field
# --------------------------------------------------------------------------


class ListTasksArgs(CommandArgs):
    project_id: str
    status: str | None = None


class ListTasksResult(CommandValue):
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0


LIST_TASKS = CommandContract(
    execution=ExecutionContract(
        name="list_tasks",
        args_model=ListTasksArgs,
        result_model=ListTasksResult,
        outcomes=(OutcomeSpec(name="listed", classification=OutcomeClass.SUCCESS),),
        capability="list_tasks",
        side_effect=SideEffectClass.READ,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        receipt_projection=("count",),
        supports_preview=True,
    ),
    presentation=CommandPresentation(title="List tasks", summary="Read the queue"),
)


# --------------------------------------------------------------------------
# gate_create — the NON-retry-safe case T-10 needs, and the empty projection
# --------------------------------------------------------------------------


class GateCreateArgs(CommandArgs):
    task_id: str
    title: str


class GateCreateResult(CommandValue):
    gate_id: str
    approved: bool = False
    secret_note: str = ""


GATE_CREATE = CommandContract(
    execution=ExecutionContract(
        name="gate_create",
        args_model=GateCreateArgs,
        result_model=GateCreateResult,
        outcomes=(
            OutcomeSpec(name="created", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="reused", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="skipped", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
        ),
        capability="gate_create",
        side_effect=SideEffectClass.CREATE,
        idempotency=IdempotencySpec(mode="none"),
        retry_safe=False,
        sensitive_result_fields=frozenset({"secret_note"}),
        receipt_projection=("gate_id", "secret_note"),
    ),
    presentation=CommandPresentation(title="Open a gate", summary="Create a review gate"),
)


# --------------------------------------------------------------------------
# two_failures — §6.1's addition: two FAILURE outcomes on different edges
# --------------------------------------------------------------------------


class TwoFailuresArgs(CommandArgs):
    project_id: str


class TwoFailuresResult(CommandValue):
    detail: str = ""


TWO_FAILURES = CommandContract(
    execution=ExecutionContract(
        name="two_failures",
        args_model=TwoFailuresArgs,
        result_model=TwoFailuresResult,
        outcomes=(
            OutcomeSpec(name="ok", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="not_found", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="conflict", classification=OutcomeClass.FAILURE),
        ),
        capability="two_failures",
        side_effect=SideEffectClass.READ,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        receipt_projection=("detail",),
    ),
    presentation=CommandPresentation(title="Two failures", summary="Routing probe"),
)


# --------------------------------------------------------------------------
# no_projection — receipt_projection=() means nothing is projected (T-4)
# --------------------------------------------------------------------------


class NoProjectionArgs(CommandArgs):
    project_id: str


class NoProjectionResult(CommandValue):
    populated: str = "value"


NO_PROJECTION = CommandContract(
    execution=ExecutionContract(
        name="no_projection",
        args_model=NoProjectionArgs,
        result_model=NoProjectionResult,
        outcomes=(OutcomeSpec(name="done", classification=OutcomeClass.SUCCESS),),
        capability="no_projection",
        side_effect=SideEffectClass.READ,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
    ),
    presentation=CommandPresentation(title="No projection", summary="Projects nothing"),
)


CONTRACT_DOUBLES: tuple[CommandContract[Any, Any], ...] = (
    ENSURE_TASK,
    LIST_TASKS,
    GATE_CREATE,
    TWO_FAILURES,
    NO_PROJECTION,
)


class ScriptedAdapter:
    """A queued-result ``invoke`` that records every call.

    One recording surface for the whole package: "the adapter was not called"
    and "these two outcomes took different edges" are both assertions about
    :attr:`calls`, so no test needs to build its own spy.

    Results come from two places.  :attr:`queue` is one FIFO shared by every
    command, which is the right script when the test controls the call order
    (one rule, or a sequential loop).  :meth:`script` queues results *per
    command name* and is consulted first; use it whenever the engine may
    invoke two commands concurrently — ``dispatch_event`` gathers one
    ``run_rule`` per matching rule, and against a real database each rule
    suspends on I/O before it reaches the adapter, so which command asks
    first is whichever connection answers first.  A shared FIFO would then
    hand a rule its sibling's result and route it to a ``failed`` terminal
    with no error, which is exactly the intermittent CI failure of
    ``test_a_dispatch_persists_one_run_and_its_receipts_per_rule[postgres]``.
    """

    def __init__(self, results: Sequence[Any] = (), *, preview: Sequence[Any] = ()) -> None:
        self.queue = list(results)
        self.preview_queue = list(preview)
        self.scripts: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, Any, Any]] = []
        self.preview_calls: list[tuple[str, Any, Any]] = []

    def script(self, name: str, *results: Any) -> None:
        """Queue *results* for command *name* alone, ahead of the shared FIFO."""
        self.scripts.setdefault(name, []).extend(results)

    def _next(self, name: str, queue: list[Any], args: Any, principal: Any) -> Any:
        named = self.scripts.get(name)
        if named and queue is self.queue:
            queue = named
        if not queue:
            raise AssertionError(f"{name} was called more times than the script allows")
        scripted = queue.pop(0)
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted) and not isinstance(scripted, CommandResult):
            return scripted(args, principal)
        return scripted

    def invoke_for(self, name: str):
        async def _invoke(args: Any, principal: Any) -> Any:
            self.calls.append((name, args, principal))
            return self._next(name, self.queue, args, principal)

        return _invoke

    def preview_for(self, name: str):
        async def _preview(args: Any, principal: Any) -> Any:
            self.preview_calls.append((name, args, principal))
            return self._next(name, self.preview_queue, args, principal)

        return _preview

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def args_for(self, name: str) -> list[Any]:
        return [args for called, args, _ in self.calls if called == name]


def registry_with(
    *contracts: CommandContract[Any, Any],
    adapter: ScriptedAdapter | None = None,
) -> tuple[ContractRegistry, ScriptedAdapter]:
    """A fresh registry carrying *contracts*, all wired to one adapter.

    A preview adapter is wired exactly for the contracts that declare
    ``supports_preview`` — Package 1's ``register`` rejects any other pairing,
    which is what makes "dry-run previewed a command that says it cannot be
    previewed" unrepresentable rather than merely untested.
    """
    adapter = adapter or ScriptedAdapter()
    registry = ContractRegistry()
    for contract in contracts or CONTRACT_DOUBLES:
        name = contract.execution.name
        registry.register(
            CommandRegistration(
                name=name,
                contract=contract,
                invoke=adapter.invoke_for(name),
                preview=(
                    adapter.preview_for(name)
                    if contract.execution.supports_preview
                    else None
                ),
            )
        )
    return registry, adapter
