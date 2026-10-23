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
from src.git.manager import is_valid_git_oid
from src.integration.models import (
    AWAITING_TRUSTED_VERIFICATION,
    BranchKey,
    Fence,
    HostedCheckPolicy,
)

DESIGN_INTEGRATION_COMMANDS = frozenset(
    {
        "integration_continue_close",
        "integration_file_children",
        "integration_checkpoint_parent",
        "integration_delivery_readiness",
        "integration_record_noop",
        "integration_record_root_noop",
        "integration_record_delivered",
        "integration_quiesce",
        "integration_supersede_completion",


        "integration_off",
        "integration_retire_legacy_park",
        "integration_migrate_provenance_refs",
        "integration_reconcile_expired_mutation",
        "integration_parent_verify",
        "integration_complete_parent",
        "delivery_receipts",
        "integration_repair_close_current",
        "integration_ci_evidence",
        "integration_repair_start",
        "integration_repair_dispatch",
        "integration_record_repair",
        "integration_repair_timeout",
        "integration_transfer_owner",
        "integration_mutate_hierarchy",
        "integration_promotion_publish",
        "integration_release",
        "integration_cleanup",
        "integration_status",
        "integration_cutover_plan",
        "integration_cutover",
        "integration_cutover_containment",
        "integration_abort_batch",
        "integration_settle_batch",
        "integration_release_withheld_source",
        "integration_pause_batch",
        "integration_resume_batch",
        "integration_repair_target",
        "integration_rerequest_check",
        "integration_seal_now",
        "integration_retire_origin",
        "integration_refresh_epic",
        "integration_reverify_epic",
        "integration_trust_manifest",
        "integration_app_verify",
        "integration_eject",
        "integration_reevaluate_repair",
        "integration_release_owner",
        "integration_restore_task_origin",
        "integration_reserve_owner",
        "integration_clear_retained_recovery",
        "integration_release_stale_owners",
        "integration_redrive_root",
        "integration_authorize_root",
        "integration_redrive_child",
        "integration_recover_completed_container",
        "integration_reopen_collection",
        "integration_close_delivered_pr",
        "integration_fast_forward_pr",
        "integration_delegate_publication",
        "integration_revoke_publication",
    }
)


def _outcomes(names, successes):
    return tuple(
        OutcomeSpec(
            name=name,
            classification=OutcomeClass.SUCCESS if name in successes else OutcomeClass.FAILURE,
        )
        for name in names
    )


def _named_contract(
    name, args_model, result_model, *, title: str, summary: str, **execution,
) -> CommandContract:
    return CommandContract(
        execution=_natural_execution(
            name=name, args_model=args_model, result_model=result_model, **execution,
        ),
        presentation=CommandPresentation(title=title, summary=summary),
    )


def _with_effects(contract: CommandContract, effects, *, title: str, summary: str):
    return contract.model_copy(update={
        "execution": contract.execution.model_copy(update={"effects": effects}),
        "presentation": CommandPresentation(title=title, summary=summary),
    })


def _natural_execution(*, name, args_model, result_model, **kwargs):
    defaults = {"receipt_projection": tuple(result_model.model_fields)}
    return ExecutionContract(
        name=name,
        args_model=args_model,
        result_model=result_model,
        capability=name,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
        **(defaults | kwargs),
    )


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


class IntegrationContainmentCutoverArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    dry_run: bool = True
    choices: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None
    expected_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class IntegrationContainmentCutoverValue(CommandValue):
    plan: dict[str, Any] | None = None
    blockers: tuple[dict[str, Any], ...] = ()
    generation: int | None = None
    replayed: bool = False


class IntegrationSettleBatchArgs(IntegrationAbortBatchArgs):
    expected_target_sha: str | None = None

    @model_validator(mode="after")
    def require_preview_target(self):
        if not self.dry_run and (
            not is_valid_git_oid(self.expected_target_sha or "") or not self.reason.strip()
        ):
            raise ValueError("apply requires the previewed target SHA and a nonblank reason")
        return self


class IntegrationRepairTargetArgs(IntegrationSettleBatchArgs):
    expected_candidate_sha: str | None = None

    @model_validator(mode="after")
    def require_preview_candidate(self):
        if not self.dry_run and not is_valid_git_oid(self.expected_candidate_sha or ""):
            raise ValueError("apply requires the previewed candidate SHA")
        return self


class IntegrationRerequestCheckArgs(IntegrationAbortBatchArgs):
    check_name: str = Field(min_length=1)
    expected_candidate_sha: str | None = None

    @model_validator(mode="after")
    def require_candidate(self):
        if not self.dry_run and (not is_valid_git_oid(self.expected_candidate_sha or "")
                                or not self.reason.strip()):
            raise ValueError("apply requires the previewed candidate SHA and a nonblank reason")
        return self


#: Refusals of a withheld-source release; each names the owner decision it needs.
RELEASE_WITHHELD_REFUSALS = (
    "unknown_batch", "not_aborted", "not_a_member", "already_ejected",
    "source_changed", "target_changed", "source_delivered", "identity_retired",
    "source_not_accepted", "operator_decision_hold", "live_writer", "batch_active",
    "repair_outstanding", "repair_exhausted",
)


