"""Typed contract registration boundary for hierarchical integration commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from src.commands.contracts.models import (
    ClausePredicate,
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
from src.integration.models import AWAITING_TRUSTED_VERIFICATION, BranchKey, Fence
from src.git.manager import is_valid_git_oid


DESIGN_INTEGRATION_COMMANDS = frozenset(
    {
        "integration_schedule_due",
        "integration_file_children",
        "integration_checkpoint_parent",
        "integration_delivery_readiness",
        "integration_record_noop",
        "integration_record_root_noop",
        "integration_record_delivered",
        "integration_quiesce",
        "integration_retire_legacy_park",
        "integration_migrate_provenance_refs",
        "integration_reconcile_expired_mutation",
        "integration_parent_verify",
        "integration_complete_parent",
        "delivery_promote",
        "delivery_receipts",
        "integration_seal",
        "integration_build_candidate",
        "integration_repair_close_current",
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
        "integration_recover_candidate_member",
        "integration_recover_unwritten_resolution",
        "integration_promote_main",
        "integration_promotion_publish",
        "integration_release",
        "integration_cleanup",
        "integration_status",
        "integration_cutover_plan",
        "integration_cutover",
        "integration_abort_batch",
        "integration_pause_batch",
        "integration_resume_batch",
        "integration_seal_now",
        "integration_retire_origin",
        "integration_refresh_epic",
        "integration_trust_manifest",
        "integration_app_verify",
        "integration_eject",
        "integration_reevaluate_repair",
        "integration_release_owner",
        "integration_reserve_owner",
        "integration_release_stale_owners",
        "integration_redrive_root",
        "integration_authorize_root",
        "integration_redrive_child",
        "integration_reopen_collection",
        "integration_rebind_repair",
        "integration_rebind_detached_repair",
        "integration_recover_preserved_repair",
        "integration_recover_parent_head",
        "integration_close_delivered_pr",
        "integration_resolve_candidate_member",
    }
)


class IntegrationScheduleDueArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    now: float
    trigger: Literal["periodic", "manual"]


class IntegrationStatusArgs(CommandArgs):
    project_id: str = Field(min_length=1)


class PromoteSchemaArgs(CommandArgs):
    pass


class PromoteSchemaValue(CommandValue):
    # Avoid shadowing BaseModel.schema while preserving the command wire field.
    schema_document: dict[str, Any] = Field(default_factory=dict, alias="schema")


class PromoteValidateArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    #: The session scope gate injects these before the handler validates arguments.
    task_id: str | None = None
    session_id: str | None = None
    flow: Any = None
    #: False when flow supplies a document, including explicit null.
    use_stored: bool = True
    remote: bool = False


class PromoteValidateValue(CommandValue):
    project_id: str | None = None
    valid: bool = False
    flow: list[dict[str, Any]] | None = None
    layer: int | None = None
    problems: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()
    protection: dict[str, Any] | None = None
    workflow_triggers: dict[str, Any] | None = None


class PromoteRulesetsValue(PromoteValidateValue):
    app_id: int | None = None
    rulesets: tuple[dict[str, Any], ...] = ()


class IntegrationPromotionPublishArgs(CommandArgs):
    batch_id: str = Field(min_length=1)


class IntegrationPromotionPublishValue(CommandValue):
    batch_id: str | None = None
    source_sha: str | None = None
    target_sha: str | None = None
    detail: dict[str, Any] | None = None


class IntegrationAbortBatchArgs(CommandArgs):
    batch_id: str = Field(min_length=1)
    reason: str = ""
    dry_run: bool = True


class IntegrationCutoverArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    flow: Any = None
    allow_epics: bool = False
    reverse: bool = False
    dry_run: bool = True
    expected_generation: StrictInt | None = Field(default=None, ge=0)
    baseline: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_saved_plan(self):
        if not self.dry_run and self.baseline is None:
            raise ValueError("apply requires the saved cutover-plan baseline")
        return self


class IntegrationCutoverValue(CommandValue):
    project_id: str | None = None
    generation: int | None = None
    dry_run: bool | None = None
    fields: tuple[str, ...] = ()
    plan: dict[str, Any] | None = None
    blockers: tuple[dict[str, Any], ...] = ()


class IntegrationRetireOriginArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    origin_id: str | None = Field(default=None, min_length=1)
    reason: str = ""
    dry_run: bool = True


class IntegrationRefreshEpicArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    dry_run: bool = True


class IntegrationSealNowArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    dry_run: bool = True


class IntegrationTrainControlValue(CommandValue):
    project_id: str | None = None
    batch_id: str | None = None
    task_id: str | None = None
    origin_id: str | None = None
    target_ref: str | None = None
    candidate_sha: str | None = None
    target_sha: str | None = None
    intent: str | None = None
    dry_run: bool | None = None
    replacement_batch_id: str | None = None
    members: tuple[str, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()


class IntegrationRefreshEpicValue(IntegrationTrainControlValue):
    default_ref: str | None = None
    default_sha: str | None = None
    ahead: int | None = None
    behind: int | None = None
    state: str | None = None
    detail: dict[str, Any] | None = None


class IntegrationStatusReadArgs(IntegrationStatusArgs):
    control_only: bool = False


class IntegrationTrustManifestArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    #: A policy document to build from instead of the bound one (``--policy FILE``).
    policy: dict[str, Any] | None = None
    #: Defaults to the project's designated integration repository.
    repository_id: str | None = Field(default=None, min_length=1)


class IntegrationTrustManifestValue(CommandValue):
    project_id: str | None = None
    repository_id: str | None = None
    policy_source: Literal["argument", "bound"] | None = None
    github_repository_id: int | None = None
    full_name: str | None = None
    attestation_app_id: int | None = None
    path: str | None = None
    manifest: dict[str, Any] | None = None
    text: str | None = None
    sha256: str | None = None
    committed: dict[str, Any] | None = None


class IntegrationAppVerifyArgs(IntegrationTrustManifestArgs):
    repository_access_only: bool = False
    #: Where the caller read ``policy`` from; only repeated in fix commands.
    policy_path: str | None = Field(default=None, min_length=1)


class IntegrationAppVerifyValue(CommandValue):
    project_id: str | None = None
    repository_id: str | None = None
    policy_source: Literal["argument", "bound"] | None = None
    github_repository_id: int | None = None
    full_name: str | None = None
    attestation_app_id: int | None = None
    default_branch: str | None = None
    #: No item is ``fail``.
    ready: bool | None = None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: One per concern: ``id``, ``status``, ``code``, ``expected``, ``observed``, ``fix``.
    items: tuple[dict[str, Any], ...] = ()
    #: The manifest text, the two variable values and the target ruleset.
    expected: dict[str, Any] | None = None


class IntegrationEjectArgs(CommandArgs):
    batch_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    reason: str = ""
    dry_run: bool = True


class IntegrationReleaseOwnerArgs(CommandArgs):
    task_id: str | None = Field(default=None, min_length=1)
    owner_row_id: str | None = Field(default=None, min_length=1)
    dry_run: bool = False

    @model_validator(mode="after")
    def exactly_one_target(self) -> "IntegrationReleaseOwnerArgs":
        if (self.task_id is None) == (self.owner_row_id is None):
            raise ValueError("exactly one of task_id or owner_row_id is required")
        return self


class IntegrationReserveOwnerArgs(CommandArgs):
    task_id: str = Field(min_length=1)


class IntegrationReleaseStaleOwnersArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    dry_run: bool = False
    #: Only rows unchanged for at least this long (``90s``, ``30m``, ``4h``, ``2d``).
    older_than: str | None = Field(default=None, min_length=1)

    @field_validator("older_than")
    @classmethod
    def older_than_is_a_duration(cls, value: str | None) -> str | None:
        if value is not None:
            # Imported here: the provider command module is not a contract leaf.
            from src.commands.provider_commands import parse_duration

            parse_duration(value)
        return value


class IntegrationRedriveRootArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    #: Diagnose only.  Applying needs the head the dry run reported and a reason.
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("expected_head_sha")
    @classmethod
    def expected_head_is_a_commit(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_git_oid(value):
            raise ValueError("expected_head_sha must be a full commit id")
        return value

    @model_validator(mode="after")
    def applying_names_the_head_and_a_reason(self) -> IntegrationRedriveRootArgs:
        if not self.dry_run and (
            self.expected_head_sha is None or self.reason is None or not self.reason.strip()
        ):
            raise ValueError("applying requires expected_head_sha and reason")
        return self




class IntegrationAuthorizeRootArgs(IntegrationRedriveRootArgs):
    """Dry run by default; applying names the dry run's exact head and a reason."""


class IntegrationRedriveChildArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    #: Diagnose only.  Applying needs the head the dry run reported and a reason.
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("expected_head_sha")
    @classmethod
    def expected_head_is_a_commit(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_git_oid(value):
            raise ValueError("expected_head_sha must be a full commit id")
        return value

    @model_validator(mode="after")
    def applying_names_the_head_and_a_reason(self) -> IntegrationRedriveChildArgs:
        if not self.dry_run and (
            self.expected_head_sha is None or self.reason is None or not self.reason.strip()
        ):
            raise ValueError("applying requires expected_head_sha and reason")
        return self


class IntegrationReopenCollectionArgs(IntegrationRedriveChildArgs):
    """Dry run by default; applying names the parent branch head the dry run reported."""






class IntegrationRebindRepairArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    dry_run: bool = True
    expected_head_sha: str | None = None

    @model_validator(mode="after")
    def apply_requires_proved_head(self) -> IntegrationRebindRepairArgs:
        if self.expected_head_sha is not None and not is_valid_git_oid(self.expected_head_sha):
            raise ValueError("expected_head_sha must be a full commit id")
        if not self.dry_run and self.expected_head_sha is None:
            raise ValueError("apply requires the candidate head from dry-run")
        return self


class IntegrationRebindRepairValue(CommandValue):
    task_id: str | None = None
    intent_id: str | None = None
    head_sha: str | None = None
    tree_sha: str | None = None
    repair_commit_shas: tuple[str, ...] = ()
    fence_token: int | None = None
    session_id: str | None = None
    reason: str | None = None
    next_step: str | None = None


class IntegrationRecoverParentHeadArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    head_sha: str
    dry_run: bool = True
    expected_episode_id: str | None = None
    expected_generation: int | None = Field(default=None, ge=0)
    expected_stage: int | None = Field(default=None, ge=0)
    expected_fence_token: int | None = Field(default=None, ge=1)
    reason: str | None = None

    @model_validator(mode="after")
    def require_exact_recovery(self) -> IntegrationRecoverParentHeadArgs:
        if not is_valid_git_oid(self.head_sha):
            raise ValueError("head_sha must be a full commit id")
        if not self.dry_run and (
            not self.expected_episode_id or self.expected_generation is None
            or self.expected_stage is None or self.expected_fence_token is None
            or not (self.reason or "").strip()
        ):
            raise ValueError("apply requires the previewed episode, generation, stage, fence and reason")
        return self


class IntegrationRecoverParentHeadValue(CommandValue):
    operation_id: str | None = None
    head_sha: str | None = None
    episode_id: str | None = None
    generation: int | None = None
    stage: int | None = None
    fence_token: int | None = None
    receipt_head_sha: str | None = None
    repair_task_id: str | None = None
    completion_id: str | None = None
    repair_head_sha: str | None = None
    collection_receipt_ids: tuple[str, ...] = ()
    attempts: int | None = None
    deadline_at: float | None = None
    stage_state: str | None = None
    operation_state: str | None = None
    apply_command: str | None = None
    reason: str | None = None


class IntegrationRecoverPreservedRepairArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    candidate_sha: str
    dry_run: bool = True
    expected_stage: int | None = Field(default=None, ge=1)
    expected_released_fence: int | None = Field(default=None, ge=1)
    reason: str | None = None

    @model_validator(mode="after")
    def require_exact_recovery(self) -> IntegrationRecoverPreservedRepairArgs:
        if not is_valid_git_oid(self.candidate_sha):
            raise ValueError("candidate_sha must be a full commit id")
        if not self.dry_run and (
            self.expected_stage is None or self.expected_released_fence is None
            or not (self.reason or "").strip()
        ):
            raise ValueError("apply requires the previewed stage, released fence and a reason")
        return self


class IntegrationRecoverPreservedRepairValue(CommandValue):
    apply_command: str | None = None
    release_id: str | None = None
    operation_id: str | None = None
    stage: int | None = None
    intent_id: str | None = None
    candidate_sha: str | None = None
    expected_target: str | None = None
    source_head: str | None = None
    tree_sha: str | None = None
    parents: tuple[str, ...] = ()
    repair_commit_shas: tuple[str, ...] = ()
    remote_head_sha: str | None = None
    preserved_ref: str | None = None
    released_fence: int | None = None
    deadline_at: float | None = None
    attempts: int | None = None
    remaining_attempts: int | None = None
    reason: str | None = None
    next_step: str | None = None


class IntegrationRebindDetachedRepairArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    dry_run: bool = True
    expected_stage: int | None = Field(default=None, ge=0)
    expected_remote_head_sha: str | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def apply_requires_proved_identity(self) -> IntegrationRebindDetachedRepairArgs:
        if self.expected_remote_head_sha is not None and not is_valid_git_oid(
            self.expected_remote_head_sha
        ):
            raise ValueError("expected_remote_head_sha must be a full commit id")
        if not self.dry_run and (
            self.expected_stage is None
            or self.expected_remote_head_sha is None
            or not (self.reason or "").strip()
        ):
            raise ValueError("apply requires the stage and remote head from dry-run and a reason")
        return self


class IntegrationRebindDetachedRepairValue(CommandValue):
    operation_id: str | None = None
    stage: int | None = None
    repair_task_id: str | None = None
    intent_id: str | None = None
    frozen_head_sha: str | None = None
    remote_head_sha: str | None = None
    frozen_head_refs: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()
    fence_token: int | None = None
    deadline_at: float | None = None
    delegate_status: str | None = None
    dispatch_outcome: str | None = None
    reason: str | None = None
    next_step: str | None = None


class IntegrationCloseDeliveredPrArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    #: Prove only.  Closing needs the head the dry run reported and a reason.
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("expected_head_sha")
    @classmethod
    def expected_head_is_a_commit(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_git_oid(value):
            raise ValueError("expected_head_sha must be a full commit id")
        return value

    @model_validator(mode="after")
    def applying_names_the_head_and_a_reason(self) -> IntegrationCloseDeliveredPrArgs:
        if not self.dry_run and (
            self.expected_head_sha is None or self.reason is None or not self.reason.strip()
        ):
            raise ValueError("applying requires expected_head_sha and reason")
        return self


class IntegrationRecoverCandidateMemberArgs(CommandArgs):
    reservation_id: str = Field(min_length=1)


class IntegrationRecoverCandidateMemberValue(CommandValue):
    batch_id: str | None = None
    revision: int | None = None
    member_ordinal: int | None = None
    invariant: str | None = None


class IntegrationOperationalValue(CommandValue):
    id: str | None = None
    head_sha: str | None = None
    recovered_task_id: str | None = None
    source_sha: str | None = None
    manifest: tuple[dict[str, Any], ...] = ()
    evidence: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    deliveries: tuple[dict[str, Any], ...] = ()
    pending_publications: tuple[str, ...] = ()
    parked: tuple[Any, ...] = ()
    preserved_owners: tuple[str, ...] = ()
    released_delegates: tuple[str, ...] = ()
    archived_delegates: tuple[str, ...] = ()
    project_id: str | None = None
    operation_id: str | None = None
    batch_id: str | None = None
    task_id: str | None = None
    effective_mode: str | None = None
    desired_mode: str | None = None
    mode: str | None = None
    generation: int | None = None
    draining: bool | None = None
    ready: bool | None = None
    rollout_ready: bool | None = None
    blockers: tuple[dict[str, Any], ...] = ()
    blocker_digest: str | None = None
    certification: dict[str, Any] | None = None
    repository_id: str | None = None
    schedule: dict[str, Any] | None = None
    active_batch: dict[str, Any] | None = None
    members: tuple[dict[str, Any], ...] = ()
    parent_readiness: tuple[dict[str, Any], ...] = ()
    ownership: tuple[dict[str, Any], ...] = ()
    lease: dict[str, Any] | None = None
    repair: tuple[dict[str, Any], ...] = ()
    ci_evidence: tuple[dict[str, Any], ...] = ()
    promotion: tuple[dict[str, Any], ...] = ()
    reconciliation: dict[str, Any] | None = None
    cleanup_pending: tuple[dict[str, Any], ...] = ()
    release: dict[str, Any] | None = None
    legacy_suppression: dict[str, Any] | None = None
    waiver_id: str | None = None
    request_id: str | None = None
    request_sequence: int | None = None
    trigger: str | None = None
    requested_at: float | None = None
    next_due_at: float | None = None
    state: str | None = None
    stage: int | None = None
    deadline_at: float | None = None
    reason: str | None = None
    count: int | None = None
    outcomes: tuple[dict[str, Any], ...] = ()
    dry_run: bool | None = None
    leases: tuple[dict[str, Any], ...] = ()
    bound: tuple[dict[str, Any], ...] = ()
    unproven: tuple[str, ...] = ()
    verifier_task_id: str | None = None
    children: tuple[dict[str, Any], ...] = ()
    retire_delegates: tuple[str, ...] = ()
    conclusion: str | None = None


class IntegrationStatusValue(IntegrationOperationalValue):
    github: dict[str, Any] | None = None
    operator_decisions: tuple[dict[str, Any], ...] = ()
    projection_kind: Literal["subjects"] | None = None
    subjects: tuple[dict[str, Any], ...] = ()
    drain_blockers: tuple[dict[str, Any], ...] = ()
    #: Non-blocking App-mode configuration warnings (spec §6.2); never part
    #: of ``blockers``, their digest or ``ready``.
    warnings: tuple[dict[str, Any], ...] = ()
    #: The stored promotion flow as a chain, re-validated on read; its
    #: targets are ``misconfigured`` when the flow no longer validates.
    promotion_flow: dict[str, Any] | None = None


class IntegrationRedriveRootValue(CommandValue):
    """What a completed train root's delivery waits on (``integration_redrive_root``)."""

    task_id: str | None = None
    project_id: str | None = None
    kind: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    remote_head_sha: str | None = None
    ahead_by: int | None = None
    pr_url: str | None = None
    checkpoint: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    reason: str | None = None




class IntegrationCloseDeliveredPrValue(CommandValue):
    """Git's proof that an open PR's work is on the default branch."""

    project_id: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    target_sha: str | None = None
    state: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    proof: dict[str, Any] | None = None
    undelivered: dict[str, Any] | None = None
    reason: str | None = None


class IntegrationAuthorizeRootValue(CommandValue):
    """The exact source an operator authorized (``integration_authorize_root``)."""

    task_id: str | None = None
    project_id: str | None = None
    task_type: str | None = None
    repository_id: str | None = None
    pr_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    generation: int | None = None
    review_kind: str | None = None
    policy_generation: int | None = None
    authorization_id: str | None = None
    authorized_by: Literal["grant", "policy_kind", "policy_allowlist"] | None = None
    reason: str | None = None


class IntegrationReopenCollectionValue(CommandValue):
    """A cancelled or failed-verification collection and what reopening it would do."""

    task_id: str | None = None
    project_id: str | None = None
    kind: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    remote_head_sha: str | None = None
    recorded_head_sha: str | None = None
    operation_id: str | None = None
    episode_id: str | None = None
    checkpoint: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None
    receipts: tuple[dict[str, Any], ...] = ()
    conflict: dict[str, Any] | None = None
    stage: dict[str, Any] | None = None
    delegates: tuple[dict[str, Any], ...] = ()
    human_gates: tuple[str, ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    collector_fence_token: int | None = None
    dispatch: dict[str, Any] | None = None
    reason: str | None = None


class IntegrationRedriveChildValue(CommandValue):
    """What a completed child's assembly waits on (``integration_redrive_child``)."""

    task_id: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    branch: str | None = None
    parent_branch: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    remote_head_sha: str | None = None
    tree_sha: str | None = None
    evidence_id: str | None = None
    collection: str | None = None
    checkpoint: dict[str, Any] | None = None
    reason: str | None = None


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
    fence: Fence | None = Field(
        default=None,
        description="Current reserved collector fence; enables recovery of unapplied child intents.",
    )


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


class IntegrationResolveCandidateMemberArgs(CommandArgs):
    """Publish the exact repair produced by this session's candidate-member assignment."""

    resolved_head_sha: str
    resolved_tree_sha: str
    repair_commit_shas: tuple[str, ...] = Field(min_length=1)
    claim_epoch: int | None = Field(default=None, ge=0)
    # These identities are injected by the authenticated API surface.  They
    # are accepted by the model so they cannot be smuggled into the domain
    # request; the handler compares them with the current principal instead.
    task_id: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    project_id: str | None = Field(default=None, min_length=1)

    @field_validator("resolved_head_sha", "resolved_tree_sha")
    @classmethod
    def exact_git_oid(cls, value: str) -> str:
        if not is_valid_git_oid(value):
            raise ValueError("candidate repair heads and trees must be exact lowercase Git OIDs")
        return value

    @field_validator("repair_commit_shas")
    @classmethod
    def exact_repair_commits(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not is_valid_git_oid(value) for value in values):
            raise ValueError("repair_commit_shas must be unique exact lowercase Git OIDs")
        return values


class IntegrationResolveCandidateMemberValue(CommandValue):
    invariant: str | None = None
    reservation_id: str | None = None
    batch_id: str | None = None
    revision: int | None = None
    member_ordinal: int | None = None
    partial_head_sha: str | None = None
    continuation: dict[str, Any] | None = None


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
    expected_revision: int | None = Field(default=None, ge=0)
    # The reconciler's observed default-branch head. A current revision built
    # on any other base is rebuilt onto it (the revision then advances).
    expected_base_sha: str | None = Field(default=None, min_length=1)


class IntegrationBuildCandidateValue(CommandValue):
    batch_id: str | None = None
    revision: int | None = None
    operation_id: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    member_ordinal: int | None = None


class IntegrationRepairCloseCurrentArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    stage: int = Field(ge=0)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    instance_token: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    fence_token: int = Field(ge=0)


class IntegrationRepairCloseCurrentValue(CommandValue):
    batch_id: str | None = None
    revision: int | None = None


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


class IntegrationRecordNoopArgs(CommandArgs):
    child_task_id: str = Field(min_length=1)
    expected_head_sha: str

    @field_validator("expected_head_sha")
    @classmethod
    def exact_git_oid(cls, value: str) -> str:
        if not is_valid_git_oid(value):
            raise ValueError("expected_head_sha must be an exact Git OID")
        return value


class IntegrationRecordNoopValue(CommandValue):
    receipt_id: str | None = None
    revision: int | None = None
    reviewed_head_sha: str | None = None
    reviewed_tree_sha: str | None = None


class IntegrationReconcileExpiredMutationArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    mutation_id: str = Field(min_length=1)
    expected_nonce: str = Field(min_length=1)
    expected_branch_fence: StrictInt = Field(ge=0)
    expected_lease_fence: StrictInt = Field(ge=0)
    reason: str = Field(min_length=1)
    dry_run: bool = True

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason must be nonblank")
        return value


class IntegrationReconcileExpiredMutationValue(CommandValue):
    project_id: str | None = None
    mutation_id: str | None = None
    disposition: str | None = None
    remote_sha: str | None = None
    dry_run: bool = True


class IntegrationQuiesceOwner(CommandArgs):
    owner_row_id: str = Field(min_length=1)
    fence_token: StrictInt = Field(ge=0)


class IntegrationMigrateProvenanceRefsArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    dry_run: bool = True
    limit: StrictInt = Field(default=50, ge=1, le=1000)
    checkout: str | None = None


class IntegrationMigrateProvenanceRefsValue(CommandValue):
    project_id: str | None = None
    dry_run: bool = True
    rows: list[dict[str, Any]] = Field(default_factory=list)
    remaining: int = 0


class IntegrationRetireLegacyParkArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    task_ids: list[str] = Field(min_length=1)
    expected_event_id: StrictInt | None = Field(default=None, ge=1)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1)
    dry_run: bool = True

    @model_validator(mode="after")
    def exact_abandonment(self):
        if not self.reason.strip() or any(not task_id.strip() for task_id in self.task_ids):
            raise ValueError("reason and task ids must be nonblank")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("task ids must be unique")
        if not self.dry_run and (self.expected_event_id is None or self.expected_sha256 is None):
            raise ValueError("apply requires the previewed event id and SHA256")
        return self


class IntegrationRetireLegacyParkValue(CommandValue):
    project_id: str | None = None
    operation_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    event_id: int | None = None
    sha256: str | None = None
    dry_run: bool = True


class IntegrationQuiesceArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    expected_generation: StrictInt = Field(ge=0)
    subject_id: str = Field(min_length=1)
    expected_version: StrictInt = Field(ge=0)
    owners: list[IntegrationQuiesceOwner] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    dry_run: bool = True

    @model_validator(mode="after")
    def explicit_control(self):
        if not self.reason.strip():
            raise ValueError("reason must be nonblank")
        ids = [owner.owner_row_id for owner in self.owners]
        if len(set(ids)) != len(ids):
            raise ValueError("owner rows must be unique")
        return self


class IntegrationQuiesceValue(CommandValue):
    project_id: str | None = None
    subject_id: str | None = None
    subject_version: int | None = None
    request_id: str | None = None
    owner_row_ids: list[str] = Field(default_factory=list)
    dry_run: bool = True


class IntegrationRecordDeliveredArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    source_sha: str
    base_sha: str
    reason: str = Field(min_length=1)
    tests: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    dry_run: bool = True

    @model_validator(mode="after")
    def exact_source(self) -> IntegrationRecordDeliveredArgs:
        if not is_valid_git_oid(self.source_sha) or not is_valid_git_oid(self.base_sha):
            raise ValueError("source_sha and base_sha require exact Git OIDs")
        if not self.reason.strip():
            raise ValueError("reason must be nonblank")
        return self


class IntegrationRecordDeliveredValue(CommandValue):
    task_id: str | None = None
    project_id: str | None = None
    completion_id: str | None = None
    source_sha: str | None = None
    base_sha: str | None = None
    target_sha: str | None = None
    target_ref: str | None = None
    dry_run: bool = True


class IntegrationRecordRootNoopArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    dry_run: bool = True
    expected_head_sha: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def exact_noop(self) -> IntegrationRecordRootNoopArgs:
        if self.expected_head_sha is not None and not is_valid_git_oid(self.expected_head_sha):
            raise ValueError("expected_head_sha must be an exact Git OID")
        if not self.dry_run and (not self.expected_head_sha or not self.reason.strip()):
            raise ValueError("apply requires the previewed head and a nonblank reason")
        return self


class IntegrationRecordRootNoopValue(CommandValue):
    task_id: str | None = None
    project_id: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    tree_sha: str | None = None
    completion_id: str | None = None
    dry_run: bool = True


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
    stage: int | None = Field(default=None, ge=0)
    starting_sha: str | None = None
    started_at: float | None = None
    deadline_at: float | None = None


class IntegrationRepairDispatchArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    stage: int | None = Field(default=None, ge=0)
    batch_id: str | None = None
    revision: int | None = Field(default=None, ge=0)
    head_sha: str | None = None

    @model_validator(mode="after")
    def complete_candidate_subject(self):
        values = (self.batch_id, self.revision, self.head_sha)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("candidate subject must be supplied as a complete tuple")
        if self.head_sha is not None and not is_valid_git_oid(self.head_sha):
            raise ValueError("head_sha must be an exact lowercase Git OID")
        return self


class IntegrationRepairDispatchValue(CommandValue):
    operation_id: str | None = None
    stage: int | None = Field(default=None, ge=0)
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
        "supervisor_recovery",
        "duplicate",
        "stage_opened",
        "no_stage_blocked",
        "stale",
    ] | None = None
    attempts: int | None = None
    stage: int | None = Field(default=None, ge=0)


class IntegrationRepairTimeoutArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    stage: int = Field(ge=0)


class IntegrationRepairTimeoutValue(CommandValue):
    operation_id: str | None = None
    stage: int | None = Field(default=None, ge=0)
    action: Literal[
        "ignore",
        "dispatch_debug",
        "block_for_human",
        "supervisor_recovery",
        "none",
        "wait",
        "awaiting_promotion",
    ] | None = None


def _operational_contract(
    name: str,
    args_model: type[CommandArgs],
    outcomes: tuple[str, ...],
    *,
    successes: frozenset[str],
    side_effect: SideEffectClass,
    result_model: type[CommandValue] = IntegrationOperationalValue,
    supports_preview: bool = False,
) -> CommandContract:
    effects = (
        (ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),)
        if side_effect is SideEffectClass.READ
        else (UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),)
    )
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
            side_effect=side_effect,
            idempotency=IdempotencySpec(mode="natural"),
            retry_safe=True,
            supports_preview=supports_preview,
            effects=effects,
            sensitive_args=frozenset({"reason"}) if "reason" in args_model.model_fields else frozenset(),
            receipt_projection=tuple(result_model.model_fields),
        ),
        presentation=CommandPresentation(
            title=name.replace("_", " ").title(),
            summary="Authenticated hierarchical integration operational control.",
        ),
    )


