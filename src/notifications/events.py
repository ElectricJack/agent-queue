"""Typed, transport-agnostic notification events.

Each event carries structured data about what happened (event type, severity,
category) and the domain objects involved (TaskDetail, AgentSummary, etc.).
Consumers subscribe to ``notify.*`` event types on the EventBus and format
the events for their specific transport (Discord embeds, WebSocket JSON,
Slack blocks, etc.).

Events use Pydantic models from ``src.api.models`` to ensure consistency
with the REST API — the same data shapes are used for both real-time
notifications and API responses.
"""

from __future__ import annotations

from pydantic import BaseModel

from src.api.models.agent import AgentSummary
from src.api.models.task import TaskDetail


class NotifyEvent(BaseModel):
    """Base for all notification events."""

    event_type: str
    severity: str = "info"  # info, warning, error, critical
    category: str = "system"  # task_lifecycle, vcs, budget, interaction, system
    project_id: str | None = None


# ---------------------------------------------------------------------------
# Task lifecycle events
# ---------------------------------------------------------------------------


class TaskStartedEvent(NotifyEvent):
    event_type: str = "notify.task_started"
    category: str = "task_lifecycle"
    task: TaskDetail
    agent: AgentSummary
    workspace_path: str = ""
    workspace_name: str = ""
    is_reopened: bool = False
    task_description: str = ""
    task_contexts: list[dict] | None = None


class TaskCompletedEvent(NotifyEvent):
    event_type: str = "notify.task_completed"
    category: str = "task_lifecycle"
    task: TaskDetail
    agent: AgentSummary
    summary: str = ""
    files_changed: list[str] = []
    tokens_used: int = 0


class TaskFailedEvent(NotifyEvent):
    event_type: str = "notify.task_failed"
    severity: str = "error"
    category: str = "task_lifecycle"
    task: TaskDetail
    agent: AgentSummary
    error_label: str = ""
    error_detail: str = ""
    fix_suggestion: str = ""
    retry_count: int = 0
    max_retries: int = 3


class TaskBlockedEvent(NotifyEvent):
    event_type: str = "notify.task_blocked"
    severity: str = "critical"
    category: str = "task_lifecycle"
    task: TaskDetail
    last_error: str = ""


class TaskStoppedEvent(NotifyEvent):
    event_type: str = "notify.task_stopped"
    category: str = "task_lifecycle"
    task: TaskDetail


# ---------------------------------------------------------------------------
# Interaction events
# ---------------------------------------------------------------------------


class AgentQuestionEvent(NotifyEvent):
    event_type: str = "notify.agent_question"
    category: str = "interaction"
    task: TaskDetail
    agent: AgentSummary
    question: str


class PlanAwaitingApprovalEvent(NotifyEvent):
    event_type: str = "notify.plan_awaiting_approval"
    category: str = "interaction"
    task: TaskDetail
    subtasks: list[dict] = []  # [{title, description}, ...]
    plan_url: str = ""
    raw_content: str = ""
    thread_url: str = ""


# ---------------------------------------------------------------------------
# VCS events
# ---------------------------------------------------------------------------


class PRCreatedEvent(NotifyEvent):
    event_type: str = "notify.pr_created"
    category: str = "vcs"
    task: TaskDetail
    pr_url: str


class MergeConflictEvent(NotifyEvent):
    event_type: str = "notify.merge_conflict"
    severity: str = "error"
    category: str = "vcs"
    task: TaskDetail
    branch: str
    target_branch: str


class PushFailedEvent(NotifyEvent):
    event_type: str = "notify.push_failed"
    severity: str = "warning"
    category: str = "vcs"
    task: TaskDetail
    branch: str = ""
    error_detail: str = ""


# ---------------------------------------------------------------------------
# Budget & system events
# ---------------------------------------------------------------------------


class BudgetWarningEvent(NotifyEvent):
    event_type: str = "notify.budget_warning"
    severity: str = "warning"
    category: str = "budget"
    project_name: str
    usage: int
    limit: int
    percentage: float


class ChainStuckEvent(NotifyEvent):
    event_type: str = "notify.chain_stuck"
    severity: str = "error"
    category: str = "task_lifecycle"
    blocked_task: TaskDetail
    stuck_task_ids: list[str] = []
    stuck_task_titles: list[str] = []


