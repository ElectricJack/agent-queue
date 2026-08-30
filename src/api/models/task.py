"""Response models for task commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from . import TaskRef


# ---------------------------------------------------------------------------
# Shared task structures
# ---------------------------------------------------------------------------


class TaskDetail(BaseModel):
    id: str
    project_id: str
    title: str
    description: str = ""
    status: str = ""
    priority: int = 0
    assigned_agent: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    requires_approval: bool = False
    # Persisted graph blockedness (work-graph design §4).
    is_blocked: bool = False
    is_plan_subtask: bool = False
    task_type: str | None = None
    parent_task_id: str | None = None
    profile_id: str | None = None
    auto_approve_plan: bool = False
    skip_verification: bool = False
    pr_url: str | None = None
    depends_on: list[TaskRef] = []
    blocks: list[TaskRef] = []
    subtasks: list[TaskRef] = []
    created_at: float = 0.0
    updated_at: float = 0.0
    parent: dict | None = None
    children: dict | None = None


class TaskDict(BaseModel):
    """Loose task dict as returned in list results."""

    model_config = {"extra": "allow"}
    id: str
    title: str = ""
    status: str = ""


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ListTasksResponse(BaseModel):
    display_mode: str = "flat"
    tasks: list[TaskDetail] = []
    total: int = 0
    hidden_completed: int = 0
    filtered: bool = False
    dependency_display: str | None = None


class CreateTaskResponse(BaseModel):
    created: str
    title: str
    project_id: str
    requires_approval: bool = False
    task_type: str | None = None
    profile_id: str | None = None
    preferred_workspace_id: str | None = None
    attachments: list[str] | None = None
    auto_approve_plan: bool = False
    skip_verification: bool = False
    warning: str | None = None
    # Worker-filed work (swarm work model §12) — always present on every
    # ``_cmd_create_task`` response, not just worker-filed ones.
    success: bool | None = None
    task_id: str | None = None
    gate_id: str | None = None
    status: str | None = None


class GetTaskResponse(TaskDetail):
    pass


class ClaimedBy(BaseModel):
    """Who currently holds a task (swarm-work-model §14).

    Assembled from three places: ``task_metadata.claimed_by_session``,
    ``tasks.assigned_agent_id`` and ``tasks.claim_epoch``.
    """

    session_id: str | None = None
    agent_id: str | None = None
    claim_epoch: int = 0


class TaskShowResponse(TaskDetail):
    """``task_show`` — ``get_task`` plus the composed sections.

    ``claimed_by`` is ``None`` when the task is unclaimed.
    """

    context: list[dict[str, Any]] = []
    labels: list[str] = []
    claimed_by: ClaimedBy | None = None


class EditTaskResponse(BaseModel):
    updated: str
    fields: list[str]
    old_status: str | None = None
    new_status: str | None = None


class DeleteTaskResponse(BaseModel):
    deleted: str
    title: str


class ApproveTaskResponse(BaseModel):
    approved: str
    title: str


class ApprovePlanResponse(BaseModel):
    approved: str
    title: str
    subtask_count: int = 0
    subtasks: list[dict[str, Any]] = []


class RejectPlanResponse(BaseModel):
    rejected: str
    title: str
    status: str = "READY"
    feedback_added: bool = False
    draft_subtasks_deleted: int = 0


class DeletePlanResponse(BaseModel):
    deleted: str
    title: str
    status: str = "COMPLETED"
    draft_subtasks_deleted: int = 0


class StopTaskResponse(BaseModel):
    stopped: str


class RestartTaskResponse(BaseModel):
    restarted: str
    title: str
    previous_status: str = ""


class ReopenWithFeedbackResponse(BaseModel):
    reopened: str
    title: str
    previous_status: str = ""
    status: str = "READY"
    feedback_added: bool = False
    requires_approval: bool = False


class SkipTaskResponse(BaseModel):
    skipped: str
    unblocked_count: int = 0
    unblocked: list[TaskRef] = []


class ArchiveTaskResponse(BaseModel):
    archived: str
    title: str
    status: str = ""


class ArchiveTasksResponse(BaseModel):
    archived_count: int = 0
    archived_ids: list[str] = []
    archived: list[dict[str, Any]] = []
    archive_dir: str | None = None
    project_id: str | None = None


class RestoreTaskResponse(BaseModel):
    restored: str
    title: str
    new_status: str = "DEFINED"


class ListArchivedResponse(BaseModel):
    tasks: list[dict[str, Any]] = []
    count: int = 0
    total: int = 0
    project_id: str | None = None


class ArchiveSettingsResponse(BaseModel):
    enabled: bool = False
    after_hours: int = 0
    statuses: list[str] = []
    archived_count: int = 0
    eligible_count: int = 0


class SetTaskStatusResponse(BaseModel):
    task_id: str
    old_status: str
    new_status: str
    title: str


class AddDependencyResponse(BaseModel):
    ok: bool = True
    task_id: str
    depends_on: str
    task_title: str
    depends_on_title: str


class RemoveDependencyResponse(BaseModel):
    ok: bool = True
    task_id: str
    removed_dependency: str
    task_title: str


class ProvenanceRef(BaseModel):
    """The task at the far end of a non-blocking edge *out of* this one.

    Provenance edges are **outgoing**, pointing from the task toward its
    origin — the same direction as ``depends_on``, which is why they share
    a query.  Today the only kind is ``discovered-from``, so this is the
    task a worker was holding when it filed the one being inspected.
    """

    id: str
    title: str = ""
    status: str = ""
    dep_type: str = ""


class TaskDepsResponse(BaseModel):
    task_id: str
    title: str
    status: str = ""
    depends_on: list[TaskRef] = []
    blocks: list[TaskRef] = []
    #: Outgoing non-blocking edges: where this task came from, as opposed
    #: to what holds it back.
    provenance: list[ProvenanceRef] = []


class GetTaskDiffResponse(BaseModel):
    diff: str = ""
    branch: str = ""


class GetTaskResultResponse(BaseModel):
    model_config = {"extra": "allow"}


class GetTaskTreeResponse(BaseModel):
    root: dict[str, Any] = {}
    formatted: str = ""
    subtask_completed: int = 0
    subtask_total: int = 0
    subtask_by_status: dict[str, int] = {}
    progress_bar: str | None = None


class TaskChildrenResponse(BaseModel):
    success: bool
    task_id: str
    count: int
    children: list[TaskDict]


class TaskProgressResponse(BaseModel):
    success: bool
    parent_id: str
    total: int
    done: int
    ready: int
    blocked: int
    in_progress: int
    waves: list[list[str]]
    max_parallelism: int
    depth: int


class ReparentTaskResponse(BaseModel):
    success: bool
    task_id: str
    old_parent: str | None = None
    new_parent: str | None = None


class GetChainHealthResponse(BaseModel):
    model_config = {"extra": "allow"}
    task_id: str | None = None
    project_id: str | None = None
    status: str | None = None
    title: str | None = None
    stuck_downstream: list[dict[str, Any]] | None = None
    stuck_count: int | None = None
    stuck_chains: list[dict[str, Any]] | None = None
    total_stuck_chains: int | None = None
    message: str | None = None


class ListActiveTasksAllProjectsResponse(BaseModel):
    by_project: dict[str, list[dict[str, Any]]] = {}
    tasks: list[dict[str, Any]] = []
    total: int = 0
    project_count: int = 0
    hidden_completed: int = 0


class ProcessPlanResponse(BaseModel):
    model_config = {"extra": "allow"}
    status: str = ""
    project_id: str = ""
    task_id: str | None = None
    plan_path: str | None = None
    title: str | None = None
    phases: int | None = None
    draft_subtasks: int | None = None
    total_plan_files_found: int | None = None
    workspaces_scanned: int | None = None
    message: str | None = None
    note: str | None = None


class ProcessTaskCompletionResponse(BaseModel):
    model_config = {"extra": "allow"}
    plan_found: bool = False
    reason: str | None = None
    plan_file: str | None = None
    archived_path: str | None = None


class ExplainReason(BaseModel):
    code: str
    detail: str = ""
    ref: str | None = None


class ExplainTaskResponse(BaseModel):
    success: bool = True
    reasons: list[ExplainReason] = []


class ReadyTask(BaseModel):
    """One frontier row.

    Two shapes share this model: the default (``task_id``, ``title``,
    ``priority``) and ``brief: true`` (``id``, ``title``, ``status``,
    ``priority``, ``is_blocked``, ``profile_id``).  Both keys are optional so
    either projection validates.
    """

    task_id: str | None = None
    id: str | None = None
    title: str
    priority: int = 0
    status: str | None = None
    is_blocked: bool | None = None
    profile_id: str | None = None


class WithheldTask(BaseModel):
    task_id: str
    reasons: list[ExplainReason] = []


class ProjectReadyResponse(BaseModel):
    success: bool = True
    ready: list[ReadyTask] = []
    withheld: list[WithheldTask] = []


class EnsureTaskResponse(BaseModel):
    success: bool = True
    task_id: str
    created: bool


class DownstreamTask(BaseModel):
    id: str
    title: str = ""
    status: str = ""


class GetDownstreamTasksResponse(BaseModel):
    success: bool = True
    tasks: list[DownstreamTask] = []


class TaskRouteResponse(BaseModel):
    success: bool = True
    task_id: str
    resolved_gate_ids: list[str] = []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SpecApproveResponse(BaseModel):
    success: bool = True


class TaskBatchProposeResponse(BaseModel):
    """A proposal is created, not applied — nothing exists in the graph yet."""

    success: bool = True
    proposal_id: str | None = None


class TaskBatchAckResponse(BaseModel):
    """``task_batch_update`` / ``task_batch_discard`` — bare acknowledgement."""

    success: bool = True


class ClaimSessionSummary(BaseModel):
    """The calling session's claim bookkeeping, echoed back on every ``task_claim`` reply."""

    id: str | None = None
    claims: int | None = None
    cap: int | None = None
    desired_state: str | None = None
    claim_phase: str | None = None


