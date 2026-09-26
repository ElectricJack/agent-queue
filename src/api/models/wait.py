"""Concrete responses for the four durable wait commands."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.agent_waits import AgentWaitRecord


class WaitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    wait: AgentWaitRecord
    next_step: str | None = None


class WaitListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    waits: list[AgentWaitRecord]
    count: int


RESPONSE_MODELS = {
    "wait_register": WaitResponse,
    "wait_get": WaitResponse,
    "wait_list": WaitListResponse,
    "wait_cancel": WaitResponse,
}
