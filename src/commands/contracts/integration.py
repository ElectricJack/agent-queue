"""Typed contract registration boundary for hierarchical integration commands."""

from __future__ import annotations

from typing import Any

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


class DeliveryPromoteArgs(CommandArgs):
    operation_key: str
    source_task_id: str
    source_head: str
    source_base: str
    expected_target: str
    fence: Fence


class PromotionCommandValue(CommandValue):
    intent_id: str | None = None
    receipt_id: str | None = None
    prepared_sha: str | None = None


class DeliveryReceiptsArgs(CommandArgs):
    source_task_id: str
    repository_id: str
    target_branch: str


class DeliveryReceiptsValue(CommandValue):
    receipts: tuple[dict[str, Any], ...] = ()


class IntegrationReconcilePromotionArgs(CommandArgs):
    intent_id: str


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


DELIVERY_PROMOTE = CommandContract(
    execution=ExecutionContract(
        name="delivery_promote",
        args_model=DeliveryPromoteArgs,
        result_model=PromotionCommandValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS
                    if name in {"promoted", "already_promoted"}
                    else OutcomeClass.FAILURE
                ),
            )
            for name in (
                "promoted",
                "already_promoted",
                "conflict",
                "source_moved",
                "target_moved",
            )
        ),
        capability="delivery_promote",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="keyed", key_field="operation_key"),
        retry_safe=True,
        effects=(
            CreateOrReuseClause(
                subject=EffectSubject.INTEGRATION_OPERATION, key_arg="operation_key"
            ),
            UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
        ),
        sensitive_args=frozenset({"source_head", "source_base", "expected_target", "fence"}),
        sensitive_result_fields=frozenset({"prepared_sha"}),
        receipt_projection=("intent_id", "receipt_id", "prepared_sha"),
    ),
    presentation=CommandPresentation(
        title="Promote reviewed child delivery",
        summary="Prepare and lease-push one reviewed squash to its immediate parent.",
    ),
)


DELIVERY_RECEIPTS = CommandContract(
    execution=ExecutionContract(
        name="delivery_receipts",
        args_model=DeliveryReceiptsArgs,
        result_model=DeliveryReceiptsValue,
        outcomes=(
            OutcomeSpec(name="found", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="not_found", classification=OutcomeClass.SUCCESS),
        ),
        capability="delivery_receipts",
        side_effect=SideEffectClass.READ,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),),
        receipt_projection=(),
    ),
    presentation=CommandPresentation(
        title="Read delivery receipts",
        summary="Read repository-qualified delivery evidence for one source task.",
    ),
)


INTEGRATION_RECONCILE_PROMOTION = CommandContract(
    execution=ExecutionContract(
        name="integration_reconcile_promotion",
        args_model=IntegrationReconcilePromotionArgs,
        result_model=PromotionCommandValue,
        outcomes=(
            OutcomeSpec(name="applied", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="not_applied", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="invariant_error", classification=OutcomeClass.FAILURE),
        ),
        capability="integration_reconcile_promotion",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="keyed", key_field="intent_id"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        sensitive_result_fields=frozenset({"prepared_sha"}),
        receipt_projection=("intent_id", "receipt_id", "prepared_sha"),
    ),
    presentation=CommandPresentation(
        title="Reconcile prepared promotion",
        summary="Compare a durable prepared intent with the remote and finalize its receipt.",
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


async def _invoke_adapter(
    command: str,
    args: CommandArgs,
    ctx: CommandContext | None,
    value_model: type[CommandValue],
    outcomes: set[str],
) -> CommandResult:
    from src.commands.contracts.builtin import _handler

    payload = args.model_dump(mode="json")
    if ctx is None:
        raw = await _handler().execute(command, payload)
    else:
        with principal_context(ctx):
            raw = await _handler().execute(command, payload)
    outcome = raw.get("outcome")
    if outcome not in outcomes | {"unauthorized", "runtime_error"}:
        return CommandResult(
            outcome="contract_violation",
            value=value_model(),
            summary=f"{command} returned an invalid outcome",
        )
    try:
        if value_model is DeliveryReceiptsValue:
            value = value_model(receipts=tuple(raw.get("receipts") or ()))
        else:
            value = value_model(
                intent_id=raw.get("intent_id"),
                receipt_id=raw.get("receipt_id"),
                prepared_sha=raw.get("prepared_sha"),
            )
    except Exception as exc:
        return CommandResult(
            outcome="contract_violation",
            value=value_model(),
            summary=f"{command} result did not match its contract: {exc}",
        )
    return CommandResult(outcome=outcome, value=value, summary=str(raw.get("error") or outcome))


async def _promote_adapter(args: DeliveryPromoteArgs, ctx: CommandContext | None) -> CommandResult:
    return await _invoke_adapter(
        "delivery_promote",
        args,
        ctx,
        PromotionCommandValue,
        {"promoted", "already_promoted", "conflict", "source_moved", "target_moved"},
    )


async def _receipts_adapter(
    args: DeliveryReceiptsArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _invoke_adapter(
        "delivery_receipts", args, ctx, DeliveryReceiptsValue, {"found", "not_found"}
    )


async def _reconcile_adapter(
    args: IntegrationReconcilePromotionArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _invoke_adapter(
        "integration_reconcile_promotion",
        args,
        ctx,
        PromotionCommandValue,
        {"applied", "not_applied", "invariant_error"},
    )


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
    for contract, adapter in (
        (DELIVERY_PROMOTE, _promote_adapter),
        (DELIVERY_RECEIPTS, _receipts_adapter),
        (INTEGRATION_RECONCILE_PROMOTION, _reconcile_adapter),
    ):
        if registry.get(contract.name) is None:
            registry.register(CommandRegistration(contract.name, contract, adapter))
