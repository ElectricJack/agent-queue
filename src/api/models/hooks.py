"""Response models for rule and hook commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class RuleRunSummary(BaseModel):
    id: str
    hook_id: str = ""
    project_id: str = ""
    status: str = ""
    trigger_reason: str = ""
    tokens_used: int = 0
    skipped_reason: str | None = None
    started_at: float | None = None
    completed_at: float | None = None


class FireRuleResponse(BaseModel):
    rule_id: str
    fired: int = 0
    skipped: int = 0
    total: int = 0


class RuleRunsResponse(BaseModel):
    rule_id: str
    rule_name: str = ""
    runs: list[RuleRunSummary] = []


class ToggleRuleResponse(BaseModel):
    rule_id: str
    action: str = ""
    hooks_updated: int = 0
    total_hooks: int = 0


class RefreshRulesResponse(BaseModel):
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
    "fire_rule": FireRuleResponse,
    "rule_runs": RuleRunsResponse,
    "toggle_rule": ToggleRuleResponse,
    "refresh_rules": RefreshRulesResponse,
    "refresh_hooks": RefreshRulesResponse,
    "browse_rules": BrowseRulesResponse,
    "list_rules": BrowseRulesResponse,
    "save_rule": RuleOperationResponse,
    "load_rule": RuleOperationResponse,
    "delete_rule": RuleOperationResponse,
}
