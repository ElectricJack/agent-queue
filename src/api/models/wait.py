"""Concrete responses for the four durable wait commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent_waits import AgentWaitRecord
from src.commands.contracts.wait import WaitCancelArgs, WaitGetArgs, WaitListArgs, WaitRegisterArgs
from src.commands.contracts.wait_v2 import PUBLIC_WAIT_V2_COMMANDS, WAIT_V2_ARGS


class WaitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    wait: AgentWaitRecord | dict[str, Any] | None = None
    wait_id: str | None = None
    condition: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    escalate_at: float | None = None
    ceiling_at: float | None = None
    continuation_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    next_step: str | None = None


class WaitListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    waits: list[AgentWaitRecord | dict[str, Any]]
    count: int


class WaitExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    explanation: dict[str, Any] | None = None
    waits: list[dict[str, Any]] = Field(default_factory=list)


class WaitGraphResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    graph: dict[str, Any]


class WaitCheckpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    continuation_id: str
    generation: int
    next_step: str | None = None


class WaitAckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    wait_id: str
    acknowledged: bool
    continuation_id: str | None = None
    generation: int | None = None
    next_step: str | None = None


class WaitDecideResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    wait_id: str
    decision: str
    outcome: str | None = None
    generation: int | None = None
    next_step: str | None = None


class WaitBridgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    result: dict[str, Any] = Field(default_factory=dict)


RESPONSE_MODELS = {
    "wait_register": WaitResponse,
    "wait_get": WaitResponse,
    "wait_list": WaitListResponse,
    "wait_cancel": WaitResponse,
    "wait_explain": WaitExplainResponse,
    "wait_graph": WaitGraphResponse,
    "wait_checkpoint": WaitCheckpointResponse,
    "wait_ack": WaitAckResponse,
    "wait_decide": WaitDecideResponse,
    "wait_lift": WaitDecideResponse,
    "wait_legacy_bridge": WaitBridgeResponse,
}

# Preserve closed models and their validators on HTTP, rather than rebuilding
# permissive models from JSON-schema properties in the route generator.
REQUEST_MODELS = {
    "wait_register": WaitRegisterArgs,
    "wait_get": WaitGetArgs,
    "wait_list": WaitListArgs,
    "wait_cancel": WaitCancelArgs,
    "wait_legacy_bridge": WAIT_V2_ARGS["wait_legacy_bridge"],
    **{name: WAIT_V2_ARGS[name] for name in PUBLIC_WAIT_V2_COMMANDS},
}
