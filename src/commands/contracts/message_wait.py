"""Typed convenience attachment to a collaboration's durable message wait."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.agent_waits import AgentWaitRecord
from src.commands.contracts.collaboration import CollaborationMessageRecord
from src.commands.contracts.models import (
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
from src.commands.contracts.wait import WaitScopeArgs
from src.commands.principal import principal_context


class MessageWaitArgs(WaitScopeArgs):
    thread_id: str = Field(min_length=1, max_length=64)
    after_seq: int = Field(ge=0, strict=True)
    timeout: int = Field(default=60, ge=1, le=60, strict=True)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class MessageWaitValue(CommandValue):
    state: Literal[
        "satisfied",
        "waiting",
        "expired",
        "cancelled",
        "thread_closed",
        "peer_failed",
        "peer_gone",
        "partner_not_running",
    ]
    wait: AgentWaitRecord
    messages: list[CollaborationMessageRecord] | None = None
    next_cursor: int | None = None
    has_more: bool | None = None
    cursor: int | None = None
    next_step: str | None = None


def register_message_wait_contract(registry):
    if registry.get("message_wait") is not None:
        return

    async def invoke(args, principal):
        from src.commands.contracts.builtin import _handler

        with principal_context(principal):
            raw = await _handler().execute("message_wait", args.model_dump(exclude_none=True))
        if raw.get("error") or raw.get("success") is False:
            return CommandResult(
                outcome="rejected",
                value=MessageWaitValue.model_construct(),
                summary=str(raw.get("error") or "rejected"),
            )
        return CommandResult(
            outcome="completed",
            value=MessageWaitValue(
                **{k: raw[k] for k in MessageWaitValue.model_fields if k in raw}
            ),
            summary="completed",
        )

    registry.register(
        CommandRegistration(
            "message_wait",
            CommandContract(
                execution=ExecutionContract(
                    name="message_wait",
                    args_model=MessageWaitArgs,
                    result_model=MessageWaitValue,
                    capability="message_wait",
                    side_effect=SideEffectClass.CREATE,
                    retry_safe=True,
                    idempotency=IdempotencySpec(mode="keyed", key_field="idempotency_key"),
                    outcomes=(
                        OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                        OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                    ),
                ),
                presentation=CommandPresentation(
                    title="Message Wait",
                    summary="Wait up to 60 seconds for collaboration messages on a durable wait.",
                    outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                ),
            ),
            invoke,
        )
    )
