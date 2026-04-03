"""Adapter layer: normalise API responses for CLI formatters.

The CLI formatters access data via attribute access (``task.status``,
``agent.state``, etc.).  Responses can arrive as either:

- **Typed models** from the generated API client (attrs-based, may contain
  ``Unset`` sentinel values)
- **Plain dicts** from the generic ``/api/execute`` fallback

The proxies here bridge that gap so formatters work unchanged regardless of
which path returned the data.
"""

from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# Unset detection — avoid importing the generated client's ``Unset`` class
# ---------------------------------------------------------------------------


def _is_unset(value: Any) -> bool:
    """Check if a value is an ``Unset`` sentinel from the generated client."""
    return type(value).__name__ == "Unset"


# ---------------------------------------------------------------------------
# Proxy classes
# ---------------------------------------------------------------------------


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


class TypedProxy:
    """Wrap a typed (attrs) response model to normalise for formatters.

    - Resolves field aliases (e.g. ``id`` → ``workspace_id``)
    - Converts ``Unset`` sentinel values to ``None``
    - Returns ``None`` for missing attributes instead of ``AttributeError``
    """

    def __init__(self, obj: Any, aliases: dict[str, str] | None = None):
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_aliases", aliases or {})

    def __getattr__(self, name: str) -> Any:
        aliases = object.__getattribute__(self, "_aliases")
        obj = object.__getattribute__(self, "_obj")
        key = aliases.get(name, name)
        try:
            value = getattr(obj, key)
        except AttributeError:
            return None
        if _is_unset(value):
            return None
        return value

    def __repr__(self) -> str:
        obj = object.__getattribute__(self, "_obj")
        return f"TypedProxy({obj!r})"

    def get(self, key: str, default: Any = None) -> Any:
        value = self.__getattr__(key)
        return value if value is not None else default


# ---------------------------------------------------------------------------
# Typed proxy constructors
# ---------------------------------------------------------------------------


def _wrap(obj: Any, aliases: dict[str, str] | None = None) -> DictProxy | TypedProxy:
    """Choose the right proxy for the given object."""
    if isinstance(obj, dict):
        return DictProxy(obj, aliases=aliases)
    return TypedProxy(obj, aliases=aliases)


def task_proxy(d: Any) -> DictProxy | TypedProxy:
    """Wrap a task response for formatters.

    Handles:
    - ``assigned_agent`` → ``assigned_agent_id`` alias
    - Dict or typed model input
    """
    if isinstance(d, dict):
        # Normalise status/task_type to uppercase strings for consistency
        patched = dict(d)
        status = d.get("status")
        if isinstance(status, str) and status:
            patched["status"] = status.upper()
        task_type = d.get("task_type")
        if isinstance(task_type, str) and task_type:
            patched["task_type"] = task_type.lower()
        return DictProxy(patched, aliases={"assigned_agent_id": "assigned_agent"})
    return TypedProxy(d, aliases={"assigned_agent_id": "assigned_agent"})


def project_proxy(d: Any) -> DictProxy | TypedProxy:
    """Wrap a project response for formatters."""
    if isinstance(d, dict):
        patched = dict(d)
        status = d.get("status")
        if isinstance(status, str) and status:
            patched["status"] = status.upper()
        patched.setdefault("total_tokens_used", 0)
        patched.setdefault("discord_channel_id", None)
        return DictProxy(patched)
    return TypedProxy(d)


def agent_proxy(d: Any) -> DictProxy | TypedProxy:
    """Wrap an agent/workspace response for formatters.

    Aliases ``workspace_id`` → ``id`` for formatters that use ``agent.id``.
    """
    if isinstance(d, dict):
        patched = dict(d)
        state = d.get("state")
        if isinstance(state, str) and state:
            patched["state"] = state.upper()
        patched.setdefault("session_tokens_used", 0)
        patched.setdefault("agent_type", "claude")
        patched.setdefault("last_heartbeat", None)
        return DictProxy(patched, aliases={"id": "workspace_id"})
    return TypedProxy(d, aliases={"id": "workspace_id"})


def hook_proxy(d: Any) -> DictProxy | TypedProxy:
    """Wrap a hook response for formatters."""
    if isinstance(d, dict):
        patched = dict(d)
        trigger = d.get("trigger")
        if isinstance(trigger, dict):
            patched["trigger"] = json.dumps(trigger)
        patched.setdefault("last_triggered_at", None)
        return DictProxy(patched)
    # For typed models, trigger may be a nested object — convert to JSON string
    proxy = TypedProxy(d)
    return proxy


def hook_run_proxy(d: Any) -> DictProxy | TypedProxy:
    """Wrap a hook-run response for formatters."""
    return _wrap(d)
