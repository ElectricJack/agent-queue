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


def get_all_response_models() -> dict[str, type[BaseModel]]:
    """Collect RESPONSE_MODELS from every category module."""
    from src.api.models import (
        agent,
        files,
        git,
        mcp,
        memory,
        playbook,
        plugin,
        project,
        system,
        task,
    )

    merged: dict[str, type[BaseModel]] = {}
    for mod in (task, project, agent, git, memory, files, system, plugin, mcp, playbook):
        merged.update(mod.RESPONSE_MODELS)
    return merged
