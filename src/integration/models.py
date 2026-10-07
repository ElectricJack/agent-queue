"""Frozen value objects shared by integration playbooks and core commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

# Owner roles whose ``owner_id`` is a task id and whose reservation therefore
# survives its writer session.  ``arelease_integration_writer_for_retry``
# self-transfers one of these back to ``reserved``; ``collector`` (owned by an
# operation/batch) and ``verifier`` (reused while still attached, see
# ``RepairService._reuse_verifier_on``) are deliberately absent.
RETRYABLE_INTEGRATION_OWNER_ROLES = frozenset({"worker", "repair"})

# Owner roles a *re-queue* may return to ``reserved``.  Returning a task to
# the frontier is a self-transfer -- same ``owner_id``, same ``owner_role``,
# no successor -- so design spec 9.1's transfer rule, which is why
# ``verifier`` and ``collector`` are absent above, is not engaged.
# ``verifier`` is admissible here for the same reason it is excluded there:
# ``RepairService._reuse_verifier_on`` reuses a verifier only while it is
# still ``attached`` to a *live* session with an ASSIGNED/IN_PROGRESS task,
# and a re-queued verifier has neither, so its ``attached`` row is dead
# weight that blocks every subsequent claim rather than a reusable writer.
# ``collector`` stays out: its ``owner_id`` is an operation/batch, not a task.
REQUEUE_INTEGRATION_OWNER_ROLES = RETRYABLE_INTEGRATION_OWNER_ROLES | {"verifier"}

# The project ``max_wait`` of the train simplification spec (section 3.6):
# the longest an undelivered integration event keeps retrying before the
# outbox quarantines it as an explicit failed delivery.
DEFAULT_INTEGRATION_MAX_WAIT_SECONDS = 3600.0

# Shared refusal code for completion without a trusted CI binding. Keep this
# in the value layer so command schemas need no database implementation imports.
AWAITING_TRUSTED_VERIFICATION = "awaiting_trusted_verification"


class BranchKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str
    branch: str


class Fence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: BranchKey
    owner_id: str
    token: int


class PromotionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_key: str
    source_task_id: str
    source_head: str
    source_base: str
    expected_target: str
    fence: Fence


class PromotionValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    receipt_id: str | None = None
    prepared_sha: str | None = None


class ConflictResolutionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    operation_id: str
    resolved_head_sha: str
    resolved_tree_sha: str
    repair_commit_shas: tuple[str, ...] = Field(min_length=1)
    fence: Fence


class RequiredCheckSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    names: tuple[str, ...] = Field(min_length=1)
    producer_id: str


class RepairPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_seconds: int = Field(default=1800, gt=0)
    primary_attempts: int = Field(default=3, gt=0)
    debug_seconds: int = Field(default=3600, gt=0)
    debug_attempts: int = Field(default=3, gt=0)
    debug_intelligence_class: str
    conflict_scope: Literal["member", "batch"] = "member"
    on_exhausted: Literal["human", "continue"] = "human"
    source_ci: bool = False
    # Cancellation-only source CI is infrastructure, never a code repair: the
    # observation waits, asks GitHub to re-run the exact head with bounded
    # backoff, and names a blocker once this many consecutive observations have
    # all been infrastructure.  A run with any genuine failing required check
    # is still red, whatever else was cancelled.
    source_ci_infra_attempts: int = Field(default=3, gt=0)
    source_ci_infra_backoff_seconds: float = Field(default=300.0, gt=0)
    source_ci_infra_backoff_max_seconds: float = Field(default=3600.0, gt=0)
    # Deprecated and ignored: mandatory routing files repairs with the
    # ``debug_intelligence_class`` hint only and the router writes the
    # route.  The field stays so stored ``policy_snapshot`` values naming it
    # still validate under ``extra="forbid"``; new writes naming it are
    # refused (``deprecated_route_fields``).
    debug_profile_id: str | None = None

    def allocation_stage(self, filed_attempts: int) -> Literal["primary", "debug", "human"]:
        """Escalate the next ordinary filing; green observations spend no budget."""
        if filed_attempts < self.primary_attempts:
            return "primary"
        if (filed_attempts >= self.primary_attempts + self.debug_attempts
                and self.on_exhausted == "human"):
            return "human"
        return "debug"

    @model_validator(mode="after")
    def ordered_source_ci_infra_backoff(self) -> "RepairPolicy":
        if self.source_ci_infra_backoff_max_seconds < self.source_ci_infra_backoff_seconds:
            raise ValueError(
                "source_ci_infra_backoff_max_seconds must be at least "
                "source_ci_infra_backoff_seconds"
            )
        return self


class ArtifactSnapshot(BaseModel):
    """The complete, versioned ``ArtifactRef`` wire identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    playbook_id: str
    artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    schema_generation: int = Field(gt=0)
    contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_build: str
    compiled_at: str | None = None
    version: int = Field(ge=0)


