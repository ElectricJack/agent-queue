"""Versioned agent handoff input; legacy subject/detail remain accepted."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    EffectSubject,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
    UpdateClause,
)
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import principal_context
from src.handoffs import HANDOFF_BYTES, LIST_FIELDS, TEXT_FIELDS


class TaskHandoffArgs(CommandArgs):
    task_id: str | None = None
    session_id: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0)
    auto: bool = False
    schema_version: Literal[1] = 1
    subject: str = ""
    detail: str = ""
    goal: str = ""
    completed: list[str] = Field(default_factory=list, max_length=20)
    next_step: str = ""
    waiting_for: str = ""
    files: list[str] = Field(default_factory=list, max_length=20)
    decisions: list[str] = Field(default_factory=list, max_length=20)
    do_not_repeat: list[str] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def text_budget(self) -> TaskHandoffArgs:
        values = [getattr(self, f) for f in TEXT_FIELDS]
        values += [v for f in LIST_FIELDS for v in getattr(self, f)]
        if sum(len(v.encode("utf-8")) for v in values) > HANDOFF_BYTES:
            raise ValueError("combined agent text must be at most 8 KiB UTF-8")
        return self


class TaskHandoffValue(CommandValue):
    success: bool
    handoff_id: str | None
    restart_requested: bool
    created: bool
    noop: bool


def register_handoff_contract(registry: ContractRegistry) -> None:
    async def invoke(args, ctx):
        from src.commands.contracts.builtin import _handler

        with principal_context(ctx):
            raw = await _handler().execute("task_handoff", args.model_dump(exclude_none=True))
        if raw.get("error"):
            return CommandResult(
                outcome="rejected",
                value=TaskHandoffValue.model_construct(),
                summary=str(raw["error"]),
            )
        outcome = "noop" if raw["noop"] else ("recorded" if raw["created"] else "reused")
        return CommandResult(outcome=outcome, value=TaskHandoffValue(**raw), summary=outcome)

    if registry.get("task_handoff") is None:
        registry.register(
            CommandRegistration(
                "task_handoff",
                CommandContract(
                    execution=ExecutionContract(
                        name="task_handoff",
                        args_model=TaskHandoffArgs,
                        result_model=TaskHandoffValue,
                        capability="task_handoff",
                        side_effect=SideEffectClass.UPDATE,
                        outcomes=tuple(
                            OutcomeSpec(name=n, classification=OutcomeClass.SUCCESS)
                            for n in ("recorded", "reused", "noop")
                        )
                        + (OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),),
                        effects=(UpdateClause(subject=EffectSubject.TASK_EXECUTION),),
                        idempotency=IdempotencySpec(mode="keyed", key_field="idempotency_key"),
                        # The key is optional for compatibility. Unkeyed non-auto
                        # calls can emit another restart request, so never auto-retry.
                        retry_safe=False,
                        sensitive_args=frozenset(TEXT_FIELDS + LIST_FIELDS),
                    ),
                    presentation=CommandPresentation(
                        title="Record a task handoff",
                        summary="Store bounded agent assertions with current daemon facts. Auto is note-only; "
                        "non-auto records a restart request, without performing a restart.",
                        outcome_labels={
                            n: n.title() for n in ("recorded", "reused", "noop", "rejected")
                        },
                        subject_labels={"task_execution": "the task's handoff note"},
                    ),
                ),
                invoke,
            )
        )