PROMOTE_SCHEMA = _operational_contract(
    "promote_schema", PromoteSchemaArgs, ("schema",),
    successes=frozenset({"schema"}), side_effect=SideEffectClass.READ,
    result_model=PromoteSchemaValue,
)
PROMOTE_VALIDATE = _operational_contract(
    "promote_validate", PromoteValidateArgs, ("valid", "invalid", "not_found"),
    successes=frozenset({"valid"}), side_effect=SideEffectClass.READ,
    result_model=PromoteValidateValue,
)

# E1 read-only configuration registration, independent of promotion intents.
PROMOTE_RULESETS = _operational_contract(
    "promote_rulesets", PromoteValidateArgs, ("rulesets", "invalid", "not_found"),
    successes=frozenset({"rulesets"}), side_effect=SideEffectClass.READ,
    result_model=PromoteRulesetsValue,
)

INTEGRATION_STATUS = _operational_contract(
    "integration_status",
    IntegrationStatusReadArgs,
    ("status", "not_found"),
    successes=frozenset({"status"}),
    side_effect=SideEffectClass.READ,
    result_model=IntegrationStatusValue,
)

INTEGRATION_PROMOTION_PUBLISH = _operational_contract(
    "integration_promotion_publish", IntegrationPromotionPublishArgs,
    ("delivered", "testing", "held", "moved", "unknown", "published",
     "unavailable", "not_found", "promotion_intent_invalid"),
    successes=frozenset({"delivered", "testing", "held", "moved", "unknown", "published"}),
    side_effect=SideEffectClass.COMPOSITE, result_model=IntegrationPromotionPublishValue,
)


