"""Typed contract registration boundary for hierarchical integration commands."""

from __future__ import annotations

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
from src.commands.contracts.registry import CommandContext, CommandRegistration, ContractRegistry
from src.commands.principal import principal_context
from src.integration.models import BranchKey, Fence


DESIGN_INTEGRATION_COMMANDS = frozenset(
    {
        "integration_schedule_due",
        "integration_file_children",
        "integration_checkpoint_parent",
        "integration_delivery_readiness",
        "integration_parent_verify",
        "integration_complete_parent",
        "delivery_promote",
        "delivery_receipts",
        "integration_seal",
        "integration_build_candidate",
        "integration_ci_evidence",
        "integration_record_repair",
        "integration_repair_timeout",
        "integration_transfer_owner",
        "integration_mutate_hierarchy",
        "integration_reconcile_promotion",
        "integration_promote_main",
        "integration_release",
    }
)


class IntegrationTransferOwnerArgs(CommandArgs):
    target: BranchKey
    expected_token: int
    next_owner_id: str
    next_role: str


class IntegrationTransferOwnerValue(CommandValue):
    fence: Fence | None = None


INTEGRATION_TRANSFER_OWNER = CommandContract(
    execution=ExecutionContract(
        name="integration_transfer_owner",
        args_model=IntegrationTransferOwnerArgs,
        result_model=IntegrationTransferOwnerValue,
        outcomes=(
            OutcomeSpec(name="transferred", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="busy", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="stale_owner", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="human_required", classification=OutcomeClass.FAILURE),
        ),
        capability="integration_transfer_owner",
        side_effect=SideEffectClass.UPDATE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),),
        sensitive_args=frozenset({"expected_token", "next_owner_id"}),
        sensitive_result_fields=frozenset({"fence"}),
        receipt_projection=(),
    ),
    presentation=CommandPresentation(
        title="Transfer integration branch owner",
        summary="Stop and detach the current branch writer before granting a fresh fence.",
        arg_labels={
            "target": "Repository branch",
            "expected_token": "Expected ownership fence",
            "next_owner_id": "Next domain owner",
            "next_role": "Next owner role",
        },
        outcome_labels={
            "transferred": "Transferred",
            "busy": "Writer still active",
            "stale_owner": "Stale owner",
            "human_required": "Human handoff required",
        },
        result_labels={"fence": "New ownership fence"},
        subject_labels={"branch_ownership": "the repository branch ownership"},
    ),
)


async def _transfer_adapter(
    args: IntegrationTransferOwnerArgs, ctx: CommandContext | None
) -> CommandResult[IntegrationTransferOwnerValue]:
    # Keep the dependency direction one-way: builtin imports this registration
    # at startup, while its legacy handler provider is reached only on invoke.
    from src.commands.contracts.builtin import _handler

    payload = args.model_dump(mode="json")
    if ctx is None:
        raw = await _handler().execute("integration_transfer_owner", payload)
    else:
        with principal_context(ctx):
            raw = await _handler().execute("integration_transfer_owner", payload)
    outcome = raw.get("outcome")
    if outcome not in {"transferred", "busy", "stale_owner", "human_required"}:
        return CommandResult(
            outcome="contract_violation",
            value=IntegrationTransferOwnerValue(),
            summary="integration_transfer_owner returned an invalid outcome",
        )
    try:
        value = IntegrationTransferOwnerValue(fence=raw.get("fence"))
    except Exception as exc:
        return CommandResult(
            outcome="contract_violation",
            value=IntegrationTransferOwnerValue(),
            summary=f"integration_transfer_owner result did not match its contract: {exc}",
        )
    return CommandResult(outcome=outcome, value=value, summary=str(raw.get("error") or outcome))


def register_integration_contracts(registry: ContractRegistry) -> None:
    """Register contracts whose real handlers have landed.

    Each implementation task adds its handler and typed authority/redaction
    declaration together.  Unavailable security-sensitive mutations remain
    outside the allowlist.
    """
    if registry.get(INTEGRATION_TRANSFER_OWNER.name) is None:
        registry.register(
            CommandRegistration(
                INTEGRATION_TRANSFER_OWNER.name,
                INTEGRATION_TRANSFER_OWNER,
                _transfer_adapter,
            )
        )
