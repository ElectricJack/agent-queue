"""The agent-task step executors — Package 4 child plan §4.5, §7.2, §7.4, T-8.

An ``AgentTaskStep`` is a separate step kind from ``LlmStep`` because it
schedules, persists, waits, costs and cancels differently.  Three properties
this module is responsible for, each of which is a security property rather
than a convenience:

* **Delegation narrows three ways and cannot widen.**  The child principal is
  ``parent ∩ child_profile ∩ step.capability_narrowing``.
  :meth:`~src.profiles.capabilities.CapabilityPolicy.intersect` is the only
  transform used, and the type exposes no union, so "the child got a
  capability the parent lacked" is unrepresentable rather than merely
  untested (§7.2).
* **Child identity is durable before the run is paused.**  The executor
  returns ``SUSPEND`` carrying ``child_task_id`` and a ``WaitSpec``; the
  engine writes the snapshot, the receipt and the wait registration in one
  boundary.  A crash after the child exists but before the boundary leaves an
  orphan the operator can see, never a paused run that has forgotten what it
  is waiting for (§4.5 step 3).
* **Cancellation grants nothing.**  ``cancel_child`` defaults to ``False`` and
  the cancel path dispatches with the *narrowed child* principal, so a parent
  whose authority has since shrunk cannot use cancellation to act on a child
  it could no longer create (§7.4).

Two live-tree deviations from the plan, both recorded in the child plan §2.1:

1. Package 2 shipped ``AgentTaskStep`` without ``capability_narrowing``.
   T-8 added it to ``src/playbooks/definition.py`` as an additive optional
   field, because the roadmap's "intersection of parent, child profile and
   explicit per-step narrowing" is not implementable without it.
2. When the child plan was written there was no *contracted*
   task-cancellation command.  ``src/commands/contracts/builtin.py`` now
   registers and adapts ``stop_task`` (outcomes ``stopped`` /
   ``not_running`` / ``rejected``), so ``cancel_child`` dispatches
   :data:`CHILD_CANCEL_COMMAND` through the contract registry on the normal
   path.  The ``UnknownContract`` branch is kept as a fail-safe for a
   registry that does not carry it: the child is left running with a
   diagnostic, which is the fail-safe direction and what
   ``cancel_child=False`` — the default — does anyway.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import ValidationError

from src.commands.contracts.models import CommandResult, OutcomeClass
from src.commands.contracts.registry import CommandRegistration, UnknownContract
from src.commands.principal import ExecutionPrincipal, PrincipalKind, check_delegation
from src.playbooks.definition import AgentTaskStep, CapabilityNarrowing
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
    project_step_receipt,
)
from src.playbooks.expressions import ValueResolutionError, resolve_value
from src.playbooks.receipts import idempotency_key
from src.playbooks.waits import WaitSpec
from src.profiles.capabilities import (
    NAMESPACES,
    CapabilityPolicy,
    capability_policy_for,
)

#: The command that creates the child task.  Package 1 contracts it; a
#: ``CommandStep`` could name it too, which is the point — an agent task is
#: created through the same authorized, receipted path as any other write.
CHILD_CREATE_COMMAND = "create_task"

#: The command that stops the child when ``cancel_child`` is set.  Not
#: contracted in the live tree; see the module docstring.
CHILD_CANCEL_COMMAND = "stop_task"

#: The business outcomes an ``AgentTaskStep`` may declare (spec, "AgentTaskStep").
AGENT_TASK_OUTCOMES: frozenset[str] = frozenset(
    {"dispatched", "completed", "failed", "timed_out", "cancelled"}
)

#: The outcome the executor reports for the boundary that *opens* the wait.
#: It is not a transition: a suspended step has not chosen an edge yet.
AWAITING_OUTCOME = "awaiting_child"

#: Child task status → the step outcome it reconciles to (§4.5 step 5).
#: Deliberately exhaustive rather than defaulting: an unrecognised status
#: becomes ``runtime_error``, because guessing ``failed`` would report a
#: definite result the engine does not actually have.
CHILD_STATUS_OUTCOMES: Mapping[str, str] = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "blocked": "failed",
}


def child_outcome_for_status(status: str) -> str:
    """Map a child task's terminal status onto this step's outcome."""
    return CHILD_STATUS_OUTCOMES.get((status or "").strip().lower(), "runtime_error")


# --------------------------------------------------------------------------
# §4.5 step 1 / §7.2 — the three-way narrowing
# --------------------------------------------------------------------------


def narrowing_policy(narrowing: CapabilityNarrowing | None, base: CapabilityPolicy) -> CapabilityPolicy:
    """*base*, restricted by every namespace *narrowing* actually names.

    ``None`` in a namespace is the identity of intersection — the step said
    nothing about it — while an explicitly empty list means *none*.  Keeping
    those apart is what lets a step say "this child gets no AQ commands"
    without also having to re-list the harness tools it does keep.
    """
    if narrowing is None:
        return base
    values: dict[str, frozenset[str]] = {}
    for namespace in NAMESPACES:
        declared = getattr(narrowing, namespace, None)
        current: frozenset[str] = getattr(base, namespace)
        values[namespace] = current if declared is None else current & frozenset(declared)
    return CapabilityPolicy(
        **values, derived_from_legacy=base.derived_from_legacy
    )


def child_policy(
    parent: CapabilityPolicy,
    profile: CapabilityPolicy,
    narrowing: CapabilityNarrowing | None,
) -> CapabilityPolicy:
    """``parent ∩ profile ∩ narrowing`` — the roadmap's delegation rule."""
    return narrowing_policy(narrowing, parent.intersect(profile))