class TaskClaimResponse(BaseModel):
    """``task_claim`` — pull-based work selection (swarm-work-model §10).

    ``task`` is the claimed task's own **row** — the scalar fields of
    ``GetTaskResponse`` plus ``claim_epoch``, without the joined
    ``depends_on`` / ``blocks`` / ``subtasks`` / ``children`` / ``context``
    / ``labels`` sections (spec §15: building those cost ~10 statements on
    every claim; ``task_show`` remains the full view).  ``None`` for every
    non-``claimed`` result code.
    """

    success: bool
    result: str
    task: dict[str, Any] | None = None
    claim_epoch: int | None = None
    session: ClaimSessionSummary = ClaimSessionSummary()
    reason: str | None = None
    error: str | None = None


class TaskBatchCommitResponse(BaseModel):
    """The ids of the tasks the commit actually created, in batch order."""

    success: bool = True
    task_ids: list[str] = []


class PoolStatusRow(BaseModel):
    """One (project, profile) worker-pool row — swarm-work-model §11."""

    project_id: str
    profile_id: str
    min_active: int
    max_active: int | None = None
    desired: int
    running_idle: int
    running_busy: int
    starting: int
    draining: int
    ready: int
    quarantined_until: float | None = None


class PoolStatusResponse(BaseModel):
    success: bool = True
    pools: list[PoolStatusRow] = []


