"""Concrete responses for the shared operator decision commands."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class OperatorDecisionRecord(BaseModel):
    """One ``operator_decisions`` row; ``active`` is present on history reads."""

    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str
    object_kind: Literal["task", "batch", "operation"]
    object_id: str
    effect: Literal["note", "hold", "release"]
    operator: str
    decision: str
    source: str
    source_ref: str
    recorded_by: str
    created_at: float
    idempotency_key: str
    releases: str | None = None
    active: bool | None = None


class DecisionRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    decision: OperatorDecisionRecord


class DecisionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    operator_decisions: list[OperatorDecisionRecord]


RESPONSE_MODELS = {
    "decision_record": DecisionRecordResponse,
    "decision_list": DecisionListResponse,
}
