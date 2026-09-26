"""Four typed public contracts for durable agent waits."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from src.agent_waits import AgentWaitRecord

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
from src.commands.principal import principal_context


class WaitScopeArgs(CommandArgs):
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None


class WaitRegisterArgs(WaitScopeArgs):
    kind: Literal["job", "task", "message", "timer"]
    ref: str | None = Field(default=None, max_length=256)
    after_seq: int | None = Field(default=None, ge=0, strict=True)
    due_at: float | None = Field(default=None, allow_inf_nan=False)
    timeout: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=1, max_length=256)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class WaitGetArgs(WaitScopeArgs):
    wait_id: str = Field(min_length=1)


class WaitListArgs(WaitScopeArgs):
    limit: int = Field(default=100, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class WaitCancelArgs(WaitGetArgs):
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class WaitValue(CommandValue):
    wait: AgentWaitRecord
    next_step: str | None = None


class WaitListValue(CommandValue):
    waits: list[AgentWaitRecord]
    count: int


def register_wait_contracts(registry):
    for name, args_model, result_model, effect in (
        ("wait_register", WaitRegisterArgs, WaitValue, SideEffectClass.CREATE),
        ("wait_get", WaitGetArgs, WaitValue, SideEffectClass.READ),
        ("wait_list", WaitListArgs, WaitListValue, SideEffectClass.READ),
        ("wait_cancel", WaitCancelArgs, WaitValue, SideEffectClass.RESOLVE),
    ):
        if registry.get(name) is not None:
            continue

        async def invoke(args, principal, name=name, result_model=result_model):
            from src.commands.contracts.builtin import _handler

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
                        idempotency=IdempotencySpec(mode="keyed", key_field="idempotency_key")
                        if name == "wait_register"
                        else IdempotencySpec(mode="natural"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary={
                            "wait_register": "Register one bounded typed wait and end the turn.",
                            "wait_get": "Read a durable wait and its bounded result pointer.",
                            "wait_list": "List wait history for the current task or supervisor project.",
                            "wait_cancel": "Cancel a current-claim wait and queue its result.",
                        }[name],
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                invoke,
            )
        )