class StuckDefinedTaskEvent(NotifyEvent):
    event_type: str = "notify.stuck_defined_task"
    severity: str = "warning"
    category: str = "task_lifecycle"
    task: TaskDetail
    blocking_deps: list[dict] = []  # [{id, title, status}, ...]
    stuck_hours: float = 0.0


class SystemOnlineEvent(NotifyEvent):
    event_type: str = "notify.system_online"
    category: str = "system"


# ---------------------------------------------------------------------------
# Thread / streaming events
# ---------------------------------------------------------------------------


class TaskThreadOpenEvent(NotifyEvent):
    event_type: str = "notify.task_thread_open"
    category: str = "task_stream"
    task_id: str = ""
    thread_name: str = ""
    initial_message: str = ""


class TaskMessageEvent(NotifyEvent):
    """A message within a task's execution stream."""

    event_type: str = "notify.task_message"
    category: str = "task_stream"
    task_id: str = ""
    message: str = ""
    message_type: str = "agent_output"  # agent_output, status, error, brief


class TaskThreadCloseEvent(NotifyEvent):
    event_type: str = "notify.task_thread_close"
    category: str = "task_stream"
    task_id: str = ""
    final_status: str = ""  # completed, failed, blocked, stopped
    final_message: str = ""  # e.g. "Work completed: title"


# ---------------------------------------------------------------------------
# Generic text notification (catch-all for simple messages)
# ---------------------------------------------------------------------------


class ProfileSyncFailedEvent(NotifyEvent):
    """Emitted when a profile.md sync to the database fails.

    Common causes include invalid JSON in structured sections, missing
    profile ID, or database errors.  The previous DB config remains active.
    """

    event_type: str = "notify.profile_sync_failed"
    severity: str = "error"
    category: str = "system"
    profile_id: str = ""
    source_path: str = ""
    errors: list[str] = []
    warnings: list[str] = []


# ---------------------------------------------------------------------------
# README summary events
# ---------------------------------------------------------------------------


class ReadmeSummaryUpdatedEvent(NotifyEvent):
    """Emitted when a project README change triggers an orchestrator summary update.

    The orchestrator watches ``vault/projects/*/README.md`` files and
    generates/updates structured summaries in
    ``vault/orchestrator/memory/project-{id}.md``.  This event fires on
    every successful create, update, or removal so hooks, dashboards, and
    other subsystems can react.

    See ``docs/specs/design/self-improvement.md`` Section 5 — Orchestrator
    Memory.
    """

    event_type: str = "notify.readme_summary_updated"
    category: str = "system"
    action: str = ""  # "created", "updated", "removed"
    source_path: str = ""
    summary_path: str = ""


class ReadmeSummaryFailedEvent(NotifyEvent):
    """Emitted when a README summary generation fails.

    Covers file-read errors, write errors, and path-resolution failures.
    The orchestrator's existing summary (if any) remains unchanged.
    """

    event_type: str = "notify.readme_summary_failed"
    severity: str = "error"
    category: str = "system"
    source_path: str = ""
    errors: list[str] = []


# ---------------------------------------------------------------------------
# Playbook compilation events
# ---------------------------------------------------------------------------


class PlaybookCompilationFailedEvent(NotifyEvent):
    """Emitted when playbook markdown compilation fails.

    The previous compiled version (if any) remains active for event
    matching and execution.  The error notification includes the file
    path and all LLM/validation error details so operators can diagnose
    and fix the markdown.

    See ``docs/specs/design/playbooks.md`` Section 4 — Authoring Model.
    """

    event_type: str = "notify.playbook_compilation_failed"
    severity: str = "error"
    category: str = "system"
    playbook_id: str = ""
    source_path: str = ""
    errors: list[str] = []
    previous_version: int | None = None
    source_hash: str = ""
    retries_used: int = 0


class PlaybookCompilationSucceededEvent(NotifyEvent):
    """Emitted when playbook markdown is successfully compiled.

    Provides observability into playbook compilation — useful for
    dashboards and audit logs.
    """

    event_type: str = "notify.playbook_compilation_succeeded"
    severity: str = "info"
    category: str = "system"
    playbook_id: str = ""
    source_path: str = ""
    version: int = 0
    source_hash: str = ""
    node_count: int = 0
    retries_used: int = 0


# ---------------------------------------------------------------------------
# Playbook run lifecycle events
# ---------------------------------------------------------------------------