def _parent_is_ai(principal: Any) -> bool:
    """Whether the parent is itself an AI state (§4.5 step 1, §7.2).

    A playbook or session principal carrying a profile is running *as* an
    agent, so the spec's extra requirement applies: the child's profile must
    be a capability subset of the parent's, and a violation is refused rather
    than silently narrowed.
    """
    kind = getattr(principal, "kind", None)
    return (
        kind in {PrincipalKind.PLAYBOOK, PrincipalKind.SESSION}
        and getattr(principal, "profile_id", None) is not None
    )


def narrow_for_child(
    step: AgentTaskStep, principal: Any, profile_policy: CapabilityPolicy, step_id: str
) -> ExecutionPrincipal:
    """The child principal: the parent's, narrowed by the three-way policy."""
    policy = child_policy(principal.policy, profile_policy, step.capability_narrowing)
    return principal.narrow(policy, reason=f"agent_task:{step_id}")


# --------------------------------------------------------------------------
# Profile resolution — fail closed on a missing identity or unknown profile
# --------------------------------------------------------------------------


def _database(services: EngineServices) -> Any | None:
    db = getattr(services, "db", None)
    if db is not None and hasattr(db, "get_profile"):
        return db
    handler_db = getattr(getattr(services, "handler", None), "db", None)
    if handler_db is not None and hasattr(handler_db, "get_profile"):
        return handler_db
    return None


def _plugin_command_names(services: EngineServices) -> frozenset[str]:
    handler = getattr(services, "handler", None)
    resolve = getattr(handler, "_plugin_command_names", None)
    if callable(resolve):
        try:
            return resolve()
        except Exception:  # noqa: BLE001 - classification, never a hard failure
            return frozenset()
    return frozenset()


async def resolve_profile_policy(
    services: EngineServices, profile_id: str
) -> tuple[CapabilityPolicy | None, str]:
    """The child profile's policy, or ``(None, reason)``.

    Every miss is a *reason*, not a permissive default: an unreadable store,
    an unknown profile and a profile that resolves to nothing are three
    different operator problems and one identical outcome — ``unauthorized``.
    """
    db = _database(services)
    if db is None:
        return None, "no profile store is wired"
    try:
        profile = await db.get_profile(profile_id)
    except Exception as exc:  # noqa: BLE001 - a store that cannot answer is a denial
        return None, f"profile lookup failed: {type(exc).__name__}"
    if profile is None:
        return None, f"unknown profile {profile_id!r}"
    return (
        capability_policy_for(
            profile, plugin_command_names=_plugin_command_names(services)
        ),
        "",
    )


