"""Response models for gate commands (work-graph WG-3)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from src.api.models.ask import AskSourceRecord


class GateSummary(BaseModel):
    """Gate row as returned by ``gate_list`` / ``gate_show``.

    Fields come from ``src/database/queries/gate_queries.py`` which returns
    dict rows including the resolved metadata columns.
    """

    model_config = {"extra": "allow"}
    ask: AskSourceRecord | None = None
    id: str
    gate_type: str
    project_id: str
    title: str
    question: str = ""
    status: str = "open"
    await_id: str | None = None
    timeout_at: float | None = None
    created_at: float | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None
    resolution: str | None = None


class GateCreatePayload(BaseModel):
    """Echoed by ``gate_create`` — matches the ``gate.created`` event payload."""

    model_config = {"extra": "allow"}
    gate_id: str
    gate_type: str
    project_id: str
    title: str
    question: str = ""
    await_id: str | None = None
    timeout_at: float | None = None
    waiter_task_ids: list[str] = []


class GateCreateResponse(BaseModel):
    success: bool = True
    gate_id: str
    gate: GateCreatePayload
    was_created: bool = True


class GateListResponse(BaseModel):
    success: bool = True
    gates: list[GateSummary] = []


class GateShowResponse(BaseModel):
    success: bool = True
    gate: GateSummary
    waiters: list[str] = []


class GateResolveResponse(BaseModel):
    success: bool = True
    gate_id: str
    unblocked_task_ids: list[str] = []
    decision_id: str | None = None
    application_state: str | None = None
    created: bool | None = None
    receipt: dict[str, Any] | None = None


class GateReopenResponse(BaseModel):
    success: bool = True
    gate_id: str
    ask: AskSourceRecord


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "gate_reopen": GateReopenResponse,
    "gate_create": GateCreateResponse,
    "gate_list": GateListResponse,
    "gate_show": GateShowResponse,
    "gate_resolve": GateResolveResponse,
}
