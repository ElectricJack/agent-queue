"""Typed receipts.  Matter measures images; AQ checks identities and evidence."""

from __future__ import annotations

import math
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256 = r"^[0-9a-f]{64}$"
IDENTIFIER = r"^[A-Za-z0-9_-]+$"
ReceiptValidity = Literal[
    "valid", "invalid_candidate", "capture_failed", "unsupported_track", "readiness_unproven"
]


class StrictReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Artifact(StrictReceipt):
    uri: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=SHA256)

    @model_validator(mode="after")
    def durable_uri(self):
        parsed = urlparse(self.uri)
        if parsed.scheme not in {"artifact", "s3", "https"} or not (parsed.path or parsed.netloc):
            raise ValueError("artifact URI must name a durable artifact")
        return self


class Capture(StrictReceipt):
    view_id: str = Field(min_length=1, max_length=128)
    frame_id: str | None = Field(default=None, min_length=1, max_length=128)
    image: Artifact | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    ready: bool
    decoded: bool


class ViewMetric(StrictReceipt):
    loss: float = Field(ge=0)
    quality_pass: bool


class ScoreReceipt(StrictReceipt):
    schema_version: Literal[1]
    object_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    attempt_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    round_id: int = Field(ge=0)
    variant_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    task_id: str = Field(min_length=1)
    base_candidate_sha256: str = Field(pattern=SHA256)
    candidate_sha256: str = Field(pattern=SHA256)
    source_closure_sha256: str = Field(pattern=SHA256)
    params_sha256: str = Field(pattern=SHA256)
    reference_sha256: str = Field(pattern=SHA256)
    rig_sha256: str = Field(pattern=SHA256)
    scorer_sha256: str = Field(pattern=SHA256)
    render_profile_sha256: str = Field(pattern=SHA256)
    capture_receipts: list[Capture] = Field(min_length=1, max_length=64)
    artifacts: list[Artifact] = Field(min_length=1, max_length=64)
    validity: ReceiptValidity
    per_view: dict[str, ViewMetric | None]
    worst_view: str | None
    hard_gates: dict[str, bool] = Field(min_length=1, max_length=64)
    quality_pass: bool
    timings: dict[str, float | None]
    resources: dict[str, float | None]
    usage_refs: list[str]
    cost_coverage: dict[str, Any]
    hypothesis: str
    patch_scope: list[str]
    predicted_effect: str
    observed_delta: str

    @model_validator(mode="after")
    def complete_valid_views(self):
        views = [capture.view_id for capture in self.capture_receipts]
        if len(set(views)) != len(views):
            raise ValueError("duplicate capture view")
        if set(views) != set(self.per_view):
            raise ValueError("capture and metric view sets differ")
        if self.validity == "valid":
            if not all(
                c.ready and c.decoded and c.frame_id and c.image and c.width and c.height
                for c in self.capture_receipts
            ):
                raise ValueError("valid score requires complete ready, decoded captures")
            if any(metric is None for metric in self.per_view.values()):
                raise ValueError("valid score requires every view metric")
            if self.worst_view not in self.per_view:
                raise ValueError("worst view is missing")
            worst_loss = max(metric.loss for metric in self.per_view.values())
            if self.per_view[self.worst_view].loss != worst_loss:
                raise ValueError("worst view does not have the worst loss")
        elif (self.quality_pass or self.worst_view is not None
              or any(metric is not None for metric in self.per_view.values())):
            raise ValueError("invalid capture must have null quality metrics")
        if self.quality_pass and not all(self.hard_gates.values()):
            raise ValueError("quality cannot pass a failed hard gate")
        if self.quality_pass and not all(
            metric is not None and metric.quality_pass for metric in self.per_view.values()
        ):
            raise ValueError("quality cannot pass a failed view")
        artifact_hashes = {artifact.sha256 for artifact in self.artifacts}
        if self.candidate_sha256 not in artifact_hashes:
            raise ValueError("candidate artifact is missing")
        if any(capture.image is not None and capture.image.sha256 not in artifact_hashes
               for capture in self.capture_receipts):
            raise ValueError("capture artifact is missing")
        reject_nonfinite(self.cost_coverage)
        return self


def reject_nonfinite(value: Any) -> None:
    """JSONB otherwise accepts nested NaN from Python even with typed outer fields."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number in receipt")
    if isinstance(value, dict):
        for item in value.values():
            reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            reject_nonfinite(item)


def validate_score_receipt(
    receipt: ScoreReceipt, *, state: dict, task_id: str, mandatory_views: set[str]
) -> None:
    """Reject foreign, stale and incomplete evidence before any promotion."""
    expected = {
        "object_id": state["object_id"],
        "attempt_id": state["attempt_id"],
        "round_id": state["round_id"],
        "task_id": task_id,
        "base_candidate_sha256": state["incumbent_sha256"],
        **{key: state[key] for key in (
            "reference_sha256", "rig_sha256", "scorer_sha256", "render_profile_sha256"
        )},
    }
    for key, value in expected.items():
        if getattr(receipt, key) != value:
            raise ValueError(f"{key} does not match this loop round")
    wave = state["wave"]
    member = next((x for x in wave if x["variant_id"] == receipt.variant_id), None)
    if member is None or member.get("task_id") != task_id:
        raise ValueError("score belongs to a foreign variant or task")
    if set(receipt.per_view) != mandatory_views:
        raise ValueError("mandatory view coverage is incomplete")