class IntegrationReleaseWithheldSourceArgs(IntegrationAbortBatchArgs):
    task_id: str = Field(min_length=1)
    expected_source_sha: str | None = None
    expected_target_sha: str | None = None

    @model_validator(mode="after")
    def require_preview_shas(self):
        if not self.dry_run and (
            not is_valid_git_oid(self.expected_source_sha or "")
            or not is_valid_git_oid(self.expected_target_sha or "")
            or not self.reason.strip()
        ):
            raise ValueError("apply requires the previewed source and target SHAs "
                             "and a nonblank reason")
        return self


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


class IntegrationReverifyEpicArgs(IntegrationRefreshEpicArgs):
    expected_head_sha: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def require_preview_source(self):
        if not self.dry_run and (
            not is_valid_git_oid(self.expected_head_sha or "") or not self.reason.strip()
        ):
            raise ValueError("apply requires the preview head and a nonblank reason")
        return self


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


class IntegrationRepairTargetValue(IntegrationTrainControlValue):
    evidence: dict[str, Any] = Field(default_factory=dict)


class IntegrationReleaseWithheldSourceValue(IntegrationTrainControlValue):
    source_sha: str | None = None
    repair_attempt_count: int | None = None


class IntegrationRefreshEpicValue(IntegrationTrainControlValue):
    default_ref: str | None = None
    default_sha: str | None = None
    ahead: int | None = None
    behind: int | None = None
    state: str | None = None
    detail: dict[str, Any] | None = None


class IntegrationReverifyEpicValue(IntegrationTrainControlValue):
    head_sha: str | None = None
    completion_id: str | None = None
    previous_completion_id: str | None = None
    state: str | None = None
    detail: dict[str, Any] | None = None


class IntegrationStatusReadArgs(IntegrationStatusArgs):
    control_only: bool = False


class IntegrationAppInputsArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    #: A policy document to build from instead of the bound one (``--policy FILE``).
    policy: dict[str, Any] | None = None
    #: Defaults to the project's designated integration repository.
    repository_id: str | None = Field(default=None, min_length=1)


class IntegrationTrustManifestArgs(IntegrationAppInputsArgs):
    #: CI-only bootstrap document (``--hosted-check-policy FILE``).
    hosted_check_policy: HostedCheckPolicy | None = None

    @model_validator(mode="after")
    def exclusive_policy_inputs(self) -> IntegrationTrustManifestArgs:
        if self.policy is not None and self.hosted_check_policy is not None:
            raise ValueError("policy and hosted_check_policy are mutually exclusive")
        return self


class IntegrationTrustManifestValue(CommandValue):
    project_id: str | None = None
    repository_id: str | None = None
    policy_source: Literal["argument", "bound", "hosted_check_policy"] | None = None
    github_repository_id: int | None = None
    full_name: str | None = None
    attestation_app_id: int | None = None
    path: str | None = None
    manifest: dict[str, Any] | None = None
    text: str | None = None
    sha256: str | None = None
    committed: dict[str, Any] | None = None


class IntegrationAppVerifyArgs(IntegrationAppInputsArgs):
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
    dry_run: bool = True
    no_push: bool = False
    self_owned: bool = False

    @model_validator(mode="after")
    def exactly_one_target(self) -> IntegrationReleaseOwnerArgs:
        if (self.task_id is None) == (self.owner_row_id is None):
            raise ValueError("exactly one of task_id or owner_row_id is required")
        return self


class IntegrationReserveOwnerArgs(CommandArgs):
    task_id: str = Field(min_length=1)


class IntegrationDelegatePublicationArgs(CommandArgs):
    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    claim_epoch: StrictInt = Field(ge=0)
    target_branch: str = Field(min_length=1)
    expected_target_oid: str
    expected_owner_fence: StrictInt = Field(ge=0)
    ttl_seconds: StrictInt = Field(default=480, ge=1, le=480)
    reason: str = Field(min_length=1)

    @field_validator("target_branch")
    @classmethod
    def valid_target(cls, value):
        from src.git.manager import GitError, _validate_ref

        value = value.removeprefix("refs/heads/")
        try:
            _validate_ref(value)
        except GitError as exc:
            raise ValueError(str(exc)) from exc
        if value.startswith("refs/"):
            raise ValueError("target must be a branch")
        return value

    @field_validator("expected_target_oid")
    @classmethod
    def valid_target_oid(cls, value):
        if not is_valid_git_oid(value) or set(value) == {"0"}:
            raise ValueError("expected target must be an existing full commit OID")
        return value.lower()

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class IntegrationRevokePublicationArgs(CommandArgs):
    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class IntegrationPublicationValue(CommandValue):
    project_id: str | None = None
    delegation_id: str | None = None
    target_branch: str | None = None
    owner_fence: int | None = None
    expires_at: float | None = None
    revoked_at: float | None = None
    published_oid: str | None = None


