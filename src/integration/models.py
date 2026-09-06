"""Frozen value objects shared by integration playbooks and core commands."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        return self


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
    """Frozen retry limits for post-promotion cleanup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=5, gt=0)
    retry_base_seconds: float = Field(default=30.0, gt=0)
    retry_max_seconds: float = Field(default=3600.0, gt=0)

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
