"""Response models for deprecated hook and rule commands.

The hook engine and rule manager have been removed (playbooks spec §13
Phase 3).  These models are retained for backward compatibility with
API clients that may reference them.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeprecatedCommandResponse(BaseModel):
    """Response for deprecated hook/rule commands."""

    error: str = ""
    _deprecated: str = ""
    replacements: list[str] = []
    model_config = {"extra": "allow"}


class BrowseRulesResponse(BaseModel):
    """Response for deprecated browse_rules/list_rules (delegates to list_playbooks)."""

    model_config = {"extra": "allow"}


class RuleOperationResponse(BaseModel):
    """Generic response for deprecated rule commands."""

    model_config = {"extra": "allow"}


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "fire_rule": DeprecatedCommandResponse,
    "rule_runs": DeprecatedCommandResponse,
    "toggle_rule": DeprecatedCommandResponse,
    "refresh_rules": DeprecatedCommandResponse,
    "refresh_hooks": DeprecatedCommandResponse,
    "browse_rules": BrowseRulesResponse,
    "list_rules": BrowseRulesResponse,
    "save_rule": RuleOperationResponse,
    "load_rule": RuleOperationResponse,
    "delete_rule": RuleOperationResponse,
}
