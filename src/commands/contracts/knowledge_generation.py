"""Operator/service-only generation maintenance contracts; no source/body arguments."""

from typing import Any

from src.commands.contracts.knowledge import _knowledge_invoke
from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration


class GenerationValue(CommandValue):
    captured: int | None = None
    states: list[str] | None = None
    jobs: list[dict[str, Any]] | None = None
    budgets: list[dict[str, Any]] | None = None
    unknown_calls: int | None = None
    page_limit: int | None = None


GENERATION_COMMANDS = (
    (
        "knowledge_generation_tick",
        SideEffectClass.CREATE,
        "Reconcile retained inputs and run bounded, explicitly enabled proposal generation.",
    ),
    (
        "knowledge_generation_status",
        SideEffectClass.READ,
        "Inspect independent generation budgets, circuits and ambiguous calls without provider access.",
    ),
)


def register_generation_contracts(registry):
    for name, effect, summary in GENERATION_COMMANDS:
        if registry.get(name):
            continue
        registry.register(
            CommandRegistration(
                name=name,
                contract=CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=CommandArgs,
                        result_model=GenerationValue,
                        capability=name,
                        side_effect=effect,
                        retry_safe=True,
                        idempotency=IdempotencySpec(mode="natural"),
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
                invoke=_knowledge_invoke(name, CommandArgs, GenerationValue),
            )
        )
