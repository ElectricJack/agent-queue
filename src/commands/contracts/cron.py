"""Public contracts for session-owned recurring prompts."""

from __future__ import annotations

from typing import Any

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


class CronScopeArgs(CommandArgs):
    session_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class CronRegisterArgs(CronScopeArgs):
    prompt: str = Field(min_length=1, max_length=8000)
    idempotency_key: str = Field(min_length=1, max_length=256)
    every: float | None = Field(default=None, ge=60, le=604800, allow_inf_nan=False)
    offset: float = Field(default=0, ge=0, allow_inf_nan=False)
    cron: str | None = Field(default=None, max_length=64)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)

    @field_validator("prompt", "idempotency_key")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CronPointerArgs(CronScopeArgs):
    schedule_id: str = Field(min_length=1, max_length=64)


class CronGetArgs(CronPointerArgs):
    consume: bool = False


class CronListArgs(CronScopeArgs):
    limit: int = Field(default=100, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class CronValue(CommandValue):
    schedule: dict[str, Any]
    next_step: str | None = None


class CronListValue(CommandValue):
    schedules: list[dict[str, Any]]
    count: int


def register_cron_contracts(registry):
    for name, args_model, result_model, effect, summary in (
        (
            "cron_register",
            CronRegisterArgs,
            CronValue,
            SideEffectClass.CREATE,
            "Register an idempotent recurring prompt for the caller's session.",
        ),
        (
            "cron_get",
            CronGetArgs,
            CronValue,
            SideEffectClass.COMPOSITE,
            "Read a schedule and optionally consume its pending prompt.",
        ),
        (
            "cron_list",
            CronListArgs,
            CronListValue,
            SideEffectClass.READ,
            "List this session instance's schedules and next-fire times.",
        ),
        (
            "cron_cancel",
            CronPointerArgs,
            CronValue,
            SideEffectClass.RESOLVE,
            "Cancel this session's recurring prompt and pending delivery.",
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
                    value=result_model.model_construct(),
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
                        if name == "cron_register"
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
