"""Typed declarations and legacy-handler adapters for pipeline commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from src.commands.contracts.models import (
    ClausePredicate,
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    CreateClause,
    CreateOrReuseClause,
    EffectSubject,
    ExecutionContract,
    IdempotencySpec,
    LinkClause,
    OutcomeClass,
    OutcomeSpec,
    ReadClause,
    ResolveClause,
    SideEffectClass,
    UpdateClause,
)
from src.commands.contracts.registry import CommandContext, CommandRegistration, ContractRegistry


class CreateTaskArgs(CommandArgs):
    title: str
    project_id: str | None = None
    description: str | None = None
    priority: int | None = None
    task_type: str | None = None
    profile_id: str | None = None
    intelligence_class: str | None = None
    preferred_workspace_id: str | None = None
    integration_mode: str | None = None
    workspace_mode: str | None = None
    requires_kinds: list[Any] | None = None
    depends_on: str | list[Any] | None = None
    parent_id: str | None = None
    labels: list[str] | None = None
    reason: str | None = None
    discovered_from: str | None = None
    affinity_agent_id: str | None = None
    affinity_reason: str | None = None
    dedup_key: str | None = None


class CreateTaskValue(CommandValue):
    created: str
    task_id: str
    status: str
    title: str
    project_id: str
    gate_id: str | None = None
    integration_mode: str | None = None
    task_type: str | None = None
    profile_id: str | None = None
    intelligence_class: str | None = None
    preferred_workspace_id: str | None = None
    affinity_agent_id: str | None = None
    affinity_reason: str | None = None
    workspace_mode: str | None = None
    requires_kinds: list[Any] | None = None
    depends_on: list[Any] | None = None
    reason: str | None = None
    parent_id: str | None = None
    labels: list[str] | None = None
    warning: str | None = None


class EnsureTaskArgs(CommandArgs):
    dedup_key: str
    title: str
    project_id: str | None = None
    description: str | None = None
    priority: int | None = None
    profile_id: str | None = None
    intelligence_class: str | None = None
    initial_status: str | None = None


class EnsureTaskValue(CommandValue):
    task_id: str
    created: bool


class EditTaskArgs(CommandArgs):
    task_id: str
    project_id: str | None = None
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    task_type: str | None = None
    status: str | None = None
    max_retries: int | None = None
    verification_type: str | None = None
    profile_id: str | None = None
    integration_mode: str | None = None
    skip_verification: bool | None = None
    intelligence_class: str | None = None
    affinity_agent_id: str | None = None
    affinity_reason: str | None = None
    workspace_mode: str | None = None


class EditTaskValue(CommandValue):
    updated: str
    fields: list[str]
    old_status: str | None = None
    new_status: str | None = None
    warning: str | None = None


class AddDependencyArgs(CommandArgs):
    task_id: str
    depends_on: str
    dep_type: str | None = None
    reason: str | None = None


class AddDependencyValue(CommandValue):
    ok: bool
    task_id: str
    depends_on: str
    dep_type: str
    reason: str | None = None
    task_title: str
    depends_on_title: str


class GateCreateArgs(CommandArgs):
    project_id: str
    gate_type: str
    title: str
    question: str | None = None
    await_id: str | None = None
    timeout_at: str | None = None
    waiter_task_ids: list[str] | None = None


class GateCreateValue(CommandValue):
    gate_id: str | None = None
    gate: dict[str, Any] | None = None
    was_created: bool | None = None
    skipped: bool | None = None
    reason: str | None = None
    created: bool | None = None


class GateResolveArgs(CommandArgs):
    gate_id: str
    resolved_by: str
    resolution: str | None = None


class GateResolveValue(CommandValue):
    gate_id: str
    unblocked_task_ids: list[str]


class ListTasksArgs(CommandArgs):
    project_id: str | None = None
    status: str | None = None
    display_mode: str | None = None
    show_dependencies: bool | None = None
    limit: int | None = None


class ListTasksValue(CommandValue):
    tasks: list[dict[str, Any]]
    by_project: dict[str, list[dict[str, Any]]]
    total: int
    project_count: int
    hidden_completed: int


class GetDownstreamTasksArgs(CommandArgs):
    task_id: str


class GetDownstreamTasksValue(CommandValue):
    tasks: list[dict[str, str]]


class TaskBatchCommitArgs(CommandArgs):
    proposal_id: str


class TaskBatchCommitValue(CommandValue):
    task_ids: list[str]


class TaskRouteArgs(CommandArgs):
    task_id: str
    profile_id: str
    intelligence_class: str | None = None
    workspace_id: str | None = None


class TaskRouteValue(CommandValue):
    task_id: str
    resolved_gate_ids: list[str]


_handler_provider: Callable[[], Any] | None = None


def set_handler_provider(provider: Callable[[], Any]) -> None:
    """Install the existing handler provider at the execution boundary."""
    global _handler_provider
    _handler_provider = provider


def _handler() -> Any:
    if _handler_provider is None:
        raise RuntimeError("no legacy CommandHandler provider installed")
    return _handler_provider()


def _outcome_of(name: str, raw: dict[str, Any]) -> str:
    """Map each legacy return shape to a declared business outcome."""
    if raw.get("error") or raw.get("success") is False:
        if name == "add_dependency" and "already exists" in str(raw.get("error", "")).lower():
            return "already_linked"
        if name == "gate_resolve" and "routing" in str(raw.get("error", "")).lower():
            return "refused_routing_gate"
        return "rejected"
    if name == "ensure_task":
        return "created" if raw.get("created") else "reused"
    if name == "gate_create":
        if raw.get("skipped"):
            return "skipped"
        return "created" if raw.get("was_created") else "reused"
    return {
        "create_task": "created",
        "edit_task": "updated",
        "add_dependency": "linked",
        "gate_resolve": "resolved",
        "list_tasks": "listed",
        "get_downstream_tasks": "listed",
        "task_batch_commit": "committed",
        "task_route": "routed",
    }[name]


def _adapter(name: str, value_type: type[CommandValue]):
    async def invoke(args: CommandArgs, _ctx: CommandContext) -> CommandResult[Any]:
        raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        outcome = _outcome_of(name, raw)
        if outcome in {"rejected", "refused_routing_gate", "already_linked"}:
            value = value_type.model_construct()
        else:
            try:
                value = value_type(
                    **{field: raw[field] for field in value_type.model_fields if field in raw}
                )
            except (KeyError, TypeError, ValidationError) as exc:
                return CommandResult(
                    outcome="contract_violation",
                    value=value_type.model_construct(),
                    summary=f"{name} result did not match its contract: {exc}",
                )
        return CommandResult(outcome=outcome, value=value, summary=str(raw.get("error") or outcome))

    return invoke


def _outcomes(*successes: str) -> tuple[OutcomeSpec, ...]:
    return tuple(
        OutcomeSpec(name=name, classification=OutcomeClass.SUCCESS) for name in successes
    ) + (OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),)


def _contract(
    name: str,
    args: type[CommandArgs],
    value: type[CommandValue],
    outcomes: tuple[OutcomeSpec, ...],
    side_effect: SideEffectClass,
    effects: tuple[Any, ...],
    idempotency: IdempotencySpec,
    retry_safe: bool,
) -> CommandContract[Any, Any]:
    return CommandContract(
        execution=ExecutionContract(
            name=name,
            args_model=args,
            result_model=value,
            outcomes=outcomes,
            capability=name,
            side_effect=side_effect,
            idempotency=idempotency,
            retry_safe=retry_safe,
            effects=effects,
        ),
        presentation=CommandPresentation(
            title=name.replace("_", " ").title(), summary=name.replace("_", " ")
        ),
    )


def register_builtin_contracts(registry: ContractRegistry) -> None:
    definitions = (
        (
            "create_task",
            CreateTaskArgs,
            CreateTaskValue,
            _outcomes("created"),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.TASK),),
            IdempotencySpec(mode="none"),
            False,
        ),
        (
            "ensure_task",
            EnsureTaskArgs,
            EnsureTaskValue,
            _outcomes("created", "reused"),
            SideEffectClass.CREATE,
            (CreateOrReuseClause(subject=EffectSubject.TASK, key_arg="dedup_key"),),
            IdempotencySpec(mode="keyed", key_field="dedup_key"),
            True,
        ),
        (
            "edit_task",
            EditTaskArgs,
            EditTaskValue,
            _outcomes("updated"),
            SideEffectClass.UPDATE,
            (UpdateClause(subject=EffectSubject.TASK),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "add_dependency",
            AddDependencyArgs,
            AddDependencyValue,
            _outcomes("linked", "already_linked"),
            SideEffectClass.LINK,
            (
                LinkClause(
                    subject=EffectSubject.DEPENDENCY_EDGE,
                    from_arg="task_id",
                    to_arg="depends_on",
                    relation_arg="dep_type",
                ),
            ),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "gate_create",
            GateCreateArgs,
            GateCreateValue,
            _outcomes("created", "reused", "skipped"),
            SideEffectClass.CREATE,
            (
                CreateClause(subject=EffectSubject.GATE),
                LinkClause(
                    subject=EffectSubject.GATE_WAITER,
                    from_arg="waiter_task_ids",
                    to_arg="await_id",
                    when=ClausePredicate(arg_present="waiter_task_ids"),
                ),
            ),
            IdempotencySpec(mode="keyed", key_field="await_id"),
            False,
        ),
        (
            "gate_resolve",
            GateResolveArgs,
            GateResolveValue,
            (
                OutcomeSpec(name="resolved", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="refused_routing_gate", classification=OutcomeClass.FAILURE),
                OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
            ),
            SideEffectClass.RESOLVE,
            (ResolveClause(subject=EffectSubject.GATE, target_arg="gate_id"),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "list_tasks",
            ListTasksArgs,
            ListTasksValue,
            (OutcomeSpec(name="listed", classification=OutcomeClass.SUCCESS),),
            SideEffectClass.READ,
            (ReadClause(subject=EffectSubject.TASK_LIST),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "get_downstream_tasks",
            GetDownstreamTasksArgs,
            GetDownstreamTasksValue,
            _outcomes("listed"),
            SideEffectClass.READ,
            (ReadClause(subject=EffectSubject.DOWNSTREAM_TASKS),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "task_batch_commit",
            TaskBatchCommitArgs,
            TaskBatchCommitValue,
            _outcomes("committed"),
            SideEffectClass.COMPOSITE,
            (CreateClause(subject=EffectSubject.TASK_GRAPH),),
            IdempotencySpec(mode="keyed", key_field="proposal_id"),
            False,
        ),
        (
            "task_route",
            TaskRouteArgs,
            TaskRouteValue,
            _outcomes("routed"),
            SideEffectClass.COMPOSITE,
            (
                UpdateClause(subject=EffectSubject.TASK_ROUTING),
                ResolveClause(subject=EffectSubject.ROUTING_GATE, target_arg="task_id"),
            ),
            IdempotencySpec(mode="natural"),
            True,
        ),
    )
    for name, args, value, outcomes, effect, clauses, idempotency, retry_safe in definitions:
        if registry.get(name) is None:
            registry.register(
                CommandRegistration(
                    name,
                    _contract(
                        name, args, value, outcomes, effect, clauses, idempotency, retry_safe
                    ),
                    _adapter(name, value),
                )
            )
