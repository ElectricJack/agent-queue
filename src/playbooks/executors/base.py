"""The typed executor protocol and everything an executor may see.

Package 4 child plan §3.1, §3.3.4 and §3.6.  This module is the parallelism
contract: the command, LLM and agent-task executors are written independently
against it, so nothing here may be renegotiated inside one of those tasks.

Two properties are what the rest of the package is built on:

* an executor **returns**, it never calls back into the engine.  There is no
  import edge from an executor module to :mod:`src.playbooks.engine`, so an
  executor structurally cannot skip a durable boundary.
* an executor sees exactly :class:`EngineServices` — no orchestrator, no
  playbook manager, no config object.  A step that needs a value reads it off
  the artifact, which is what makes the artifact the determinism boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from src.playbooks.expressions import ResolutionScope
from src.playbooks.receipts import project_receipt
from src.llm.types import TokenUsage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.commands.authorization import CommandResolver
    from src.commands.contracts.models import ExecutionContract
    from src.commands.contracts.registry import ContractRegistry
    from src.commands.principal import ExecutionPrincipal
    from src.playbooks.artifact_ref import ArtifactRef
    from src.playbooks.artifact_store import ArtifactStore
    from src.playbooks.definition import PlaybookDefinition
    from src.playbooks.run_state import LoopFrame
    from src.playbooks.waits import WaitSpec
    from src.llm.client import LLMToolTurn


class UnknownStepType(KeyError):
    """A step kind with no registered executor.

    Raised rather than treated as the end of the walk: V1's dry-run silently
    ended a path at a shape it did not handle, which answers a different
    question from the one the operator asked (child plan §2.2 item 3).
    """


class ExecutionMode(StrEnum):
    LIVE = "live"
    DRY_RUN = "dry_run"
    SHADOW = "shadow"


class StepControl(StrEnum):
    """What the engine does with an :class:`ExecutorResult`."""

    #: Engine selects ``step.transitions[result.outcome]``.
    ADVANCE = "advance"
    #: Engine jumps to ``result.goto_step_id``, which MUST be one of the
    #: step's statically declared targets (§3.1.3).  Only ``decision`` and
    #: ``foreach`` may use it.
    GOTO = "goto"
    #: Engine persists ``result.wait`` and pauses the run.
    SUSPEND = "suspend"
    #: Engine ends the run with ``result.terminal_outcome``.
    TERMINATE = "terminate"
    #: Engine pauses with reason ``operator_decision_required`` (§4.8).  No
    #: transition is selected and no binding is written.
    OPERATOR_DECISION = "operator_decision"
    #: Dry-run / shadow only: the boundary could not be resolved.  The engine
    #: forks symbolically across every declared outgoing outcome (§4.10).
    UNRESOLVED = "unresolved"


#: The step kinds whose typed contract exposes a runtime-chosen target.  A
#: ``GOTO`` from anything else is a ``contract_violation`` — the mechanical
#: form of "runtime output cannot alter control flow unless the typed step
#: contract explicitly exposes the referenced field" (§3.1.3).
GOTO_CAPABLE_STEP_KINDS: frozenset[str] = frozenset({"decision", "foreach"})


#: §3.6.  Closed set, produced by the engine and by the executors; an outcome
#: outside it that is not declared by the step is a ``contract_violation``.
ENGINE_RESERVED_OUTCOMES: frozenset[str] = frozenset(
    {
        "input_resolution_failed",
        "unavailable",
        "contract_violation",
        "state_limit_exceeded",
        "interrupted",
        "timed_out",
        "cancelled",
        "unauthorized",
        "runtime_error",
        "invalid_output",
        "budget_exceeded",
        "provider_error",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    """One step attempt's whole answer.  Nothing else crosses the boundary."""

    control: StepControl
    #: ALWAYS set.  Either a declared outcome for this step, or a member of
    #: :data:`ENGINE_RESERVED_OUTCOMES`.  Never empty, never ``None``.
    outcome: str
    #: The step's declared output, already validated by the executor against
    #: the contract's result model or ``step.output_schema``.  ``None`` when
    #: the step declares no ``save_result_as``.
    value: Any | None = None
    goto_step_id: str | None = None
    wait: WaitSpec | None = None
    terminal_outcome: str | None = None
    usage: TokenUsage | None = None
    #: The narrowed identity that actually executed the step, when different
    #: from the invoking principal (for example an LLM profile).
    effective_principal: Any | None = None
    idempotency_key: str | None = None
    #: Receipt projections.  ALREADY REDACTED by the executor per §3.3.4 —
    #: the engine writes them verbatim and performs no further redaction.
    receipt_inputs: Mapping[str, Any] = field(default_factory=dict)
    receipt_result: Mapping[str, Any] = field(default_factory=dict)
    #: Short operation descriptor, e.g. ``"command:ensure_task"``.  Never
    #: contains argument values.
    operation: str | None = None
    #: Set by the agent-task executor before it suspends (§4.5).
    child_task_id: str | None = None
    #: Human-readable, non-sensitive notes for the receipt and for dry-run
    #: ``unresolved`` reasons.
    diagnostics: tuple[str, ...] = ()
    #: Dry-run / shadow only: the outcomes a resolved run could have taken.
    possible_outcomes: tuple[str, ...] = ()
    #: The loop frame the engine must persist with this boundary (§4.7).  The
    #: foreach executor is a *pure state transition over the frame*, so it
    #: computes the next frame and hands it back rather than writing it: the
    #: engine is still the only thing that touches durable state.
    loop_frame: LoopFrame | None = None
    #: Drop the run's loop frame at this boundary — the loop is over.  A
    #: separate flag rather than ``loop_frame=None`` because "no change" and
    #: "the loop ended" are different instructions and conflating them would
    #: leave a finished loop's item readable on the continuation path.
    clear_loop: bool = False

    def __post_init__(self) -> None:
        if not self.outcome:
            raise ValueError("an ExecutorResult always carries an outcome")