class PlaybookRoute(BaseModel):
    """Stable activation address plus the exact compiled artifact it resolved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    playbook_id: str
    scope: Literal["system", "project", "agent_type", "supervisor"]
    scope_identifier: str
    activation_id: str | None = None
    artifact: ArtifactSnapshot

    @model_validator(mode="after")
    def artifact_matches_route(self) -> "PlaybookRoute":
        if self.artifact.playbook_id != self.playbook_id:
            raise ValueError("route artifact belongs to another playbook")
        if self.scope not in ("system", "project"):
            raise ValueError("integration routes support only system or project scope")
        if self.scope == "system" and self.scope_identifier != "":
            raise ValueError("system integration routes require an empty scope identifier")
        if self.scope == "project" and not self.scope_identifier:
            raise ValueError("project integration routes require a scope identifier")
        return self

    def is_available_to_project(self, project_id: str) -> bool:
        """Whether this canonical route may serve ``project_id``."""
        return self.scope == "system" or (
            self.scope == "project" and self.scope_identifier == project_id
        )


class IntegrationBoundaryPolicy(BaseModel):
    """Frozen inputs for one parent or root integration boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_checks: RequiredCheckSet
    admission: Literal["reviewed", "authorized"] = "reviewed"
    authorized_task_ids: tuple[str, ...] = ()
    repair: RepairPolicy
    route: PlaybookRoute
    primary_intelligence_class: str | None = Field(default=None, min_length=1)
    # Deprecated and ignored, like ``RepairPolicy.debug_profile_id``: repair
    # and verifier tasks carry the ``*_intelligence_class`` hint only.
    primary_profile_id: str | None = Field(default=None, min_length=1)
    verifier_intelligence_class: str | None = Field(default=None, min_length=1)
    # Deprecated and ignored; see ``primary_profile_id``.
    verifier_profile_id: str | None = Field(default=None, min_length=1)


class IntegrationCleanupPolicy(BaseModel):
    """Frozen retention and retry limits for post-promotion cleanup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=5, gt=0)
    retry_base_seconds: float = Field(default=30.0, gt=0)
    retry_max_seconds: float = Field(default=3600.0, gt=0)
    successful_source_refs: Literal["delete", "retain"] = "delete"
    failed_work_retention_seconds: int = Field(default=604800, ge=0)

    @model_validator(mode="after")
    def ordered_backoff(self) -> "IntegrationCleanupPolicy":
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be at least retry_base_seconds")
        return self


class IntegrationTrainPolicy(BaseModel):
    """Root admission timing; epic collection does not wait for this window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cadence_seconds: int = Field(default=300, gt=0)
    settling_cap_seconds: int = Field(default=1800, gt=0)


CISource = Literal["hosted", "local", "hybrid"]
#: The train's target kinds a CI source can be chosen for.  ``root`` covers a
#: root candidate and the root PR gate, ``epic`` an epic (parent boundary)
#: candidate, and ``promotion`` a promotion step's gate.
CITargetKind = Literal["root", "epic", "promotion"]
CI_TARGET_KINDS: tuple[CITargetKind, ...] = ("root", "epic", "promotion")


