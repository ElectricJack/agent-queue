"""Response models for hooks commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class HookSummary(BaseModel):
    id: str
    project_id: str = ""
    name: str = ""
    enabled: bool = True
    trigger: dict[str, Any] = {}
    cooldown_seconds: int = 0
    prompt_template: str = ""


class CreateHookResponse(BaseModel):
    created: str
    name: str
    project_id: str = ""
    note: str = ""


class EditHookResponse(BaseModel):
    updated: str
    fields: list[str] = []
    warning: str = ""


class DeleteHookResponse(BaseModel):
    deleted: str
    name: str


class FireHookResponse(BaseModel):
    fired: str
    status: str = "running"


class FireAllScheduledHooksResponse(BaseModel):
    fired: list[str] = []
    count: int = 0


class ListHooksResponse(BaseModel):
    hooks: list[HookSummary] = []


class ListHookRunsResponse(BaseModel):
    hook_id: str
    hook_name: str = ""
    runs: list[dict[str, Any]] = []


class HookSchedulesResponse(BaseModel):
    hooks: list[dict[str, Any]] = []
    message: str | None = None


class ScheduleHookResponse(BaseModel):
    created: str
    name: str = ""
    project_id: str = ""
    fire_at: str = ""
    fire_at_epoch: float = 0
    fires_in: str = ""


class CancelScheduledResponse(BaseModel):
    cancelled: str
    name: str = ""


class ListScheduledResponse(BaseModel):
    scheduled_hooks: list[dict[str, Any]] = []
    count: int = 0


class ToggleProjectHooksResponse(BaseModel):
    project_id: str
    action: str = ""
    total_hooks: int = 0
    updated_count: int = 0
    updated_hooks: list[str] = []


class RefreshHooksResponse(BaseModel):
    success: bool = False
    rules_scanned: int = 0
    active_rules: int = 0
    hooks_regenerated: int = 0
    hooks_unchanged: int = 0
    errors: int = 0


class BrowseRulesResponse(BaseModel):
    rules: list[dict[str, Any]] = []


class RuleOperationResponse(BaseModel):
    """Generic response for rule save/load/delete (delegated to rule_manager)."""

    model_config = {"extra": "allow"}


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "create_hook": CreateHookResponse,
    "edit_hook": EditHookResponse,
    "delete_hook": DeleteHookResponse,
    "fire_hook": FireHookResponse,
    "fire_all_scheduled_hooks": FireAllScheduledHooksResponse,
    "list_hooks": ListHooksResponse,
    "list_hook_runs": ListHookRunsResponse,
    "hook_schedules": HookSchedulesResponse,
    "schedule_hook": ScheduleHookResponse,
    "cancel_scheduled": CancelScheduledResponse,
    "list_scheduled": ListScheduledResponse,
    "toggle_project_hooks": ToggleProjectHooksResponse,
    "refresh_hooks": RefreshHooksResponse,
    "browse_rules": BrowseRulesResponse,
    "list_rules": BrowseRulesResponse,
    "save_rule": RuleOperationResponse,
    "load_rule": RuleOperationResponse,
    "delete_rule": RuleOperationResponse,
}