async def _promotion_publish_adapter(args, ctx):
    return await _hierarchy_adapter(
        "integration_promotion_publish", args, ctx, IntegrationPromotionPublishValue,
        {"delivered", "testing", "held", "moved", "unknown", "published", "unauthorized",
         "unavailable", "not_found", "promotion_intent_invalid"},
    )
#: Every refusal names its cause (spec §3 I6); ``manifest`` is the only success.
TRUST_MANIFEST_OUTCOMES = (
    "manifest",
    "not_found",
    "policy_missing",
    "policy_invalid",
    "repository_not_designated",
    "repository_mismatch",
    "repository_default_branch_missing",
    "provider_not_wired",
    "repository_binding_failed",
    "provider_binding_failed",
    "not_app_mode",
    "ci_policy_invalid",
    "ci_producer_not_numeric",
    "ci_producer_mismatch",
    "trust_manifest_invalid",
)
INTEGRATION_TRUST_MANIFEST = _operational_contract(
    "integration_trust_manifest",
    IntegrationTrustManifestArgs,
    TRUST_MANIFEST_OUTCOMES,
    successes=frozenset({"manifest"}),
    side_effect=SideEffectClass.READ,
    result_model=IntegrationTrustManifestValue,
)
INTEGRATION_TRUST_MANIFEST = INTEGRATION_TRUST_MANIFEST.model_copy(update={
    "presentation": INTEGRATION_TRUST_MANIFEST.presentation.model_copy(update={
        "summary": (
            "Render the App-mode trust manifest from the policy, the authenticated "
            "binding and the daemon's App, and compare the default-branch copy."
        ),
    }),
})
#: A finding is an item, never a refusal; the refusals are the inputs' (spec I6).
APP_VERIFY_OUTCOMES = (
    "verified",
    "not_found",
    "policy_missing",
    "policy_invalid",
    "repository_not_designated",
    "repository_mismatch",
    "repository_default_branch_missing",
    "provider_not_wired",
    "repository_binding_failed",
    "provider_binding_failed",
)
INTEGRATION_APP_VERIFY = _operational_contract(
    "integration_app_verify",
    IntegrationAppVerifyArgs,
    APP_VERIFY_OUTCOMES,
    successes=frozenset({"verified"}),
    side_effect=SideEffectClass.READ,
    result_model=IntegrationAppVerifyValue,
)
INTEGRATION_APP_VERIFY = INTEGRATION_APP_VERIFY.model_copy(update={
    "presentation": INTEGRATION_APP_VERIFY.presentation.model_copy(update={
        "summary": (
            "Check the App credential, repository, producer, trust manifest, Actions "
            "variables, protection and audit workflow App mode depends on; one item each."
        ),
    }),
})
class IntegrationEjectValue(IntegrationOperationalValue):
    replacement_batch_id: str | None = None
    intent: str | None = None
    target_ref: str | None = None
    target_sha: str | None = None
    candidate_sha: str | None = None
    members: tuple[dict[str, Any] | str, ...] = ()