class IntegrationRestoreTaskOriginArgs(CommandArgs):
    task_id: str = Field(min_length=1)
    dry_run: bool = True
    origin_id: str | None = Field(default=None, min_length=1)
    expected_remote_oid: str | None = None
    expected_fence_token: StrictInt | None = Field(default=None, ge=0)
    reason: str = ""

    @model_validator(mode="after")
    def require_exact_restore(self):
        if not self.dry_run and (
            not self.origin_id or not is_valid_git_oid(self.expected_remote_oid or "")
            or self.expected_fence_token is None or not self.reason.strip()
        ):
            raise ValueError("apply requires origin_id, expected_remote_oid, "
                             "expected_fence_token and a nonblank reason")
        return self


class IntegrationClearRetainedRecoveryArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    #: Clear only this incident; omitted clears every hold the project's caches show.
    incident_id: str | None = Field(default=None, min_length=1)


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


class _PreviewHeadArgs(CommandArgs):
    """Shared validation; subclasses retain the reviewed argument field order."""

    @field_validator("expected_head_sha", check_fields=False)
    @classmethod
    def expected_head_is_a_commit(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_git_oid(value):
            raise ValueError("expected_head_sha must be a full commit id")
        return value

    @model_validator(mode="after")
    def applying_names_the_head_and_a_reason(self) -> _PreviewHeadArgs:
        if not self.dry_run and (
            self.expected_head_sha is None or self.reason is None or not self.reason.strip()
        ):
            raise ValueError("applying requires expected_head_sha and reason")
        return self


class IntegrationRedriveRootArgs(_PreviewHeadArgs):
    task_id: str = Field(min_length=1)
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)


class IntegrationAuthorizeRootArgs(IntegrationRedriveRootArgs):
    """Dry run by default; applying names the dry run's exact head and a reason."""


class IntegrationRedriveChildArgs(_PreviewHeadArgs):
    task_id: str = Field(min_length=1)
    adopt_source: bool = False
    dispose_obsolete: bool = False
    superseded_by: str | None = None
    expected_parent_head_sha: str | None = None
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("superseded_by", "expected_parent_head_sha")
    @classmethod
    def disposal_commit_is_full(cls, value):
        if value is not None and not is_valid_git_oid(value):
            raise ValueError("obsolete disposition requires full commit ids")
        return value

    @model_validator(mode="after")
    def disposal_names_proof(self):
        if self.dispose_obsolete:
            if self.adopt_source or not self.superseded_by:
                raise ValueError("dispose_obsolete requires superseded_by and excludes adopt_source")
            if not self.dry_run and not self.expected_parent_head_sha:
                raise ValueError("applying obsolete disposition requires expected_parent_head_sha")
        elif self.superseded_by or self.expected_parent_head_sha:
            raise ValueError("superseded_by and expected_parent_head_sha require dispose_obsolete")
        return self


class IntegrationRecoverCompletedContainerArgs(IntegrationRedriveRootArgs):
    project_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    expected_completion_id: str | None = Field(default=None, min_length=1)
    expected_generation: StrictInt | None = Field(default=None, ge=0)
    expected_version: StrictInt | None = Field(default=None, ge=0)
    expected_origin_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def applying_pins_preview(self):
        if not self.dry_run and any(getattr(self, name) is None for name in (
            "expected_completion_id", "expected_generation", "expected_version",
            "expected_origin_sha256", "expected_snapshot_sha256",
        )):
            raise ValueError("apply requires completion, generation, version, origin and snapshot from preview")
        if self.branch.startswith("refs/") or self.branch.startswith("-"):
            raise ValueError("branch must name the exact task branch without a refs prefix")
        return self


class IntegrationRecoverCompletedContainerValue(CommandValue):
    remote_head_sha: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    repository_id: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    completion_id: str | None = None
    origin_sha256: str | None = None
    snapshot_sha256: str | None = None
    generation: int | None = None
    version: int | None = None
    old_head_sha: str | None = None
    tree_sha: str | None = None
    receipt: dict[str, Any] | None = None
    reason: str | None = None


class IntegrationReopenCollectionArgs(IntegrationRedriveRootArgs):
    """Dry run by default; applying names the parent branch head the dry run reported."""


class IntegrationCloseDeliveredPrArgs(_PreviewHeadArgs):
    project_id: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)


class IntegrationFastForwardPrArgs(_PreviewHeadArgs):
    task_id: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    dry_run: bool = True
    expected_head_sha: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)


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


class IntegrationFastForwardPrValue(CommandValue):
    """A separately filed repair's head adopted into its root PR branch."""

    task_id: str | None = None
    project_id: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    target_branch: str | None = None
    previous_oid: str | None = None
    published_oid: str | None = None
    root_task_id: str | None = None
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
    parent_head_sha: str | None = None
    merge_head_sha: str | None = None
    superseded_by: str | None = None
    tree_sha: str | None = None
    evidence_id: str | None = None
    completion_id: str | None = None
    collection: str | None = None
    checkpoint: dict[str, Any] | None = None
    reason: str | None = None


