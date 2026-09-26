"""Typed, bounded agent waits and pure deadline arbitration.

Waits retain their task's seat and workspace. Producer adapters read durable
state; a bus notification is never evidence that a condition was satisfied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_TIMEOUT = 7200
MAX_TIMEOUT = 86400
MAX_DIGEST_BYTES = 4096
WAIT_KINDS = ("job", "task", "message", "timer")
WAIT_STATES = ("active", "satisfied", "expired", "cancelled")
TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED", "BLOCKED")


class AgentWaitRecord(BaseModel):
    """Persisted wait/result shape shared by contracts and generated clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    project_id: str
    owner_kind: Literal["task", "supervisor"]
    owner_id: str
    session_id: str
    session_instance_token: str
    claim_epoch: int
    kind: Literal["job", "task", "message", "timer"]
    match: dict[str, Any]
    state: Literal["active", "satisfied", "expired", "cancelled"]
    version: int
    created_at: float
    deadline_at: float
    resolved_at: float | None = None
    wait_resumed_at: float | None = None
    checked_at: float = 0
    result_ref: str | None = None
    digest: dict[str, Any] | None = None
    idempotency_key: str
    result_message_id: str | None = None


class WaitError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JobMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: str = Field(min_length=1, max_length=256)
    timeout: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)


class TaskMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(min_length=1, max_length=256)


class MessageMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    thread_id: str = Field(min_length=1, max_length=256)
    after_seq: int = Field(ge=0, strict=True)


class TimerMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    due_at: float


def typed_match(
    kind: str, ref: str | None, after_seq: int | None, due_at: float | None
) -> dict[str, Any]:
    if kind == "job" and after_seq is None and due_at is None:
        return JobMatch(job_id=ref or "").model_dump(exclude_none=True)
    if kind == "task" and after_seq is None and due_at is None:
        return TaskMatch(task_id=ref or "").model_dump()
    if kind == "message" and due_at is None:
        return MessageMatch(thread_id=ref or "", after_seq=after_seq).model_dump()
    if kind == "timer" and ref is None and after_seq is None:
        return TimerMatch(due_at=due_at).model_dump()
    raise WaitError("wait.invalid", "provide only the typed fields for this wait kind")


def deadline_for(now: float, timeout: float | None, match: dict[str, Any]) -> float:
    seconds = DEFAULT_TIMEOUT if timeout is None else timeout
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= MAX_TIMEOUT:
        raise WaitError("wait.invalid", "timeout must be positive and at most 86400 seconds")
    deadline = now + seconds
    if match.get("due_at", now) > deadline:
        raise WaitError("wait.invalid", "timer due_at must not exceed its deadline")
    return deadline


def job_wait_deadline(job: dict, now: float) -> float:
    """Remaining queue/run budget plus grace, bounded by the wait's hard cap."""
    base = job["started_at"] if job["started_at"] is not None else job["queue_deadline"]
    end = base + job["run_timeout"] + 300
    return min(now + MAX_TIMEOUT, max(now + 300, end))


@dataclass(frozen=True)
class ProducerObservation:
    available: bool = True
    completed_at: float | None = None
    result_ref: str | None = None
    digest: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WaitResolution:
    state: Literal["satisfied", "expired", "cancelled"]
    result_ref: str | None
    digest: dict[str, Any]


def resolve_wait(
    observation: ProducerObservation, *, deadline: float, now: float
) -> WaitResolution | None:
    """Completion at the deadline wins even when the daemon observes it late."""
    if observation.completed_at is not None and observation.completed_at <= min(deadline, now):
        return WaitResolution("satisfied", observation.result_ref, observation.digest)
    if now >= deadline:
        return WaitResolution("expired", observation.result_ref, {"reason": "deadline_expired"})
    if not observation.available:
        return WaitResolution("satisfied", observation.result_ref, {"reason": "source_unavailable"})
    return None


class AgentWaitReconciler:
    """Restart-safe bounded scan. All writes belong to the command boundary."""

    def __init__(self, handler):
        self.handler = handler

    async def tick(
        self, *, now: float | None = None, wait_id: str | None = None
    ) -> dict[str, Any]:
        from src.commands.principal import ExecutionPrincipal, principal_context

        with principal_context(ExecutionPrincipal.service("agent-waits")):
            return await self.handler.execute(
                "reconcile_agent_waits", {"now": now, "wait_id": wait_id}
            )
