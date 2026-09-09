"""Frozen value objects shared by integration playbooks and core commands."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    debug_profile_id: str | None = None


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
    repair: RepairPolicy
    route: PlaybookRoute
    primary_intelligence_class: str | None = Field(default=None, min_length=1)
    primary_profile_id: str | None = Field(default=None, min_length=1)
    verifier_intelligence_class: str | None = Field(default=None, min_length=1)
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


class HierarchicalIntegrationPolicy(BaseModel):
    """Validated project policy consumed when reserving an operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    parent: IntegrationBoundaryPolicy
    root: IntegrationBoundaryPolicy
    branchless_parent: Literal["skip", "declared", "verifier"]
    on_failed_child: Literal["block", "ask"]
    on_main_moved: Literal["rebuild", "wait"] = "rebuild"
    cleanup: IntegrationCleanupPolicy = Field(default_factory=IntegrationCleanupPolicy)
