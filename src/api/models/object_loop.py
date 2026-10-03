"""Response models for durable object evaluation commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ObjectLoopResponse(BaseModel):
    success: bool = True
    object_id: str
    version: int
    state: dict[str, Any]


class ObjectLoopStartResponse(ObjectLoopResponse):
    created: bool


class ObjectLoopReconcileResponse(ObjectLoopResponse):
    outcome: Literal[
        "brief_held", "checkpoint_held", "checkpoint_approved", "waiting_for_wave",
        "waiting_for_score", "stopped", "reconciled",
    ]


class ObjectScoreRecordResponse(ObjectLoopResponse):
    outcome: Literal["reused", "continue", "checkpoint", "stop"]


class ObjectCheckpointReadResponse(ObjectLoopResponse):
    approved: bool


class ObjectLoopInputsResponse(BaseModel):
    success: bool = True
    starts: list[dict[str, Any]]
    loops: list[dict[str, Any]]


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "object_loop_inputs": ObjectLoopInputsResponse,
    "object_loop_start": ObjectLoopStartResponse,
    "object_loop_reconcile": ObjectLoopReconcileResponse,
    "object_score_record": ObjectScoreRecordResponse,
    "object_checkpoint_read": ObjectCheckpointReadResponse,
}
