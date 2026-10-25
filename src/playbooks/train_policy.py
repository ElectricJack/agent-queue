"""Small decision data for the Git train, embedded in a V2 policy artifact."""

from typing import Literal

from pydantic import Field, model_validator

from src.integration.models import RequiredCheckSet
from src.playbooks.expressions import V2Base


class TrainPackingPolicy(V2Base):
    """Versioned, deterministic selection inputs; never a CI permission.

    The activated artifact digest is the policy revision in signature/cache
    identities. These versions describe the wire contract, not tuned weights.
    """

    signature_version: int = Field(default=1, strict=True, ge=1, le=1)
    policy_version: int = Field(default=1, strict=True, ge=1, le=1)
    enabled: bool = Field(default=False, strict=True)
    max_members: int = Field(default=4, strict=True, ge=1, le=8)
    green_weight: int = Field(default=100, strict=True, ge=0, le=1000)
    merge_clean_weight: int = Field(default=40, strict=True, ge=0, le=1000)
    signature_size_weight: int = Field(default=10, strict=True, ge=0, le=100)
    nonhot_weight: int = Field(default=10, strict=True, ge=0, le=100)
    age_per_hour: float = Field(default=1.0, strict=True, ge=0, le=100, allow_inf_nan=False)
    max_age_score: int = Field(default=80, strict=True, ge=0, le=1000)
    pass_rate_weight: int = Field(default=10, strict=True, ge=0, le=100)
    pass_rate_neutral: int = Field(default=5, strict=True, ge=0, le=100)
    pass_rate_min_checks: int = Field(default=20, strict=True, ge=20, le=10000)
    hotness_window_batches: int = Field(default=50, strict=True, ge=1, le=1000)
    oldest_slot_after_seconds: int = Field(default=7200, strict=True, gt=0, le=7200)

    @model_validator(mode="after")
    def _neutral_within_weight(self) -> "TrainPackingPolicy":
        if self.pass_rate_neutral > self.pass_rate_weight:
            raise ValueError("pass_rate_neutral exceeds pass_rate_weight")
        return self


class TrainBisectionPolicy(V2Base):
    """Opt-in failure isolation; probe revisions cannot authorize publication."""

    enabled: bool = Field(default=False, strict=True)
    max_probes: int = Field(default=3, strict=True, ge=1, le=3)


class TrainPolicy(V2Base):
    # Mechanism enforces source ancestry, exact CI, human intent and writer fences.
    # Policy chooses whether to dispatch/repair now or revisit after a finite wait.
    idle: Literal["merge", "wait"] = "merge"
    red: Literal["repair", "wait"] = "repair"
    conflict: Literal["repair", "wait"] = "repair"
    wait_seconds: int = Field(default=30, gt=0, le=3600)
    required_checks: RequiredCheckSet | None = None
    # Optional blocks preserve canonical bytes of installed legacy artifacts.
    # The reviewed new bundle explicitly supplies all defaults for future readers.
    packing: TrainPackingPolicy | None = None
    bisection: TrainBisectionPolicy | None = None
    parallel_disjoint: bool = Field(default=False, strict=True, exclude_if=lambda v: not v)

    def validate_project_policy(self, policy: dict) -> None:
        """Legacy speculation remains project-owned; never enable both modes."""
        if self.parallel_disjoint and (policy.get("train") or {}).get("speculative_ci"):
            raise ValueError("parallel_disjoint conflicts with speculative_ci")

    def action(self, state: str) -> str:
        if state == "green":
            return "publish"
        return getattr(self, state, "wait") if state in {"idle", "red", "conflict"} else "wait"