# --------------------------------------------------------------------------
# Result helpers
# --------------------------------------------------------------------------


def _attempt_key(ctx: StepContext) -> str:
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


def _classification(registration: CommandRegistration, outcome: str) -> OutcomeClass | None:
    for spec in registration.contract.execution.outcomes:
        if spec.name == outcome:
            return spec.classification
    return None


class LiveAgentTaskExecutor:
    """Creates the child task, then suspends on it.

    The whole of the executor's authority handling happens *before*
    ``create_task`` is dispatched: an unresolvable profile or a widening
    delegation returns ``unauthorized`` with the adapter never called, which
    is the assertion that distinguishes a fail-closed rule from one that
    fails after the side effect.
    """

    step_type: ClassVar[str] = "agent_task"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = False

    async def execute(self, step: AgentTaskStep, ctx: StepContext) -> ExecutorResult:
        operation = f"agent_task:{step.profile_id}"
        key = _attempt_key(ctx)

        if ctx.principal is None:
            return _fail(
                "unauthorized",
                operation=operation,
                diagnostics=("no execution principal",),
                key=key,
            )

        profile_policy, reason = await resolve_profile_policy(ctx.services, step.profile_id)
        if profile_policy is None:
            return _fail("unauthorized", operation=operation, diagnostics=(reason,), key=key)

        # §7.2: for an AI parent the child profile must be a *subset*, not
        # something to clamp.  Intersection alone would silently narrow a
        # too-broad child, and the spec calls that a refusal.
        if _parent_is_ai(ctx.principal):
            escalation = check_delegation(ctx.principal.policy, profile_policy)
            if escalation:
                return _fail(
                    "unauthorized", operation=operation, diagnostics=(escalation,), key=key
                )

        child_principal = narrow_for_child(step, ctx.principal, profile_policy, ctx.step_id)

        try:
            objective = resolve_value(step.objective, ctx.scope)
        except ValueResolutionError as exc:
            return _fail(
                "input_resolution_failed",
                operation=operation,
                diagnostics=(exc.reason,),
                key=key,
            )

        try:
            registration = ctx.services.contracts.require(CHILD_CREATE_COMMAND)
        except (UnknownContract, KeyError):
            return _fail(
                "contract_violation",
                operation=operation,
                diagnostics=(f"{CHILD_CREATE_COMMAND} has no contract",),
                key=key,
            )

        execution = registration.contract.execution
        payload: dict[str, Any] = dict(ctx.inputs)
        payload.setdefault("title", str(objective))
        payload.setdefault("profile_id", step.profile_id)
        try:
            args = execution.args_model(**payload)
        except ValidationError as exc:
            return _fail(
                "input_resolution_failed",
                operation=operation,
                diagnostics=(f"{exc.error_count()} argument(s) failed validation",),
                key=key,
            )

        try:
            result = await registration.invoke(args, child_principal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - §3.4 step 6: the exception
            # *type* only; a message can carry an argument value.
            return _fail(
                "runtime_error", operation=operation, diagnostics=(type(exc).__name__,), key=key
            )

        if not isinstance(result, CommandResult):
            return _fail(
                "contract_violation",
                operation=operation,
                diagnostics=(f"adapter returned {type(result).__name__}, not CommandResult",),
                key=key,
            )
        if _classification(registration, result.outcome) is OutcomeClass.FAILURE:
            # The child was refused.  That is this step's ``failed`` edge, not
            # a reserved outcome: the run asked for work and did not get it.
            return _fail(
                "failed",
                operation=operation,
                diagnostics=(f"{CHILD_CREATE_COMMAND} returned {result.outcome!r}",),
                key=key,
            )
        child_task_id = getattr(result.value, "task_id", None)
        if not child_task_id:
            return _fail(
                "contract_violation",
                operation=operation,
                diagnostics=(f"{CHILD_CREATE_COMMAND} reported no task_id",),
                key=key,
            )

        dumped = result.value.model_dump()
        receipt_inputs, receipt_result = project_step_receipt(
            payload, dumped, contract=execution, run_id=ctx.run_id
        )
        # The child's identity is receipted whatever happens next, so an
        # orphan created just before a crash is visible to an operator.
        receipt_result = dict(receipt_result) | {"child_task_id": child_task_id}
        value = {"task_id": child_task_id} if step.save_result_as else None

        if not step.wait_for_completion:
            # §4.5 step 4 — fire and forget, no wait registered.
            return ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="dispatched",
                value=value,
                child_task_id=child_task_id,
                idempotency_key=key,
                receipt_inputs=receipt_inputs,
                receipt_result=receipt_result,
                operation=operation,
                diagnostics=(f"child {child_task_id} dispatched without waiting",),
            )

        now = ctx.services.clock()
        wait = WaitSpec(
            wait_id=uuid.uuid4().hex,
            run_id=ctx.run_id,
            step_id=ctx.step_id,
            iteration=-1 if ctx.iteration_index is None else ctx.iteration_index,
            kind="agent_task",
            match={"task_id": child_task_id},
            deadline_at=(now + step.timeout_seconds) if step.timeout_seconds else None,
            created_at=now,
        )
        return ExecutorResult(
            control=StepControl.SUSPEND,
            outcome=AWAITING_OUTCOME,
            value=value,
            wait=wait,
            child_task_id=child_task_id,
            idempotency_key=key,
            receipt_inputs=receipt_inputs,
            receipt_result=receipt_result,
            operation=operation,
        )


