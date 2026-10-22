"""Typed conditions and server-owned verification evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol


class WaitError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict:
        return {"success": False, "code": self.code, "error": str(self), **self.details}


@dataclass(frozen=True)
class Scope:
    project_id: str
    owner_kind: str
    owner_id: str
    session_id: str | None = None
    instance_token: str | None = None
    claim_epoch: int | None = None
    principal: str = "agent"
    elevated: bool = False


@dataclass(frozen=True)
class NodeKey:
    key: str
    project_id: str


@dataclass(frozen=True)
class Edge:
    waiter: NodeKey
    blocker: NodeKey
    relation: str = "semantic"


@dataclass(frozen=True)
class SourceKey:
    kind: str
    key: str
    generation: int

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.key}"


@dataclass(frozen=True)
class Condition:
    kind: str
    parameters: Mapping[str, Any]
    generation: int | None = None
    # Never accepted from an agent. Persisted so observation after a restart
    # retains recipient, scope, source class and graph bindings.
    context: Mapping[str, Any] = field(default_factory=dict)

    def bind(self, *, generation: int, **context: Any) -> Condition:
        return replace(self, generation=generation, context={**self.context, **context})

    def as_dict(self) -> dict:
        result = {"kind": self.kind, **self.parameters}
        if self.generation is not None:
            result["generation"] = self.generation
        return result

    def as_record(self) -> dict:
        return {**self.as_dict(), "_verified": dict(self.context)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Condition:
        return cls(
            record["kind"],
            {k: v for k, v in record.items() if k not in {"kind", "generation", "_verified"}},
            record.get("generation"),
            record.get("_verified", {}),
        )


@dataclass(frozen=True)
class Observation:
    revision: str
    state: Literal["satisfied", "unsatisfied", "unavailable"]
    result_ref: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """An authoritative adapter read, including source identity and generation.

    Generation fences source replacement, not every update. Current resource
    and exact-head CI/PR streams use 0; changes are opaque observation revisions.
    Observation.revision describes the read and need not equal an event revision.
    """

    project_id: str
    generation: int
    observation: Observation
    source_class: str
    blockers: tuple[NodeKey, ...] = ()


@dataclass(frozen=True)
class VerifiedEvidence:
    condition: Condition
    source: SourceKey
    observation: Observation
    evidence_ref: str


@dataclass(frozen=True)
class DeadlinePolicy:
    escalate_after_seconds: float | None
    due_at: float | None = None


@dataclass(frozen=True)
class SleepDecision:
    mode: Literal["in_place", "hibernate"]
    reason: str
    inputs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceEvent:
    """A committed source change; callers re-observe the predicate before resolving.

    The writer's revision is an opaque idempotency key within the source generation.
    An event alone does not prove a target status, addressed reply or human decision.
    """

    source: SourceKey
    revision: str
    outcome_ref: str
    payload: Mapping[str, Any] = field(default_factory=dict)


class WaitPolicy(Protocol):
    """Resolved operator policy; no defaults or activation live in this package."""

    in_place_ceiling_seconds: float

    def escalation_seconds(self, kind: str) -> float: ...


class ConditionAdapter(Protocol):
    kind: str

    def normalise(self, raw: Mapping[str, Any]) -> Condition: ...
    async def verify(self, conn, cond: Condition, scope: Scope) -> VerifiedEvidence: ...
    async def observe(self, conn, cond: Condition) -> Observation: ...
    def source_key(self, cond: Condition) -> SourceKey: ...
    def edges(self, cond: Condition, waiter: NodeKey) -> list[Edge]: ...
    def deadline_policy(self, cond: Condition, policy: WaitPolicy) -> DeadlinePolicy: ...
    def project_result(self, cond: Condition, event: SourceEvent) -> str: ...
    def source_class(self, cond: Condition) -> str: ...


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
