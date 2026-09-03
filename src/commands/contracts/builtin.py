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
from src.commands.principal import principal_context


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
    needs_attention: str | None = None
    clear_needs_attention: bool | None = None


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


class StopTaskArgs(CommandArgs):
    task_id: str


class StopTaskValue(CommandValue):
    stopped: str


_handler_provider: Callable[[], Any] | None = None


def set_handler_provider(provider: Callable[[], Any] | None) -> None:
    """Install the legacy ``CommandHandler`` lookup used by every adapter.

    Production installs this from ``Orchestrator.set_command_handler`` — the
    one seam every ``CommandHandler`` construction site in the process goes
    through (``src/main.py``, ``src/api/app.py``, ``src/embedded_mcp.py``).
    The provider is a late-bound callable rather than a handler instance so a
    later re-set is honoured and no reference outlives the orchestrator.
    Passing ``None`` uninstalls it, which is what a test teardown wants.
    """
    global _handler_provider
    _handler_provider = provider


def handler_provider_installed() -> bool:
    """True when :func:`set_handler_provider` has been wired up."""
    return _handler_provider is not None


def _handler() -> Any:
    if _handler_provider is None:
        raise RuntimeError(
            "no CommandHandler provider installed; "
            "Orchestrator.set_command_handler installs it in production"
        )
    handler = _handler_provider()
    if handler is None:
        raise RuntimeError("the installed CommandHandler provider returned None")
    return handler


def _outcome_of(name: str, raw: dict[str, Any]) -> str:
    """Map each legacy return shape to a declared business outcome."""
    if raw.get("error") or raw.get("success") is False:
        if name == "add_dependency" and "already exists" in str(raw.get("error", "")).lower():
            return "already_linked"
        if name == "gate_resolve" and "routing" in str(raw.get("error", "")).lower():
            return "refused_routing_gate"
        if name == "stop_task" and "not in progress" in str(raw.get("error", "")).lower():
            # The task already reached a terminal or non-running state, which
            # is the state a cancellation asks for.  A caller cancelling a
            # child it no longer owns must not see that as a failure; a
            # missing task still does (``rejected``).
            return "not_running"
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
        "stop_task": "stopped",
    }[name]


def _adapter(name: str, value_type: type[CommandValue]):
    async def invoke(args: CommandArgs, ctx: CommandContext | None) -> CommandResult[Any]:
        if ctx is None:
            raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        else:
            # The typed adapter is a dispatch boundary, not merely a value
            # converter.  Re-enter CommandHandler under the principal the
            # executor supplied so delegation narrowing cannot be replaced by
            # a broader ambient request principal.
            with principal_context(ctx):
                raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        outcome = _outcome_of(name, raw)
        if outcome in {"rejected", "refused_routing_gate", "already_linked", "not_running"}:
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


# -- Presentation ---------------------------------------------------------
#
# Copy is authored here and nowhere else.  It is the single source of every
# human-facing string in a rendered explanation: the renderer only ever reads
# these labels, and the dashboard reads the goldens in
# ``tests/fixtures/contracts/`` that the renderer produces from them.  Auto
# generating a title from the command name (what this file did before) meant
# the backend and the hand-written dashboard fixtures disagreed about what an
# operator sees, which is exactly the divergence the contract exists to stop.
#
# Nothing here is fingerprinted, so improving a label never stales a playbook
# (child plan §3.1).  Every ``arg_labels`` / ``result_labels`` key must name a
# real model field and every ``subject_labels`` key a subject the command's own
# effect clauses use; ``test_presentation_labels_name_real_fields`` pins that.