class SymbolicAgentTaskExecutor:
    """Dry-run and shadow: creates nothing, waits for nothing.

    There is no preview of "an agent did some work", so the honest answer is
    ``UNRESOLVED`` and a fork across the step's declared outcomes.  It has no
    code path to ``registration.invoke`` at all, which is the structural half
    of the no-side-effect proof for this step kind.
    """

    step_type: ClassVar[str] = "agent_task"
    mode: ClassVar[ExecutionMode] = ExecutionMode.DRY_RUN
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: AgentTaskStep, ctx: StepContext) -> ExecutorResult:
        possible = tuple(
            sorted(name for name in step.transitions if name in AGENT_TASK_OUTCOMES)
        )
        return ExecutorResult(
            control=StepControl.UNRESOLVED,
            outcome="unavailable",
            operation=f"agent_task:{step.profile_id}",
            diagnostics=("an agent task is not simulated",),
            possible_outcomes=possible,
        )


async def cancel_child_task(
    task_id: str,
    *,
    principal: ExecutionPrincipal,
    services: EngineServices,
) -> tuple[bool, str]:
    """Stop one child task as *principal* — the **narrowed child** principal.

    Returns ``(cancelled, diagnostic)``.  A missing contract is reported and
    not worked around: reaching ``CommandHandler`` directly would skip the
    dispatch-boundary authorization that makes the narrowed principal mean
    anything, and a child left running is the fail-safe direction (§7.4).
    """
    try:
        registration = services.contracts.require(CHILD_CANCEL_COMMAND)
    except (UnknownContract, KeyError):
        return False, f"{CHILD_CANCEL_COMMAND} has no contract; child {task_id} left running"
    try:
        args = registration.contract.execution.args_model(task_id=task_id)
    except ValidationError:
        return False, f"{CHILD_CANCEL_COMMAND} does not accept a task_id"
    try:
        await registration.invoke(args, principal)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a cancel that fails is reported
        return False, f"{CHILD_CANCEL_COMMAND} raised {type(exc).__name__}"
    return True, ""


__all__ = [
    "AGENT_TASK_OUTCOMES",
    "AWAITING_OUTCOME",
    "CHILD_CANCEL_COMMAND",
    "CHILD_CREATE_COMMAND",
    "CHILD_STATUS_OUTCOMES",
    "LiveAgentTaskExecutor",
    "SymbolicAgentTaskExecutor",
    "cancel_child_task",
    "child_outcome_for_status",
    "child_policy",
    "narrow_for_child",
    "narrowing_policy",
    "resolve_profile_policy",
]
