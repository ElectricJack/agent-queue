"""Versioned source decisions and their durable application result."""

from typing import Any

from pydantic import BaseModel


class AskVersion(BaseModel):
    decision_generation: int
    source_fingerprint: str
    feedback_version: int


class AskSourceRecord(AskVersion):
    model_config = {"extra": "allow"}
    id: str
    context_state: str
    context: dict[str, Any]
    application_state: str
    decision_state: str


class AskPreview(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    packet: dict[str, Any]
    packet_sha256: str
    expires_at: float


class AskPrepareResponse(BaseModel):
    success: bool = True
    preview: AskPreview
    expected: AskVersion


class AskDecisionResponse(BaseModel):
    model_config = {"extra": "allow"}
    success: bool
    decision_id: str
    application_state: str
    created: bool | None = None
    receipt: dict[str, Any] | None = None


RESPONSE_MODELS = {
    "ask_prepare_decision": AskPrepareResponse,
    "ask_decide": AskDecisionResponse,
    "ask_retry_operation": AskDecisionResponse,
}