PRESENTATIONS: dict[str, CommandPresentation] = {
    "create_task": CommandPresentation(
        title="Create a task",
        summary="Create a new task, without checking whether a similar one exists.",
        arg_labels={
            "title": "Title",
            "project_id": "Project",
            "description": "Description",
            "priority": "Priority",
            "task_type": "Task type",
            "profile_id": "Agent profile",
            "intelligence_class": "Intelligence class",
            "preferred_workspace_id": "Preferred workspace",
            "integration_mode": "Integration mode",
            "workspace_mode": "Workspace mode",
            "requires_kinds": "Required workspace kinds",
            "depends_on": "Depends on",
            "parent_id": "Parent task",
            "labels": "Labels",
            "reason": "Reason",
            "discovered_from": "Discovered from",
            "affinity_agent_id": "Preferred agent",
            "affinity_reason": "Preferred-agent reason",
            "dedup_key": "Deduplication key",
        },
        outcome_labels={"created": "Created", "rejected": "Rejected"},
        result_labels={"task_id": "Task", "status": "Status", "gate_id": "Routing gate"},
        subject_labels={"task": "a task"},
    ),
    "ensure_task": CommandPresentation(
        title="Ensure a task exists",
        summary="Create the task, or reuse the one already keyed by this deduplication key.",
        arg_labels={
            "dedup_key": "Deduplication key",
            "title": "Title",
            "project_id": "Project",
            "description": "Description",
            "priority": "Priority",
            "profile_id": "Agent profile",
            "intelligence_class": "Intelligence class",
            "initial_status": "Initial status",
        },
        outcome_labels={"created": "Created", "reused": "Reused", "rejected": "Rejected"},
        result_labels={"task_id": "Task", "created": "Was created"},
        subject_labels={"task": "a task"},
    ),
    "edit_task": CommandPresentation(
        title="Edit a task",
        summary="Change fields on an existing task.",
        arg_labels={
            "task_id": "Task",
            "project_id": "Project",
            "title": "Title",
            "description": "Description",
            "priority": "Priority",
            "task_type": "Task type",
            "status": "Status",
            "max_retries": "Maximum retries",
            "verification_type": "Verification type",
            "profile_id": "Agent profile",
            "integration_mode": "Integration mode",
            "skip_verification": "Skip verification",
            "intelligence_class": "Intelligence class",
            "affinity_agent_id": "Preferred agent",
            "affinity_reason": "Preferred-agent reason",
            "workspace_mode": "Workspace mode",
            "needs_attention": "Needs-attention code",
            "clear_needs_attention": "Clear needs attention",
        },
        outcome_labels={"updated": "Updated", "rejected": "Rejected"},
        result_labels={"fields": "Changed fields", "old_status": "Old status", "new_status": "New status"},
        subject_labels={"task": "the task"},
    ),
    "add_dependency": CommandPresentation(
        title="Link a task dependency",
        summary="Record that one task depends on another.",
        arg_labels={
            "task_id": "Task",
            "depends_on": "Depends on",
            "dep_type": "Dependency type",
            "reason": "Reason",
        },
        outcome_labels={
            "linked": "Linked",
            "already_linked": "Already linked",
            "rejected": "Rejected",
        },
        result_labels={"task_title": "Task", "depends_on_title": "Depends on"},
        subject_labels={"dependency_edge": "a dependency between two tasks"},
    ),
    "gate_create": CommandPresentation(
        title="Open a gate",
        summary="Open a gate, and block the tasks waiting on it until it resolves.",
        arg_labels={
            "project_id": "Project",
            "gate_type": "Gate type",
            "title": "Title",
            "question": "Question",
            "timeout_at": "Times out at",
            "waiter_task_ids": "Waiting tasks",
        },
        outcome_labels={
            "created": "Created",
            "reused": "Reused",
            "skipped": "Skipped",
            "rejected": "Rejected",
        },
        result_labels={"gate_id": "Gate", "was_created": "Was created", "reason": "Reason"},
        subject_labels={"gate": "a gate", "gate_waiter": "the waiting tasks to the gate"},
    ),
    "gate_resolve": CommandPresentation(
        title="Resolve a gate",
        summary="Resolve an open gate and unblock every task waiting on it.",
        arg_labels={
            "gate_id": "Gate",
            "resolved_by": "Resolved by",
            "resolution": "Resolution",
        },
        outcome_labels={
            "resolved": "Resolved",
            "refused_routing_gate": "Refused — routing gate",
            "rejected": "Rejected",
        },
        result_labels={"unblocked_task_ids": "Unblocked tasks"},
        subject_labels={"gate": "the gate"},
    ),
    "list_tasks": CommandPresentation(
        title="List tasks",
        summary="Read the task list, without changing anything.",
        arg_labels={
            "project_id": "Project",
            "status": "Status",
            "display_mode": "Display mode",
            "show_dependencies": "Show dependencies",
            "limit": "Limit",
        },
        outcome_labels={"listed": "Listed"},
        result_labels={"tasks": "Tasks", "total": "Total", "by_project": "Tasks by project"},
        subject_labels={"task_list": "the task list"},
    ),
    "get_downstream_tasks": CommandPresentation(
        title="List downstream tasks",
        summary="Read the tasks that depend on this one, without changing anything.",
        arg_labels={"task_id": "Task"},
        outcome_labels={"listed": "Listed", "rejected": "Rejected"},
        result_labels={"tasks": "Downstream tasks"},
        subject_labels={"downstream_tasks": "the tasks that depend on this one"},
    ),
    "task_batch_commit": CommandPresentation(
        title="Commit a proposed task batch",
        summary="Turn an approved proposal into real tasks and dependencies.",
        arg_labels={"proposal_id": "Proposal"},
        outcome_labels={"committed": "Committed", "rejected": "Rejected"},
        result_labels={"task_ids": "Created tasks"},
        subject_labels={"task_graph": "the proposed task graph"},
    ),
    "stop_task": CommandPresentation(
        title="Stop a running task",
        summary="Stop the agent working on a task and leave the task blocked.",
        arg_labels={"task_id": "Task"},
        outcome_labels={
            "stopped": "Stopped",
            "not_running": "Was not running",
            "rejected": "Rejected",
        },
        result_labels={"stopped": "Stopped task"},
        subject_labels={"task_execution": "the task's execution"},
    ),
    "task_route": CommandPresentation(
        title="Route a task to a profile",
        summary="Assign the agent profile that will run the task, and clear its routing gate.",
        arg_labels={
            "task_id": "Task",
            "profile_id": "Agent profile",
            "intelligence_class": "Intelligence class",
            "workspace_id": "Workspace",
        },
        outcome_labels={"routed": "Routed", "rejected": "Rejected"},
        result_labels={"resolved_gate_ids": "Resolved gates"},
        subject_labels={
            "task_routing": "the task's routing",
            "routing_gate": "the task's routing gate",
        },
    ),
}


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
        presentation=PRESENTATIONS[name],
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
        (
            "stop_task",
            StopTaskArgs,
            StopTaskValue,
            (
                OutcomeSpec(name="stopped", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="not_running", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
            ),
            SideEffectClass.UPDATE,
            (UpdateClause(subject=EffectSubject.TASK_EXECUTION),),
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
