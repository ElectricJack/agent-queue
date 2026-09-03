"""The command step executors — Package 4 child plan §3.2, §4.3 and T-2.

`_consume` is the **only** place in ``src/playbooks/`` that reads a
``CommandResult``.  That is a deliberate concentration: V1 inferred success
from the shape of a handler dict (``pipeline_runner.py:145``), so a command
returning ``{}`` read as a success and one returning ``{"error": None}`` read
as a failure.  With one consumer, "how is a command result interpreted?" has
exactly one answer, and the six ordered checks below are that answer.

Three executors, one per mode.  Selection is by mode, not by a branch inside
one class, so a mode can never leak a live invocation: ``ShadowCommandExecutor``
has no code path to ``registration.invoke`` at all.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import CommandRegistration, UnknownContract
from src.playbooks.definition import CommandStep
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
    project_step_receipt,
)
from src.playbooks.receipts import idempotency_key
from src.playbooks.run_state import DEFAULT_MAX_RESULT_BYTES, canonical_json


def _attempt_key(ctx: StepContext) -> str:
    """Package 3's four-part attempt identity (child plan §2.5 item 2).

    The iteration is part of it because a three-part key collides across
    iterations of the same step inside a loop, which would suppress the
    second iteration's side effect as a duplicate.
    """
    iteration = -1 if ctx.iteration_index is None else ctx.iteration_index
    return idempotency_key(ctx.run_id, ctx.step_id, iteration, ctx.attempt)


def _fail(
    outcome: str,
    *,
    operation: str,
    diagnostics: tuple[str, ...] = (),
    key: str | None = None,
) -> ExecutorResult:
    return ExecutorResult(
        control=StepControl.ADVANCE,
        outcome=outcome,
        operation=operation,
        diagnostics=diagnostics,
        idempotency_key=key,
    )


def _declared_outcomes(registration: CommandRegistration) -> frozenset[str]:
    return frozenset(spec.name for spec in registration.contract.execution.outcomes)


def _consume(
    result: Any,
    registration: CommandRegistration,
    step: CommandStep,
    ctx: StepContext,
    *,
    resolved_inputs: dict[str, Any],
    key: str,
) -> ExecutorResult:
    """§3.2's six checks, in order.  No step may be reordered.

    Each has its own failure mode, and the distinction between them is what
    an operator reads off the receipt: ``contract_violation`` means the
    *contract* was breached, ``input_resolution_failed`` means a *value* was
    wrong, ``state_limit_exceeded`` means the run refused to grow.
    """
    execution = registration.contract.execution
    operation = f"command:{step.command}"

    # 1. Type.  Package 1's adapter is the only caller of CommandHandler,
    #    so a bare dict cannot normally arrive — but the failure is silent if
    #    it does, which is exactly why it is checked rather than assumed.
    if not isinstance(result, CommandResult):
        return _fail(
            "contract_violation",
            operation=operation,
            diagnostics=(f"adapter returned {type(result).__name__}, not CommandResult",),
            key=key,
        )

    # 2. Outcome membership.  An unknown outcome is a contract fault, never a
    #    transient one, so it is never softened to runtime_error.
    if result.outcome not in _declared_outcomes(registration) | RESERVED_PASS_THROUGH:
        return _fail(
            "contract_violation",
            operation=operation,
            diagnostics=(f"outcome {result.outcome!r} is not declared by the contract",),
            key=key,
        )

    # 3. Result-model conformance.  ``extra="forbid"`` already rejects an
    #    unknown field at construction; the round-trip catches a
    #    ``model_construct`` bypass that skipped validation entirely.
    if type(result.value) is not execution.result_model:
        return _fail(
            "contract_violation",
            operation=operation,
            diagnostics=(f"result is {type(result.value).__name__}, not {execution.result_model.__name__}",),
            key=key,
        )
    try:
        dumped = result.value.model_dump()
        execution.result_model.model_validate(dumped)
    except ValidationError:
        return _fail(
            "contract_violation",
            operation=operation,
            diagnostics=("result does not round-trip through its declared model",),
            key=key,
        )

    # 4. Transition coverage.  Package 2 validates this statically; it is
    #    re-checked because an artifact can be pinned across a contract
    #    change, and a business outcome with no edge must fail loudly rather
    #    than end the run as completed (the replacement for
    #    ``pipeline_runner.py:151-158``).
    if result.outcome not in step.transitions and "runtime_error" not in step.transitions:
        return _fail(
            "contract_violation",
            operation=operation,
            diagnostics=(f"outcome {result.outcome!r} has no transition and there is no runtime_error edge",),
            key=key,
        )

    # 5. Binding.  The bound object is the *declared result model*, never the
    #    handler's dict, so a receipt projection stays meaningful.
    value = dumped if step.save_result_as else None

    # 6. Size.  Rejected, never truncated — a truncated binding is a wrong
    #    answer that looks like a right one.
    if value is not None and len(canonical_json(value)) > DEFAULT_MAX_RESULT_BYTES:
        return _fail(
            "state_limit_exceeded",
            operation=operation,
            diagnostics=("bound result exceeds the per-result byte limit",),
            key=key,
        )

    receipt_inputs, receipt_result = project_step_receipt(
        resolved_inputs, dumped, contract=execution, run_id=ctx.run_id
    )
    return ExecutorResult(
        control=StepControl.ADVANCE,
        outcome=result.outcome,
        value=value,
        idempotency_key=key,
        receipt_inputs=receipt_inputs,
        receipt_result=receipt_result,
        operation=operation,
    )


#: Package 1's adapter-produced reserved outcomes.  They pass through
#: unchanged — the engine never rewrites one into a different reserved
#: outcome (§3.2's reserved-outcome mapping).
RESERVED_PASS_THROUGH: frozenset[str] = frozenset(
    {"contract_violation", "unauthorized", "runtime_error"}
)


def _lookup(step: CommandStep, services: EngineServices) -> CommandRegistration | None:
    try:
        return services.contracts.require(step.command)
    except (UnknownContract, KeyError):
        return None


def _build_args(
    registration: CommandRegistration,
    ctx: StepContext,
    *,
    key: str | None,
) -> tuple[BaseModel | None, dict[str, Any], str | None]:
    """Validate the resolved inputs against the contract's argument model.

    The compiler validated the *types of the references*; this validates the
    *resolved values*, which the compiler cannot see.  A failure here is
    ``input_resolution_failed`` — the value was wrong — and never
    ``contract_violation``, because the contract itself was fine.
    """
    execution = registration.contract.execution
    payload = dict(ctx.inputs)
    if key is not None and execution.idempotency.mode == "keyed":
        payload[execution.idempotency.key_field or "idempotency_key"] = key
    try:
        return execution.args_model(**payload), payload, None
    except ValidationError as exc:
        return None, payload, f"{exc.error_count()} argument(s) failed validation"


class LiveCommandExecutor:
    """Invokes the contracted command for real."""

    step_type: ClassVar[str] = "command"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = False

    async def execute(self, step: CommandStep, ctx: StepContext) -> ExecutorResult:
        operation = f"command:{step.command}"
        key = _attempt_key(ctx)
        registration = _lookup(step, ctx.services)
        if registration is None:
            # The runtime half of "a CommandStep can reference only a
            # contracted command".
            return _fail(
                "contract_violation",
                operation=operation,
                diagnostics=(f"{step.command} has no contract",),
                key=key,
            )

        args, resolved, error = _build_args(registration, ctx, key=key)
        if args is None:
            return _fail(
                "input_resolution_failed", operation=operation, diagnostics=(error or "",), key=key
            )

        timeout = registration.contract.execution.timeout_seconds
        try:
            if timeout:
                async with asyncio.timeout(timeout):
                    result = await registration.invoke(args, ctx.principal)
            else:
                result = await registration.invoke(args, ctx.principal)
        except TimeoutError:
            return _fail("timed_out", operation=operation, diagnostics=("step",), key=key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - §3.4 step 6: any executor
            # exception becomes runtime_error rather than escaping the boundary
            # The exception *type*, never its message: a message can carry an
            # argument value, and a receipt is operator-visible.
            return _fail(
                "runtime_error", operation=operation, diagnostics=(type(exc).__name__,), key=key
            )

        return _consume(result, registration, step, ctx, resolved_inputs=resolved, key=key)


class PreviewCommandExecutor:
    """Dry-run: the contract's preview adapter, or nothing at all.

    A contract without ``supports_preview`` has no simulation the engine may
    trust, so the answer is ``UNRESOLVED`` and the dry-run forks across the
    step's declared outcomes rather than inventing one.
    """

    step_type: ClassVar[str] = "command"
    mode: ClassVar[ExecutionMode] = ExecutionMode.DRY_RUN
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: CommandStep, ctx: StepContext) -> ExecutorResult:
        operation = f"command:{step.command}"
        key = _attempt_key(ctx)
        registration = _lookup(step, ctx.services)
        if registration is None:
            return _fail(
                "contract_violation",
                operation=operation,
                diagnostics=(f"{step.command} has no contract",),
                key=key,
            )
        if not registration.contract.execution.supports_preview or registration.preview is None:
            return _unresolved(step, registration, operation, "no preview adapter")

        args, resolved, error = _build_args(registration, ctx, key=key)
        if args is None:
            return _fail(
                "input_resolution_failed", operation=operation, diagnostics=(error or "",), key=key
            )
        try:
            result = await registration.preview(args, ctx.principal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - §3.4 step 6: any executor
            # exception becomes runtime_error rather than escaping the boundary
            return _fail(
                "runtime_error", operation=operation, diagnostics=(type(exc).__name__,), key=key
            )
        return _consume(result, registration, step, ctx, resolved_inputs=resolved, key=key)


class ShadowCommandExecutor:
    """Shadow: records the intended call and invokes nothing.

    Not even the preview adapter — a preview may read a database snapshot,
    and shadow mode runs against production.  What it records is what
    Package 6's parity harness compares.
    """

    step_type: ClassVar[str] = "command"
    mode: ClassVar[ExecutionMode] = ExecutionMode.SHADOW
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: CommandStep, ctx: StepContext) -> ExecutorResult:
        operation = f"command:{step.command}"
        registration = _lookup(step, ctx.services)
        if registration is None:
            return _fail(
                "contract_violation",
                operation=operation,
                diagnostics=(f"{step.command} has no contract",),
            )
        args, _resolved, error = _build_args(registration, ctx, key=None)
        if args is None:
            return _fail(
                "input_resolution_failed", operation=operation, diagnostics=(error or "",)
            )
        unresolved = _unresolved(step, registration, operation, "shadow mode invokes nothing")
        return ExecutorResult(
            control=unresolved.control,
            outcome=unresolved.outcome,
            operation=unresolved.operation,
            diagnostics=unresolved.diagnostics,
            possible_outcomes=unresolved.possible_outcomes,
            recorded_command_args=args.model_dump(mode="json"),
        )


def _unresolved(
    step: CommandStep,
    registration: CommandRegistration,
    operation: str,
    reason: str,
) -> ExecutorResult:
    """A boundary the mode cannot resolve, with the fork it implies."""
    declared = _declared_outcomes(registration)
    possible = tuple(sorted(name for name in step.transitions if name in declared))
    return ExecutorResult(
        control=StepControl.UNRESOLVED,
        outcome="unavailable",
        operation=operation,
        diagnostics=(reason,),
        possible_outcomes=possible,
    )


__all__ = [
    "LiveCommandExecutor",
    "PreviewCommandExecutor",
    "ShadowCommandExecutor",
]