@dataclass(frozen=True, slots=True)
class EngineServices:
    """Exactly what an executor may reach.  Nothing is added per step kind.

    ``db`` is ``None`` in :attr:`ExecutionMode.SHADOW` and a read-only handle
    in :attr:`ExecutionMode.DRY_RUN` (§3.3.5), which is the structural half of
    the no-side-effect proof.
    """

    contracts: ContractRegistry
    clock: Callable[[], float]
    artifact_store: ArtifactStore | None = None
    llm: Any | None = None
    handler: Any | None = None
    db: Any | None = None
    bus: Any | None = None
    #: Package 0's dispatch-boundary authorization needs both; §2.5 item 11.
    resolver: CommandResolver | None = None
    authorization_mode: str = "audit"


@dataclass(frozen=True, slots=True)
class StepContext:
    """Everything an executor may read, and nothing it may write."""

    run_id: str
    dispatch_id: str
    artifact_ref: ArtifactRef
    artifact: PlaybookDefinition
    rule_id: str
    step_id: str
    principal: ExecutionPrincipal
    scope: ResolutionScope
    services: EngineServices
    mode: ExecutionMode = ExecutionMode.LIVE
    attempt: int = 1
    iteration_index: int | None = None
    run_deadline_at: float | None = None
    step_deadline_at: float | None = None
    cancel_requested: bool = False
    #: Resolved ``step.inputs`` (§3.4 step 4).  The engine resolves them so a
    #: resolution failure is an outcome *before* any executor runs.
    inputs: Mapping[str, Any] = field(default_factory=dict)
    #: The run's active loop frame, or ``None``.  Read by the foreach
    #: executor, which is otherwise stateless; every other executor ignores
    #: it and sees only :attr:`iteration_index`.
    loop_frame: LoopFrame | None = None
    #: Completed tool-turn deltas for this run.  The LLM executor filters by
    #: step/iteration/attempt and reconstructs the provider transcript.
    llm_turns: tuple[Mapping[str, Any], ...] = ()
    #: Engine-owned durable boundary.  Only live tool-enabled LLM execution
    #: receives one; the executor never imports the engine or repository.
    on_tool_turn: Callable[[LLMToolTurn], Awaitable[None]] | None = None


@runtime_checkable
class Executor(Protocol):
    step_type: ClassVar[str]
    mode: ClassVar[ExecutionMode]
    #: Structural marker: every executor registered for SHADOW must declare
    #: True, and the assertion is on the class, not on a call.
    no_side_effects: ClassVar[bool]

    async def execute(self, step: Any, ctx: StepContext) -> ExecutorResult: ...


@runtime_checkable
class Cancellable(Protocol):
    """Optional.  The engine calls this at most once per in-flight step (§4.9)."""

    async def request_cancel(self, step: Any, ctx: StepContext) -> None: ...


def project_step_receipt(
    inputs: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    contract: ExecutionContract[Any, Any] | None = None,
    allowed: Collection[str] | None = None,
    sensitive: Collection[str] = (),
    run_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reduce a step's inputs and result to what a receipt may carry.

    A thin adapter over Package 3's :func:`~src.playbooks.receipts.project_receipt`
    rather than a second implementation (child plan §2.5 item 3): redaction is
    default-deny, and there is exactly one place that decides what "allowed"
    means.  Passing neither *contract* nor *allowed* projects nothing but the
    counts, which is what an executor with no declared projection gets.
    """
    if contract is not None:
        return project_receipt(
            inputs,
            result,
            receipt_projection=contract.receipt_projection,
            sensitive_args=contract.sensitive_args,
            sensitive_result_fields=contract.sensitive_result_fields,
            run_id=run_id,
        )
    return project_receipt(
        inputs,
        result,
        receipt_projection=tuple(allowed or ()),
        sensitive_result_fields=sensitive,
        run_id=run_id,
    )


__all__ = [
    "ENGINE_RESERVED_OUTCOMES",
    "GOTO_CAPABLE_STEP_KINDS",
    "Cancellable",
    "EngineServices",
    "ExecutionMode",
    "Executor",
    "ExecutorResult",
    "StepContext",
    "StepControl",
    "TokenUsage",
    "UnknownStepType",
    "project_step_receipt",
]