class FormulaSummary(BaseModel):
    """One entry in ``formula_list``'s ``formulas`` array."""

    model_config = {"extra": "allow"}
    name: str
    description: str = ""
    scope: str = ""
    extends: str | None = None
    vars: dict = {}
    path: str = ""


class FormulaListResponse(BaseModel):
    success: bool = True
    formulas: list[FormulaSummary] = []


class FormulaShowResponse(BaseModel):
    """``formula_show``'s shape varies by outcome (same reasoning as
    ``create_task_graph``): a validation failure reports ``errors`` and the
    raw merged document, ``as_cooked`` omits ``chain``/``chain_sha`` details
    that only apply to a live registry resolution.  ``extra="allow"`` plus
    all-optional fields keeps the model honest about that without pinning
    down a shape narrower than what the command actually returns.
    """

    model_config = {"extra": "allow"}
    success: bool
    error: str | None = None
    name: str | None = None
    scope: str | None = None
    path: str | None = None
    chain: list[str] | None = None
    chain_sha: str | None = None
    vars: dict | None = None
    graph: dict | None = None
    errors: list[dict] = []
    warnings: list[dict] = []
    as_cooked: str | None = None


class FormulaCookResponse(BaseModel):
    """``formula_cook`` shares ``create_task_graph``'s build_report envelope
    (``parent_id``/``nodes``/``dry_run``/...) plus formula-specific fields
    (``container_id``, ``provenance``) and an error envelope on failure.
    """

    model_config = {"extra": "allow"}
    success: bool
    error: str | None = None
    errors: list[dict] = []
    warnings: list[dict] = []
    container_id: str | None = None
    project_id: str | None = None
    parent_id: str | None = None
    parent_title: str | None = None
    provisional: bool | None = None
    task_ids: list[str] = []
    nodes: list[dict] = []
    dependency_count: int | None = None
    context_count: int | None = None
    dry_run: bool | None = None
    created: bool | None = None
    provenance: dict | None = None