INTEGRATION_EJECT = _operational_contract(
    "integration_eject",
    IntegrationEjectArgs,
    ("preview", "ejected", "refused", "unknown_batch", "not_a_member", "invalid_state"),
    successes=frozenset({"preview", "ejected"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationEjectValue,
    supports_preview=True,
)
class IntegrationReevaluateRepairArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    dry_run: bool = True
    expected_head_sha: str | None = None
    expected_episode_id: str | None = None
    expected_generation: int | None = Field(default=None, ge=0)
    expected_stage: int | None = Field(default=None, ge=0)
    expected_fence_token: int | None = Field(default=None, ge=1)
    expected_snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = None

    @model_validator(mode="after")
    def require_exact_preview(self) -> IntegrationReevaluateRepairArgs:
        if self.expected_head_sha and not is_valid_git_oid(self.expected_head_sha):
            raise ValueError("expected_head_sha must be a full commit id")
        if not self.dry_run and (
            not self.expected_head_sha or self.expected_generation is None
            or self.expected_stage is None or self.expected_fence_token is None
            or not self.expected_snapshot_digest
            or not (self.reason or "").strip()
        ):
            raise ValueError("apply requires the previewed head, generation, stage, fence, snapshot and reason")
        return self


class IntegrationReevaluateRepairValue(CommandValue):
    operation_id: str | None = None
    stage: int | None = None
    subject: dict[str, Any] | None = None
    evidence_ids: tuple[str, ...] = ()
    attempts: int | None = None
    head_sha: str | None = None
    episode_id: str | None = None
    generation: int | None = None
    fence_token: int | None = None
    completion_id: str | None = None
    deadline_at: float | None = None
    apply_command: str | None = None
    snapshot_digest: str | None = None
    planned_steps: tuple[str, ...] = ()
    reason: str | None = None


REEVALUATE_REPAIR_OUTCOMES = (
    "would_reevaluate", "reevaluated", "already_settled", "blocked", "stale", "not_found", "configuration_blocked",
)
INTEGRATION_REEVALUATE_REPAIR = _operational_contract(
    "integration_reevaluate_repair",
    IntegrationReevaluateRepairArgs,
    REEVALUATE_REPAIR_OUTCOMES,
    successes=frozenset({"would_reevaluate", "reevaluated", "already_settled"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationReevaluateRepairValue,
)


INTEGRATION_RELEASE_OWNER = _operational_contract(
    "integration_release_owner",
    IntegrationReleaseOwnerArgs,
    ("released", "preserved_and_released", "not_eligible", "not_found"),
    successes=frozenset({"released", "preserved_and_released"}),
    side_effect=SideEffectClass.UPDATE,
)

INTEGRATION_RESERVE_OWNER = _operational_contract(
    "integration_reserve_owner",
    IntegrationReserveOwnerArgs,
    ("acquired", "already_reserved", "not_eligible", "not_found"),
    successes=frozenset({"acquired", "already_reserved"}),
    side_effect=SideEffectClass.UPDATE,
)

RELEASE_STALE_OWNERS_OUTCOMES = ("released", "nothing_to_release", "invalid", "not_found")

INTEGRATION_RELEASE_STALE_OWNERS = _operational_contract(
    "integration_release_stale_owners",
    IntegrationReleaseStaleOwnersArgs,
    RELEASE_STALE_OWNERS_OUTCOMES,
    successes=frozenset({"released", "nothing_to_release"}),
    side_effect=SideEffectClass.UPDATE,
)


REDRIVE_ROOT_OUTCOMES = (
    "would_open",
    "opened",
    "would_collect",
    "collecting",
    "nothing_to_redrive",
    "blocked",
    "changed",
    "not_eligible",
    "not_found",
    "invalid",
)

INTEGRATION_REDRIVE_ROOT = _operational_contract(
    "integration_redrive_root",
    IntegrationRedriveRootArgs,
    REDRIVE_ROOT_OUTCOMES,
    successes=frozenset({"would_open", "opened", "would_collect", "collecting", "nothing_to_redrive"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationRedriveRootValue,
)

AUTHORIZE_ROOT_OUTCOMES = (
    "would_authorize", "authorized", "already_authorized", "changed", "blocked",
    "not_eligible", "not_found", "invalid",
)

INTEGRATION_AUTHORIZE_ROOT = _operational_contract(
    "integration_authorize_root", IntegrationAuthorizeRootArgs, AUTHORIZE_ROOT_OUTCOMES,
    successes=frozenset({"would_authorize", "authorized", "already_authorized"}),
    side_effect=SideEffectClass.CREATE, result_model=IntegrationAuthorizeRootValue,
)

REDRIVE_CHILD_OUTCOMES = (
    "would_advance",
    "advanced",
    "nothing_to_redrive",
    "blocked",
    "changed",
    "not_eligible",
    "not_found",
    "invalid",
)

INTEGRATION_REDRIVE_CHILD = _operational_contract(
    "integration_redrive_child",
    IntegrationRedriveChildArgs,
    REDRIVE_CHILD_OUTCOMES,
    successes=frozenset({"would_advance", "advanced", "nothing_to_redrive"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationRedriveChildValue,
)

REOPEN_COLLECTION_OUTCOMES = (
    "would_reopen",
    "reopened",
    "nothing_to_reopen",
    "ambiguous",
    "blocked",
    "changed",
    "not_eligible",
    "not_found",
    "invalid",
)

INTEGRATION_REOPEN_COLLECTION = _operational_contract(
    "integration_reopen_collection",
    IntegrationReopenCollectionArgs,
    REOPEN_COLLECTION_OUTCOMES,
    successes=frozenset({"would_reopen", "reopened", "nothing_to_reopen"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationReopenCollectionValue,
)



INTEGRATION_REBIND_REPAIR = _operational_contract(
    "integration_rebind_repair",
    IntegrationRebindRepairArgs,
    ("would_rebind", "rebound", "already_reserved", "changed", "blocked", "not_found"),
    successes=frozenset({"would_rebind", "rebound", "already_reserved"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationRebindRepairValue,
)

PRESERVED_REPAIR_OUTCOMES = (
    "would_recover", "recovered", "already_recovered", "changed", "blocked",
)

INTEGRATION_RECOVER_PRESERVED_REPAIR = _operational_contract(
    "integration_recover_preserved_repair",
    IntegrationRecoverPreservedRepairArgs,
    PRESERVED_REPAIR_OUTCOMES,
    successes=frozenset({"would_recover", "recovered", "already_recovered"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationRecoverPreservedRepairValue,
)

INTEGRATION_RECOVER_PARENT_HEAD = _operational_contract(
    "integration_recover_parent_head",
    IntegrationRecoverParentHeadArgs,
    PRESERVED_REPAIR_OUTCOMES,
    successes=frozenset({"would_recover", "recovered", "already_recovered"}),
    side_effect=SideEffectClass.UPDATE,
    result_model=IntegrationRecoverParentHeadValue,
)

REBIND_DETACHED_REPAIR_OUTCOMES = (
    "would_rebind",
    "rebound",
    "already_rebound",
    "changed",
    "blocked",
    "not_found",
)

INTEGRATION_REBIND_DETACHED_REPAIR = _operational_contract(
    "integration_rebind_detached_repair",
    IntegrationRebindDetachedRepairArgs,
    REBIND_DETACHED_REPAIR_OUTCOMES,
    successes=frozenset({"would_rebind", "rebound", "already_rebound"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationRebindDetachedRepairValue,
)


CLOSE_DELIVERED_PR_OUTCOMES = (
    "would_close",
    "closed",
    "nothing_to_close",
    "undelivered",
    "changed",
    "blocked",
    "not_eligible",
    "not_found",
    "invalid",
)

INTEGRATION_CLOSE_DELIVERED_PR = _operational_contract(
    "integration_close_delivered_pr",
    IntegrationCloseDeliveredPrArgs,
    CLOSE_DELIVERED_PR_OUTCOMES,
    successes=frozenset({"would_close", "closed", "nothing_to_close"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationCloseDeliveredPrValue,
)

INTEGRATION_RECOVER_CANDIDATE_MEMBER = CommandContract(
    execution=ExecutionContract(
        name="integration_recover_candidate_member",
        args_model=IntegrationRecoverCandidateMemberArgs,
        result_model=IntegrationRecoverCandidateMemberValue,
        outcomes=tuple(
            OutcomeSpec(name=name, classification=(
                OutcomeClass.SUCCESS if name in {"accepted", "already_accepted", "rejected"}
                else OutcomeClass.FAILURE
            ))
            for name in ("accepted", "already_accepted", "rejected", "stale", "wait")
        ),
        capability="integration_recover_candidate_member",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="keyed", key_field="reservation_id"),
        retry_safe=True,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        receipt_projection=tuple(IntegrationRecoverCandidateMemberValue.model_fields),
    ),
    presentation=CommandPresentation(
        title="Resolve pushed candidate member",
        summary="Accept one valid frozen repair or retain its failed invariant for a fresh recovery.",
    ),
)


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
    summary: str,
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
            title=name.replace("_", " ").title(), summary=summary
        ),
    )


INTEGRATION_REPAIR_START = _repair_contract(
    "integration_repair_start",
    IntegrationRepairStartArgs,
    IntegrationRepairStartValue,
    ("started", "already_started", "stale", "invariant_error"),
    summary="Activate or durably continue one operation's bounded repair stage.",
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
    summary="Create the repair task and hand the current branch writer fence to it.",
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
    ("continue", "started", "escalate", "human_required", "budget_exhausted"),
    summary="Record one exact repair check attempt against the current stage budget.",
    effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
)

INTEGRATION_REPAIR_TIMEOUT = _repair_contract(
    "integration_repair_timeout",
    IntegrationRepairTimeoutArgs,
    IntegrationRepairTimeoutValue,
    ("expired", "not_due", "already_terminal", "stale"),
    summary="Expire the current repair stage once its absolute deadline has passed.",
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
            OutcomeSpec(name="superseded", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="continued", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="waiting", classification=OutcomeClass.FAILURE),
            OutcomeSpec(name="target_moved", classification=OutcomeClass.FAILURE),
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


INTEGRATION_RESOLVE_CANDIDATE_MEMBER = CommandContract(
    execution=ExecutionContract(
        name="integration_resolve_candidate_member",
        args_model=IntegrationResolveCandidateMemberArgs,
        result_model=IntegrationResolveCandidateMemberValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS
                    if name in {"accepted", "already_accepted"}
                    else OutcomeClass.FAILURE
                ),
            )
            for name in (
                "accepted",
                "already_accepted",
                "wait",
                "stale",
                "invariant_error",
            )
        ),
        capability="integration_resolve_candidate_member",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
            UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),
        ),
        sensitive_args=frozenset(
            {"resolved_head_sha", "resolved_tree_sha", "repair_commit_shas"}
        ),
        sensitive_result_fields=frozenset({"partial_head_sha", "continuation"}),
        receipt_projection=("reservation_id", "batch_id", "revision", "member_ordinal"),
    ),
    presentation=CommandPresentation(
        title="Resolve candidate member",
        summary=(
            "Reserve, publish, accept, and continue the exact conflicted candidate member "
            "owned by the authenticated repair session."
        ),
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

INTEGRATION_REPAIR_CLOSE_CURRENT = _root_subject_contract(
    "integration_repair_close_current",
    IntegrationRepairCloseCurrentArgs,
    IntegrationRepairCloseCurrentValue,
    ("current", "not_batch", "stale"),
    frozenset({"current", "not_batch"}),
    "Resolve exact current repair close",
)

INTEGRATION_CI_EVIDENCE = _root_subject_contract(
    "integration_ci_evidence",
    IntegrationCIEvidenceArgs,
    IntegrationCIEvidenceValue,
    (
        "green", "red", "pending", "full_suite_required", "stale_subject",
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


def _parent_contract(name, args_model, result_model, outcomes, *, side_effect, summary):
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
                        if outcome in {"ready", "verified", "completed", "already_completed"}
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
        presentation=CommandPresentation(
            title=name.replace("_", " ").title(),
            summary=summary,
        ),
    )


INTEGRATION_DELIVERY_READINESS = _parent_contract(
    "integration_delivery_readiness",
    IntegrationDeliveryReadinessArgs,
    IntegrationDeliveryReadinessValue,
    ("ready", "waiting", "failed", "invariant_error"),
    side_effect=SideEffectClass.READ,
    summary="Read whether every child of one parent has delivered, changing nothing.",
)
INTEGRATION_RECORD_NOOP = CommandContract(
    execution=ExecutionContract(
        name="integration_record_noop",
        args_model=IntegrationRecordNoopArgs,
        result_model=IntegrationRecordNoopValue,
        outcomes=tuple(
            OutcomeSpec(
                name=name,
                classification=(
                    OutcomeClass.SUCCESS if name == "recorded" else OutcomeClass.FAILURE
                ),
            )
            for name in ("recorded", "stale_head", "invalid", "delivery_target_fixed")
        ),
        capability="integration_record_noop",
        side_effect=SideEffectClass.UPDATE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.DELIVERY_EVIDENCE),
            UpdateClause(subject=EffectSubject.TASK),
        ),
        sensitive_args=frozenset({"expected_head_sha"}),
        sensitive_result_fields=frozenset({"reviewed_head_sha", "reviewed_tree_sha"}),
        receipt_projection=("receipt_id", "revision"),
    ),
    presentation=CommandPresentation(
        title="Record verified no-code child disposition",
        summary="Bind a child's current no-op completion and exact Git head to its parent receipt.",
    ),
)
INTEGRATION_PARENT_VERIFY = _parent_contract(
    "integration_parent_verify",
    IntegrationParentVerifyArgs,
    IntegrationParentVerifyValue,
    ("verified", "stale_generation", "stale_head", "invalid_evidence"),
    side_effect=SideEffectClass.UPDATE,
    summary="Record one parent verification against its exact checkpoint head and evidence.",
)
INTEGRATION_COMPLETE_PARENT = _parent_contract(
    "integration_complete_parent",
    IntegrationCompleteParentArgs,
    IntegrationCompleteParentValue,
    # ``ParentEpisodeRecords.complete_parent`` answers ``already_completed`` on the
    # crash-retry replay path, and returns the readiness projection verbatim when
    # the parent is not ready, which is ``waiting`` or ``failed``.  Each is a
    # state a playbook must be able to route, not a wiring bug.
    (
        "completed",
        "already_completed",
        "waiting",
        "failed",
        "stale_verification",
        "invariant_error",
    ),
    side_effect=SideEffectClass.UPDATE,
    summary="Complete a verified parent task at its exact verified generation and head.",
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
        {"applied", "not_applied", "invariant_error", "superseded", "continued",
         "waiting", "target_moved"},
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


async def _resolve_candidate_member_adapter(
    args: IntegrationResolveCandidateMemberArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _invoke_adapter(
        "integration_resolve_candidate_member",
        args,
        ctx,
        IntegrationResolveCandidateMemberValue,
        {
            "accepted",
            "already_accepted",
            "wait",
            "stale",
            "unauthorized",
            "invariant_error",
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


async def _repair_close_current_adapter(
    args: IntegrationRepairCloseCurrentArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _hierarchy_adapter(
        "integration_repair_close_current",
        args,
        ctx,
        IntegrationRepairCloseCurrentValue,
        {"current", "not_batch", "stale"},
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
            "green", "red", "pending", "full_suite_required", "stale_subject",
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
    aliases: dict[str, str] | None = None,
) -> CommandResult:
    from src.commands.contracts.builtin import _handler

    payload = args.model_dump(mode="json")
    if ctx is None:
        raw = await _handler().execute(command, payload)
    else:
        with principal_context(ctx):
            raw = await _handler().execute(command, payload)
    outcome = raw.get("outcome")
    if aliases and outcome in aliases:
        # A handler outcome newer than the frozen contract maps onto a declared
        # one with the same routing; the exact reason stays in the summary.
        raw = raw | {"error": raw.get("reason") or raw.get("error") or outcome}
        outcome = aliases[outcome]
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


async def _record_noop_adapter(args: IntegrationRecordNoopArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_record_noop",
        args,
        ctx,
        IntegrationRecordNoopValue,
        {"recorded", "stale_head", "invalid", "delivery_target_fixed"},
    )


async def _complete_parent_adapter(
    args: IntegrationCompleteParentArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_complete_parent",
        args,
        ctx,
        IntegrationCompleteParentValue,
        {
            "completed",
            "already_completed",
            "waiting",
            "failed",
            "stale_verification",
            "invariant_error",
        },
        # A checkpoint with no trusted verification binding for this subject is
        # the same routing as ``waiting`` -- the next tick re-reads readiness
        # and the ``task.integration_verified`` event re-runs the rule.  The
        # reviewed playbooks pin this contract's fingerprint, so the precise
        # reason reaches them in the summary.
        aliases={AWAITING_TRUSTED_VERIFICATION: "waiting"},
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
        # ``unknown`` is a mechanical, retryable refusal.  Reviewed playbooks
        # pin this contract's fingerprint, so it reaches them as ``busy``.
        aliases={"unknown": "busy"},
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


async def _status_adapter(args: IntegrationStatusReadArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_status", args, ctx, IntegrationStatusValue, {"status", "not_found"}
    )


async def _promote_schema_adapter(args: PromoteSchemaArgs, ctx: CommandContext | None):
    from src.commands.contracts.builtin import _handler

    if ctx is None:
        raw = await _handler().execute("promote_schema", {})
    else:
        with principal_context(ctx):
            raw = await _handler().execute("promote_schema", {})
    return CommandResult(outcome=raw["outcome"], value=PromoteSchemaValue(
        schema=raw.get("schema", {})
    ), summary="Promotion-flow JSON schema")


async def _promote_validate_adapter(args: PromoteValidateArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "promote_validate", args, ctx, PromoteValidateValue, {"valid", "invalid", "not_found"},
    )


async def _promote_rulesets_adapter(args: PromoteValidateArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "promote_rulesets", args, ctx, PromoteRulesetsValue,
        {"rulesets", "invalid", "not_found", "unauthorized"},
    )


async def _trust_manifest_adapter(
    args: IntegrationTrustManifestArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_trust_manifest", args, ctx, IntegrationTrustManifestValue,
        set(TRUST_MANIFEST_OUTCOMES),
    )


async def _app_verify_adapter(args: IntegrationAppVerifyArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_app_verify", args, ctx, IntegrationAppVerifyValue,
        set(APP_VERIFY_OUTCOMES),
    )


async def _eject_adapter(args: IntegrationEjectArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_eject",
        args,
        ctx,
        IntegrationEjectValue,
        {"preview", "ejected", "refused", "unknown_batch", "not_a_member", "invalid_state"},
    )


async def _reevaluate_repair_adapter(
    args: IntegrationReevaluateRepairArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_reevaluate_repair", args, ctx, IntegrationReevaluateRepairValue,
        set(REEVALUATE_REPAIR_OUTCOMES),
    )


async def _release_owner_adapter(args: IntegrationReleaseOwnerArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_release_owner",
        args,
        ctx,
        IntegrationOperationalValue,
        {"released", "preserved_and_released", "not_eligible", "not_found"},
    )


async def _reserve_owner_adapter(args: IntegrationReserveOwnerArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_reserve_owner",
        args,
        ctx,
        IntegrationOperationalValue,
        {"acquired", "already_reserved", "not_eligible", "not_found"},
    )


async def _release_stale_owners_adapter(
    args: IntegrationReleaseStaleOwnersArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_release_stale_owners",
        args,
        ctx,
        IntegrationOperationalValue,
        set(RELEASE_STALE_OWNERS_OUTCOMES),
    )


async def _redrive_root_adapter(args: IntegrationRedriveRootArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_redrive_root",
        args,
        ctx,
        IntegrationRedriveRootValue,
        set(REDRIVE_ROOT_OUTCOMES),
    )




async def _authorize_root_adapter(
    args: IntegrationAuthorizeRootArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_authorize_root", args, ctx,
        IntegrationAuthorizeRootValue, set(AUTHORIZE_ROOT_OUTCOMES),
    )


async def _redrive_child_adapter(args: IntegrationRedriveChildArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_redrive_child",
        args,
        ctx,
        IntegrationRedriveChildValue,
        set(REDRIVE_CHILD_OUTCOMES),
    )


async def _reopen_collection_adapter(
    args: IntegrationReopenCollectionArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_reopen_collection",
        args,
        ctx,
        IntegrationReopenCollectionValue,
        set(REOPEN_COLLECTION_OUTCOMES),
    )




async def _rebind_repair_adapter(args: IntegrationRebindRepairArgs, ctx: CommandContext | None):
    return await _hierarchy_adapter(
        "integration_rebind_repair",
        args,
        ctx,
        IntegrationRebindRepairValue,
        {"would_rebind", "rebound", "already_reserved", "changed", "blocked", "not_found"},
    )


async def _recover_preserved_repair_adapter(
    args: IntegrationRecoverPreservedRepairArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_recover_preserved_repair", args, ctx,
        IntegrationRecoverPreservedRepairValue, set(PRESERVED_REPAIR_OUTCOMES),
    )


async def _recover_parent_head_adapter(
    args: IntegrationRecoverParentHeadArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_recover_parent_head", args, ctx,
        IntegrationRecoverParentHeadValue, set(PRESERVED_REPAIR_OUTCOMES),
    )


async def _rebind_detached_repair_adapter(
    args: IntegrationRebindDetachedRepairArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_rebind_detached_repair",
        args,
        ctx,
        IntegrationRebindDetachedRepairValue,
        set(REBIND_DETACHED_REPAIR_OUTCOMES),
    )


async def _close_delivered_pr_adapter(
    args: IntegrationCloseDeliveredPrArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_close_delivered_pr", args, ctx,
        IntegrationCloseDeliveredPrValue, set(CLOSE_DELIVERED_PR_OUTCOMES),
    )


async def _recover_candidate_member_adapter(
    args: IntegrationRecoverCandidateMemberArgs, ctx: CommandContext | None
):
    return await _hierarchy_adapter(
        "integration_recover_candidate_member",
        args,
        ctx,
        IntegrationRecoverCandidateMemberValue,
        {"accepted", "already_accepted", "rejected", "stale", "wait"},
    )


def register_integration_contracts(registry: ContractRegistry) -> None:
    """Register contracts whose real handlers have landed.

    Each implementation task adds its handler and typed authority/redaction
    declaration together.  Unavailable security-sensitive mutations remain
    outside the allowlist.
    """
    for name in ("integration_cutover_plan", "integration_cutover"):
        if registry.get(name) is not None:
            continue
        read_only = name == "integration_cutover_plan"
        outcomes = ("planned",) if read_only else ("preview", "configured", "blocked", "stale",
                                                   "busy", "invalid", "in_use", "not_found")
        outcomes += ("refused",)
        contract = _operational_contract(
            name, IntegrationCutoverArgs, outcomes,
            successes=frozenset({"planned", "preview", "configured"}),
            side_effect=SideEffectClass.READ if read_only else SideEffectClass.COMPOSITE,
            result_model=IntegrationCutoverValue, supports_preview=not read_only)

        async def cutover_control(args, ctx, command=name, outcomes=outcomes):
            return await _hierarchy_adapter(command, args, ctx, IntegrationCutoverValue, set(outcomes))

        async def cutover_preview(args, ctx, invoke=cutover_control):
            return await invoke(args.model_copy(update={"dry_run": True}), ctx)

        registry.register(CommandRegistration(
            name, contract, cutover_control, None if read_only else cutover_preview))
    for name, args_model, applied, value_model in (
        ("integration_migrate_provenance_refs", IntegrationMigrateProvenanceRefsArgs, "migrated",
         IntegrationMigrateProvenanceRefsValue),
        ("integration_retire_legacy_park", IntegrationRetireLegacyParkArgs, "retired",
         IntegrationRetireLegacyParkValue),
        ("integration_quiesce", IntegrationQuiesceArgs, "quiesced", IntegrationQuiesceValue),
        ("integration_record_delivered", IntegrationRecordDeliveredArgs, "recorded",
         IntegrationRecordDeliveredValue),
        ("integration_record_root_noop", IntegrationRecordRootNoopArgs, "recorded",
         IntegrationRecordRootNoopValue),
        ("integration_abort_batch", IntegrationAbortBatchArgs, "aborted", IntegrationTrainControlValue),
        ("integration_pause_batch", IntegrationAbortBatchArgs, "paused", IntegrationTrainControlValue),
        ("integration_resume_batch", IntegrationAbortBatchArgs, "resumed", IntegrationTrainControlValue),
        ("integration_seal_now", IntegrationSealNowArgs, "sealed", IntegrationTrainControlValue),
        ("integration_retire_origin", IntegrationRetireOriginArgs, "retired", IntegrationTrainControlValue),
        ("integration_refresh_epic", IntegrationRefreshEpicArgs, "refreshed", IntegrationRefreshEpicValue),
    ):
        if registry.get(name) is not None:
            continue
        outcomes = ("preview", applied, "refused")
        successes = {"preview", applied}
        if name == "integration_migrate_provenance_refs":
            outcomes += ("blocked",)
        if name == "integration_seal_now":
            outcomes += ("no_ready_work", "existing_batch")
            successes.update({"no_ready_work", "existing_batch"})
        elif name == "integration_refresh_epic":
            outcomes += ("pending", "current")
            successes.update({"pending", "current"})
        contract = _operational_contract(
            name, args_model, outcomes, successes=frozenset(successes),
            side_effect=SideEffectClass.COMPOSITE, result_model=value_model,
            supports_preview=True,
        )
        if name == "integration_migrate_provenance_refs":
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"effects": (
                    ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),
                    UpdateClause(subject=EffectSubject.DELIVERY_EVIDENCE,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                )}),
                "presentation": CommandPresentation(
                    title="Move Git provenance out of branches",
                    summary="Local operator copies and verifies immutable provenance refs before deleting legacy heads.",
                ),
            })
        if name == "integration_retire_legacy_park":
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"effects": (
                    ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                    UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                )}),
                "presentation": CommandPresentation(
                    title="Retire a historical parked operation",
                    summary="Local operator abandons every explicitly selected source under an exact journal fence.",
                ),
            })
        if name in {"integration_record_root_noop", "integration_record_delivered"}:
            applying = ClausePredicate(arg_equals=("dry_run", False))
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"effects": (
                    ReadClause(subject=EffectSubject.TASK),
                    UpdateClause(subject=EffectSubject.TASK, when=applying),
                    CreateOrReuseClause(subject=EffectSubject.DELIVERY_EVIDENCE,
                                        key_arg="task_id", when=applying),
                    UpdateClause(subject=EffectSubject.DOWNSTREAM_TASKS, when=applying),
                )}),
                "presentation": CommandPresentation(
                    title="Record verified no-code root completion",
                    summary="Preview or complete an unheld root with exact no-artifact Git provenance.",
                ),
            })

        if name == "integration_quiesce":
            applying = ClausePredicate(arg_equals=("dry_run", False))
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"effects": (
                    ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                    UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION, when=applying),
                    UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP, when=applying),
                )}),
                "presentation": CommandPresentation(
                    title="Quiesce idle train admission",
                    summary="Local operator closes an unfrozen root and releases explicitly fenced idle reservations.",
                ),
            })
        if name == "integration_record_delivered":
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"effects": (
                    *contract.execution.effects,
                    UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                )}),
                "presentation": CommandPresentation(
                    title="Record exact externally delivered completion",
                    summary="Local operator verifies exact source on the designated default and records shipped work.",
                ),
            })

        async def train_control(args, ctx, command=name, outcomes=outcomes, value_model=value_model):
            return await _hierarchy_adapter(
                command, args, ctx, value_model, set(outcomes),
            )

        async def train_control_preview(args, ctx, invoke=train_control):
            return await invoke(args.model_copy(update={"dry_run": True}), ctx)

        registry.register(CommandRegistration(name, contract, train_control, train_control_preview))
    name = "integration_reconcile_expired_mutation"
    if registry.get(name) is None:
        outcomes = ("preview", "applied", "superseded", "refused")
        value_model = IntegrationReconcileExpiredMutationValue
        contract = _operational_contract(
            name, IntegrationReconcileExpiredMutationArgs, outcomes,
            successes=frozenset({"preview", "applied", "superseded"}),
            side_effect=SideEffectClass.COMPOSITE, result_model=value_model, supports_preview=True,
        )
        contract = contract.model_copy(update={
            "execution": contract.execution.model_copy(update={"effects": (
                ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION,
                             when=ClausePredicate(arg_equals=("dry_run", False))),
            )}),
            "presentation": CommandPresentation(
                title="Reconcile an expired mutation after its operation ended",
                summary="Local operator verifies nonce, fences, stopped authority and actual remote target.",
            ),
        })

        async def reconcile_expired(args, ctx):
            return await _hierarchy_adapter(
                "integration_reconcile_expired_mutation", args, ctx,
                IntegrationReconcileExpiredMutationValue,
                {"preview", "applied", "superseded", "refused"},
            )

        async def reconcile_expired_preview(args, ctx):
            return await reconcile_expired(args.model_copy(update={"dry_run": True}), ctx)

        registry.register(CommandRegistration(name, contract, reconcile_expired, reconcile_expired_preview))
    name = "integration_release_held_gate"
    if registry.get(name) is None:
        contract = _operational_contract(
            name, IntegrationReleaseHeldGateArgs, ("preview", "released", "refused"),
            successes=frozenset({"preview", "released"}),
            side_effect=SideEffectClass.COMPOSITE, result_model=IntegrationReleaseHeldGateValue,
        )
        contract = contract.model_copy(update={
            "presentation": contract.presentation.model_copy(update={
                "title": "Release a held parent gate",
                "summary": "A local human releases an exact parent hold with an audited reason.",
            }),
        })

        async def release_held_gate(args, ctx):
            return await _hierarchy_adapter(
                "integration_release_held_gate", args, ctx, IntegrationReleaseHeldGateValue,
                {"preview", "released", "refused"},
            )

        registry.register(CommandRegistration(name, contract, release_held_gate))
    name = "integration_engine_transfer"
    if registry.get(name) is None:
        contract = _operational_contract(name, IntegrationEngineTransferArgs,
            ("preview", "transferred", "refused"), successes=frozenset({"preview", "transferred"}),
            side_effect=SideEffectClass.COMPOSITE, result_model=IntegrationEngineTransferValue)

        async def transfer_engine(args, ctx):
            return await _hierarchy_adapter("integration_engine_transfer", args, ctx, IntegrationEngineTransferValue,
                                             {"preview", "transferred", "refused"})

        registry.register(CommandRegistration(name, contract, transfer_engine))
    name = "integration_development_engine_transfer"
    if registry.get(name) is None:
        contract = _operational_contract(name, IntegrationDevelopmentEngineTransferArgs,
            ("preview", "transferred", "refused"),
            successes=frozenset({"preview", "transferred"}),
            side_effect=SideEffectClass.COMPOSITE,
            result_model=IntegrationDevelopmentEngineTransferValue)
        contract = contract.model_copy(update={
            "presentation": contract.presentation.model_copy(update={
                "title": "Development engine transfer",
                "summary": (
                    "Preview or transfer every Development subject of one project "
                    "to the reconciler, at the exact "
                    "previewed versions; the same command rolls it back."
                ),
            }),
        })

        async def transfer_development(args, ctx):
            return await _hierarchy_adapter(
                "integration_development_engine_transfer", args, ctx,
                IntegrationDevelopmentEngineTransferValue,
                {"preview", "transferred", "refused"},
            )

        registry.register(CommandRegistration(name, contract, transfer_development))
    if registry.get(INTEGRATION_TRANSFER_OWNER.name) is None:
        registry.register(
            CommandRegistration(
                INTEGRATION_TRANSFER_OWNER.name,
                INTEGRATION_TRANSFER_OWNER,
                _transfer_adapter,
            )
        )
    for contract, adapter in (
        (INTEGRATION_STATUS, _status_adapter),
        (INTEGRATION_PROMOTION_PUBLISH, _promotion_publish_adapter),
        (PROMOTE_SCHEMA, _promote_schema_adapter),
        (PROMOTE_VALIDATE, _promote_validate_adapter),
        (PROMOTE_RULESETS, _promote_rulesets_adapter),
        (INTEGRATION_TRUST_MANIFEST, _trust_manifest_adapter),
        (INTEGRATION_APP_VERIFY, _app_verify_adapter),
        (INTEGRATION_EJECT, _eject_adapter),
        (INTEGRATION_REEVALUATE_REPAIR, _reevaluate_repair_adapter),
        (INTEGRATION_RELEASE_OWNER, _release_owner_adapter),
        (INTEGRATION_RESERVE_OWNER, _reserve_owner_adapter),
        (INTEGRATION_RELEASE_STALE_OWNERS, _release_stale_owners_adapter),
        (INTEGRATION_REDRIVE_ROOT, _redrive_root_adapter),
        (INTEGRATION_AUTHORIZE_ROOT, _authorize_root_adapter),
        (INTEGRATION_REDRIVE_CHILD, _redrive_child_adapter),
        (INTEGRATION_REOPEN_COLLECTION, _reopen_collection_adapter),
        (INTEGRATION_REBIND_REPAIR, _rebind_repair_adapter),
        (INTEGRATION_REBIND_DETACHED_REPAIR, _rebind_detached_repair_adapter),
        (INTEGRATION_RECOVER_PRESERVED_REPAIR, _recover_preserved_repair_adapter),
        (INTEGRATION_RECOVER_PARENT_HEAD, _recover_parent_head_adapter),
        (INTEGRATION_CLOSE_DELIVERED_PR, _close_delivered_pr_adapter),
        (INTEGRATION_RECOVER_CANDIDATE_MEMBER, _recover_candidate_member_adapter),
        (INTEGRATION_SCHEDULE_DUE, _schedule_due_adapter),
        (INTEGRATION_SEAL, _seal_adapter),
        (INTEGRATION_FILE_CHILDREN, _file_children_adapter),
        (INTEGRATION_CHECKPOINT_PARENT, _checkpoint_parent_adapter),
        (INTEGRATION_MUTATE_HIERARCHY, _mutate_hierarchy_adapter),
        (INTEGRATION_DELIVERY_READINESS, _delivery_readiness_adapter),
        (INTEGRATION_RECORD_NOOP, _record_noop_adapter),
        (INTEGRATION_PARENT_VERIFY, _parent_verify_adapter),
        (INTEGRATION_COMPLETE_PARENT, _complete_parent_adapter),
        (DELIVERY_PROMOTE, _promote_adapter),
        (DELIVERY_RECEIPTS, _receipts_adapter),
        (INTEGRATION_RECONCILE_PROMOTION, _reconcile_adapter),
        (INTEGRATION_RESOLVE_CONFLICT, _resolve_conflict_adapter),
        (INTEGRATION_PUSH_CONFLICT_RESOLUTION, _push_conflict_resolution_adapter),
        (INTEGRATION_RECOVER_UNWRITTEN_RESOLUTION, _recover_unwritten_resolution_adapter),
        (INTEGRATION_RESOLVE_CANDIDATE_MEMBER, _resolve_candidate_member_adapter),
        (INTEGRATION_PROMOTE_MAIN, _promote_main_adapter),
        (INTEGRATION_CLEANUP, _cleanup_adapter),
        (INTEGRATION_BUILD_CANDIDATE, _build_candidate_adapter),
        (INTEGRATION_REPAIR_CLOSE_CURRENT, _repair_close_current_adapter),
        (INTEGRATION_CI_EVIDENCE, _ci_evidence_adapter),
        (INTEGRATION_RELEASE, _release_adapter),
        (INTEGRATION_REPAIR_START, _repair_start_adapter),
        (INTEGRATION_REPAIR_DISPATCH, _repair_dispatch_adapter),
        (INTEGRATION_RECORD_REPAIR, _record_repair_adapter),
        (INTEGRATION_REPAIR_TIMEOUT, _repair_timeout_adapter),
    ):
        if registry.get(contract.name) is None:
            preview = None
            if contract.execution.supports_preview:
                async def preview(args, ctx, invoke=adapter):
                    return await invoke(args.model_copy(update={"dry_run": True}), ctx)

            registry.register(CommandRegistration(contract.name, contract, adapter, preview))