class IntegrationCIPolicy(BaseModel):
    """Which runner produces each boundary's required checks.

    ``hosted`` reads the GitHub Actions checks the boundary requires.
    ``local`` runs them on this box through the local job runner, one command
    per required check name, and reads no GitHub CI: GitHub is just the push
    remote. ``hybrid`` gates candidates locally, with hosted candidate checks
    and App attestation where ``hosted_attestation`` requires them. Root
    candidates require both by default. Root PR checks remain hosted, and
    ``promotion: hybrid`` uses the hosted promotion gate.

    Whichever runner produced them, green means the boundary's same named
    required checks passed, so baselines, repairs and gate states read alike.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: CISource = "hosted"
    #: Per target kind overrides of ``source``; unset kinds use ``source``.
    root: CISource | None = None
    epic: CISource | None = None
    promotion: CISource | None = None
    #: Hybrid candidate boundaries needing hosted checks and App attestation.
    #: An explicit false opts a boundary out; hosted sources always require both.
    hosted_attestation: dict[Literal["root", "epic"], bool] = Field(
        default_factory=lambda: {"root": True, "epic": False}, strict=True,
    )
    #: The local runner's command for each required check name, e.g.
    #: ``{"lint": "ruff check src", "unit": "aq test tests/test_x.py"}``.
    commands: dict[str, str] = Field(default_factory=dict)
    #: Seconds a local command may wait for a test slot, then may run.
    queue_seconds: int = Field(default=600, ge=0, le=3600)
    run_seconds: int = Field(default=300, gt=0, le=3600)

    @model_validator(mode="after")
    def finite_commands(self) -> IntegrationCIPolicy:
        from src.jobs.adapters import finite_command
        from src.jobs.policy import JobError

        for name, command in self.commands.items():
            if not name:
                raise ValueError("ci.commands names a check with an empty name")
            try:
                finite_command(command)
            except JobError:
                raise ValueError(
                    f"ci.commands[{name!r}] is not a finite job command: {command!r}"
                ) from None
        if self.local_kinds() and not self.commands:
            raise ValueError("a local or hybrid ci source requires ci.commands")
        return self

    def source_for(self, kind: CITargetKind) -> CISource:
        return getattr(self, kind) or self.source

    def requires_hosted(self, kind: CITargetKind) -> bool:
        source = self.source_for(kind)
        return source == "hosted" or (source == "hybrid" and (
            kind == "promotion" or self.hosted_attestation.get(kind, kind == "root")
        ))

    @model_serializer(mode="wrap")
    def _omit_default_hosted_attestation(self, handler: SerializerFunctionWrapHandler):
        dumped = handler(self)
        if self.hosted_attestation == {"root": True, "epic": False}:
            dumped.pop("hosted_attestation", None)
        return dumped

    def local_kinds(self) -> tuple[CITargetKind, ...]:
        """Kinds with a gate the local runner produces.

        A promotion step has no candidate gate of its own, only the checks
        GitHub enforces on its PR, so ``hybrid`` there is ``hosted``.
        """
        return tuple(kind for kind in CI_TARGET_KINDS if self.source_for(kind) == "local"
                     or (self.source_for(kind) == "hybrid" and kind != "promotion"))


class HierarchicalIntegrationPolicy(BaseModel):
    """Validated project policy consumed when reserving an operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    parent: IntegrationBoundaryPolicy
    root: IntegrationBoundaryPolicy
    branchless_parent: Literal["skip", "declared", "verifier"]
    on_failed_child: Literal["block", "ask"]
    on_main_moved: Literal["rebuild", "wait"] = "rebuild"
    cross_epic_prerequisites: Literal["default_branch", "completed"] = "default_branch"
    prerequisite_branches: Literal["stacked", "wait-for-parent"] | None = None
    cleanup: IntegrationCleanupPolicy = Field(default_factory=IntegrationCleanupPolicy)
    train: IntegrationTrainPolicy | None = None
    max_wait_seconds: float = Field(
        default=DEFAULT_INTEGRATION_MAX_WAIT_SECONDS, gt=0, allow_inf_nan=False
    )
    #: Unset is hosted for every target kind.
    ci: IntegrationCIPolicy | None = None

    @model_validator(mode="after")
    def local_commands_cover_required_checks(self) -> HierarchicalIntegrationPolicy:
        # A local runner must produce the boundary's own named checks, or a
        # green local candidate would mean something a hosted one does not.
        if self.ci is None:
            return self
        for kind, boundary in (("root", self.root), ("epic", self.parent)):
            if self.ci.source_for(kind) == "hosted":
                continue
            missing = sorted(set(boundary.required_checks.names) - set(self.ci.commands))
            if missing:
                raise ValueError(
                    f"ci source for {kind} is {self.ci.source_for(kind)} but ci.commands "
                    f"has no command for required check(s) {', '.join(missing)}"
                )
        return self

    @model_serializer(mode="wrap")
    def _omit_optional_defaults(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Batches, operations and stages compare frozen snapshots by dict
        # equality. Older snapshots lack optional policy additions (``train``,
        # ``ci`` and others), so omit their defaults rather than invalidating
        # in-flight work on upgrade.
        dumped = handler(self)
        if self.cross_epic_prerequisites == "default_branch":
            dumped.pop("cross_epic_prerequisites", None)
        if self.prerequisite_branches is None:
            dumped.pop("prerequisite_branches", None)
        if self.max_wait_seconds == DEFAULT_INTEGRATION_MAX_WAIT_SECONDS:
            dumped.pop("max_wait_seconds", None)
        if self.train is None:
            dumped.pop("train", None)
        if self.ci is None:
            dumped.pop("ci", None)
        return dumped


def integration_ci_sources(raw_policy: Any) -> dict[str, Any]:
    """The CI source each target kind uses under a stored project policy.

    ``origin`` says where the answer came from: ``policy`` for a configured
    ``ci`` block, ``development`` for a pinned development source (its root
    candidates run that source's local jobs; nothing else changes), else
    ``default``.  A stored policy that no longer validates reads as default,
    which is hosted, matching what the train's lanes do with it.
    """
    from src.integration.development_runtime import project_development_pin

    hosted = dict.fromkeys(CI_TARGET_KINDS, "hosted")
    if isinstance(raw_policy, dict) and project_development_pin(
            {"hierarchical_integration_policy": raw_policy}) is not None:
        return {**hosted, "root": "local", "origin": "development"}
    ci = integration_ci_policy(raw_policy)
    if ci is None:
        return {**hosted, "origin": "default"}
    return {**{kind: ci.source_for(kind) for kind in CI_TARGET_KINDS}, "origin": "policy"}


def integration_ci_policy(raw_policy: Any) -> IntegrationCIPolicy | None:
    """The validated ``ci`` block of a stored project policy, or ``None``.

    ``None`` is hosted everywhere: an unconfigured project, a development
    policy (it has no ``ci`` block) and a stored policy that no longer
    validates all keep reading hosted checks.
    """
    if not isinstance(raw_policy, dict) or raw_policy.get("ci") is None:
        return None
    try:
        return HierarchicalIntegrationPolicy.model_validate(raw_policy).ci
    except (TypeError, ValueError):
        return None


def integration_max_wait_seconds(raw_policy: Any) -> float:
    """The validated ``max_wait_seconds`` a stored project policy names.

    Development mode stores a ``DevelopmentPolicy`` in the same column and an
    unconfigured project stores nothing; neither names a bound, and neither
    does a stored policy that no longer validates.  They get the shipped
    default rather than an unbounded wait.
    """
    if raw_policy is None:
        return DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
    try:
        return HierarchicalIntegrationPolicy.model_validate(raw_policy).max_wait_seconds
    except (TypeError, ValueError):
        return DEFAULT_INTEGRATION_MAX_WAIT_SECONDS


# Profile fields mandatory routing retired.  Each stays on its model so stored
# snapshots still validate, but nothing reads it and a new policy write that
# sets one is refused.
DEPRECATED_BOUNDARY_ROUTE_FIELDS = ("primary_profile_id", "verifier_profile_id")
DEPRECATED_REPAIR_ROUTE_FIELDS = ("debug_profile_id",)


def deprecated_route_fields(policy: HierarchicalIntegrationPolicy) -> list[str]:
    """Dotted paths of every deprecated profile field ``policy`` sets non-null.

    For example ``root.primary_profile_id`` or ``parent.repair.debug_profile_id``.
    An empty list means the policy is admissible as a new write.
    """
    named: list[str] = []
    for boundary_name in ("parent", "root"):
        boundary: IntegrationBoundaryPolicy = getattr(policy, boundary_name)
        for field in DEPRECATED_BOUNDARY_ROUTE_FIELDS:
            if getattr(boundary, field) is not None:
                named.append(f"{boundary_name}.{field}")
        for field in DEPRECATED_REPAIR_ROUTE_FIELDS:
            if getattr(boundary.repair, field) is not None:
                named.append(f"{boundary_name}.repair.{field}")
    return named
