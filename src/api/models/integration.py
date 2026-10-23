"""Response shapes for author-assisted integration close continuations."""

from typing import Any, Literal

from pydantic import BaseModel


class IntegrationContinueCloseResponse(BaseModel):
    success: bool
    outcome: Literal["completed", "pending", "refused"]
    operation_id: str | None = None
    close: dict[str, Any] | None = None
    error: str | None = None


class IntegrationBatchFixResponse(BaseModel):
    success: bool
    outcome: str | None = None
    fixer: dict[str, Any] | None = None
    head_sha: str | None = None
    branch: str | None = None
    error: str | None = None


RESPONSE_MODELS = {
    "integration_continue_close": IntegrationContinueCloseResponse,
    "integration_batch_fix": IntegrationBatchFixResponse,
}
