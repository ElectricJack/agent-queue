"""Typed contract registration boundary for hierarchical integration commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

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
from src.git.manager import is_valid_git_oid


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
        "integration_repair_start",
        "integration_repair_dispatch",
        "integration_record_repair",
        "integration_repair_timeout",
        "integration_transfer_owner",
        "integration_mutate_hierarchy",
        "integration_reconcile_promotion",
        "integration_resolve_conflict",
        "integration_push_conflict_resolution",
        "integration_promote_main",
        "integration_release",
        "integration_cleanup",
    }
)


class IntegrationScheduleDueArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    now: float
    trigger: Literal["periodic", "manual"]


class IntegrationScheduleDueValue(CommandValue):
    project_id: str | None = None
    request_id: str | None = None
    trigger: Literal["periodic", "manual"] | None = None
    requested_at: float | None = None
    request_sequence: int | None = None
    next_due_at: float | None = None


class IntegrationSealArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    now: float | None = None


class IntegrationSealValue(CommandValue):
    project_id: str | None = None
    request_id: str | None = None
    batch_id: str | None = None
    operation_id: str | None = None


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


class IntegrationResolveConflictArgs(CommandArgs):
    intent_id: str
    operation_id: str
    resolved_head_sha: str
    resolved_tree_sha: str
    repair_commit_shas: tuple[str, ...] = Field(min_length=1)
    fence: Fence


class IntegrationPushConflictResolutionArgs(CommandArgs):
    intent_id: str
    fence: Fence


class IntegrationPromoteMainArgs(CommandArgs):
    batch_id: str = Field(min_length=1)
    revision: int = Field(ge=0)


class IntegrationPromoteMainValue(CommandValue):
    batch_id: str | None = None
    revision: int | None = None
    intent_id: str | None = None
    receipt_ids: tuple[str, ...] = ()
    head_sha: str | None = None


class IntegrationBuildCandidateArgs(CommandArgs):
    batch_id: str = Field(min_length=1)


class IntegrationBuildCandidateValue(CommandValue):
    batch_id: str | None = None
    revision: int | None = None
    operation_id: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    member_ordinal: int | None = None


class IntegrationCIEvidenceArgs(CommandArgs):
    batch_id: str = Field(min_length=1)
    revision: int = Field(ge=0)


class IntegrationCIEvidenceValue(CommandValue):
    batch_id: str | None = None
    revision: int | None = None
    evidence_ids: tuple[str, ...] = ()
    aggregate_evidence_id: str | None = None


class IntegrationReleaseArgs(CommandArgs):
    batch_id: str = Field(min_length=1)


class IntegrationReleaseValue(CommandValue):
    project_id: str | None = None
    batch_id: str | None = None
    request_id: str | None = None
    catchup_request_id: str | None = None
    operation_id: str | None = None


class IntegrationCleanupArgs(CommandArgs):
    batch_id: str = Field(min_length=1)


class IntegrationCleanupValue(CommandValue):
    batch_id: str | None = None
    item_count: int | None = None
    completed_count: int | None = None
    conflict_count: int | None = None


class IntegrationFileChildrenArgs(CommandArgs):
    parent_id: str
    children: list[dict[str, Any]]
    expected_generation: int


class IntegrationFileChildrenValue(CommandValue):
    generation: int | None = None
    children: tuple[dict[str, Any], ...] = ()
    origins: tuple[dict[str, Any], ...] = ()


class IntegrationCheckpointParentArgs(CommandArgs):
    task_id: str
    head_sha: str
    generation: int


class IntegrationCheckpointParentValue(CommandValue):
    task_id: str | None = None
    generation: int | None = None
    head_sha: str | None = None
    episode_id: str | None = None
    operation_id: str | None = None


class IntegrationDeliveryReadinessArgs(CommandArgs):
    task_id: str


class IntegrationDeliveryReadinessValue(CommandValue):
    task_id: str | None = None
    episode_id: str | None = None
    operation_id: str | None = None
    generation: int | None = None
    checkpoint_sha: str | None = None
    head_sha: str | None = None
    receipts: tuple[dict[str, Any], ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    required_checks: dict[str, Any] | None = None
    on_failed_child: Literal["block", "ask"] | None = None


class IntegrationParentVerifyArgs(CommandArgs):
    task_id: str
    generation: int
    head_sha: str
    evidence_ids: list[str]


class IntegrationParentVerifyValue(CommandValue):
    task_id: str | None = None
    generation: int | None = None
    head_sha: str | None = None
    verification_id: str | None = None


class IntegrationCompleteParentArgs(CommandArgs):
    task_id: str
    generation: int
    head_sha: str


class IntegrationCompleteParentValue(CommandValue):
    task_id: str | None = None
    generation: int | None = None
    head_sha: str | None = None
    operation_id: str | None = None


class IntegrationMutateHierarchyArgs(CommandArgs):
    task_id: str
    mutation: str
    arguments: dict[str, Any]


class IntegrationMutateHierarchyValue(CommandValue):
    task_id: str | None = None
    old_parent_id: str | None = None
    new_parent_id: str | None = None
    old_parent_generation: int | None = None
    new_parent_generation: int | None = None


class IntegrationRepairStartArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    starting_sha: str
    trigger_id: str = Field(min_length=1)

    @field_validator("starting_sha")
    @classmethod
    def exact_git_oid(cls, value: str) -> str:
        if not is_valid_git_oid(value):
            raise ValueError("starting_sha must be an exact lowercase Git OID")
        return value


class IntegrationRepairStartValue(CommandValue):
    operation_id: str | None = None
    stage: Literal[0, 1] | None = None
    starting_sha: str | None = None
    started_at: float | None = None
    deadline_at: float | None = None


class IntegrationRepairDispatchArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    stage: Literal[0, 1]


class IntegrationRepairDispatchValue(CommandValue):
    operation_id: str | None = None
    stage: Literal[0, 1] | None = None
    repair_task_id: str | None = None
    writer_kind: Literal["repair_delegate", "existing_verifier"] | None = None
    fence: Fence | None = None


class IntegrationRecordRepairArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)


class IntegrationRecordRepairValue(CommandValue):
    action: Literal[
        "repair",
        "infrastructure_retry",
        "inconclusive",
        "completion_ready",
        "dispatch_debug",
        "block_for_human",
        "duplicate",
        "stale",
    ] | None = None
    attempts: int | None = None
    stage: Literal[0, 1] | None = None


class IntegrationRepairTimeoutArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    stage: Literal[0, 1]


class IntegrationRepairTimeoutValue(CommandValue):
    operation_id: str | None = None
    stage: Literal[0, 1] | None = None
    action: Literal[
        "ignore",
        "dispatch_debug",
        "block_for_human",
        "none",
        "wait",
        "awaiting_promotion",
    ] | None = None


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


def _repair_contract(
    name: str,
    args_model: type[CommandArgs],
    result_model: type[CommandValue],
    outcomes: tuple[str, ...],
    *,
    effects,
    sensitive_args: frozenset[str] = frozenset(),
    sensitive_results: frozenset[str] = frozenset(),
) -> CommandContract:
    successes = {
        "started",
        "already_started",
        "dispatched",
        "already_dispatched",
        "writer_reused",
        "continue",
        "escalate",
        "expired",
        "not_due",
        "already_terminal",
    }
    return CommandContract(
        execution=ExecutionContract(
            name=name,
            args_model=args_model,
            result_model=result_model,
            outcomes=tuple(
                OutcomeSpec(
                    name=outcome,
                    classification=(
                        OutcomeClass.SUCCESS
                        if outcome in successes
                        else OutcomeClass.FAILURE
                    ),
                )
                for outcome in outcomes
            ),
            capability=name,
            side_effect=SideEffectClass.COMPOSITE,
            idempotency=IdempotencySpec(mode="natural"),
            retry_safe=True,
            effects=effects,
            sensitive_args=sensitive_args,
            sensitive_result_fields=sensitive_results,
            receipt_projection=tuple(result_model.model_fields),
        ),
        presentation=CommandPresentation(
            title=name.replace("_", " ").title(), summary=""
        ),
    )


INTEGRATION_REPAIR_START = _repair_contract(
    "integration_repair_start",
    IntegrationRepairStartArgs,
    IntegrationRepairStartValue,
    ("started", "already_started", "stale", "invariant_error"),
    effects=(
        CreateOrReuseClause(
            subject=EffectSubject.INTEGRATION_OPERATION, key_arg="operation_id"
        ),
        UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),
    ),
    sensitive_args=frozenset({"starting_sha"}),
    sensitive_results=frozenset({"starting_sha"}),
)

INTEGRATION_REPAIR_DISPATCH = _repair_contract(
    "integration_repair_dispatch",
    IntegrationRepairDispatchArgs,
    IntegrationRepairDispatchValue,
    (
        "dispatched",
        "already_dispatched",
        "writer_reused",
        "busy",
        "configuration_blocked",
        "stale",
        "human_required",
    ),
    effects=(
        UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),
        UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
        CreateOrReuseClause(
            subject=EffectSubject.TASK_EXECUTION, key_arg="operation_id"
        ),
    ),
    sensitive_results=frozenset({"fence"}),
)

INTEGRATION_RECORD_REPAIR = _repair_contract(
    "integration_record_repair",
    IntegrationRecordRepairArgs,
    IntegrationRecordRepairValue,
    ("continue", "escalate", "human_required", "budget_exhausted"),
    effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
)

INTEGRATION_REPAIR_TIMEOUT = _repair_contract(
    "integration_repair_timeout",
    IntegrationRepairTimeoutArgs,
    IntegrationRepairTimeoutValue,
    ("expired", "not_due", "already_terminal", "stale"),
    effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
)


INTEGRATION_SCHEDULE_DUE = CommandContract(
    execution=ExecutionContract(
        name="integration_schedule_due",
        args_model=IntegrationScheduleDueArgs,
        result_model=IntegrationScheduleDueValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS
                    if name in {"due", "not_due", "coalesced"}
                    else OutcomeClass.FAILURE
                ),
            )
            for name in ("due", "not_due", "coalesced", "disabled")
        ),
        capability="integration_schedule_due",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        receipt_projection=tuple(IntegrationScheduleDueValue.model_fields),
    ),
    presentation=CommandPresentation(
        title="Schedule integration sweep",
        summary="Coalesce a periodic or manual trigger into one durable sweep request.",
    ),
)


INTEGRATION_SEAL = CommandContract(
    execution=ExecutionContract(
        name="integration_seal",
        args_model=IntegrationSealArgs,
        result_model=IntegrationSealValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS
                    if name in {"sealed", "empty"}
                    else OutcomeClass.FAILURE
                ),
            )
            for name in ("sealed", "empty", "busy")
        ),
        capability="integration_seal",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        receipt_projection=tuple(IntegrationSealValue.model_fields),
    ),
    presentation=CommandPresentation(
        title="Seal integration frontier",
        summary="Atomically snapshot the full eligible integration frontier.",
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


INTEGRATION_RESOLVE_CONFLICT = CommandContract(
    execution=ExecutionContract(
        name="integration_resolve_conflict",
        args_model=IntegrationResolveConflictArgs,
        result_model=PromotionCommandValue,
        outcomes=(
            OutcomeSpec(name="reserved", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="already_reserved", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="stale", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="invariant_error", classification=OutcomeClass.FAILURE),
        ),
        capability="integration_resolve_conflict",
        side_effect=SideEffectClass.CREATE,
        idempotency=IdempotencySpec(mode="keyed", key_field="intent_id"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        sensitive_args=frozenset(
            {"resolved_head_sha", "resolved_tree_sha", "repair_commit_shas"}
        ),
        sensitive_result_fields=frozenset({"prepared_sha"}),
        receipt_projection=("intent_id", "receipt_id"),
    ),
    presentation=CommandPresentation(
        title="Reserve conflict resolution",
        summary="Freeze an active repair session's exact conflict resolution before push.",
    ),
)


INTEGRATION_PUSH_CONFLICT_RESOLUTION = CommandContract(
    execution=ExecutionContract(
        name="integration_push_conflict_resolution",
        args_model=IntegrationPushConflictResolutionArgs,
        result_model=PromotionCommandValue,
        outcomes=(
            OutcomeSpec(name="pushed", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="already_applied", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="target_moved", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="stale", classification=OutcomeClass.FAILURE),
        ),
        capability="integration_push_conflict_resolution",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="keyed", key_field="intent_id"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
            UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),
        ),
        sensitive_args=frozenset({"fence"}),
        sensitive_result_fields=frozenset({"prepared_sha"}),
        receipt_projection=("intent_id", "receipt_id"),
    ),
    presentation=CommandPresentation(
        title="Push conflict resolution",
        summary="Push a frozen conflict resolution under the current repair writer fence.",
    ),
)


INTEGRATION_PROMOTE_MAIN = CommandContract(
    execution=ExecutionContract(
        name="integration_promote_main",
        args_model=IntegrationPromoteMainArgs,
        result_model=IntegrationPromoteMainValue,
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
                "base_moved",
                "ci_missing",
                "non_fast_forward",
                "wait",
                "reconciliation_blocked",
                "stale",
                "configuration_blocked",
            )
        ),
        capability="integration_promote_main",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),
            UpdateClause(subject=EffectSubject.DELIVERY_EVIDENCE),
        ),
        sensitive_result_fields=frozenset({"head_sha"}),
        receipt_projection=tuple(IntegrationPromoteMainValue.model_fields),
    ),
    presentation=CommandPresentation(
        title="Promote exact root candidate",
        summary="Reconcile and fast-forward main to the exact trusted green candidate.",
    ),
)


INTEGRATION_CLEANUP = CommandContract(
    execution=ExecutionContract(
        name="integration_cleanup",
        args_model=IntegrationCleanupArgs,
        result_model=IntegrationCleanupValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS
                    if name in {"materialized", "advanced", "complete", "already_complete"}
                    else OutcomeClass.FAILURE
                ),
            )
            for name in (
                "materialized",
                "advanced",
                "complete",
                "already_complete",
                "wait",
                "retryable",
                "conflict",
                "failed",
                "stale",
                "invariant_error",
            )
        ),
        capability="integration_cleanup",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        receipt_projection=tuple(IntegrationCleanupValue.model_fields),
    ),
    presentation=CommandPresentation(
        title="Advance integration cleanup",
        summary="Materialize and advance bounded cleanup for one terminal root batch.",
    ),
)


def _root_subject_contract(
    name: str,
    args_model: type[CommandArgs],
    value_model: type[CommandValue],
    outcomes: tuple[str, ...],
    successes: frozenset[str],
    title: str,
) -> CommandContract:
    return CommandContract(
        execution=ExecutionContract(
            name=name,
            args_model=args_model,
            result_model=value_model,
            outcomes=tuple(
                OutcomeSpec(
                    name=outcome,
                    classification=(
                        OutcomeClass.SUCCESS
                        if outcome in successes
                        else OutcomeClass.FAILURE
                    ),
                )
                for outcome in outcomes
            ),
            capability=name,
            side_effect=SideEffectClass.COMPOSITE,
            idempotency=IdempotencySpec(mode="natural"),
            retry_safe=True,
            effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
            receipt_projection=tuple(value_model.model_fields),
        ),
        presentation=CommandPresentation(title=title, summary=title),
    )


INTEGRATION_BUILD_CANDIDATE = _root_subject_contract(
    "integration_build_candidate",
    IntegrationBuildCandidateArgs,
    IntegrationBuildCandidateValue,
    (
        "empty", "built", "already_built", "conflict", "source_moved", "base_moved",
        "stale_revision", "wait", "human_required", "configuration_blocked",
    ),
    frozenset({"empty", "built", "already_built"}),
    "Build exact root candidate",
)

INTEGRATION_CI_EVIDENCE = _root_subject_contract(
    "integration_ci_evidence",
    IntegrationCIEvidenceArgs,
    IntegrationCIEvidenceValue,
    (
        "green", "red", "not_green", "full_suite_required", "stale_subject",
        "configuration_blocked",
    ),
    frozenset({"green"}),
    "Observe exact root candidate CI",
)

INTEGRATION_RELEASE = _root_subject_contract(
    "integration_release",
    IntegrationReleaseArgs,
    IntegrationReleaseValue,
    ("released", "already_released", "empty", "wait", "stale", "invariant_error"),
    frozenset({"released", "already_released", "empty"}),
    "Release terminal root train",
)


INTEGRATION_FILE_CHILDREN = CommandContract(
    execution=ExecutionContract(
        name="integration_file_children",
        args_model=IntegrationFileChildrenArgs,
        result_model=IntegrationFileChildrenValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(OutcomeClass.SUCCESS if name == "filed" else OutcomeClass.FAILURE),
            )
            for name in ("filed", "stale_parent", "invalid")
        ),
        capability="integration_file_children",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.TASK_GRAPH),
            UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
        ),
        receipt_projection=("generation", "children", "origins"),
    ),
    presentation=CommandPresentation(
        title="File isolated child tasks",
        summary="Reserve child origins and advance the parent integration generation atomically.",
    ),
)


INTEGRATION_CHECKPOINT_PARENT = CommandContract(
    execution=ExecutionContract(
        name="integration_checkpoint_parent",
        args_model=IntegrationCheckpointParentArgs,
        result_model=IntegrationCheckpointParentValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS
                    if name in {"checkpointed", "already_waiting"}
                    else OutcomeClass.FAILURE
                ),
            )
            for name in ("checkpointed", "already_waiting", "dirty", "stale")
        ),
        capability="integration_checkpoint_parent",
        side_effect=SideEffectClass.UPDATE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.TASK),),
        sensitive_args=frozenset({"head_sha"}),
        sensitive_result_fields=frozenset({"head_sha"}),
        receipt_projection=("task_id", "generation", "head_sha"),
    ),
    presentation=CommandPresentation(
        title="Checkpoint integration parent",
        summary="Pin the parent head and generation before waiting for child deliveries.",
    ),
)


def _parent_contract(name, args_model, result_model, outcomes, *, side_effect):
    return CommandContract(
        execution=ExecutionContract(
            name=name,
            args_model=args_model,
            result_model=result_model,
            outcomes=tuple(
                OutcomeSpec(
                    name=outcome,
                    classification=(
                        OutcomeClass.SUCCESS
                        if outcome in {"ready", "verified", "completed"}
                        else OutcomeClass.FAILURE
                    ),
                )
                for outcome in outcomes
            ),
            capability=name,
            side_effect=side_effect,
            idempotency=IdempotencySpec(mode="natural"),
            retry_safe=True,
            effects=(
                ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE)
                if side_effect is SideEffectClass.READ
                else UpdateClause(subject=EffectSubject.TASK)
            ,),
            sensitive_args=frozenset(
                {"head_sha", "evidence_ids"} & set(args_model.model_fields)
            ),
            sensitive_result_fields=frozenset(
                {"head_sha", "checkpoint_sha"} & set(result_model.model_fields)
            ),
            receipt_projection=tuple(result_model.model_fields),
        ),
        presentation=CommandPresentation(title=name.replace("_", " ").title(), summary=""),
    )


INTEGRATION_DELIVERY_READINESS = _parent_contract(
    "integration_delivery_readiness",
    IntegrationDeliveryReadinessArgs,
    IntegrationDeliveryReadinessValue,
    ("ready", "waiting", "failed", "invariant_error"),
    side_effect=SideEffectClass.READ,
)
INTEGRATION_PARENT_VERIFY = _parent_contract(
    "integration_parent_verify",
    IntegrationParentVerifyArgs,
    IntegrationParentVerifyValue,
    ("verified", "stale_generation", "stale_head", "invalid_evidence"),
    side_effect=SideEffectClass.UPDATE,
)
INTEGRATION_COMPLETE_PARENT = _parent_contract(
    "integration_complete_parent",
    IntegrationCompleteParentArgs,
    IntegrationCompleteParentValue,
    ("completed", "waiting", "stale_verification", "invariant_error"),
    side_effect=SideEffectClass.UPDATE,
)


INTEGRATION_MUTATE_HIERARCHY = CommandContract(
    execution=ExecutionContract(
        name="integration_mutate_hierarchy",
        args_model=IntegrationMutateHierarchyArgs,
        result_model=IntegrationMutateHierarchyValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(OutcomeClass.SUCCESS if name == "updated" else OutcomeClass.FAILURE),
            )
            for name in ("updated", "sealed", "delivery_target_fixed", "reopen_required", "invalid")
        ),
        capability="integration_mutate_hierarchy",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.TASK_GRAPH),
            UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
        ),
        receipt_projection=(
            "task_id",
            "old_parent_id",
            "new_parent_id",
            "old_parent_generation",
            "new_parent_generation",
        ),
    ),
    presentation=CommandPresentation(
        title="Mutate integration hierarchy",
        summary="Apply a guarded hierarchy change and invalidate affected parent generations.",
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


async def _resolve_conflict_adapter(
    args: IntegrationResolveConflictArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _invoke_adapter(
        "integration_resolve_conflict",
        args,
        ctx,
        PromotionCommandValue,
        {"reserved", "already_reserved", "unauthorized", "stale", "invariant_error"},
    )


async def _push_conflict_resolution_adapter(
    args: IntegrationPushConflictResolutionArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _invoke_adapter(
        "integration_push_conflict_resolution",
        args,
        ctx,
        PromotionCommandValue,
        {
            "pushed",
            "already_applied",
            "target_moved",
            "stale",
            "unauthorized",
            "runtime_error",
        },
    )


async def _promote_main_adapter(
    args: IntegrationPromoteMainArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _hierarchy_adapter(
        "integration_promote_main",
        args,
        ctx,
        IntegrationPromoteMainValue,
        {
            "promoted",
            "already_promoted",
            "base_moved",
            "ci_missing",
            "non_fast_forward",
            "wait",
            "reconciliation_blocked",
            "stale",
            "configuration_blocked",
        },
    )


async def _cleanup_adapter(
    args: IntegrationCleanupArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _hierarchy_adapter(
        "integration_cleanup",
        args,
        ctx,
        IntegrationCleanupValue,
        {
            "materialized",
            "advanced",
            "complete",
            "already_complete",
            "wait",
            "retryable",
            "conflict",
            "failed",
            "stale",
            "invariant_error",
        },
    )


async def _build_candidate_adapter(
    args: IntegrationBuildCandidateArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _hierarchy_adapter(
        "integration_build_candidate",
        args,
        ctx,
        IntegrationBuildCandidateValue,
        {
            "empty", "built", "already_built", "conflict", "source_moved", "base_moved",
            "stale_revision", "wait", "human_required", "configuration_blocked",
        },
    )


async def _ci_evidence_adapter(
    args: IntegrationCIEvidenceArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _hierarchy_adapter(
        "integration_ci_evidence",
        args,
        ctx,
        IntegrationCIEvidenceValue,
        {
            "green", "red", "not_green", "full_suite_required", "stale_subject",
            "configuration_blocked",
        },
    )


async def _release_adapter(
    args: IntegrationReleaseArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _hierarchy_adapter(
        "integration_release",
        args,
        ctx,
        IntegrationReleaseValue,
        {"released", "already_released", "empty", "wait", "stale", "invariant_error"},
    )


async def _hierarchy_adapter(
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
    fields = set(value_model.model_fields)
    try:
        value = value_model(**{key: raw[key] for key in fields if key in raw})
    except Exception as exc:
        return CommandResult(
            outcome="contract_violation",
            value=value_model(),
            summary=f"{command} result did not match its contract: {exc}",
        )
    return CommandResult(outcome=outcome, value=value, summary=str(raw.get("error") or outcome))


async def _file_children_adapter(args: IntegrationFileChildrenArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_file_children",
        args,
        ctx,
        IntegrationFileChildrenValue,
        {"filed", "stale_parent", "invalid"},
    )


async def _checkpoint_parent_adapter(
    args: IntegrationCheckpointParentArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_checkpoint_parent",
        args,
        ctx,
        IntegrationCheckpointParentValue,
        {"checkpointed", "already_waiting", "dirty", "stale"},
    )


async def _mutate_hierarchy_adapter(
    args: IntegrationMutateHierarchyArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_mutate_hierarchy",
        args,
        ctx,
        IntegrationMutateHierarchyValue,
        {"updated", "sealed", "delivery_target_fixed", "reopen_required", "invalid"},
    )


async def _delivery_readiness_adapter(
    args: IntegrationDeliveryReadinessArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_delivery_readiness",
        args,
        ctx,
        IntegrationDeliveryReadinessValue,
        {"ready", "waiting", "failed", "invariant_error"},
    )


async def _parent_verify_adapter(args: IntegrationParentVerifyArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_parent_verify",
        args,
        ctx,
        IntegrationParentVerifyValue,
        {"verified", "stale_generation", "stale_head", "invalid_evidence"},
    )


async def _complete_parent_adapter(
    args: IntegrationCompleteParentArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_complete_parent",
        args,
        ctx,
        IntegrationCompleteParentValue,
        {"completed", "waiting", "stale_verification", "invariant_error"},
    )


async def _repair_start_adapter(
    args: IntegrationRepairStartArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_repair_start",
        args,
        ctx,
        IntegrationRepairStartValue,
        {"started", "already_started", "stale", "invariant_error"},
    )


async def _repair_dispatch_adapter(
    args: IntegrationRepairDispatchArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_repair_dispatch",
        args,
        ctx,
        IntegrationRepairDispatchValue,
        {
            "dispatched",
            "already_dispatched",
            "writer_reused",
            "busy",
            "configuration_blocked",
            "stale",
            "human_required",
        },
    )


async def _record_repair_adapter(
    args: IntegrationRecordRepairArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_record_repair",
        args,
        ctx,
        IntegrationRecordRepairValue,
        {"continue", "escalate", "human_required", "budget_exhausted"},
    )


async def _repair_timeout_adapter(
    args: IntegrationRepairTimeoutArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_repair_timeout",
        args,
        ctx,
        IntegrationRepairTimeoutValue,
        {"expired", "not_due", "already_terminal", "stale"},
    )


async def _schedule_due_adapter(
    args: IntegrationScheduleDueArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_schedule_due",
        args,
        ctx,
        IntegrationScheduleDueValue,
        {"due", "not_due", "coalesced", "disabled"},
    )


async def _seal_adapter(args: IntegrationSealArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_seal",
        args,
        ctx,
        IntegrationSealValue,
        {"sealed", "empty", "busy"},
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
        (INTEGRATION_SCHEDULE_DUE, _schedule_due_adapter),
        (INTEGRATION_SEAL, _seal_adapter),
        (INTEGRATION_FILE_CHILDREN, _file_children_adapter),
        (INTEGRATION_CHECKPOINT_PARENT, _checkpoint_parent_adapter),
        (INTEGRATION_MUTATE_HIERARCHY, _mutate_hierarchy_adapter),
        (INTEGRATION_DELIVERY_READINESS, _delivery_readiness_adapter),
        (INTEGRATION_PARENT_VERIFY, _parent_verify_adapter),
        (INTEGRATION_COMPLETE_PARENT, _complete_parent_adapter),
        (DELIVERY_PROMOTE, _promote_adapter),
        (DELIVERY_RECEIPTS, _receipts_adapter),
        (INTEGRATION_RECONCILE_PROMOTION, _reconcile_adapter),
        (INTEGRATION_RESOLVE_CONFLICT, _resolve_conflict_adapter),
        (INTEGRATION_PUSH_CONFLICT_RESOLUTION, _push_conflict_resolution_adapter),
        (INTEGRATION_PROMOTE_MAIN, _promote_main_adapter),
        (INTEGRATION_CLEANUP, _cleanup_adapter),
        (INTEGRATION_BUILD_CANDIDATE, _build_candidate_adapter),
        (INTEGRATION_CI_EVIDENCE, _ci_evidence_adapter),
        (INTEGRATION_RELEASE, _release_adapter),
        (INTEGRATION_REPAIR_START, _repair_start_adapter),
        (INTEGRATION_REPAIR_DISPATCH, _repair_dispatch_adapter),
        (INTEGRATION_RECORD_REPAIR, _record_repair_adapter),
        (INTEGRATION_REPAIR_TIMEOUT, _repair_timeout_adapter),
    ):
        if registry.get(contract.name) is None:
            registry.register(CommandRegistration(contract.name, contract, adapter))
