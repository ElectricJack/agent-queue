"""Five typed public contracts for bounded task collaboration threads."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src import collaboration as policy
from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration
from src.commands.principal import principal_context

ThreadId = Annotated[str, Field(min_length=1, max_length=64)]
TaskId = Annotated[str, Field(min_length=1, max_length=256)]
CloseReason = Literal["closed", "budget_exhausted", "expired", "members_below_two"]


class CollaborationScopeArgs(CommandArgs):
    """Scope fields the session gate injects; a worker cannot nominate others."""

    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None


class CollaborationCreateArgs(CollaborationScopeArgs):
    task_ids: list[TaskId] = Field(min_length=policy.MIN_MEMBERS, max_length=policy.MAX_MEMBERS)
    goal: str | None = Field(default=None, max_length=policy.MAX_GOAL_CHARS)
    deadline_seconds: int = Field(
        default=policy.DEFAULT_DEADLINE_SECONDS,
        ge=policy.MIN_DEADLINE_SECONDS,
        le=policy.MAX_DEADLINE_SECONDS,
    )
    message_budget: int = Field(default=policy.MAX_MESSAGES, ge=1, le=policy.MAX_MESSAGES)
    idempotency_key: str = Field(min_length=1, max_length=256)


class CollaborationAcceptArgs(CollaborationScopeArgs):
    thread_id: ThreadId
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class CollaborationGetArgs(CollaborationScopeArgs):
    thread_id: ThreadId
    after_seq: int | None = Field(default=None, ge=0, strict=True)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class CollaborationListArgs(CollaborationScopeArgs):
    state: Literal["active", "closed", "expired"] | None = None
    limit: int = Field(default=20, ge=1, le=50)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class CollaborationCloseArgs(CollaborationScopeArgs):
    thread_id: ThreadId | None = None
    note: str | None = Field(default=None, max_length=policy.MAX_GOAL_CHARS)
    remove_task_id: TaskId | None = None
    all_active: bool = False
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class CollaborationMemberRecord(BaseModel):
    """One member task; ``needs_accept`` means its live claim has not joined."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    state: Literal["invited", "accepted", "removed"]
    invited_at: float
    accepted_at: float | None = None
    accepted_claim_epoch: int | None = None
    removed_at: float | None = None
    task_status: str
    task_claim_epoch: int | None = None
    running: bool
    needs_accept: bool


class CollaborationThreadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    project_id: str
    created_by_kind: Literal["operator", "supervisor"]
    created_by_id: str
    idempotency_key: str
    goal: str | None = None
    state: Literal["active", "closed", "expired"]
    close_reason: CloseReason | None = None
    created_at: float
    deadline_at: float
    closed_at: float | None = None
    remaining_seconds: float
    message_budget: int
    message_count: int
    last_seq: int
    version: int
    final_result: dict[str, Any] | None = None
    members: list[CollaborationMemberRecord]


class CollaborationMessageRecord(BaseModel):
    """One accepted send; ``body`` is null once content retention has elapsed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    seq: int
    message_id: str | None = None
    sender_task_id: str
    subject: str | None = None
    body: str | None = None
    created_at: float


class CollaborationValue(CommandValue):
    thread: CollaborationThreadRecord
    replayed: bool | None = None
    next_step: str | None = None


class CollaborationGetValue(CommandValue):
    thread: CollaborationThreadRecord
    messages: list[CollaborationMessageRecord]
    next_cursor: int | None = None
    has_more: bool
    capacity_hold: bool
    next_step: str


class CollaborationListValue(CommandValue):
    threads: list[CollaborationThreadRecord]
    count: int


class CollaborationCloseValue(CommandValue):
    thread: CollaborationThreadRecord | None = None
    closed_count: int | None = None
    thread_ids: list[str] | None = None


_SUMMARIES = {
    "collaboration_create": "Create a bounded thread between 2 to 4 tasks and invite each once.",
    "collaboration_accept": "Join a collaboration thread for the held task's live claim.",
    "collaboration_get": "Read a thread, its members, capacity hold and ordered messages.",
    "collaboration_list": "List collaboration threads for the held task or a project.",
    "collaboration_close": "Close a thread without changing any member task.",
}


def register_collaboration_contracts(registry):
    for name, args_model, result_model, effect in (
        (
            "collaboration_create",
            CollaborationCreateArgs,
            CollaborationValue,
            SideEffectClass.CREATE,
        ),
        (
            "collaboration_accept",
            CollaborationAcceptArgs,
            CollaborationValue,
            SideEffectClass.UPDATE,
        ),
        ("collaboration_get", CollaborationGetArgs, CollaborationGetValue, SideEffectClass.READ),
        ("collaboration_list", CollaborationListArgs, CollaborationListValue, SideEffectClass.READ),
        (
            "collaboration_close",
            CollaborationCloseArgs,
            CollaborationCloseValue,
            SideEffectClass.RESOLVE,
        ),
    ):
        if registry.get(name) is not None:
            continue

        async def invoke(args, principal, name=name, result_model=result_model):
            from src.commands.contracts.builtin import _handler

            with principal_context(principal):
                raw = await _handler().execute(name, args.model_dump(exclude_none=True))
            if raw.get("success") is False or raw.get("error"):
                return CommandResult(
                    outcome="rejected",
                    value=result_model.model_construct(),
                    summary=str(raw.get("error") or "rejected"),
                )
            return CommandResult(
                outcome="completed",
                value=result_model(
                    **{key: raw[key] for key in result_model.model_fields if key in raw}
                ),
                summary="completed",
            )

        registry.register(
            CommandRegistration(
                name,
                CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=args_model,
                        result_model=result_model,
                        capability=name,
                        side_effect=effect,
                        retry_safe=True,
                        idempotency=IdempotencySpec(mode="keyed", key_field="idempotency_key")
                        if name == "collaboration_create"
                        else IdempotencySpec(mode="natural"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary=_SUMMARIES[name],
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                invoke,
            )
        )
