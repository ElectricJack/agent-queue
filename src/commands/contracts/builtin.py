"""Typed declarations and legacy-handler adapters for pipeline commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import Field, ValidationError

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
    root: bool | None = None
    # A keyed standing parent: resolve-or-create one container per
    # (project, parent_key) and file the task inside it.  Mutually exclusive
    # with ``parent_id``/``root`` and refused for worker sessions.
    parent_key: str | None = None
    parent_title: str | None = None
    labels: list[str] | None = None
    reason: str | None = None
    discovered_from: str | None = None
    affinity_agent_id: str | None = None
    affinity_reason: str | None = None
    dedup_key: str | None = None
    # Provider intent (provider-failover D9): ``pinned`` | ``preferred`` |
    # ``class_only``; ``pin`` is sugar for ``pinned``.  An ``agent_task``
    # step's ``pin_provider`` arrives here as ``pin``.
    provider_intent: str | None = None
    pin: bool | None = None


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
    provider_intent: str | None = None
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
    # Placement is deliberately tri-state: omitted inherits the task-scoped
    # emergent-work policy, a string selects that parent, and an explicit
    # null (or ``root``) deliberately files at the project root.
    parent_id: str | None = None
    root: bool | None = None
    parent_key: str | None = None
    parent_title: str | None = None
    reason: str | None = None
    discovered_from: str | None = None


class EnsureTaskValue(CommandValue):
    task_id: str
    created: bool
    parent_id: str | None = None


class GitHubIssueTriageArgs(CommandArgs):
    project_id: str


class GitHubIssueTriageValue(CommandValue):
    filed: list[int]
    recovered: list[int]
    remaining_capacity: int


class GitHubIssueFixApprovedArgs(CommandArgs):
    project_id: str
    review_id: str
    revision: int


class GitHubIssueFixApprovedValue(CommandValue):
    outcome: str
    task_id: str | None = None


class GitHubIssueRejectionArgs(CommandArgs):
    project_id: str
    review_id: str
    revision: int


class GitHubIssueRejectionValue(CommandValue):
    outcome: str
    number: int | None = None


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
    """The arguments ``_cmd_list_tasks`` actually reads.

    ``limit`` is deliberately absent.  No path in
    ``src/commands/task_commands.py`` reads it, so a step that set it got the
    handler's own 200-row cap and no warning; leaving it undeclared makes
    ``extra="forbid"`` reject it at compile time instead, which is a
    diagnostic an author can act on.
    """

    project_id: str | None = None
    status: str | None = None
    display_mode: str | None = None
    show_dependencies: bool | None = None


class ListTasksValue(CommandValue):
    """What ``list_tasks`` returns — in both of the shapes it has.

    ``_cmd_list_tasks`` (``src/commands/task_commands.py``) answers with one
    of two payloads, chosen by ``display_mode``: flat mode fills ``tasks``,
    ``total``, ``hidden_completed`` and ``filtered``; tree and compact mode
    fill ``trees``, ``total_root_tasks`` and ``total_tasks``.  Neither fills
    the other half, and ``_adapter`` copies only the keys the handler sent,
    so every field carries a default — a required one on either side makes
    the *other* display mode raise ``ValidationError`` and return
    ``contract_violation``.  Read ``display_mode`` to know which half is
    populated; ``total`` is the flat count and ``total_tasks`` the
    hierarchical one, and they are never both meaningful.

    ``by_project`` and ``project_count`` are not here and never were
    returned: they belong to ``list_active_tasks_all_projects``.  This model
    used to *require* them, so every playbook step calling ``list_tasks``
    failed its contract, in either display mode, while the compiler validated
    against the same fiction and reported nothing.

    Only keys this contract's own arguments can produce are modelled.
    ``label_filter_scope`` is left out deliberately: it appears only for a
    ``labels`` / ``any_label`` filter, which ``ListTasksArgs`` does not
    expose, so no playbook step can reach it.
    """

    display_mode: str = "flat"

    # Flat mode only.
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    hidden_completed: int = 0
    filtered: bool = False
    dependency_display: str | None = None

    # Tree and compact mode only.
    trees: list[dict[str, Any]] = Field(default_factory=list)
    total_root_tasks: int = 0
    total_tasks: int = 0


class ListProjectsArgs(CommandArgs):
    pass


class ListProjectsValue(CommandValue):
    projects: list[dict[str, Any]]


class GetTaskArgs(CommandArgs):
    task_id: str


class GetTaskValue(CommandValue):
    id: str
    project_id: str
    title: str
    description: str
    status: str
    branch_name: str | None = None
    pr_url: str | None = None
    completion: dict[str, Any] | None = None


class RenderPromptArgs(CommandArgs):
    project_id: str | None = None
    name: str | None = None
    path: str | None = None
    variables: dict[str, Any] | None = None


class RenderPromptValue(CommandValue):
    rendered: str
    name: str | None = None
    path: str | None = None
    variables_used: dict[str, Any] = Field(default_factory=dict)


class ReadProjectMemoryFileArgs(CommandArgs):
    project_id: str
    path: str


class ReadProjectMemoryFileValue(CommandValue):
    project_id: str | None = None
    path: str | None = None
    content: str | None = None
    missing: bool = False


class CountProjectMemoryFilesArgs(CommandArgs):
    project_id: str
    path: str
    newer_than: str | None = None


class CountProjectMemoryFilesValue(CommandValue):
    project_id: str
    path: str
    count: int
    total: int
    missing: bool = False
    newer_than: str | None = None


class GitDiffArgs(CommandArgs):
    project_id: str
    base_branch: str | None = None
    workspace: str | None = None


class GitDiffValue(CommandValue):
    project_id: str
    base_branch: str
    diff: str


class MemorySaveArgs(CommandArgs):
    project_id: str
    content: str
    scope: str | None = None


class MemorySaveValue(CommandValue):
    success: bool
    action: str | None = None
    chunk_hash: str | None = None


class MemorySearchArgs(CommandArgs):
    project_id: str
    query: str
    scope: str | None = None


class MemorySearchValue(CommandValue):
    success: bool
    count: int
    results: list[dict[str, Any]] = Field(default_factory=list)


class GetDownstreamTasksArgs(CommandArgs):
    task_id: str


class GetDownstreamTasksValue(CommandValue):
    tasks: list[dict[str, str]]


class TaskBatchCommitArgs(CommandArgs):
    proposal_id: str
    #: The human gate whose resolution approves this proposal.  Omitted, the
    #: command reads the newest human gate awaiting the proposal instead.
    gate_id: str | None = None
    #: Must equal the proposal's (and the gate's) project when given.
    project_id: str | None = None


class TaskBatchCommitValue(CommandValue):
    task_ids: list[str]


class TaskRouteArgs(CommandArgs):
    task_id: str
    profile_id: str
    intelligence_class: str | None = None
    workspace_id: str | None = None
    reason: str | None = None
    # Provider intent (provider-failover D9).  The routing playbook passes
    # ``class_only``; a caller that omits it means the profile as a
    # preference.  ``task_route`` never downgrades an intent on the same
    # provider.
    provider_intent: str | None = None
    pin: bool | None = None


class TaskRouteOptionsArgs(CommandArgs):
    task_id: str


class TaskRouteOptionsValue(CommandValue):
    task_id: str
    project_id: str
    title: str
    description: str
    priority: int
    task_type: str
    intelligence_class: str | None = None
    profile_id: str | None = None
    default_profile_id: str | None = None
    explicit_profile_id: str | None = None
    options: list[dict[str, Any]]
    # Rows on an unavailable provider (provider-failover D11 mechanism 3):
    # reported, never offered for automatic selection.
    unavailable_options: list[dict[str, Any]] = Field(default_factory=list)


class TaskRouteValue(CommandValue):
    task_id: str
    resolved_gate_ids: list[str]
    provider_intent: str | None = None


class CiBaselineStatusArgs(CommandArgs):
    project_id: str
    ref: str | None = None
    max_attempts: int | None = None


class CiBaselineStatusValue(CommandValue):
    state: str
    ref: str
    head_sha: str | None = None
    failing_checks: list[str] = []
    failing_tests: list[str] = []
    run_url: str | None = None
    signature: str | None = None
    attempt: int = 0
    escalated: bool = False
    dedup_key: str | None = None
    title: str | None = None
    description: str | None = None
    escalation_key: str | None = None
    escalation_title: str | None = None
    escalation_question: str | None = None
    in_flight: list[str] = Field(default_factory=list)
    repair_signature: str | None = None
    repair_tests: list[str] = Field(default_factory=list)
    repair_checks: list[str] = Field(default_factory=list)


class CiRepairAdoptArgs(CommandArgs):
    """``ci_repair_adopt``: make a live task the repair that owns a red failure.

    With neither ``failing_tests`` nor ``failing_checks`` the command reads the
    ref's CI itself; the sentinel passes the failure it just observed.
    """

    project_id: str
    task_id: str
    ref: str | None = None
    head_sha: str | None = None
    failing_tests: list[str] | None = None
    failing_checks: list[str] | None = None


class CiRepairAdoptValue(CommandValue):
    task_id: str
    dedup_key: str
    ref: str | None = None
    head_sha: str | None = None
    signature: str | None = None
    failing_tests: list[str] = Field(default_factory=list)
    failing_checks: list[str] = Field(default_factory=list)
    in_flight: list[str] = Field(default_factory=list)


class ProviderUsageProbeArgs(CommandArgs):
    """``provider_usage_probe`` as a playbook step.

    ``provider`` is optional because there is exactly one probeable provider
    today; naming it in the shipped playbook keeps the step readable and
    keeps a second provider from silently inheriting the first one's timer.
    """

    provider: str | None = None


class ProviderUsageProbeValue(CommandValue):
    outcome: str
    provider: str
    recorded: int = 0
    unparsed: bool = False
    snapshots: list[Any] = []
    detail: str | None = None


class MessageSendArgs(CommandArgs):
    """``message_send`` as a playbook step: queue one message on the substrate.

    ``from_kind`` defaults to ``system`` because a playbook is not a user and
    not a session; ``from_id`` names the playbook so the recipient can tell
    an automated escalation from a human's note.
    """

    to_kind: str
    to_id: str
    body: str
    from_id: str
    from_kind: str = "system"
    project_id: str | None = None
    subject: str | None = None
    thread_id: str | None = None
    priority: int | None = None


class MessageSendValue(CommandValue):
    message_id: str
    state: str


class StopTaskArgs(CommandArgs):
    task_id: str


class StopTaskValue(CommandValue):
    stopped: str


class TaskRecoveryNotifyArgs(CommandArgs):
    """``task_recovery_notify`` as a playbook step: wake one durable recovery incident.

    The periodic recovery scan reaches the same incident, so a failure event,
    its replay and the scan share one record and one supervisor notice.
    """

    task_id: str
    project_id: str | None = None


class TaskRecoveryNotifyValue(CommandValue):
    task_id: str | None = None
    incident_id: str | None = None
    operation_id: str | None = None
    detail: str | None = None
    redelivered: bool | None = None


class TaskFailureTriageNotifyArgs(TaskRecoveryNotifyArgs):
    """``task_failure_triage_notify``: idempotently wake failure triage."""


class TaskFailureTriageNotifyValue(TaskRecoveryNotifyValue):
    """The durable incident receipt returned to the triage playbook."""


class ProviderAvailabilityNotifyArgs(CommandArgs):
    """``provider_availability_notify`` as a playbook step (provider-failover D19).

    Idempotent per ``(provider, generation)``: the daemon sends it on every
    change of half, and an event replay or a periodic timer that asks again
    for the same generation queues nothing.  ``generation`` defaults to the
    provider's current one.
    """

    provider: str
    generation: int | None = None


class ProviderRerouteArgs(CommandArgs):
    """``provider_reroute`` as a playbook step (provider-failover D11).

    One sweep under D12-D16: move eligible queued and provider-paused tasks
    off an unavailable provider onto the equivalent rung of an available one,
    within the trickle.  A playbook never forces: the operator-only arguments
    (named tasks, ``to_profile``, ``force``) are not part of the step.
    """

    provider: str | None = None
    dry_run: bool | None = None


class ProviderRerouteValue(CommandValue):
    outcome: str
    dry_run: bool | None = None
    applied: bool | None = None
    disabled_reason: str | None = None
    unavailable_providers: list[str] = Field(default_factory=list)
    moved: list[dict[str, Any]] = Field(default_factory=list)
    held: list[dict[str, Any]] = Field(default_factory=list)
    held_by_kind: dict[str, int] = Field(default_factory=dict)
    resumed: list[str] = Field(default_factory=list)
    batch_ids: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)


class ProviderAvailabilityNotifyValue(CommandValue):
    outcome: str
    provider: str
    generation: int | None = None
    message_ids: list[str] = Field(default_factory=list)
    held: int | None = None
    roles: list[str] = Field(default_factory=list)


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
    refused = bool(raw.get("error")) or raw.get("success") is False
    if name == "ci_baseline_status" and (raw.get("success") is True or not refused):
        # A successful verdict explains ``unknown`` (no GitHub remote, checks
        # unreadable) in ``error``; that is the next tick's problem, not a
        # broken step.  Refusals and the handler's exception path, which
        # carries no ``success`` key, still fall through to ``rejected``.
        state = str(raw.get("state") or "unknown")
        if state == "red" and raw.get("escalated"):
            return "red_escalated"
        return state if state in {"green", "red", "pending", "unknown"} else "unknown"
    if refused:
        if name == "read_project_memory_file" and raw.get("missing"):
            return "missing"
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
        if name == "task_batch_commit" and raw.get("not_approved"):
            # No human decision approves this exact proposal: nothing was
            # created, and the step must not read as a plain rejection of a
            # valid batch.
            return "not_approved"
        return "rejected"
    if name == "task_batch_commit":
        return "already_committed" if raw.get("already_committed") else "committed"
    if name == "provider_usage_probe":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in _PROBE_OUTCOMES else "rejected"
    if name == "ci_baseline_status":
        state = str(raw.get("state") or "unknown")
        if state == "red" and raw.get("escalated"):
            return "red_escalated"
        return state if state in {"green", "red", "pending", "unknown"} else "unknown"
    if name == "ci_repair_adopt":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in {"adopted", "recorded", "unchanged"} else "rejected"
    if name == "ensure_task":
        return "created" if raw.get("created") else "reused"
    if name == "github_issue_triage":
        return "swept"
    if name == "github_issue_fix_approved":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in {"created", "reused", "ignored"} else "rejected"
    if name == "github_issue_rejection":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in {"closed", "ignored"} else "rejected"
    if name == "task_route_options":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in _ROUTE_OPTION_OUTCOMES else "rejected"
    if name == "gate_create":
        if raw.get("skipped"):
            return "skipped"
        return "created" if raw.get("was_created") else "reused"
    if name in {"task_recovery_notify", "task_failure_triage_notify"}:
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in _RECOVERY_NOTIFY_OUTCOMES else "rejected"
    if name == "provider_availability_notify":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in _PROVIDER_NOTIFY_OUTCOMES else "rejected"
    if name == "provider_reroute":
        outcome = str(raw.get("outcome") or "")
        return outcome if outcome in _PROVIDER_REROUTE_OUTCOMES else "rejected"
    return {
        "create_task": "created",
        "edit_task": "updated",
        "add_dependency": "linked",
        "gate_resolve": "resolved",
        "list_tasks": "listed",
        "list_projects": "listed",
        "get_task": "read",
        "render_prompt": "rendered",
        "read_project_memory_file": "missing" if raw.get("missing") else "read",
        "count_project_memory_files": "counted",
        "git_diff": "read",
        "memory_save": "saved",
        "memory_search": "searched",
        "get_downstream_tasks": "listed",
        "task_route": "routed",
        "stop_task": "stopped",
        "message_send": "queued",
    }[name]


def _handler_args(args: CommandArgs) -> dict[str, Any]:
    """The ``CommandHandler.execute`` payload for one typed step's args.

    ``exclude_none`` alone collapses an omitted parent and an explicit
    ``parent_id: null`` — parent placement is one of the few command inputs
    where that distinction changes durable state.  ``exclude_unset`` alone
    keeps it but drops every declared default, so the handler applies its
    own instead: ``MessageSendArgs.from_kind`` defaults to ``system`` while
    ``message_send`` falls back to ``user``.  Send both: every value that is
    not ``None``, defaults included, plus whatever the step set explicitly.
    """
    return {**args.model_dump(exclude_none=True), **args.model_dump(exclude_unset=True)}


def _adapter(name: str, value_type: type[CommandValue]):
    async def invoke(args: CommandArgs, ctx: CommandContext | None) -> CommandResult[Any]:
        if ctx is None:
            raw = await _handler().execute(name, _handler_args(args))
        else:
            # The typed adapter is a dispatch boundary, not merely a value
            # converter.  Re-enter CommandHandler under the principal the
            # executor supplied so delegation narrowing cannot be replaced by
            # a broader ambient request principal.
            with principal_context(ctx):
                raw = await _handler().execute(name, _handler_args(args))
        outcome = _outcome_of(name, raw)
        if name == "provider_usage_probe" and outcome == "rejected":
            value = value_type(
                outcome=outcome,
                provider=str(raw.get("provider") or getattr(args, "provider", None) or "claude"),
                detail=raw.get("error"),
            )
        elif outcome in {
            "rejected", "refused_routing_gate", "already_linked", "not_running", "not_approved",
        }:
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


#: ``held`` (provider-failover D13a): the task has options in principle, but
#: every one is on an unavailable provider.  The routing playbook ends the
#: rule quietly on it instead of failing a run every two minutes per task.
_ROUTE_OPTION_OUTCOMES = frozenset(
    {"already_routed", "explicit", "undecided", "no_options", "held"}
)
#: Every ``provider_reroute`` success (D11): moved something, held
#: something, nothing to do, or re-routing is off.
_PROVIDER_REROUTE_OUTCOMES = frozenset({"rerouted", "held", "idle", "disabled"})
#: Every outcome ``provider_usage_probe`` reports as a success.  A box
#: without the CLI, an API-key account and a disabled probe are all facts
#: about the install, not broken steps: a failing step every ten minutes
#: would fill the run overlay with noise no operator can act on.
_PROBE_OUTCOMES = frozenset(
    {"probed", "unparsed", "not_applicable", "unavailable", "disabled"}
)
#: Every failure-triage notification success. A task with no stopped attempt
#: yet, or a delegate its ended integration operation retired, is a fact
#: about the failure, not a broken step; the scan records it later if needed.
_RECOVERY_NOTIFY_OUTCOMES = frozenset({"queued", "existing", "not_actionable", "retired"})
#: Every ``provider_availability_notify`` success.  A notice already sent for
#: this generation, a flap-damped one and a same-half change are facts about
#: the provider's history, not broken steps.
_PROVIDER_NOTIFY_OUTCOMES = frozenset(
    {
        "notified",
        "flapping",
        "already_notified",
        "flap_damped",
        "not_a_half_change",
        "disabled",
        "messages_disabled",
    }
)


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
    "list_projects": CommandPresentation(
        title="List projects",
        summary="Read the configured projects without changing them.",
        outcome_labels={"listed": "Listed", "rejected": "Rejected"},
        result_labels={"projects": "Projects"},
    ),
    "get_task": CommandPresentation(
        title="Read a task",
        summary="Read one task and its completion record.",
        arg_labels={"task_id": "Task"},
        outcome_labels={"read": "Read", "rejected": "Rejected"},
        result_labels={"id": "Task", "status": "Status"},
    ),
    "render_prompt": CommandPresentation(
        title="Render a prompt",
        summary="Render a bundled or project prompt with explicit variables.",
        arg_labels={
            "project_id": "Project",
            "name": "Prompt name",
            "path": "Prompt path",
            "variables": "Variables",
        },
        outcome_labels={"rendered": "Rendered", "rejected": "Rejected"},
        result_labels={"rendered": "Rendered prompt"},
    ),
    "read_project_memory_file": CommandPresentation(
        title="Read project memory",
        summary="Read one file from a project's memory directory.",
        arg_labels={"project_id": "Project", "path": "Path"},
        outcome_labels={"read": "Read", "missing": "Missing", "rejected": "Rejected"},
        result_labels={"content": "Content", "missing": "Missing"},
    ),
    "count_project_memory_files": CommandPresentation(
        title="Count project memory files",
        summary="Count project-memory files, optionally newer than a timestamp.",
        arg_labels={
            "project_id": "Project",
            "path": "Path",
            "newer_than": "Newer than",
        },
        outcome_labels={"counted": "Counted", "rejected": "Rejected"},
        result_labels={"count": "Count", "total": "Total"},
    ),
    "git_diff": CommandPresentation(
        title="Read a Git diff",
        summary="Read a project's working-tree or branch diff.",
        arg_labels={
            "project_id": "Project",
            "base_branch": "Base branch",
            "workspace": "Workspace",
        },
        outcome_labels={"read": "Read", "rejected": "Rejected"},
        result_labels={"diff": "Diff"},
    ),
    "memory_save": CommandPresentation(
        title="Save memory",
        summary="Save one reusable insight to memory.",
        arg_labels={"project_id": "Project", "content": "Content", "scope": "Scope"},
        outcome_labels={"saved": "Saved", "rejected": "Rejected"},
        result_labels={"success": "Saved", "action": "Action", "chunk_hash": "Memory hash"},
    ),
    "memory_search": CommandPresentation(
        title="Search memory",
        summary="Search for related reusable insights.",
        arg_labels={"query": "Query", "project_id": "Project", "scope": "Scope"},
        outcome_labels={"searched": "Searched", "rejected": "Rejected"},
        result_labels={"results": "Results"},
    ),
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
            "root": "Create at project root",
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
    "github_issue_triage": CommandPresentation(
        title="Triage GitHub issues",
        summary="File bounded investigations and label the corresponding issues.",
        arg_labels={"project_id": "Project"},
        outcome_labels={"swept": "Scanned", "rejected": "Rejected"},
        result_labels={"filed": "Filed issues", "recovered": "Recovered labels"},
        subject_labels={"task": "investigation tasks"},
    ),
    "github_issue_fix_approved": CommandPresentation(
        title="File an approved issue fix",
        summary="Create or reuse the fix task for an approved investigation.",
        arg_labels={"project_id": "Project", "review_id": "Review", "revision": "Revision"},
        outcome_labels={"created": "Created", "reused": "Reused", "ignored": "Ignored",
                        "rejected": "Rejected"},
        result_labels={"task_id": "Fix task"},
        subject_labels={"task": "a fix task"},
    ),
    "github_issue_rejection": CommandPresentation(
        title="Apply an explicit issue closure request",
        summary="Close an issue only when Jack explicitly asks in a rejected review.",
        arg_labels={"project_id": "Project", "review_id": "Review", "revision": "Revision"},
        outcome_labels={"closed": "Closed", "ignored": "Left open", "rejected": "Rejected"},
        result_labels={"number": "Issue number"},
        subject_labels={},
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
        },
        outcome_labels={"listed": "Listed"},
        result_labels={
            "display_mode": "Display mode",
            "tasks": "Tasks",
            "total": "Total",
            "hidden_completed": "Hidden completed tasks",
            "filtered": "Completed tasks hidden",
            "dependency_display": "Dependency view",
            "trees": "Task trees",
            "total_root_tasks": "Root tasks",
            "total_tasks": "Total tasks",
        },
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
        arg_labels={"proposal_id": "Proposal", "gate_id": "Approval gate", "project_id": "Project"},
        outcome_labels={
            "committed": "Committed",
            "already_committed": "Already committed",
            "not_approved": "Not approved",
            "rejected": "Rejected",
        },
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
    "ci_baseline_status": CommandPresentation(
        title="Read the default branch's CI verdict",
        summary=(
            "Judge the head commit's check runs, name the failing checks and tests, "
            "and derive the repair task keyed by their failure signature."
        ),
        arg_labels={
            "project_id": "Project",
            "ref": "Branch or commit",
            "max_attempts": "Repair attempts before escalating",
        },
        outcome_labels={
            "green": "Green",
            "red": "Red",
            "red_escalated": "Red, repairs exhausted",
            "pending": "Pending",
            "unknown": "Unknown",
            "rejected": "Rejected",
        },
        result_labels={
            "state": "CI state",
            "head_sha": "Head commit",
            "failing_checks": "Failing checks",
            "failing_tests": "Failing tests",
            "signature": "Failure signature",
            "dedup_key": "Repair task key",
            "attempt": "Repair attempt",
            "in_flight": "In-flight repairs",
            "repair_signature": "Repair failure signature",
            "repair_tests": "Tests the repair owns",
            "repair_checks": "Checks the repair owns",
        },
        subject_labels={},
    ),
    "ci_repair_adopt": CommandPresentation(
        title="Adopt a task as the CI repair",
        summary=(
            "Key a live task as the repair for a red branch and record the failing "
            "tests it owns, so the CI sentinel reuses it instead of filing another."
        ),
        arg_labels={
            "project_id": "Project",
            "task_id": "Task",
            "ref": "Branch",
            "head_sha": "Commit the failure was read at",
            "failing_tests": "Failing tests the repair owns",
            "failing_checks": "Failing checks",
        },
        outcome_labels={
            "adopted": "Adopted",
            "recorded": "Recorded",
            "unchanged": "Already recorded",
            "rejected": "Rejected",
        },
        result_labels={
            "task_id": "Repair task",
            "dedup_key": "Repair task key",
            "signature": "Failure signature",
            "failing_tests": "Owned tests",
            "failing_checks": "Owned checks",
            "in_flight": "Other in-flight repairs",
        },
        subject_labels={"task": "the repair task"},
    ),
    "provider_usage_probe": CommandPresentation(
        title="Probe a provider's remaining quota",
        summary=(
            "Ask a provider's own CLI what is left of the account's limit windows "
            "and record the reading. Free to run and never billed against the quota "
            "it reports."
        ),
        arg_labels={"provider": "Provider"},
        outcome_labels={
            "probed": "Probed",
            "unparsed": "Output did not parse",
            "not_applicable": "No subscription window",
            "unavailable": "CLI not installed",
            "disabled": "Probe disabled",
            "rejected": "Rejected",
        },
        result_labels={
            "outcome": "Outcome",
            "provider": "Provider",
            "recorded": "Snapshots written",
            "unparsed": "Output did not parse",
            "snapshots": "Readings",
        },
        subject_labels={"provider_usage": "the provider's recorded quota readings"},
    ),
    "message_send": CommandPresentation(
        title="Send a message",
        summary="Queue one message to a session, task, profile, or user; delivery is asynchronous.",
        arg_labels={
            "to_kind": "Recipient kind",
            "to_id": "Recipient",
            "body": "Body",
            "from_id": "Sender",
            "from_kind": "Sender kind",
            "project_id": "Project",
            "subject": "Subject",
            "thread_id": "Thread",
            "priority": "Priority",
        },
        outcome_labels={"queued": "Queued", "rejected": "Rejected"},
        result_labels={"message_id": "Message", "state": "State"},
        subject_labels={"message": "a message"},
    ),
    "task_recovery_notify": CommandPresentation(
        title="Wake a task's recovery incident",
        summary=(
            "Record or reuse the one durable recovery incident for a blocked task and queue "
            "its single supervisor notice; a replayed failure reuses the same incident."
        ),
        arg_labels={"task_id": "Task", "project_id": "Project"},
        outcome_labels={
            "queued": "Incident queued",
            "existing": "Existing incident reused",
            "not_actionable": "Nothing to recover yet",
            "retired": "Delegate retired",
            "rejected": "Rejected",
        },
        result_labels={
            "task_id": "Task",
            "incident_id": "Incident",
            "operation_id": "Integration operation",
            "detail": "Detail",
            "redelivered": "Redelivered",
        },
        subject_labels={"message": "the supervisor's incident notice"},
    ),
    "task_failure_triage_notify": CommandPresentation(
        title="Wake supervisor failure triage",
        summary=(
            "Record or reuse the durable incident for a terminal task failure and queue its "
            "single supervisor triage notice; replayed failures reuse the same incident."
        ),
        arg_labels={"task_id": "Task", "project_id": "Project"},
        outcome_labels={
            "queued": "Triage incident queued",
            "existing": "Existing incident reused",
            "not_actionable": "Nothing to triage yet",
            "retired": "Delegate retired",
            "rejected": "Rejected",
        },
        result_labels={
            "task_id": "Task",
            "incident_id": "Incident",
            "operation_id": "Integration operation",
            "detail": "Detail",
            "redelivered": "Redelivered",
        },
        subject_labels={"message": "the supervisor's triage notice"},
    ),
    "provider_reroute": CommandPresentation(
        title="Re-route work off an unavailable provider",
        summary=(
            "Move queued work whose provider is unavailable to the same intelligence "
            "class on an available provider, a few tasks at a time; pinned tasks and "
            "single-provider classes hold."
        ),
        arg_labels={"provider": "Provider", "dry_run": "Plan only"},
        outcome_labels={
            "rerouted": "Tasks re-routed",
            "held": "Tasks held",
            "idle": "Nothing to re-route",
            "disabled": "Re-routing is off",
            "rejected": "Rejected",
        },
        result_labels={
            "outcome": "Outcome",
            "dry_run": "Plan only",
            "applied": "Applied",
            "disabled_reason": "Why re-routing is off",
            "unavailable_providers": "Unavailable providers",
            "moved": "Moved tasks",
            "held": "Held tasks",
            "held_by_kind": "Held, by reason",
            "resumed": "Resumed tasks",
            "batch_ids": "Batches",
            "notices": "Supervisor notices",
        },
        subject_labels={
            "task_routing": "the re-routed tasks' routes",
            "message": "the per-project batch notice",
        },
    ),
    "provider_availability_notify": CommandPresentation(
        title="Announce a provider's availability change",
        summary=(
            "Message the global supervisor and the human once when a provider moves "
            "between launchable and unavailable; a repeat for the same change sends "
            "nothing."
        ),
        arg_labels={"provider": "Provider", "generation": "State generation"},
        outcome_labels={
            "notified": "Notified",
            "flapping": "Flapping notice sent",
            "already_notified": "Already notified",
            "flap_damped": "Damped while flapping",
            "not_a_half_change": "Not a change of availability",
            "disabled": "Notifications disabled",
            "messages_disabled": "Messaging disabled",
            "rejected": "Rejected",
        },
        result_labels={
            "outcome": "Outcome",
            "provider": "Provider",
            "generation": "State generation",
            "message_ids": "Messages",
            "held": "Held tasks",
            "roles": "Affected role profiles",
        },
        subject_labels={"message": "the provider state-change notice"},
    ),
    "task_route_options": CommandPresentation(
        title="Read a task's routing options",
        summary=(
            "Report whether the task is routed, whether its class is explicit, and which "
            "class, provider and profile combinations could execute it."
        ),
        arg_labels={"task_id": "Task"},
        outcome_labels={
            "already_routed": "Already routed",
            "explicit": "Explicit class",
            "undecided": "Needs a decision",
            "no_options": "Nothing can run it",
            "held": "Held: every option's provider is unavailable",
            "rejected": "Rejected",
        },
        result_labels={
            "intelligence_class": "Intelligence class",
            "profile_id": "Agent profile",
            "explicit_profile_id": "Profile serving the class",
            "options": "Routing options",
            "unavailable_options": "Options on an unavailable provider",
        },
        subject_labels={},
    ),
    "task_route": CommandPresentation(
        title="Route a task to a profile",
        summary="Assign the agent profile that will run the task, and clear its routing gate.",
        arg_labels={
            "task_id": "Task",
            "profile_id": "Agent profile",
            "intelligence_class": "Intelligence class",
            "workspace_id": "Workspace",
            "reason": "Reason",
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
            "list_projects", ListProjectsArgs, ListProjectsValue, _outcomes("listed"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
        (
            "get_task", GetTaskArgs, GetTaskValue, _outcomes("read"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
        (
            "render_prompt", RenderPromptArgs, RenderPromptValue, _outcomes("rendered"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
        (
            "read_project_memory_file", ReadProjectMemoryFileArgs,
            ReadProjectMemoryFileValue, _outcomes("read", "missing"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
        (
            "count_project_memory_files", CountProjectMemoryFilesArgs,
            CountProjectMemoryFilesValue, _outcomes("counted"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
        (
            "git_diff", GitDiffArgs, GitDiffValue, _outcomes("read"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
        (
            "memory_save", MemorySaveArgs, MemorySaveValue, _outcomes("saved"),
            SideEffectClass.CREATE, (), IdempotencySpec(mode="none"), False,
        ),
        (
            "memory_search", MemorySearchArgs, MemorySearchValue, _outcomes("searched"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
        ),
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
            "github_issue_triage",
            GitHubIssueTriageArgs,
            GitHubIssueTriageValue,
            _outcomes("swept"),
            SideEffectClass.COMPOSITE,
            (CreateClause(subject=EffectSubject.TASK),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "github_issue_fix_approved",
            GitHubIssueFixApprovedArgs,
            GitHubIssueFixApprovedValue,
            _outcomes("created", "reused", "ignored"),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.TASK),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "github_issue_rejection",
            GitHubIssueRejectionArgs,
            GitHubIssueRejectionValue,
            _outcomes("closed", "ignored"),
            SideEffectClass.COMPOSITE,
            (),
            IdempotencySpec(mode="natural"),
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
            _outcomes("committed", "already_committed")
            + (OutcomeSpec(name="not_approved", classification=OutcomeClass.FAILURE),),
            SideEffectClass.COMPOSITE,
            (CreateClause(subject=EffectSubject.TASK_GRAPH),),
            IdempotencySpec(mode="keyed", key_field="proposal_id"),
            False,
        ),
        (
            "task_route_options", TaskRouteOptionsArgs, TaskRouteOptionsValue,
            _outcomes("already_routed", "explicit", "undecided", "no_options", "held"),
            SideEffectClass.READ, (), IdempotencySpec(mode="natural"), True,
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
            "ci_baseline_status",
            CiBaselineStatusArgs,
            CiBaselineStatusValue,
            (
                OutcomeSpec(name="green", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="red", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="red_escalated", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="pending", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="unknown", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
            ),
            SideEffectClass.READ,
            (),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "ci_repair_adopt",
            CiRepairAdoptArgs,
            CiRepairAdoptValue,
            (
                OutcomeSpec(name="adopted", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="recorded", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="unchanged", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
            ),
            SideEffectClass.UPDATE,
            (UpdateClause(subject=EffectSubject.TASK),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "provider_usage_probe",
            ProviderUsageProbeArgs,
            ProviderUsageProbeValue,
            (
                OutcomeSpec(name="probed", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="unparsed", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="not_applicable", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="unavailable", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="disabled", classification=OutcomeClass.SUCCESS),
                OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
            ),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.PROVIDER_USAGE),),
            IdempotencySpec(mode="none"),
            True,
        ),
        (
            "message_send",
            MessageSendArgs,
            MessageSendValue,
            _outcomes("queued"),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.MESSAGE),),
            IdempotencySpec(mode="none"),
            False,
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
        (
            "task_recovery_notify",
            TaskRecoveryNotifyArgs,
            TaskRecoveryNotifyValue,
            _outcomes("queued", "existing", "not_actionable", "retired"),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.MESSAGE),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "task_failure_triage_notify",
            TaskFailureTriageNotifyArgs,
            TaskFailureTriageNotifyValue,
            _outcomes("queued", "existing", "not_actionable", "retired"),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.MESSAGE),),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "provider_reroute",
            ProviderRerouteArgs,
            ProviderRerouteValue,
            _outcomes("rerouted", "held", "idle", "disabled"),
            SideEffectClass.COMPOSITE,
            (
                UpdateClause(subject=EffectSubject.TASK_ROUTING),
                CreateClause(subject=EffectSubject.MESSAGE),
            ),
            IdempotencySpec(mode="natural"),
            True,
        ),
        (
            "provider_availability_notify",
            ProviderAvailabilityNotifyArgs,
            ProviderAvailabilityNotifyValue,
            _outcomes(
                "notified",
                "flapping",
                "already_notified",
                "flap_damped",
                "not_a_half_change",
                "disabled",
                "messages_disabled",
            ),
            SideEffectClass.CREATE,
            (CreateClause(subject=EffectSubject.MESSAGE),),
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
    from src.commands.contracts.integration import register_integration_contracts
    from src.commands.contracts.escalation import register_escalation_contracts
    from src.commands.contracts.report import register_report_contracts
    from src.commands.contracts.supervisor_inbox import register_supervisor_inbox_contracts

    register_integration_contracts(registry)
    register_escalation_contracts(registry)
    register_report_contracts(registry)
    register_supervisor_inbox_contracts(registry)
    from src.commands.contracts.wait import register_wait_contracts

    register_wait_contracts(registry)
    from src.commands.contracts.job import register_job_contracts

    register_job_contracts(registry)
    from src.commands.contracts.handoff import register_handoff_contract

    register_handoff_contract(registry)
