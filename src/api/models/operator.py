"""Response models for the scoped operator-worker commands.

``operator_execute`` returns the dispatched command's own result with the
audit references merged in, so it stays permissive (``extra="allow"``) and
pins only the fields the wrapper adds.  ``operator_preflight`` returns one
report of every blocker found.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OperatorExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: bool = True
    operator_audit_id: int
    operator_task_id: str


class OperatorPreflightAction(BaseModel):
    command: str
    args_sha256: str
    decision_id: str | None = None


class OperatorPreflightInputs(BaseModel):
    branches: list[str] = []
    runner_labels: list[str] = []
    app_ids: list[int] = []


class OperatorPreflightResponse(BaseModel):
    success: bool = True
    preflight_id: int
    ready: bool
    actions: list[OperatorPreflightAction] = []
    blockers: list[str] = []
    inputs: OperatorPreflightInputs
    probes: dict[str, Any] = {}
    notification_error: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "operator_execute": OperatorExecuteResponse,
    "operator_preflight": OperatorPreflightResponse,
}
