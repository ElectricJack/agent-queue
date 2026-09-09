"""Typed API response models for durable human escalations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EscalationRecord(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    project_id: str
    task_id: str | None = None
    source_kind: str
    source_identity: str
    incident_key: str
    supervisor_owner: str
    task_title: str | None = None
    task_status: str | None = None
    summary: str
    investigation: str
    decision_requested: str
    choices: list[Any] | None = None
    severity: str
    state: str
    revision: int
    terminal_outcome: str | None = None
    terminal_evidence: dict[str, Any] | None = None
    created_at: float
    updated_at: float
    terminal_at: float | None = None
    delivery_statuses: list[str] | None = None
    pending_delivery: bool | None = None


class EscalationMessage(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    escalation_id: str
    direction: str
    transport: str
    verified_actor: str
    text: str
    external_message_id: str | None = None
    received_sequence: int | None = None
    received_at: float
    supervisor_message_id: str | None = None
    created_at: float


class EscalationDelivery(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    escalation_id: str
    status: str
    kind: str
    attempt_count: int
    generation: int


class EscalationAction(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    escalation_id: str
    reply_id: str
    idempotency_key: str
    action_kind: str
    target_id: str
    parameters: dict[str, Any]
    executor: str
    started_revision: int
    status: str
    outcome: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float
    completed_at: float | None = None


class EscalationErrorResponse(BaseModel):
    """Stable error envelope shared by all escalation endpoints."""

    model_config = {"extra": "allow"}

    success: bool = False
    error_code: str
    error: str


class EscalationCreateResponse(BaseModel):
    success: bool = True
    created: bool
    escalation: EscalationRecord


class EscalationListResponse(BaseModel):
    success: bool = True
    escalations: list[EscalationRecord]
    count: int


class EscalationGetResponse(BaseModel):
    success: bool = True
    escalation: EscalationRecord
    messages: list[EscalationMessage]
    deliveries: list[EscalationDelivery]
    actions: list[EscalationAction]


class EscalationReplyResponse(BaseModel):
    success: bool = True
    reply: EscalationMessage
    escalation: EscalationRecord
    created: bool
    supervisor_enqueued: bool
    terminal: bool


class EscalationUpdateResponse(BaseModel):
    success: bool = True
    escalation: EscalationRecord


class EscalationApplyReplyResponse(BaseModel):
    success: bool = True
    applied: bool
    replayed: bool
    action: EscalationAction
    escalation: EscalationRecord
    action_result: dict[str, Any] | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "escalation_create": EscalationCreateResponse,
    "escalation_list": EscalationListResponse,
    "escalation_get": EscalationGetResponse,
    "escalation_reply": EscalationReplyResponse,
    "escalation_update": EscalationUpdateResponse,
    "escalation_apply_reply": EscalationApplyReplyResponse,
}
