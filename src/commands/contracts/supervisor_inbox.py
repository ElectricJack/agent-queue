"""Contracts for verified intake, explicit replies and global conversation reads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

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
    SideEffectClass,
    UpdateClause,
)
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import principal_context
from src.conversations.envelope import ConversationEnvelope


class SupervisorInboxPostArgs(CommandArgs):
    envelope: ConversationEnvelope
    conversation_id: str | None = None
    source: Literal["gateway", "backfill"] | None = None
    provenance: Literal["replay", "test"] | None = None


class SupervisorInboxReplyArgs(CommandArgs):
    conversation_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class SupervisorInboxStatusArgs(CommandArgs):
    pass


class SupervisorInboxHistoryArgs(CommandArgs):
    conversation_id: str | None = Field(default=None, min_length=1)
    states: list[Literal["opening", "open", "closed", "delivery_blocked"]] | None = None
    limit: int = Field(default=50, ge=1, le=100)
    before: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="Exclusive epoch-second boundary; alone, a strict time filter.",
    )
    before_id: str | None = Field(
        default=None,
        min_length=1,
        description="Row id breaking ties at `before`; requires `before`.",
    )


class SupervisorInboxPostValue(CommandValue):
    created: bool
    conversation_id: str
    input_id: str
    supervisor_message_id: str | None
    state: str


class SupervisorInboxReplyValue(CommandValue):
    created: bool
    reply_message_id: str
    delivery_dedup_key: str
    discord_text_chars: int
    truncated: bool


class SupervisorInboxStatusValue(CommandValue):
    enabled: bool
    preconditions: dict[str, Any]
    diagnostics: dict[str, Any]
    limits: dict[str, int]
    counts: dict[str, Any]
    backfill: dict[str, Any]
    intake: dict[str, Any]


class SupervisorInboxHistoryValue(CommandValue):
    conversations: list[dict[str, Any]]
    next_before: float | None = Field(description="Next page's `before`; null when exhausted.")
    next_before_id: str | None = Field(description="Next page's `before_id`, with `next_before`.")


def _register(registry, name, args_model, value_model, summary, effects, side_effect, idem):
    successes = ("created", "replayed") if side_effect is SideEffectClass.CREATE else ("read",)
    outcomes = tuple(
        OutcomeSpec(name=s, classification=OutcomeClass.SUCCESS) for s in successes
    ) + (OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),)

    async def invoke(args, principal):
        from src.commands.contracts.builtin import _handler

        with principal_context(principal):
            raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        if raw.get("success") is False or raw.get("error"):
            return CommandResult(
                outcome="rejected",
                value=value_model.model_construct(),
                summary=str(raw.get("error") or "rejected"),
            )
        outcome = ("created" if raw["created"] else "replayed") if "created" in raw else "read"
        return CommandResult(
            outcome=outcome,
            value=value_model(**{f: raw[f] for f in value_model.model_fields}),
            summary=outcome,
        )

    copy = {
        "conversation": "the supervisor conversation",
        "conversation_input": "the verified operator input",
        "conversation_reply": "the explicit supervisor reply",
    }
    if registry.get(name) is None:
        registry.register(
            CommandRegistration(
                name=name,
                contract=CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=args_model,
                        result_model=value_model,
                        outcomes=outcomes,
                        capability=name,
                        side_effect=side_effect,
                        effects=effects,
                        idempotency=idem,
                        retry_safe=True,
                        sensitive_args=(
                            frozenset({"envelope"})
                            if name == "supervisor_inbox_post"
                            else frozenset({"text"})
                            if name == "supervisor_inbox_reply"
                            else frozenset()
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary=summary,
                        outcome_labels={o.name: o.name.title() for o in outcomes},
                        subject_labels={e.subject.value: copy[e.subject.value] for e in effects},
                    ),
                ),
                invoke=invoke,
            )
        )


def register_supervisor_inbox_contracts(registry: ContractRegistry) -> None:
    _register(
        registry,
        "supervisor_inbox_post",
        SupervisorInboxPostArgs,
        SupervisorInboxPostValue,
        "Accept verified gateway input or local test/replay provenance into the global inbox.",
        (
            CreateOrReuseClause(subject=EffectSubject.CONVERSATION_INPUT, key_arg="envelope"),
            CreateOrReuseClause(subject=EffectSubject.CONVERSATION, key_arg="envelope"),
        ),
        SideEffectClass.CREATE,
        IdempotencySpec(mode="keyed", key_field="envelope"),
    )
    _register(
        registry,
        "supervisor_inbox_reply",
        SupervisorInboxReplyArgs,
        SupervisorInboxReplyValue,
        "Record an explicit live global-supervisor answer and queue its Discord delivery.",
        (
            CreateOrReuseClause(
                subject=EffectSubject.CONVERSATION_REPLY, key_arg="idempotency_key"
            ),
            UpdateClause(subject=EffectSubject.CONVERSATION_INPUT),
        ),
        SideEffectClass.CREATE,
        IdempotencySpec(mode="keyed", key_field="idempotency_key"),
    )
    _register(
        registry,
        "supervisor_inbox_status",
        SupervisorInboxStatusArgs,
        SupervisorInboxStatusValue,
        "Read installation-wide conversation preconditions, limits, counts and intake health.",
        (ReadClause(subject=EffectSubject.CONVERSATION),),
        SideEffectClass.READ,
        IdempotencySpec(mode="natural"),
    )
    _register(
        registry,
        "supervisor_inbox_history",
        SupervisorInboxHistoryArgs,
        SupervisorInboxHistoryValue,
        "Page conversations by update time or one conversation's inputs by receipt time.",
        (
            ReadClause(subject=EffectSubject.CONVERSATION),
            ReadClause(subject=EffectSubject.CONVERSATION_INPUT),
        ),
        SideEffectClass.READ,
        IdempotencySpec(mode="natural"),
    )
