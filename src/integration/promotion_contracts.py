"""Retained repository identities, attestation DTOs and promotion error contracts.

Legacy builders and the current train share these data types without sharing
execution services. Compatibility exports preserve exception and DTO identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from src.models import RepoConfig


class RootPromotionInvariantError(RuntimeError):
    """Durable root promotion state is internally inconsistent."""


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


class PromotionSourceMoved(PromotionError):
    pass


class PromotionTargetMoved(PromotionError):
    pass


class PromotionNotApplied(PromotionError):
    pass


class PromotionInvariantError(PromotionError):
    pass


class PromotionRuntimeError(PromotionError):
    pass


class PromotionAuthorizationError(PromotionError):
    pass


@dataclass(frozen=True)
class ResolvedRepository:
    """The repository and retained Git directory captured for a promotion."""

    repo: RepoConfig
    origin_url: str
    retained_git_dir: Path


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


class CandidateAuthorizationError(ValueError):
    """Raised when caller-supplied data attempts to stand in for durable authority."""


class CandidateStaleAuthority(RuntimeError):
    """The snapshotted hierarchy, lease, revision, or branch fence changed."""