class PlaybookRunStartedEvent(NotifyEvent):
    """Emitted when a playbook run begins executing its entry node.

    Routes to the project channel (when ``project_id`` is set via the trigger
    event) or the system channel (for system-scoped playbooks).
    """

    event_type: str = "notify.playbook_run_started"
    category: str = "system"
    playbook_id: str = ""
    run_id: str = ""
    trigger_event_type: str = ""
    scope: str = "system"  # "system", "project", or "agent-type:{type}"


class PlaybookRunCompletedEvent(NotifyEvent):
    """Emitted when a playbook run completes successfully.

    Provides observability into playbook execution outcomes — useful for
    dashboards, audit logs, and cross-playbook composition (roadmap 5.3.11).

    See ``docs/specs/design/playbooks.md`` Section 7 — Event System.
    """

    event_type: str = "notify.playbook_run_completed"
    category: str = "system"
    playbook_id: str = ""
    run_id: str = ""
    final_context: str | None = None
    tokens_used: int = 0
    duration_seconds: float = 0.0
    node_count: int = 0


class PlaybookRunFailedEvent(NotifyEvent):
    """Emitted when a playbook run fails.

    Includes the node where execution failed and the error details for
    operator diagnosis and meta-monitoring.

    See ``docs/specs/design/playbooks.md`` Section 7 — Event System.
    """

    event_type: str = "notify.playbook_run_failed"
    severity: str = "error"
    category: str = "system"
    playbook_id: str = ""
    run_id: str = ""
    failed_at_node: str = ""
    error: str = ""
    tokens_used: int = 0
    duration_seconds: float = 0.0


class PlaybookRunPausedEvent(NotifyEvent):
    """Emitted when a playbook run pauses at a ``wait_for_human`` node.

    Surfaces the review request to human operators via Discord, Telegram, or
    other notification transports.  The ``last_response`` field contains the
    last assistant message (capped at 2000 chars) so the reviewer has
    immediate context about what the playbook has done and why human input
    is needed.

    See ``docs/specs/design/playbooks.md`` Section 9 — Human-in-the-Loop.
    Roadmap 5.4.2.
    """

    event_type: str = "notify.playbook_run_paused"
    category: str = "interaction"
    playbook_id: str = ""
    run_id: str = ""
    node_id: str = ""
    last_response: str = ""
    running_seconds: float = 0.0
    tokens_used: int = 0
    paused_at: float = 0.0


class PlaybookRunResumedEvent(NotifyEvent):
    """Emitted when a paused playbook run is resumed after human review.

    Confirms that a previously paused run has been resumed with the human's
    input, allowing notification transports to inform the team.  The
    ``decision`` field contains the human's review response (capped at
    2000 chars).

    See ``docs/specs/design/playbooks.md`` Section 9 — Human-in-the-Loop.
    Roadmap 5.4.3.
    """

    event_type: str = "notify.playbook_run_resumed"
    category: str = "interaction"
    playbook_id: str = ""
    run_id: str = ""
    node_id: str = ""
    decision: str = ""


class PlaybookRunTimedOutEvent(NotifyEvent):
    """Emitted when a paused playbook run exceeds its configured pause timeout.

    Routes to the same notification channel as the original
    ``PlaybookRunPausedEvent`` so the human reviewer is informed in-context
    that the review window has closed.

    If the run transitioned to an ``on_timeout`` node instead of failing,
    ``transitioned_to`` contains the target node ID.

    See ``docs/specs/design/playbooks.md`` Section 9 — Human-in-the-Loop.
    Roadmap 5.4.4 / 5.4.7.
    """

    event_type: str = "notify.playbook_run_timed_out"
    severity: str = "warning"
    category: str = "interaction"
    playbook_id: str = ""
    run_id: str = ""
    node_id: str = ""
    timeout_seconds: int = 0
    waited_seconds: float = 0.0
    tokens_used: int = 0
    transitioned_to: str | None = None


# ---------------------------------------------------------------------------
# Generic text notification (catch-all for simple messages)
# ---------------------------------------------------------------------------


class TextNotifyEvent(NotifyEvent):
    """Plain-text notification for messages that don't warrant a typed event."""

    event_type: str = "notify.text"
    category: str = "system"
    message: str = ""
    embed_data: dict | None = None  # optional structured data for rich rendering
