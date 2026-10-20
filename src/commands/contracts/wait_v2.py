"""Explicit contracts and caller boundaries for verified waits (§7).

Runtime owners install handlers on ``orchestrator.wait_v2_handlers``. These
declarations never activate a listener, sleep a session or manufacture evidence.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

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
from src.commands.contracts.registry import CommandRegistration
from src.commands.principal import PrincipalKind, principal_context

WAIT_ERROR_CODES = frozenset(
    {
        "wait.condition_required",
        "wait.source_unresolved",
        "wait.already_satisfied",
        "wait.out_of_scope",
        "wait.active_exists",
        "wait.cycle",
        "wait.idempotency_conflict",
        "wait.loop_detected",
        "wait.checkpoint_invalid",
        "continuation.stale_generation",
        "wait.not_escalated",
        "wait.not_human",
        "wait.not_in_place",
        "wait.deadline_stale",
        "wait.decide_required",
        "continuation.head_drift",
        "wait.upgrade_required",
        "wait.disabled",
        "wait.unavailable",
        "wait.invalid",
        "continuation.stop_uncertain",
    }
)
SERVICE_WAIT_COMMANDS = frozenset(
    {
        "wait_source_commit",
        "wait_deadline_fire",
        "wait_resume_dispatch",
        "wait_sleep_decide",
        "wait_evict",
        "scheduler_wait_sync",
    }
)
SUPERVISOR_WAIT_COMMANDS = frozenset({"wait_graph", "wait_decide", "wait_lift"})
PUBLIC_WAIT_V2_COMMANDS = frozenset(
    {
        "wait_explain",
        "wait_graph",
        "wait_checkpoint",
        "wait_ack",
        "wait_decide",
        "wait_lift",
    }
)


class WaitV2ScopeArgs(CommandArgs):
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class WaitExplainArgs(WaitV2ScopeArgs):
    wait_id: str | None = Field(default=None, min_length=1)
    history: bool = False

    @model_validator(mode="after")
    def target(self):
        if bool(self.wait_id) == bool(self.task_id):
            raise ValueError("specify exactly one wait_id or task_id")
        return self


class WaitGraphArgs(CommandArgs):
    project_id: str | None = None
    fleet: bool = False

    @model_validator(mode="after")
    def target(self):
        if self.fleet and self.project_id:
            raise ValueError("fleet and project_id are mutually exclusive")
        return self


class WaitCheckpointArgs(WaitV2ScopeArgs):
    wait_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=8192)
    continuation_id: str | None = None
    generation: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def summary_budget(self):
        if not self.summary.strip() or len(self.summary.encode("utf-8")) > 8192:
            raise ValueError("summary must contain 1–8192 UTF-8 bytes")
        return self


class WaitAckArgs(WaitV2ScopeArgs):
    wait_id: str = Field(min_length=1)
    continuation_id: str | None = None
    generation: int | None = Field(default=None, ge=1, strict=True)


class WaitDecideArgs(CommandArgs):
    project_id: str | None = None
    wait_id: str = Field(min_length=1)
    decision: Literal["extend", "wake", "cancel", "reroute", "escalate_to_human"]
    note: str = Field(min_length=1, max_length=4000)
    extend_seconds: float | None = Field(default=None, ge=60, le=86400, allow_inf_nan=False)
    to: str | None = Field(default=None, min_length=1)
    expected_generation: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def decision_arguments(self):
        if not self.note.strip():
            raise ValueError("note must not be blank")
        if self.extend_seconds is not None and self.decision != "extend":
            raise ValueError("extend_seconds is only valid for extend")
        if (self.decision == "reroute") != (self.to is not None):
            raise ValueError("to is required only for reroute")
        return self


class WaitLiftArgs(CommandArgs):
    project_id: str | None = None
    wait_id: str = Field(min_length=1)
    response: str = Field(min_length=1, max_length=4000)
    expected_generation: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def response_nonblank(self):
        if not self.response.strip():
            raise ValueError("response must not be blank")
        return self


class WaitSourceCommitArgs(CommandArgs):
    source_key: str = Field(min_length=1)
    generation: int = Field(ge=0, strict=True)
    revision: str = Field(min_length=1)
    outcome_ref: str = Field(min_length=1)
    payload: dict[str, Any]
    occurred_at: float | None = Field(default=None, allow_inf_nan=False)


class WaitDeadlineFireArgs(CommandArgs):
    wait_id: str = Field(min_length=1)
    deadline_type: Literal["due", "escalation", "ceiling", "transition", "reservation"]
    generation: int = Field(ge=1, strict=True)
    now: float | None = Field(default=None, allow_inf_nan=False)


class WaitResumeDispatchArgs(CommandArgs):
    continuation_id: str = Field(min_length=1)
    generation: int = Field(ge=1, strict=True)


class WaitSleepDecideArgs(CommandArgs):
    wait_id: str = Field(min_length=1)
    now: float | None = Field(default=None, allow_inf_nan=False)


class WaitEvictArgs(WaitSleepDecideArgs):
    reason: str = Field(min_length=1)


class SchedulerWaitSyncArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    blockers: list[dict[str, Any]]
    cycle_id: str = Field(min_length=1)
    now: float | None = Field(default=None, allow_inf_nan=False)


class WaitLegacyBridgeArgs(CommandArgs):
    project_id: str | None = None
    apply: bool = False


class WaitExplainValue(CommandValue):
    explanation: dict[str, Any] | None = None
    waits: list[dict[str, Any]] = Field(default_factory=list)


class WaitGraphValue(CommandValue):
    graph: dict[str, Any]


class WaitCheckpointValue(CommandValue):
    continuation_id: str
    generation: int
    next_step: str | None = None


class WaitAckValue(CommandValue):
    wait_id: str
    acknowledged: bool
    continuation_id: str | None = None
    generation: int | None = None
    next_step: str | None = None


class WaitDecideValue(CommandValue):
    wait_id: str
    decision: str
    outcome: str | None = None
    generation: int | None = None
    next_step: str | None = None


class WaitServiceValue(CommandValue):
    result: dict[str, Any] = Field(default_factory=dict)


WAIT_V2_DEFINITIONS = (
    ("wait_explain", WaitExplainArgs, WaitExplainValue, SideEffectClass.READ),
    ("wait_graph", WaitGraphArgs, WaitGraphValue, SideEffectClass.READ),
    ("wait_checkpoint", WaitCheckpointArgs, WaitCheckpointValue, SideEffectClass.UPDATE),
    ("wait_ack", WaitAckArgs, WaitAckValue, SideEffectClass.RESOLVE),
    ("wait_decide", WaitDecideArgs, WaitDecideValue, SideEffectClass.RESOLVE),
    ("wait_lift", WaitLiftArgs, WaitDecideValue, SideEffectClass.RESOLVE),
    ("wait_source_commit", WaitSourceCommitArgs, WaitServiceValue, SideEffectClass.CREATE),
    ("wait_deadline_fire", WaitDeadlineFireArgs, WaitServiceValue, SideEffectClass.RESOLVE),
    ("wait_resume_dispatch", WaitResumeDispatchArgs, WaitServiceValue, SideEffectClass.COMPOSITE),
    ("wait_sleep_decide", WaitSleepDecideArgs, WaitServiceValue, SideEffectClass.UPDATE),
    ("wait_evict", WaitEvictArgs, WaitServiceValue, SideEffectClass.COMPOSITE),
    ("scheduler_wait_sync", SchedulerWaitSyncArgs, WaitServiceValue, SideEffectClass.COMPOSITE),
    ("wait_legacy_bridge", WaitLegacyBridgeArgs, WaitServiceValue, SideEffectClass.COMPOSITE),
)
WAIT_V2_ARGS = {name: args for name, args, _, _ in WAIT_V2_DEFINITIONS}


def operator_wait_grant(name, principal):
    """A project-scoped, audited operator dispatch grants these two verbs only."""
    from src.commands.operator_authority import operator_grant

    grant = operator_grant(name)
    if (
        name in {"wait_graph", "wait_lift"}
        and principal is not None
        and principal.kind == PrincipalKind.SESSION
        and grant is not None
        and grant.session_id == principal.session_id
        and (principal.task_id is None or grant.task_id == principal.task_id)
        and grant.project_id == principal.project_id
    ):
        return grant
    return None


def caller_allowed(name, principal):
    if principal is None:
        return False
    if name in SERVICE_WAIT_COMMANDS:
        return principal.kind == PrincipalKind.SERVICE
    if name == "wait_legacy_bridge":
        return principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}
    if name in {"wait_checkpoint", "wait_ack"}:
        return principal.kind == PrincipalKind.SESSION
    if name in SUPERVISOR_WAIT_COMMANDS:
        return (
            operator_wait_grant(name, principal) is not None
            or principal.kind == PrincipalKind.LOCAL
            or (principal.kind == PrincipalKind.SESSION and principal.elevated)
        )
    return principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}


def register_wait_v2_contracts(registry):
    for name, args_model, result_model, effect in WAIT_V2_DEFINITIONS:
        if registry.get(name) is not None:
            continue

        async def invoke(args, principal, name=name, result_model=result_model):
            from src.commands.contracts.builtin import _handler

            if not caller_allowed(name, principal):
                return CommandResult(
                    outcome="rejected",
                    value=result_model.model_construct(),
                    summary="wait.out_of_scope",
                )
            with principal_context(principal):
                raw = await _handler().execute(name, args.model_dump(exclude_none=True))
            if raw.get("success") is False or raw.get("error"):
                return CommandResult(
                    outcome="rejected",
                    value=result_model.model_construct(),
                    summary=str(raw.get("error") or "rejected"),
                )
            return CommandResult(
                outcome="completed",
                value=result_model(
                    **{key: raw[key] for key in result_model.model_fields if key in raw}
                ),
                summary="completed",
            )

        registry.register(
            CommandRegistration(
                name,
                CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=args_model,
                        result_model=result_model,
                        capability=name,
                        side_effect=effect,
                        retry_safe=True,
                        idempotency=IdempotencySpec(mode="natural"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                        sensitive_args=frozenset({"summary"})
                        if name == "wait_checkpoint"
                        else frozenset(),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary=f"{name.replace('_', ' ').capitalize()} through verified wait state.",
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                invoke,
            )
        )