class IntegrationTransferOwnerArgs(CommandArgs):
    target: BranchKey
    expected_token: int
    next_owner_id: str
    next_role: str


class IntegrationTransferOwnerValue(CommandValue):
    fence: Fence | None = None


class DeliveryReceiptsArgs(CommandArgs):
    source_task_id: str
    repository_id: str
    target_branch: str


class DeliveryReceiptsValue(CommandValue):
    receipts: tuple[dict[str, Any], ...] = ()


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


class IntegrationSupersedeCompletionArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    expected_version: StrictInt = Field(ge=0)
    expected_claim_epoch: StrictInt = Field(ge=0)
    completion_id: str = Field(min_length=1)
    expected_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    human_reply_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expected_proof_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    dry_run: bool = True

    @model_validator(mode="after")
    def exact_supersession(self):
        if not self.reason.strip():
            raise ValueError("reason must be nonblank")
        if not self.dry_run and self.expected_proof_sha256 is None:
            raise ValueError("apply requires the previewed proof SHA256")
        return self


class IntegrationSupersedeCompletionValue(CommandValue):
    task_id: str | None = None
    subject_id: str | None = None
    proof_sha256: str | None = None
    proof: dict[str, Any] | None = None
    journal_seq: int | None = None
    cleanup: dict[str, Any] | None = None
    dry_run: bool = True


class IntegrationOffArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    dry_run: bool = True
    expected_plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_control(self):
        if not self.reason.strip():
            raise ValueError("reason must be nonblank")
        if not self.dry_run and self.expected_plan_digest is None:
            raise ValueError("apply requires the exact preview plan digest")
        return self


class IntegrationManualValue(CommandValue):
    project_id: str | None = None
    dry_run: bool = True
    plan: dict[str, Any] | None = None
    plan_digest: str | None = None


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
    return _named_contract(
        name, args_model, result_model,
        outcomes=_outcomes(outcomes, successes),
        side_effect=side_effect,
        supports_preview=supports_preview,
        effects=effects,
        sensitive_args=frozenset({"reason"}) if "reason" in args_model.model_fields else frozenset(),
        title=name.replace("_", " ").title(),
        summary="Authenticated hierarchical integration operational control.",
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
            "Render the App-mode trust manifest from a full or hosted-check policy, the authenticated "
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
    ("acquired", "already_reserved", "running", "not_eligible", "not_found"),
    successes=frozenset({"acquired", "already_reserved", "running"}),
    side_effect=SideEffectClass.UPDATE,
)

INTEGRATION_DELEGATE_PUBLICATION = _operational_contract(
    "integration_delegate_publication", IntegrationDelegatePublicationArgs,
    ("issued", "refused"), successes=frozenset({"issued"}),
    side_effect=SideEffectClass.UPDATE, result_model=IntegrationPublicationValue,
)

INTEGRATION_REVOKE_PUBLICATION = _operational_contract(
    "integration_revoke_publication", IntegrationRevokePublicationArgs,
    ("revoked", "refused"), successes=frozenset({"revoked"}),
    side_effect=SideEffectClass.UPDATE, result_model=IntegrationPublicationValue,
)

INTEGRATION_RESTORE_TASK_ORIGIN = _operational_contract(
    "integration_restore_task_origin",
    IntegrationRestoreTaskOriginArgs,
    ("preview", "restored", "not_eligible", "not_found"),
    successes=frozenset({"preview", "restored"}),
    side_effect=SideEffectClass.COMPOSITE,
    supports_preview=True,
)

CLEAR_RETAINED_RECOVERY_OUTCOMES = ("cleared", "not_clean", "unavailable", "not_found")

