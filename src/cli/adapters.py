"""Adapter layer: make CommandHandler dicts look like model objects.

The CLI formatters (``formatters.py``) access data via attribute access on
model objects (``task.status.value``, ``agent.state.value``, etc.).
CommandHandler returns plain dicts with slightly different key names and
string enum values.  The proxies here bridge that gap so formatters work
unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from src.models import AgentState, ProjectStatus, TaskStatus, TaskType


class DictProxy:
    """Wrap a dict to allow attribute access with key aliasing.

    Missing keys return ``None`` instead of raising ``AttributeError``,
    which matches how formatters handle optional fields.
    """

    def __init__(self, data: dict[str, Any], aliases: dict[str, str] | None = None):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_aliases", aliases or {})

    def __getattr__(self, name: str) -> Any:
        aliases = object.__getattribute__(self, "_aliases")
        data = object.__getattribute__(self, "_data")
        key = aliases.get(name, name)
        if key in data:
            return data[key]
        return None

    def __repr__(self) -> str:
        data = object.__getattribute__(self, "_data")
        return f"DictProxy({data!r})"

    def get(self, key: str, default: Any = None) -> Any:
        data = object.__getattribute__(self, "_data")
        return data.get(key, default)


# ---------------------------------------------------------------------------
# Enum helpers
# ---------------------------------------------------------------------------

def _to_task_status(value: str | TaskStatus | None) -> TaskStatus | None:
    if value is None:
        return None
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(value)
    except ValueError:
        return TaskStatus(value.upper())


def _to_task_type(value: str | TaskType | None) -> TaskType | None:
    if value is None:
        return None
    if isinstance(value, TaskType):
        return value
    try:
        return TaskType(value)
    except ValueError:
        return None


def _to_project_status(value: str | ProjectStatus | None) -> ProjectStatus | None:
    if value is None:
        return None
    if isinstance(value, ProjectStatus):
        return value
    try:
        return ProjectStatus(value)
    except ValueError:
        return ProjectStatus(value.upper())


def _to_agent_state(value: str | AgentState | None) -> AgentState | None:
    if value is None:
        return None
    if isinstance(value, AgentState):
        return value
    try:
        return AgentState(value)
    except ValueError:
        return AgentState(value.upper())


# ---------------------------------------------------------------------------
# Typed proxy constructors
# ---------------------------------------------------------------------------

def task_proxy(d: dict[str, Any]) -> DictProxy:
    """Wrap a CommandHandler task dict for formatters.

    Handles:
    - ``assigned_agent`` → ``assigned_agent_id`` alias
    - String status → ``TaskStatus`` enum
    - String task_type → ``TaskType`` enum or None
    - Missing optional fields default to None
    """
    patched = dict(d)
    patched["status"] = _to_task_status(d.get("status"))
    patched["task_type"] = _to_task_type(d.get("task_type"))
    return DictProxy(patched, aliases={"assigned_agent_id": "assigned_agent"})


def project_proxy(d: dict[str, Any]) -> DictProxy:
    """Wrap a CommandHandler project dict for formatters.

    Handles:
    - String status → ``ProjectStatus`` enum
    - Missing ``total_tokens_used`` defaults to 0
    - Missing ``discord_channel_id`` defaults to None
    """
    patched = dict(d)
    patched["status"] = _to_project_status(d.get("status"))
    patched.setdefault("total_tokens_used", 0)
    patched.setdefault("discord_channel_id", None)
    return DictProxy(patched)


def agent_proxy(d: dict[str, Any]) -> DictProxy:
    """Wrap a CommandHandler agent/workspace dict for formatters.

    CommandHandler returns workspace-as-agent dicts with ``workspace_id``,
    ``state`` as lowercase "busy"/"idle".  Formatters expect ``agent.id``,
    ``agent.state`` as ``AgentState`` enum (uppercase), etc.
    """
    patched = dict(d)
    patched["state"] = _to_agent_state(d.get("state"))
    patched.setdefault("session_tokens_used", 0)
    patched.setdefault("agent_type", "claude")
    patched.setdefault("last_heartbeat", None)
    return DictProxy(patched, aliases={"id": "workspace_id"})


def hook_proxy(d: dict[str, Any]) -> DictProxy:
    """Wrap a CommandHandler hook dict for formatters.

    The formatter checks ``isinstance(hook.trigger, str)`` and tries to
    JSON-parse it.  CommandHandler already parses trigger to a dict, so
    we convert it back to a JSON string for the formatter.
    """
    patched = dict(d)
    trigger = d.get("trigger")
    if isinstance(trigger, dict):
        patched["trigger"] = json.dumps(trigger)
    patched.setdefault("last_triggered_at", None)
    return DictProxy(patched)


def hook_run_proxy(d: dict[str, Any]) -> DictProxy:
    """Wrap a CommandHandler hook-run dict for formatters."""
    return DictProxy(dict(d))
