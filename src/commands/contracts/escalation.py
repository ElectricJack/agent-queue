"""Typed command contracts for the durable human-escalation boundary."""

from __future__ import annotations

from typing import Any, Literal

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    CreateOrReuseClause,
    EffectSubject,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    ReadClause,
    ResolveClause,
    SideEffectClass,
    UpdateClause,
)
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import principal_context


class EscalationCreateArgs(CommandArgs):
    project_id: str
    task_id: str | None = None
    source_kind: str
    source_identity: str
    incident_key: str
    summary: str
    investigation: str
    decision_requested: str
    choices: list[str] | None = None
    severity: Literal["critical", "high", "medium", "low"]


class EscalationListArgs(CommandArgs):
    project_id: str | None = None
    task_id: str | None = None
    states: list[str] | None = None
    limit: int = 100


class EscalationGetArgs(CommandArgs):
    escalation_id: str


class EscalationReplyArgs(CommandArgs):
    escalation_id: str
    text: str
    external_message_id: str
    received_sequence: int | None = None


class EscalationUpdateArgs(CommandArgs):
    escalation_id: str
    expected_revision: int
    state: str | None = None
    summary: str | None = None
    investigation: str | None = None
    decision_requested: str | None = None
    choices: list[str] | None = None
    severity: Literal["critical", "high", "medium", "low"] | None = None
    terminal_outcome: str | None = None
    terminal_evidence: dict[str, Any] | None = None


class EscalationApplyReplyArgs(CommandArgs):
    escalation_id: str
    reply_id: str
    expected_revision: int
    idempotency_key: str
    action_kind: Literal["question_answer", "gate_resolve", "task_recover"]
    target_id: str
    decision: Literal["retry", "hold"] | None = None


class EscalationCreateValue(CommandValue):
    created: bool
    escalation: dict[str, Any]


class EscalationListValue(CommandValue):
    escalations: list[dict[str, Any]]
    count: int


class EscalationGetValue(CommandValue):
    escalation: dict[str, Any]
    messages: list[dict[str, Any]]
    deliveries: list[dict[str, Any]]
    actions: list[dict[str, Any]]


class EscalationReplyValue(CommandValue):
    reply: dict[str, Any]
    escalation: dict[str, Any]
    created: bool
    supervisor_enqueued: bool
    terminal: bool


class EscalationUpdateValue(CommandValue):
    escalation: dict[str, Any]


class EscalationApplyReplyValue(CommandValue):
    applied: bool
    replayed: bool
    action: dict[str, Any]
    escalation: dict[str, Any]
    action_result: dict[str, Any] | None = None


def _contract(
    name: str,
    args_model: type[CommandArgs],
    result_model: type[CommandValue],
    outcomes: tuple[OutcomeSpec, ...],
    side_effect: SideEffectClass,
    effects: tuple[Any, ...],
    idempotency: IdempotencySpec,
    retry_safe: bool,
) -> CommandContract[Any, Any]:
    return CommandContract(
        execution=ExecutionContract(
            name=name,
            args_model=args_model,
            result_model=result_model,
            outcomes=outcomes,
            capability=name,
            side_effect=side_effect,
            effects=effects,
            idempotency=idempotency,
            retry_safe=retry_safe,
            sensitive_args=frozenset({"text"}) if name == "escalation_reply" else frozenset(),
        ),
        presentation=CommandPresentation(
            title=name.replace("_", " ").title(),
            summary={
                "escalation_create": "Open or reuse a durable human decision incident.",
                "escalation_list": "List visible human decision incidents.",
                "escalation_get": "Read one incident and its authoritative history.",
                "escalation_reply": "Record authenticated human evidence and notify its supervisor.",
                "escalation_update": "CAS-update an incident owned by the supervisor.",
                "escalation_apply_reply": "Apply verified evidence through its bound guarded service.",
            }[name],
            outcome_labels={outcome.name: outcome.name.replace("_", " ").title() for outcome in outcomes},
            subject_labels={"escalation": "the escalation"},
        ),
    )


def _adapter(name: str, value_type: type[CommandValue], success_outcome):
    async def invoke(args, ctx):
        from src.commands.contracts.builtin import _handler

        with principal_context(ctx):
            raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        if raw.get("error") or raw.get("success") is False:
            return CommandResult(
                outcome="rejected",
                value=value_type.model_construct(),
                summary=str(raw.get("error") or "rejected"),
            )
        outcome = success_outcome(raw)
        value = value_type(**{field: raw[field] for field in value_type.model_fields if field in raw})
        return CommandResult(outcome=outcome, value=value, summary=outcome)

    return invoke


def _outcomes(*successes: str) -> tuple[OutcomeSpec, ...]:
    return tuple(OutcomeSpec(name=name, classification=OutcomeClass.SUCCESS) for name in successes) + (
        OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
    )


def register_escalation_contracts(registry: ContractRegistry) -> None:
    definitions = (
        (
            "escalation_create", EscalationCreateArgs, EscalationCreateValue,
            _outcomes("created", "reused"), SideEffectClass.CREATE,
            (CreateOrReuseClause(subject=EffectSubject.ESCALATION, key_arg="incident_key"),),
            IdempotencySpec(mode="keyed", key_field="incident_key"), True,
            lambda raw: "created" if raw["created"] else "reused",
        ),
        (
            "escalation_list", EscalationListArgs, EscalationListValue,
            _outcomes("listed"), SideEffectClass.READ,
            (ReadClause(subject=EffectSubject.ESCALATION),),
            IdempotencySpec(mode="natural"), True, lambda raw: "listed",
        ),
        (
            "escalation_get", EscalationGetArgs, EscalationGetValue,
            _outcomes("read"), SideEffectClass.READ,
            (ReadClause(subject=EffectSubject.ESCALATION),),
            IdempotencySpec(mode="natural"), True, lambda raw: "read",
        ),
        (
            "escalation_reply", EscalationReplyArgs, EscalationReplyValue,
            _outcomes("received", "replayed"), SideEffectClass.CREATE,
            (
                CreateOrReuseClause(
                    subject=EffectSubject.ESCALATION_REPLY, key_arg="external_message_id"
                ),
            ),
            IdempotencySpec(mode="keyed", key_field="external_message_id"), True,
            lambda raw: "received" if raw["created"] else "replayed",
        ),
        (
            "escalation_update", EscalationUpdateArgs, EscalationUpdateValue,
            _outcomes("updated"), SideEffectClass.UPDATE,
            (UpdateClause(subject=EffectSubject.ESCALATION),),
            IdempotencySpec(mode="natural"), True, lambda raw: "updated",
        ),
        (
            "escalation_apply_reply", EscalationApplyReplyArgs, EscalationApplyReplyValue,
            _outcomes("applied", "replayed"), SideEffectClass.RESOLVE,
            (
                ResolveClause(subject=EffectSubject.ESCALATION, target_arg="escalation_id"),
                CreateOrReuseClause(
                    subject=EffectSubject.ESCALATION_ACTION, key_arg="idempotency_key"
                ),
            ),
            IdempotencySpec(mode="keyed", key_field="idempotency_key"), True,
            lambda raw: "applied" if raw["applied"] else "replayed",
        ),
    )
    for name, args, value, outcomes, side_effect, effects, idem, safe, outcome in definitions:
        if registry.get(name) is None:
            registry.register(
                CommandRegistration(
                    name,
                    _contract(name, args, value, outcomes, side_effect, effects, idem, safe),
                    _adapter(name, value, outcome),
                )
            )