INTEGRATION_CLEAR_RETAINED_RECOVERY = _operational_contract(
    "integration_clear_retained_recovery",
    IntegrationClearRetainedRecoveryArgs,
    CLEAR_RETAINED_RECOVERY_OUTCOMES,
    successes=frozenset({"cleared"}),
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

INTEGRATION_FAST_FORWARD_PR = _operational_contract(
    "integration_fast_forward_pr",
    IntegrationFastForwardPrArgs,
    ("would_publish", "published", "already_published", "changed", "blocked", "not_eligible",
     "not_found", "invalid"),
    successes=frozenset({"would_publish", "published", "already_published"}),
    side_effect=SideEffectClass.COMPOSITE,
    result_model=IntegrationFastForwardPrValue,
)


INTEGRATION_TRANSFER_OWNER = CommandContract(
    execution=_natural_execution(
        name="integration_transfer_owner",
        args_model=IntegrationTransferOwnerArgs,
        result_model=IntegrationTransferOwnerValue,
        outcomes=_outcomes(
            ("transferred", "busy", "stale_owner", "human_required"),
            {"transferred"},
        ),
        side_effect=SideEffectClass.UPDATE,
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
    return _named_contract(
        name, args_model, result_model,
        outcomes=_outcomes(outcomes, successes),
        side_effect=SideEffectClass.COMPOSITE,
        effects=effects,
        sensitive_args=sensitive_args,
        sensitive_result_fields=sensitive_results,
        title=name.replace("_", " ").title(),
        summary=summary,
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


DELIVERY_RECEIPTS = _named_contract(
    "delivery_receipts", DeliveryReceiptsArgs, DeliveryReceiptsValue,
    outcomes=_outcomes(('found', 'not_found'), {'found', 'not_found'}),
    side_effect=SideEffectClass.READ,
    effects=(ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),),
    receipt_projection=(),
    title="Read delivery receipts",
    summary="Read repository-qualified delivery evidence for one source task.",
)


INTEGRATION_CLEANUP = _named_contract(
    "integration_cleanup", IntegrationCleanupArgs, IntegrationCleanupValue,
    outcomes=_outcomes(
        (
            "materialized", "advanced", "complete", "already_complete", "wait", "retryable",
            "conflict", "failed", "stale", "invariant_error",
        ),
        {"materialized", "advanced", "complete", "already_complete"},
    ),
    side_effect=SideEffectClass.COMPOSITE,
    effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
    title="Advance integration cleanup",
    summary="Materialize and advance bounded cleanup for one terminal root batch.",
)


def _root_subject_contract(
    name: str,
    args_model: type[CommandArgs],
    value_model: type[CommandValue],
    outcomes: tuple[str, ...],
    successes: frozenset[str],
    title: str,
) -> CommandContract:
    return _named_contract(
        name, args_model, value_model,
        outcomes=_outcomes(outcomes, successes),
        side_effect=SideEffectClass.COMPOSITE,
        effects=(UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION),),
        title=title,
        summary=title,
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


INTEGRATION_FILE_CHILDREN = _named_contract(
    "integration_file_children", IntegrationFileChildrenArgs, IntegrationFileChildrenValue,
    outcomes=_outcomes(('filed', 'stale_parent', 'invalid'), {'filed'}),
    side_effect=SideEffectClass.COMPOSITE,
    effects=(
        UpdateClause(subject=EffectSubject.TASK_GRAPH),
        UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP),
    ),
    receipt_projection=("generation", "children", "origins"),
    title="File isolated child tasks",
    summary="Reserve child origins and advance the parent integration generation atomically.",
)


INTEGRATION_CHECKPOINT_PARENT = _named_contract(
    "integration_checkpoint_parent", IntegrationCheckpointParentArgs, IntegrationCheckpointParentValue,
    outcomes=_outcomes(
        ("checkpointed", "already_waiting", "dirty", "stale"),
        {"checkpointed", "already_waiting"},
    ),
    side_effect=SideEffectClass.UPDATE,
    effects=(UpdateClause(subject=EffectSubject.TASK),),
    sensitive_args=frozenset({"head_sha"}),
    sensitive_result_fields=frozenset({"head_sha"}),
    receipt_projection=("task_id", "generation", "head_sha"),
    title="Checkpoint integration parent",
    summary="Pin the parent head and generation before waiting for child deliveries.",
)


def _parent_contract(name, args_model, result_model, outcomes, *, side_effect, summary):
    return _named_contract(
        name, args_model, result_model,
        outcomes=_outcomes(outcomes, {'ready', 'verified', 'completed', 'already_completed'}),
        side_effect=side_effect,
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
        title=name.replace("_", " ").title(),
        summary=summary,
    )


INTEGRATION_DELIVERY_READINESS = _parent_contract(
    "integration_delivery_readiness",
    IntegrationDeliveryReadinessArgs,
    IntegrationDeliveryReadinessValue,
    ("ready", "waiting", "failed", "invariant_error"),
    side_effect=SideEffectClass.READ,
    summary="Read whether every child of one parent has delivered, changing nothing.",
)
INTEGRATION_RECORD_NOOP = _named_contract(
    "integration_record_noop", IntegrationRecordNoopArgs, IntegrationRecordNoopValue,
    outcomes=_outcomes(
        ("recorded", "stale_head", "invalid", "delivery_target_fixed"),
        {"recorded"},
    ),
    side_effect=SideEffectClass.UPDATE,
    effects=(
        UpdateClause(subject=EffectSubject.DELIVERY_EVIDENCE),
        UpdateClause(subject=EffectSubject.TASK),
    ),
    sensitive_args=frozenset({"expected_head_sha"}),
    sensitive_result_fields=frozenset({"reviewed_head_sha", "reviewed_tree_sha"}),
    receipt_projection=("receipt_id", "revision"),
    title="Record verified no-code child disposition",
    summary="Bind a child's current no-op completion and exact Git head to its parent receipt.",
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


INTEGRATION_MUTATE_HIERARCHY = _named_contract(
    "integration_mutate_hierarchy", IntegrationMutateHierarchyArgs, IntegrationMutateHierarchyValue,
    outcomes=_outcomes(
        ("updated", "sealed", "delivery_target_fixed", "reopen_required", "invalid"),
        {"updated"},
    ),
    side_effect=SideEffectClass.COMPOSITE,
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
    title="Mutate integration hierarchy",
    summary="Apply a guarded hierarchy change and invalidate affected parent generations.",
)


def _contract_adapter(contract: CommandContract):
    """Bind the shared typed adapter to its authoritative contract declaration."""
    execution = contract.execution
    outcomes = {row.name for row in execution.outcomes}
    aliases = None
    failure_outcomes = frozenset({"unauthorized", "runtime_error"})
    if contract is INTEGRATION_COMPLETE_PARENT:
        # Keep the frozen playbook routing; surface the precise reason in summary.
        aliases = {AWAITING_TRUSTED_VERIFICATION: "waiting"}
    elif contract is INTEGRATION_REPAIR_DISPATCH:
        aliases = {"unknown": "busy"}
    elif contract is INTEGRATION_RECORD_REPAIR:
        # "started" is declared for compatibility but never returned by this adapter.
        outcomes -= {"started"}
    elif contract is INTEGRATION_TRANSFER_OWNER:
        failure_outcomes = frozenset()

    async def invoke(args: CommandArgs, ctx: CommandContext | None) -> CommandResult:
        return await _hierarchy_adapter(
            contract.name, args, ctx, execution.result_model, outcomes,
            aliases=aliases, failure_outcomes=failure_outcomes,
        )

    return invoke


async def _hierarchy_adapter(
    command: str,
    args: CommandArgs,
    ctx: CommandContext | None,
    value_model: type[CommandValue],
    outcomes: set[str],
    aliases: dict[str, str] | None = None,
    *, failure_outcomes: frozenset[str] = frozenset({"unauthorized", "runtime_error"}),
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
    if outcome not in outcomes | failure_outcomes:
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


def _register_contract(
    registry: ContractRegistry, contract: CommandContract, *, preview: bool | None = None,
):
    adapter = (_promote_schema_adapter if contract is PROMOTE_SCHEMA else _contract_adapter(contract))
    preview_adapter = None
    if preview or (preview is None and contract.execution.supports_preview):
        async def preview_adapter(args, ctx):
            return await adapter(args.model_copy(update={"dry_run": True}), ctx)

    registry.register(CommandRegistration(contract.name, contract, adapter, preview_adapter))


class IntegrationContinueCloseArgs(CommandArgs):
    operation_id: str = Field(min_length=1)
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None


class IntegrationContinueCloseValue(CommandValue):
    operation_id: str | None = None
    close: dict[str, Any] | None = None


async def _continue_close(args, ctx):
    return await _hierarchy_adapter("integration_continue_close", args, ctx,
        IntegrationContinueCloseValue, {"completed", "pending", "refused"})


def register_integration_contracts(registry: ContractRegistry) -> None:
    """Register contracts whose real handlers have landed.

    Each implementation task adds its handler and typed authority/redaction
    declaration together.  Unavailable security-sensitive mutations remain
    outside the allowlist.
    """
    registry.register(CommandRegistration(
        "integration_continue_close",
        _operational_contract("integration_continue_close", IntegrationContinueCloseArgs,
            ("completed", "pending", "refused"), successes=frozenset({"completed"}),
            side_effect=SideEffectClass.COMPOSITE, result_model=IntegrationContinueCloseValue),
        _continue_close,
    ))
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

        _register_contract(registry, contract, preview=not read_only)
    for name, args_model, applied, value_model in (
        ("integration_off", IntegrationOffArgs, "disabled", IntegrationManualValue),
        ("integration_cutover_containment", IntegrationContainmentCutoverArgs, "prepared",
         IntegrationContainmentCutoverValue),
        ("integration_migrate_provenance_refs", IntegrationMigrateProvenanceRefsArgs, "migrated",
         IntegrationMigrateProvenanceRefsValue),
        ("integration_retire_legacy_park", IntegrationRetireLegacyParkArgs, "retired",
         IntegrationRetireLegacyParkValue),
        ("integration_quiesce", IntegrationQuiesceArgs, "quiesced", IntegrationQuiesceValue),
        ("integration_supersede_completion", IntegrationSupersedeCompletionArgs, "superseded",
         IntegrationSupersedeCompletionValue),
        ("integration_record_delivered", IntegrationRecordDeliveredArgs, "recorded",
         IntegrationRecordDeliveredValue),
        ("integration_record_root_noop", IntegrationRecordRootNoopArgs, "recorded",
         IntegrationRecordRootNoopValue),
        ("integration_abort_batch", IntegrationAbortBatchArgs, "aborted", IntegrationTrainControlValue),
        ("integration_settle_batch", IntegrationSettleBatchArgs, "settled", IntegrationTrainControlValue),
        ("integration_release_withheld_source", IntegrationReleaseWithheldSourceArgs, "released",
         IntegrationReleaseWithheldSourceValue),
        ("integration_pause_batch", IntegrationAbortBatchArgs, "paused", IntegrationTrainControlValue),
        ("integration_resume_batch", IntegrationAbortBatchArgs, "resumed", IntegrationTrainControlValue),
        ("integration_repair_target", IntegrationRepairTargetArgs, "repairing", IntegrationRepairTargetValue),
        ("integration_rerequest_check", IntegrationRerequestCheckArgs, "reconciled",
         IntegrationRepairTargetValue),
        ("integration_seal_now", IntegrationSealNowArgs, "sealed", IntegrationTrainControlValue),
        ("integration_retire_origin", IntegrationRetireOriginArgs, "retired", IntegrationTrainControlValue),
        ("integration_refresh_epic", IntegrationRefreshEpicArgs, "refreshed", IntegrationRefreshEpicValue),
        ("integration_reverify_epic", IntegrationReverifyEpicArgs, "visited",
         IntegrationReverifyEpicValue),
    ):
        if registry.get(name) is not None:
            continue
        outcomes = ("preview", applied, "refused")
        successes = {"preview", applied}
        if name == "integration_supersede_completion":
            outcomes += ("already_superseded",)
            successes.add("already_superseded")
        if name in {"integration_migrate_provenance_refs", "integration_cutover_containment"}:
            outcomes += ("blocked",)
        if name == "integration_seal_now":
            outcomes += ("no_ready_work", "existing_batch")
            successes.update({"no_ready_work", "existing_batch"})
        elif name == "integration_settle_batch":
            outcomes += ("unknown_batch", "target_not_settled")
        elif name == "integration_release_withheld_source":
            outcomes += ("already_released", *RELEASE_WITHHELD_REFUSALS)
            successes.add("already_released")
        elif name == "integration_refresh_epic":
            outcomes += ("pending", "current")
            successes.update({"pending", "current"})
        elif name == "integration_reverify_epic":
            outcomes += ("pending", "changed")
            successes.add("pending")
        contract = _operational_contract(
            name, args_model, outcomes, successes=frozenset(successes),
            side_effect=SideEffectClass.COMPOSITE, result_model=value_model,
            supports_preview=True,
        )
        if name == "integration_cutover_containment":
            contract = contract.model_copy(update={
                "execution": contract.execution.model_copy(update={"effects": (
                    ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),
                    UpdateClause(subject=EffectSubject.DELIVERY_EVIDENCE,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                )}),
                "presentation": CommandPresentation(
                    title="Prepare strict-containment history",
                    summary="Local operator previews and applies exact historical lifecycle exclusions.",
                ),
            })
        if name == "integration_off":
            subject = EffectSubject.INTEGRATION_OPERATION
            contract = _with_effects(
                contract, (
                    ReadClause(subject=subject),
                    UpdateClause(subject=subject,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                ),
                title="Disable automatic integration",
                summary="Local operator applies an exact preview while preserving sources and frozen work.",
            )
        if name == "integration_migrate_provenance_refs":
            contract = _with_effects(
                contract, (
                    ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),
                    UpdateClause(subject=EffectSubject.DELIVERY_EVIDENCE,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                ),
                title="Move Git provenance out of branches",
                summary="Local operator copies and verifies immutable provenance refs before deleting legacy heads.",
            )
        if name == "integration_retire_legacy_park":
            contract = _with_effects(
                contract, (
                    ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                    UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                ),
                title="Retire a historical parked operation",
                summary="Local operator abandons every explicitly selected source under an exact journal fence.",
            )
        if name in {"integration_record_root_noop", "integration_record_delivered"}:
            applying = ClausePredicate(arg_equals=("dry_run", False))
            contract = _with_effects(
                contract, (
                    ReadClause(subject=EffectSubject.TASK),
                    UpdateClause(subject=EffectSubject.TASK, when=applying),
                    CreateOrReuseClause(subject=EffectSubject.DELIVERY_EVIDENCE,
                                        key_arg="task_id", when=applying),
                    UpdateClause(subject=EffectSubject.DOWNSTREAM_TASKS, when=applying),
                ),
                title="Record verified no-code root completion",
                summary="Preview or complete an unheld root with exact no-artifact Git provenance.",
            )

        if name == "integration_supersede_completion":
            applying = ClausePredicate(arg_equals=("dry_run", False))
            contract = _with_effects(
                contract, (
                    ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                    ReadClause(subject=EffectSubject.ESCALATION_REPLY),
                    ReadClause(subject=EffectSubject.DELIVERY_EVIDENCE),
                    UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION, when=applying),
                    UpdateClause(subject=EffectSubject.TASK, when=applying),
                    UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP, when=applying),
                    UpdateClause(subject=EffectSubject.DOWNSTREAM_TASKS, when=applying),
                ),
                title="Supersede one retained completion",
                summary="Operator or owning supervisor retires an exact stopped Subject with original verified human evidence.",
            )
        if name == "integration_quiesce":
            applying = ClausePredicate(arg_equals=("dry_run", False))
            contract = _with_effects(
                contract, (
                    ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                    UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION, when=applying),
                    UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP, when=applying),
                ),
                title="Quiesce idle train admission",
                summary="Local operator closes an unfrozen root and releases explicitly fenced idle reservations.",
            )
        if name == "integration_record_delivered":
            contract = _with_effects(
                contract, (
                    *contract.execution.effects,
                    UpdateClause(subject=EffectSubject.BRANCH_OWNERSHIP,
                                 when=ClausePredicate(arg_equals=("dry_run", False))),
                ),
                title="Record exact externally delivered completion",
                summary="Local operator verifies exact source on the designated default and records shipped work.",
            )

        _register_contract(registry, contract)
    name = "integration_recover_completed_container"
    if registry.get(name) is None:
        outcomes = {"preview", "recovered", "already_recovered", "refused", "invalid"}
        contract = _operational_contract(
            name, IntegrationRecoverCompletedContainerArgs, sorted(outcomes),
            successes=frozenset({"preview", "recovered", "already_recovered"}),
            side_effect=SideEffectClass.COMPOSITE,
            result_model=IntegrationRecoverCompletedContainerValue, supports_preview=True,
        )
        contract = _with_effects(
            contract, (
                ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                ReadClause(subject=EffectSubject.BRANCH_OWNERSHIP),
                UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION,
                             when=ClausePredicate(arg_equals=("dry_run", False))),
            ),
            title="Recover a completed container checkpoint",
            summary="Prove accepted published pass provenance and CAS only its checkpoint; delivery gates remain.",
        )

        _register_contract(registry, contract)
    name = "integration_reconcile_expired_mutation"
    if registry.get(name) is None:
        outcomes = ("preview", "applied", "superseded", "refused")
        value_model = IntegrationReconcileExpiredMutationValue
        contract = _operational_contract(
            name, IntegrationReconcileExpiredMutationArgs, outcomes,
            successes=frozenset({"preview", "applied", "superseded"}),
            side_effect=SideEffectClass.COMPOSITE, result_model=value_model, supports_preview=True,
        )
        contract = _with_effects(
            contract, (
                ReadClause(subject=EffectSubject.INTEGRATION_OPERATION),
                UpdateClause(subject=EffectSubject.INTEGRATION_OPERATION,
                             when=ClausePredicate(arg_equals=("dry_run", False))),
            ),
            title="Reconcile an expired mutation after its operation ended",
            summary="Local operator verifies nonce, fences, stopped authority and actual remote target.",
        )

        _register_contract(registry, contract)
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

        _register_contract(registry, contract, preview=False)
    name = "integration_engine_transfer"
    if registry.get(name) is None:
        contract = _operational_contract(name, IntegrationEngineTransferArgs,
            ("preview", "transferred", "refused"), successes=frozenset({"preview", "transferred"}),
            side_effect=SideEffectClass.COMPOSITE, result_model=IntegrationEngineTransferValue)

        _register_contract(registry, contract, preview=False)
    for contract in (
        INTEGRATION_TRANSFER_OWNER,
        INTEGRATION_STATUS,
        INTEGRATION_PROMOTION_PUBLISH,
        PROMOTE_SCHEMA,
        PROMOTE_VALIDATE,
        PROMOTE_RULESETS,
        INTEGRATION_TRUST_MANIFEST,
        INTEGRATION_APP_VERIFY,
        INTEGRATION_EJECT,
        INTEGRATION_REEVALUATE_REPAIR,
        INTEGRATION_RELEASE_OWNER,
        INTEGRATION_RESTORE_TASK_ORIGIN,
        INTEGRATION_RESERVE_OWNER,
        INTEGRATION_DELEGATE_PUBLICATION,
        INTEGRATION_REVOKE_PUBLICATION,
        INTEGRATION_CLEAR_RETAINED_RECOVERY,
        INTEGRATION_RELEASE_STALE_OWNERS,
        INTEGRATION_REDRIVE_ROOT,
        INTEGRATION_AUTHORIZE_ROOT,
        INTEGRATION_REDRIVE_CHILD,
        INTEGRATION_REOPEN_COLLECTION,
        INTEGRATION_CLOSE_DELIVERED_PR,
        INTEGRATION_FAST_FORWARD_PR,
        INTEGRATION_FILE_CHILDREN,
        INTEGRATION_CHECKPOINT_PARENT,
        INTEGRATION_MUTATE_HIERARCHY,
        INTEGRATION_DELIVERY_READINESS,
        INTEGRATION_RECORD_NOOP,
        INTEGRATION_PARENT_VERIFY,
        INTEGRATION_COMPLETE_PARENT,
        DELIVERY_RECEIPTS,
        INTEGRATION_CLEANUP,
        INTEGRATION_REPAIR_CLOSE_CURRENT,
        INTEGRATION_CI_EVIDENCE,
        INTEGRATION_RELEASE,
        INTEGRATION_REPAIR_START,
        INTEGRATION_REPAIR_DISPATCH,
        INTEGRATION_RECORD_REPAIR,
        INTEGRATION_REPAIR_TIMEOUT,
    ):
        if registry.get(contract.name) is None:
            _register_contract(registry, contract)


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
