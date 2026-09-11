"""Pydantic response models for the agent-queue REST API.

Shared base models and re-exports for per-category model files.
Each category module exports a ``RESPONSE_MODELS`` dict mapping
command names to their response model class.
"""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Shared base / mixin models
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Standard error response returned by all endpoints on failure."""

    error: str


class TaskRef(BaseModel):
    """Minimal task reference used in dependency lists, unblocked lists, etc."""

    id: str
    title: str
    status: str = ""
    dep_type: str | None = None
    reason: str | None = None


class TaskBrief(BaseModel):
    """Brief task info used in status overviews and list results."""

    id: str
    title: str
    project_id: str
    status: str = ""
    assigned_agent: str | None = None


# ---------------------------------------------------------------------------
# Aggregate RESPONSE_MODELS from all category modules
# ---------------------------------------------------------------------------


def _category_modules() -> tuple[object, ...]:
    from src.api.models import (
        agent,
        dashboard,
        digest,
        discord,
        escalation,
        files,
        gate,
        git,
        mcp,
        memory,
        message,
        playbook,
        graph,
        playbook_v2,
        plugin,
        project,
        project_onboarding,
        session,
        system,
        task,
    )

    return (
        task,
        project,
        project_onboarding,
        agent,
        dashboard,
        git,
        memory,
        files,
        system,
        plugin,
        mcp,
        playbook,
        playbook_v2,
        session,
        gate,
        message,
        discord,
        digest,
        escalation,
        graph,
    )


def get_all_response_models() -> dict[str, type[BaseModel]]:
    """Collect RESPONSE_MODELS from every category module."""
    merged: dict[str, type[BaseModel]] = {}
    for mod in _category_modules():
        merged.update(mod.RESPONSE_MODELS)
    return merged


def get_all_request_models() -> dict[str, object]:
    """Collect explicit request-model overrides for commands with nested types."""
    merged: dict[str, object] = {}
    for mod in _category_modules():
        merged.update(getattr(mod, "REQUEST_MODELS", {}))
    return merged
