"""Retained repository identities, attestation DTOs and promotion error contracts.

Legacy builders and the current train share these data types without sharing
execution services. Compatibility exports preserve exception and DTO identity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from src.integration.models import Fence, PromotionValue
from src.models import RepoConfig

class RootPromotionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "prepared",
        "promoted",
        "already_promoted",
        "base_moved",
        "ci_missing",
        "non_fast_forward",
        "wait",
        "reconciliation_blocked",
        "stale",
        "configuration_blocked",
    ]
    batch_id: str
    revision: int
    intent_id: str | None = None
    receipt_ids: tuple[str, ...] = ()
    head_sha: str | None = None
    #: Safe operator-facing explanation of a refusal; never part of the
    #: command contract's result fields.
    reason: str | None = None


class RootPromotionInvariantError(RuntimeError):
    """Durable root promotion state is internally inconsistent."""


class RootPromotionConstraintError(RootPromotionInvariantError):
    """The database refused the root reservation, and no canonical intent explains it.

    Only a ``unique_violation`` answered by a readable canonical intent is a
    lost race; every other refusal names its SQLSTATE and constraint.
    """

    def __init__(self, message: str, *, sqlstate: str | None, constraint: str | None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.constraint = constraint


class RootAttestationSubject(BaseModel):
    """Exact server-derived root candidate identity requiring attestation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    repository_numeric_id: StrictInt = Field(gt=0)
    repository_full_name: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    revision: StrictInt = Field(ge=0)
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    required_check_version: str = Field(min_length=1)


class RootAttestationProof(RootAttestationSubject):
    """Live CI receipt identity, or legacy App publication proof, supplied by Task10."""

    check_run_id: StrictInt = Field(gt=0)
    external_id: str = Field(
        pattern=r"^aq-(?:attestation|ci-receipt)-v1:[0-9a-f]{64}$"
    )

    def subject(self) -> RootAttestationSubject:
        fields = RootAttestationSubject.model_fields
        return RootAttestationSubject.model_validate(
            {name: getattr(self, name) for name in fields}
        )


class PromotionError(RuntimeError):
    """Base failure with a deterministic command outcome."""


class PromotionConflict(PromotionError):
    def __init__(self, value: PromotionValue, diagnostics: dict[str, Any]):
        super().__init__("reviewed source conflicts with the expected target")
        self.value = value
        self.diagnostics = diagnostics


class PromotionSourceMoved(PromotionError):
    pass


class PromotionTargetMoved(PromotionError):
    pass


class PromotionNotApplied(PromotionError):
    pass


class PromotionRecovery(PromotionError):
    """A fenced recovery advanced the intent without delivering a receipt."""

    def __init__(self, outcome: str, value: PromotionValue):
        super().__init__(outcome)
        self.outcome = outcome
        self.value = value


class PromotionInvariantError(PromotionError):
    pass


class PromotionRuntimeError(PromotionError):
    pass


class PromotionAuthorizationError(PromotionError):
    pass


@dataclass(frozen=True)
class ResolvedRepository:
    repo: RepoConfig
    origin_url: str
    retained_git_dir: Path


RepositoryResolver = Callable[[str], Awaitable[RepoConfig | None] | RepoConfig | None]


class CandidateBuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "empty",
        "built",
        "already_built",
        "conflict",
        "source_moved",
        "base_moved",
        "stale_revision",
        "wait",
        "human_required",
        "configuration_blocked",
    ]
    batch_id: str
    revision: int
    operation_id: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    member_ordinal: int | None = None
    #: Safe operator-facing explanation of a wait; not a contract result field.
    reason: str | None = None


class AuditPullRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    url: str
    number: int
    head_sha: str
    head_branch: str
    base_branch: str
    repository_numeric_id: int
    repository_full_name: str
    idempotency_key: str
    state: Literal["open", "closed"] = "open"


class CandidateRepairLineage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    batch_id: str
    revision: int
    member_ordinal: int
    operation_id: str
    operation_stage: int
    partial_head_sha: str
    source_base_sha: str
    source_head_sha: str
    resolved_head_sha: str
    repair_commit_shas: tuple[str, ...]
    conflict_scope: Literal["member", "batch"] = "member"
    source_head_shas: tuple[str, ...] = ()


class CandidateAuthorizationError(ValueError):
    """Raised when caller-supplied data attempts to stand in for durable authority."""


class CandidateStaleAuthority(RuntimeError):
    """The snapshotted hierarchy, lease, revision, or branch fence changed."""


class MergeConflictError(RuntimeError):
    """The member overlap could not be resolved by generation or absorbed by driver."""

    def __init__(self, evidence: str) -> None:
        super().__init__(evidence or "merge conflict")
        self.evidence = evidence or "merge conflict"


class CandidateConstraintError(CandidateStaleAuthority):
    """The database refused a candidate row itself, not because a writer won a race.

    Callers keep treating it as stale authority; the message and attributes
    name the SQLSTATE and constraint instead of reporting a race.
    """

    def __init__(self, message: str, *, sqlstate: str | None, constraint: str | None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.constraint = constraint


class CandidateRepairResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["accepted", "already_accepted", "rejected", "stale", "wait"]
    batch_id: str
    revision: int
    member_ordinal: int
    # ``stale`` deliberately covers a changed authority snapshot as well as a
    # rejected frozen Git proof.  Keep the latter observable: an operator
    # cannot safely recover a pushed reservation from an opaque stale result.
    invariant: str | None = None


class CandidateResolutionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    batch_id: str
    revision: int
    member_ordinal: int
    operation_id: str
    resolved_head_sha: str
    resolved_tree_sha: str
    repair_commit_shas: tuple[str, ...]
    fence: Fence