class PoolScaleResponse(BaseModel):
    success: bool
    project_id: str | None = None
    profile_id: str | None = None
    min_active: int | None = None
    max_active: int | None = None
    terminated: list[str] = []
    error: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "list_tasks": ListTasksResponse,
    "create_task": CreateTaskResponse,
    "get_task": GetTaskResponse,
    "task_show": TaskShowResponse,
    "edit_task": EditTaskResponse,
    "delete_task": DeleteTaskResponse,
    "approve_task": ApproveTaskResponse,
    "approve_plan": ApprovePlanResponse,
    "reject_plan": RejectPlanResponse,
    "delete_plan": DeletePlanResponse,
    "stop_task": StopTaskResponse,
    "restart_task": RestartTaskResponse,
    "reopen_with_feedback": ReopenWithFeedbackResponse,
    "skip_task": SkipTaskResponse,
    "archive_task": ArchiveTaskResponse,
    "archive_tasks": ArchiveTasksResponse,
    "restore_task": RestoreTaskResponse,
    "list_archived": ListArchivedResponse,
    "archive_settings": ArchiveSettingsResponse,
    "set_task_status": SetTaskStatusResponse,
    "add_dependency": AddDependencyResponse,
    "remove_dependency": RemoveDependencyResponse,
    "task_deps": TaskDepsResponse,
    "get_task_dependencies": TaskDepsResponse,
    "get_task_diff": GetTaskDiffResponse,
    "get_task_result": GetTaskResultResponse,
    "get_task_tree": GetTaskTreeResponse,
    "task_children": TaskChildrenResponse,
    "task_progress": TaskProgressResponse,
    "reparent_task": ReparentTaskResponse,
    "get_chain_health": GetChainHealthResponse,
    "list_active_tasks_all_projects": ListActiveTasksAllProjectsResponse,
    "process_plan": ProcessPlanResponse,
    "process_task_completion": ProcessTaskCompletionResponse,
    "explain_task": ExplainTaskResponse,
    "project_ready": ProjectReadyResponse,
    "ensure_task": EnsureTaskResponse,
    "get_downstream_tasks": GetDownstreamTasksResponse,
    "task_route": TaskRouteResponse,
    "spec_approve": SpecApproveResponse,
    "task_batch_propose": TaskBatchProposeResponse,
    "task_batch_update": TaskBatchAckResponse,
    "task_batch_commit": TaskBatchCommitResponse,
    "task_batch_discard": TaskBatchAckResponse,
    "task_claim": TaskClaimResponse,
    "pool_status": PoolStatusResponse,
    "pool_scale": PoolScaleResponse,
    "formula_list": FormulaListResponse,
    "formula_show": FormulaShowResponse,
    "formula_cook": FormulaCookResponse,
}
