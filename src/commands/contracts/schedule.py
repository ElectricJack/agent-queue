"""Public contracts for project-owned one-shot notifications."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

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


class ScheduleScopeArgs(CommandArgs):
    project_id: str = Field(min_length=1, max_length=256)


class ScheduleRegisterArgs(ScheduleScopeArgs):
    body: str = Field(min_length=1, max_length=8000)
    due_at: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("body", "idempotency_key", "project_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SchedulePointerArgs(ScheduleScopeArgs):
    schedule_id: str = Field(min_length=1, max_length=64)


class ScheduleListArgs(ScheduleScopeArgs):
    state: Literal["pending", "fired", "cancelled"] | None = None
    limit: int = Field(default=100, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ScheduleValue(CommandValue):
    schedule: dict[str, Any] | None = None


class ScheduleListValue(CommandValue):
    schedules: list[dict[str, Any]] = []
    count: int = 0


def register_schedule_contracts(registry):
    for name, args_model, result_model, effect, summary in (
        (
            "schedule_register",
            ScheduleRegisterArgs,
            ScheduleValue,
            SideEffectClass.CREATE,
            "Schedule one supervisor notification at an absolute UTC instant, without holding a worker.",
        ),
        (
            "schedule_get",
            SchedulePointerArgs,
            ScheduleValue,
            SideEffectClass.READ,
            "Read a project schedule and its durable firing or cancellation receipt.",
        ),
        (
            "schedule_list",
            ScheduleListArgs,
            ScheduleListValue,
            SideEffectClass.READ,
            "List project schedules, including long-horizon pending notices.",
        ),
        (
            "schedule_cancel",
            SchedulePointerArgs,
            ScheduleValue,
            SideEffectClass.RESOLVE,
            "Cancel a pending project schedule; a fired notice cannot be recalled.",
        ),
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
                    value=result_model(),
                    summary=str(raw.get("error") or "rejected"),
                )
            return CommandResult(
                outcome="completed",
                summary="completed",
                value=result_model(
                    **{key: raw[key] for key in result_model.model_fields if key in raw}
                ),
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
                        if name == "schedule_register"
                        else IdempotencySpec(mode="natural"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary=summary,
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                invoke,
            )
        )