class IntegrationReleaseHeldGateArgs(CommandArgs):
    subject_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    expected_version: StrictInt | None = Field(default=None, ge=0)
    reason: str = ""
    dry_run: bool = True


class IntegrationReleaseHeldGateValue(CommandValue):
    subject_id: str | None = None
    gate_id: str | None = None
    expected_version: int | None = None
    subject_version: int | None = None
    engine: Literal["reconciler"] | None = None


class IntegrationEngineTransferArgs(CommandArgs):
    parent_task_id: str | None = Field(default=None, min_length=1)
    repository_id: str = Field(min_length=1)
    engine: Literal["reconciler"]
    expected_versions: dict[str, StrictInt] = Field(default_factory=dict)
    reason: str = ""
    evidence: tuple[str, ...] = ()
    dry_run: bool = True

    @field_validator("expected_versions")
    @classmethod
    def nonnegative_versions(cls, value):
        if any(not key or isinstance(version, bool) or version < 0 for key, version in value.items()):
            raise ValueError("expected_versions must name exact nonnegative subject versions")
        return value


class IntegrationEngineTransferValue(CommandValue):
    repository_id: str | None = None
    engine: Literal["reconciler"] | None = None
    subject_ids: tuple[str, ...] = ()
    expected_versions: dict[str, int] = Field(default_factory=dict)
    current_engines: dict[str, str] = Field(default_factory=dict)
    reason: str | None = None


