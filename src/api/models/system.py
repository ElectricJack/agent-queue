"""Response models for system commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TaskStatusSummary(BaseModel):
    total: int = 0
    by_status: dict[str, int] = {}
    in_progress: list[dict[str, Any]] = []
    ready_to_work: list[dict[str, Any]] = []


class GetStatusResponse(BaseModel):
    projects: int = 0
    tasks: TaskStatusSummary = TaskStatusSummary()
    orchestrator_paused: bool = False


class GetTokenUsageResponse(BaseModel):
    model_config = {"extra": "allow"}
    task_id: str | None = None
    project_id: str | None = None
    breakdown: list[dict[str, Any]] = []
    total: int = 0


class TokenAuditResponse(BaseModel):
    total: int = 0
    days: int = 7
    since: str = ""
    until: str = ""
    project_id: str | None = None
    by_project: list[dict[str, Any]] = []
    top_tasks: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []


class ClaudeUsageResponse(BaseModel):
    model_config = {"extra": "allow"}
    subscription: str | None = None
    rate_limit_tier: str | None = None
    active_sessions: list[dict[str, Any]] = []
    active_session_count: int = 0
    active_total_tokens: int = 0
    total_sessions: int | None = None
    total_messages: int | None = None
    model_usage: dict[str, Any] | None = None
    stats_date: str | None = None
    stats_error: str | None = None
    rate_limit: dict[str, Any] | None = None
    rate_limit_error: str | None = None


class GetRecentEventsResponse(BaseModel):
    events: list[Any] = []


class OrchestratorControlResponse(BaseModel):
    status: str = ""
    message: str | None = None
    running_tasks: int | None = None


class ProvideInputResponse(BaseModel):
    task_id: str
    title: str = ""
    status: str = "READY"


class ListPromptsResponse(BaseModel):
    project_id: str
    prompts: list[dict[str, Any]] = []
    categories: list[str] = []
    total: int = 0


class ReadPromptResponse(BaseModel):
    model_config = {"extra": "allow"}
    content: str = ""


class RenderPromptResponse(BaseModel):
    name: str = ""
    rendered: str = ""
    variables_used: dict[str, Any] = {}


class ReloadConfigResponse(BaseModel):
    message: str = ""
    changed_sections: list[str] | None = None
    applied: list[str] | None = None
    restart_required: list[str] | None = None
    summary: str | None = None


class RestartDaemonResponse(BaseModel):
    status: str = "restarting"
    message: str = ""
    reason: str = ""


class ShutdownResponse(BaseModel):
    status: str = "shutting_down"
    mode: str = "graceful"
    reason: str = ""
    timestamp: str = ""
    tasks_stopped: int = 0


class UpdateAndRestartResponse(BaseModel):
    status: str = "updating"
    message: str = ""
    pull_output: str = ""
    reason: str = ""


class RunCommandResponse(BaseModel):
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class EventTrigger(BaseModel):
    name: str
    category: str


class ListEventTriggersResponse(BaseModel):
    events: list[EventTrigger] = []
    count: int = 0


class LogEntry(BaseModel):
    model_config = {"extra": "allow"}
    timestamp: str | None = None
    level: str | None = None
    event: str | None = None
    message: str | None = None
    component: str | None = None


class ReadLogsResponse(BaseModel):
    log_file: str
    level_filter: str
    count: int = 0
    entries: list[LogEntry] = []


class StuckTask(BaseModel):
    id: str
    project_id: str
    status: str
    assigned_agent: str | None = None
    updated_at: float
    seconds_in_state: float


class StuckTasksThresholds(BaseModel):
    assigned: int
    in_progress: int


class GetStuckTasksResponse(BaseModel):
    stuck: list[StuckTask] = []
    now_used: float
    thresholds: StuckTasksThresholds


class StubScanEntry(BaseModel):
    stub_name: str
    status: str
    source_path: str | None = None
    recorded_hash: str | None = None
    current_hash: str | None = None
    last_synced: str | None = None
    is_enriched: bool = False


class StubScanProject(BaseModel):
    project_id: str
    total: int = 0
    stale: int = 0
    missing_source: int = 0
    unenriched: int = 0
    orphaned: int = 0
    current: int = 0
    stubs: list[StubScanEntry] = []


class StubScanTotals(BaseModel):
    total: int = 0
    stale: int = 0
    missing_source: int = 0
    unenriched: int = 0
    orphaned: int = 0
    current: int = 0


class ScanStubStalenessResponse(BaseModel):
    projects: list[StubScanProject] = []
    totals: StubScanTotals | None = None
    summary: str = ""


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "get_status": GetStatusResponse,
    "get_token_usage": GetTokenUsageResponse,
    "token_audit": TokenAuditResponse,
    "claude_usage": ClaudeUsageResponse,
    "get_recent_events": GetRecentEventsResponse,
    "orchestrator_control": OrchestratorControlResponse,
    "provide_input": ProvideInputResponse,
    "list_prompts": ListPromptsResponse,
    "read_prompt": ReadPromptResponse,
    "render_prompt": RenderPromptResponse,
    "reload_config": ReloadConfigResponse,
    "restart_daemon": RestartDaemonResponse,
    "shutdown": ShutdownResponse,
    "update_and_restart": UpdateAndRestartResponse,
    "run_command": RunCommandResponse,
    "list_event_triggers": ListEventTriggersResponse,
    "read_logs": ReadLogsResponse,
    "get_stuck_tasks": GetStuckTasksResponse,
    "scan_stub_staleness": ScanStubStalenessResponse,
}
