"""Durable human instructions shared by every supervisor."""
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from src.commands.contracts.models import (
    CommandArgs, CommandContract, CommandPresentation, CommandValue, ExecutionContract,
    IdempotencySpec, OutcomeClass, OutcomeSpec, SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration
from src.commands.contracts.records import _record_invoke


class DecisionListArgs(CommandArgs):
    object_kind: Literal["task", "batch", "operation"]
    object_id: str = Field(min_length=1)


class DecisionRecordArgs(DecisionListArgs):
    effect: Literal["note", "hold", "release"]
    operator: str = Field(min_length=1, max_length=256)
    decision: str = Field(min_length=1, max_length=16000)
    source: Literal["chat", "discord", "cli"]
    source_ref: str = Field(min_length=1, max_length=2048)
    idempotency_key: str = Field(min_length=1, max_length=256)
    releases: str | None = Field(default=None, min_length=1)

    @field_validator("operator", "decision", "source_ref", "idempotency_key")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def release_identity(self):
        if (self.effect == "release") != (self.releases is not None):
            raise ValueError("release requires the exact hold ID; other effects forbid releases")
        return self


class DecisionValue(CommandValue):
    decision: dict[str, Any] | None = None
    operator_decisions: list[dict[str, Any]] | None = None


def register_decision_contracts(registry):
    for name, args, effect in (
        ("decision_record", DecisionRecordArgs, SideEffectClass.CREATE),
        ("decision_list", DecisionListArgs, SideEffectClass.READ),
    ):
        registry.register(CommandRegistration(
            name,
            CommandContract(
                execution=ExecutionContract(
                    name=name, args_model=args, result_model=DecisionValue, capability=name,
                    side_effect=effect, retry_safe=True,
                    idempotency=IdempotencySpec(mode="keyed", key_field="idempotency_key")
                    if name == "decision_record" else IdempotencySpec(mode="natural"),
                    outcomes=(
                        OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                        OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                    ),
                ),
                presentation=CommandPresentation(
                    title=name.replace("_", " ").title(),
                    summary="Record or read durable operator instructions on a task or integration.",
                    outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                ),
            ),
            _record_invoke(name, DecisionValue),
        ))