class IntegrationDevelopmentEngineTransferArgs(CommandArgs):
    """The audited per-project forward Development engine transfer.

    ``expected_versions`` is the exact subject-version set the preview named, so
    an apply is a CAS over the whole Development subject set rather than a
    partial move. ``reason`` is required for an apply and ``evidence`` for a
    reconciler activation; disabling visits preserves ownership, because
    it never adopts a write the old publisher left ambiguous.
    """

    project_id: str = Field(min_length=1)
    engine: Literal["reconciler"]
    expected_versions: dict[str, StrictInt] = Field(default_factory=dict)
    reason: str = ""
    evidence: tuple[str, ...] = ()
    dry_run: bool = True

    @field_validator("expected_versions")
    @classmethod
    def nonnegative_versions(cls, value):
        if any(not key or isinstance(version, bool) or version < 0 for key, version in value.items()):
            raise ValueError("expected_versions must name exact nonnegative subject versions")
        return value


class IntegrationDevelopmentEngineTransferValue(CommandValue):
    project_id: str | None = None
    engine: Literal["reconciler"] | None = None
    subject_ids: tuple[str, ...] = ()
    expected_versions: dict[str, int] = Field(default_factory=dict)
    current_engines: dict[str, str] = Field(default_factory=dict)


class IntegrationRecoverUnwrittenResolutionArgs(CommandArgs):
    """Operator recovery of a malformed reservation which never wrote remotely."""

    intent_id: str = Field(min_length=1)

