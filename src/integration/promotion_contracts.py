"""Retained repository identities, attestation DTOs and promotion error contracts.

Legacy builders and the current train share these data types without sharing
execution services. Compatibility exports preserve exception and DTO identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from src.models import RepoConfig


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


class PromotionInvariantError(PromotionError):
    pass


class PromotionRuntimeError(PromotionError):
    pass


@dataclass(frozen=True)
class ResolvedRepository:
    """The repository and retained Git directory captured for a promotion."""

    repo: RepoConfig
    origin_url: str
    retained_git_dir: Path
