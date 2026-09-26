"""Typed contracts for the durable supervisor report boundary."""

from __future__ import annotations

from typing import Any

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
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import principal_context


class ReportRequestArgs(CommandArgs):
    request_id: str


class ReportBriefArgs(CommandArgs):
    request_id: str
    offset: int = 0
    limit: int = 20


class ReportSubmitArgs(CommandArgs):
    request_id: str
    brief_hash: str
    expected_version: int
    text: str
    evidence_refs: list[str] = []


class ReportRequestValue(CommandValue):
    request_id: str
    state: str
    message_id: str
    deadline: float


class ReportBriefValue(CommandValue):
    request_id: str
    state: str
    deadline: float
    version: int
    brief_hash: str
    brief: dict[str, Any]
    facts: list[dict[str, Any]]
    active: list[dict[str, Any]]
    total_facts: int
    total_active: int


class ReportSubmitValue(CommandValue):
    request_id: str
    window_id: str
    state: str
    version: int


def _registration(
    name: str,
    args_model: type[CommandArgs],
    result_model: type[CommandValue],
    side_effect: SideEffectClass,
) -> CommandRegistration:
    async def invoke(args, principal):
        from src.commands.contracts.builtin import _handler

        with principal_context(principal):
            raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        if raw.get("success") is False or raw.get("error"):
            return CommandResult(
                outcome="rejected",
                value=result_model.model_construct(),
                summary=str(raw.get("error") or "rejected"),
            )
        value = result_model(**{field: raw[field] for field in result_model.model_fields})
        return CommandResult(outcome="completed", value=value, summary="completed")

    return CommandRegistration(
        name=name,
        contract=CommandContract(
            execution=ExecutionContract(
                name=name,
                args_model=args_model,
                result_model=result_model,
                outcomes=(
                    OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                    OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                ),
                capability=name,
                side_effect=side_effect,
                idempotency=IdempotencySpec(mode="natural"),
                retry_safe=name != "report_submit",
                sensitive_args=frozenset({"text"}) if name == "report_submit" else frozenset(),
            ),
            presentation=CommandPresentation(
                title=name.replace("_", " ").title(),
                summary={
                    "report_request": "Queue one author wake for a reserved report.",
                    "report_brief": "Read a bounded, paged report brief and its CAS version.",
                    "report_submit": "Submit one authored report before its deadline.",
                }[name],
                outcome_labels={"completed": "Completed", "rejected": "Rejected"},
            ),
        ),
        invoke=invoke,
    )


def register_report_contracts(registry: ContractRegistry) -> None:
    for name, args, result, effect in (
        ("report_request", ReportRequestArgs, ReportRequestValue, SideEffectClass.CREATE),
        ("report_brief", ReportBriefArgs, ReportBriefValue, SideEffectClass.READ),
        ("report_submit", ReportSubmitArgs, ReportSubmitValue, SideEffectClass.UPDATE),
    ):
        if registry.get(name) is None:
            registry.register(_registration(name, args, result, effect))