INTEGRATION_RECOVER_UNWRITTEN_RESOLUTION = CommandContract(
    execution=ExecutionContract(
        name="integration_recover_unwritten_resolution",
        args_model=IntegrationRecoverUnwrittenResolutionArgs,
        result_model=PromotionCommandValue,
        outcomes=(
            OutcomeSpec(name="recovered", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="already_recovered", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="not_recoverable", classification=OutcomeClass.FAILURE),
        ),
        capability="integration_recover_unwritten_resolution",
        side_effect=SideEffectClass.COMPOSITE,
        idempotency=IdempotencySpec(mode="keyed", key_field="intent_id"),
        retry_safe=True,
        effects=(
            UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
            UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),
        ),
        receipt_projection=("intent_id", "receipt_id"),
    ),
    presentation=CommandPresentation(
        title="Recover unwritten conflict resolution",
        summary="Supersede a malformed reservation only after an operator proves no remote write occurred.",
    ),
)

async def _recover_unwritten_resolution_adapter(
    args: IntegrationRecoverUnwrittenResolutionArgs, ctx: CommandContext | None
) -> CommandResult:
    return await _invoke_adapter(
        "integration_recover_unwritten_resolution",
        args,
        ctx,
        PromotionCommandValue,
        {"recovered", "already_recovered", "not_recoverable"},
    )
