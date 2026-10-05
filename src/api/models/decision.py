"""Response models for durable operator decisions and their shared history."""

from typing import Literal

from pydantic import BaseModel


class OperatorDecisionModel(BaseModel):
    id: str
    project_id: str
    object_kind: Literal["task", "batch", "operation"]
    object_id: str
    effect: Literal["note", "hold", "release"]
    operator: str
    decision: str
    source: Literal["chat", "discord", "cli"]
    source_ref: str
    recorded_by: str
    created_at: float
    idempotency_key: str
    releases: str | None = None
    # History computes whether a hold remains active; record returns the stored row.
    active: bool | None = None


class DecisionRecordResponse(BaseModel):
    success: bool = True
    decision: OperatorDecisionModel


class DecisionListResponse(BaseModel):
    success: bool = True
    operator_decisions: list[OperatorDecisionModel]


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "decision_record": DecisionRecordResponse,
    "decision_list": DecisionListResponse,
}
