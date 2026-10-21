"""Typed wait receipts and persisted explanation projections."""

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


class WaitVerifiedEvidence(BaseModel):
    """AQ observations only; claimed commentary is never a source fact."""

    kind: str | None = None
    condition: dict[str, Any] = Field(default_factory=dict)
    state: str | None = None
    evidence: str | None = None
    source_key: str | None = None
    source_generation: int | None = None
    source_revision: str | int | None = None
    observed_at: float | None = None
    freshness_seconds: float | None = None
    facts: dict[str, Any] = Field(default_factory=dict)


class WaitSleepExplanation(BaseModel):
    decision: str | None = None
    reason: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class WaitDeadline(BaseModel):
    type: str
    due_at: float
    generation: int | None = None
    fired_at: float | None = None
    cancelled_at: float | None = None


class WaitChainEdge(BaseModel):
    id: str | None = None
    wait_id: str | None = None
    from_: str = Field(alias="from")
    to: str
    relation: str
    source_revision: str | int | None = None
    active: bool | None = None
    created_at: float | None = None
    cleared_at: float | None = None


class WaitEffectivePriority(BaseModel):
    value: int | None = None
    base: int | None = None
    donor: str | None = None
    donor_path: list[str] = Field(default_factory=list)
    graph_version: int | None = None
    computed_at: float | None = None


class WaitExplanation(BaseModel):
    wait_id: str | None = None
    project_id: str | None = None
    owner: dict[str, str] = Field(default_factory=dict)
    lifecycle: str | None = None
    outcome: str | None = None
    result_ref: str | None = None
    age_seconds: float | None = None
    age_unknown: bool = False
    verified: WaitVerifiedEvidence = Field(default_factory=WaitVerifiedEvidence)
    claimed: str | None = None
    sleep: WaitSleepExplanation = Field(default_factory=WaitSleepExplanation)
    deadlines: list[WaitDeadline] = Field(default_factory=list)
    chain: list[WaitChainEdge] = Field(default_factory=list)
    chain_truncated: bool = False
    chain_at: float | None = None
    edge_history: list[dict[str, Any]] = Field(default_factory=list)
    effective_priority: WaitEffectivePriority = Field(default_factory=WaitEffectivePriority)
    escalation: dict[str, Any] | None = None
    escalations: list[dict[str, Any]] = Field(default_factory=list)
    reservations: list[dict[str, Any]] = Field(default_factory=list)
    continuation: dict[str, Any] | None = None
    policy_layer: str = "default"


class WaitTaskExplanation(BaseModel):
    id: str
    lifecycle: str
    base_priority: int
    effective_priority: int
    donor_task_id: str | None = None
    donor_path: list[str] = Field(default_factory=list)
    graph_version: int | None = None


class WaitExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    explanation: WaitExplanation | None = None
    waits: list[WaitExplanation] = Field(default_factory=list)
    task: WaitTaskExplanation | None = None
    total: int = 0
    offset: int = 0
    truncated: bool = False


class WaitGraphChain(BaseModel):
    components: list[list[str]]
    length: int
    truncated: bool = False


class WaitGraphProject(BaseModel):
    project_id: str
    counts: list[dict[str, Any]] = Field(default_factory=list)
    waits: list[WaitExplanation] = Field(default_factory=list)
    total_waits: int = 0
    escalated_waits: list[dict[str, Any]] = Field(default_factory=list)
    branch_blockers: list[dict[str, Any]] = Field(default_factory=list)
    held_message_backlog: list[dict[str, Any]] = Field(default_factory=list)
    longest_chains: list[WaitGraphChain] = Field(default_factory=list)
    deadlocks: list[dict[str, Any]] = Field(default_factory=list)
    truncated: dict[str, bool] = Field(default_factory=dict)


class WaitGraphProjection(BaseModel):
    projects: list[WaitGraphProject] = Field(default_factory=list)
    total_projects: int = 0
    offset: int = 0
    truncated: bool = False


class WaitGraphResponse(WaitGraphProjection):
    model_config = ConfigDict(extra="forbid")
    success: bool = True
    graph: WaitGraphProjection | None = None


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
