"""Versioned JSON output envelope for the ``aq`` CLI.

See ``docs/specs/design/aq-surface.md`` §4 (Output Contract) for the
contract this module implements, and
``docs/specs/implementation/aq-surface.md`` §5.2 for the API this module
must expose.

The envelope is a presentation-layer concern only: it wraps the unchanged
``CommandHandler`` result dicts and the unchanged ``/api/execute``
``{"ok", "result"|"error"}`` wire format.  Neither of those change.

Every ``--json`` invocation of a command routed through :func:`emit` prints
exactly one JSON object on stdout::

    {"schema_version": 1, "data": {...}}
    {"schema_version": 1, "data": [...], "pagination": {"returned": 20, "total": 143, "truncated": true}}
    {"schema_version": 1, "error": {"code": "...", "message": "..."}, "data": null}

Setting ``AQ_JSON_LEGACY=1`` restores the pre-envelope behavior (the raw
command payload printed as-is) for one release, per design §9.2, with a
deprecation warning on stderr.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import click

SCHEMA_VERSION = 1

# Escape-hatch env var name (design §9.2 / §11). Removed one release after S0.
AQ_JSON_LEGACY_ENV = "AQ_JSON_LEGACY"

_LEGACY_WARNING = (
    "Warning: AQ_JSON_LEGACY=1 is set - printing raw (pre-envelope) JSON. "
    "This escape hatch is removed one release after aq-surface Phase S0; "
    'migrate scripts to the versioned envelope ({"schema_version", "data", ...}).'
)

# --brief lite projections (design §4.2). Keyed by entity name, used by
# `emit(..., entity=...)`. Centrally defined so no command hand-rolls its
# own trimming.
BRIEF_PROJECTIONS: dict[str, tuple[str, ...]] = {
    "task": ("id", "title", "status", "priority", "project_id", "assigned_agent"),
    "session": ("id", "task_id", "state", "harness", "last_activity"),
    "gate": ("id", "gate_type", "status", "task_id"),
    "message": ("id", "from", "subject", "created_at", "read"),
    "workspace": ("id", "kind_id", "path", "locked_by"),
    # `aq task create` (single task). The payload is a creation receipt, not a
    # task row, so it has its own projection: `created` is the id a caller
    # should read (`task_id` is its alias, kept because both ship today).
    "task_created": ("created", "task_id", "title", "status", "project_id"),
    "agent": ("id", "name", "state", "profile_id", "current_task_id"),
    "project": ("id", "name", "status", "workspace", "max_concurrent_agents"),
    "pool": (
        "profile_id",
        "enabled",
        "min_active",
        "max_active",
        "desired",
        "running_idle",
        "running_busy",
        "starting",
        "draining",
        "ready",
    ),
    "integration": (
        "outcome",
        "project_id",
        "operation_id",
        "batch_id",
        "effective_mode",
        "desired_mode",
        "generation",
        "draining",
        "ready",
        "blockers",
        "blocker_digest",
        "state",
        "stage",
        "count",
    ),
}

# Public names in the output contract occasionally differ from the internal
# CommandHandler row.  Keep those translations beside the projections so
# every command (handwritten and generated) produces the same brief shape.
_BRIEF_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "workspace": {
        "path": ("path", "workspace_path"),
        "locked_by": ("locked_by", "locked_by_agent_id", "locked_by_task_id"),
    },
}


def _json_legacy_active() -> bool:
    return os.environ.get(AQ_JSON_LEGACY_ENV) == "1"


def _is_unset(value: Any) -> bool:
    """Recognise generated-client ``Unset`` values without importing it."""
    return type(value).__name__ == "Unset"


def to_jsonable(value: Any) -> Any:
    """Convert CLI payloads to lossless JSON-compatible Python values.

    Generic ``default=str`` silently turned generated models (and nested
    ``Unset`` sentinels) into repr strings.  The CLI can receive either plain
    CommandHandler dictionaries or generated-client/Pydantic models, so the
    conversion deliberately uses their public projection methods and then
    recurses.  Missing ``Unset`` mapping fields are omitted; a scalar Unset is
    represented as JSON null.
    """
    if _is_unset(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items() if not _is_unset(item)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value if not _is_unset(item)]
    if hasattr(value, "to_dict"):
        return to_jsonable(value.to_dict())
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json", exclude_unset=True))
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _project_item(item: Any, fields: tuple[str, ...], entity: str) -> Any:
    """Trim a single entity (dict or attribute-bearing object) to *fields*."""
    aliases = _BRIEF_ALIASES.get(entity, {})
    projected: dict[str, Any] = {}
    for field in fields:
        candidates = aliases.get(field, (field,))
        value = None
        for candidate in candidates:
            if isinstance(item, Mapping):
                if candidate in item and not _is_unset(item[candidate]):
                    value = item[candidate]
                    break
            else:
                candidate_value = getattr(item, candidate, None)
                if not _is_unset(candidate_value) and candidate_value is not None:
                    value = candidate_value
                    break
        projected[field] = value
    return projected


def apply_brief(data: Any, entity: str | None) -> Any:
    """Trim *data* to ``BRIEF_PROJECTIONS[entity]`` when registered.

    ``data`` may be a single entity (dict) or a list of entities. Unknown
    entity names, or ``entity=None``, pass *data* through unchanged.
    """
    if not entity:
        return data
    fields = BRIEF_PROJECTIONS.get(entity)
    if not fields:
        return data
    if isinstance(data, list):
        return [_project_item(item, fields, entity) for item in data]
    if isinstance(data, Mapping) or hasattr(data, "to_dict") or hasattr(data, "model_dump"):
        return _project_item(data, fields, entity)
    return data


def envelope(data: Any, *, total: int | None = None) -> dict:
    """Build the versioned success envelope (design §4.1).

    ``pagination`` is only added when *data* is a list: ``returned`` is
    ``len(data)``; ``total`` defaults to ``returned`` when the caller
    doesn't know the server-side matching count; ``truncated`` is
    ``returned < total``.
    """
    env: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "data": data}
    if isinstance(data, list):
        returned = len(data)
        resolved_total = returned if total is None else total
        env["pagination"] = {
            "returned": returned,
            "total": resolved_total,
            "truncated": returned < resolved_total,
        }
    return env


def error_envelope(code: str, message: str, details: Any = None) -> dict:
    """Build the versioned error envelope (design §4.1).

    Known codes: ``usage_error``, ``command_error``, ``not_found``,
    ``out_of_scope``, ``daemon_unreachable``, ``paused``.

    *details* is the command's structured error payload when it has one
    (``create_task_graph`` returns ``errors``/``warnings`` finding lists).
    Additive under ``error``, so it does not bump ``schema_version``.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {
        "schema_version": SCHEMA_VERSION,
        "error": error,
        "data": None,
    }


def emit_error(code: str, message: str, details: Any = None) -> None:
    """Print one error envelope as valid JSON on **stdout**.

    Deliberately ``click.echo`` and not the Rich console: the CLI's console
    writes to stdout, so a Rich-formatted error under ``--json`` landed in
    the stream the consumer is parsing and broke ``json.loads`` on
    ``Expecting value: line 1 column 1`` — and Rich hard-wraps at terminal
    width, splitting the message mid-sentence. Agent-facing commands
    (``aq reply``, ``aq inbox``) depend on this staying machine-readable.
    """
    click.echo(json.dumps(to_jsonable(error_envelope(code, message, details)), ensure_ascii=False))


def reject_json_mode(ctx: click.Context, command: str, reason: str) -> None:
    """Reject a human-only command without leaking its output into JSON stdout.

    Local operator workflows can own interactive prompts, subprocess progress,
    or multi-step diagnostics that are not a single command result.  They must
    make that boundary explicit before doing work when the global ``--json``
    flag is present.
    """
    if not bool((ctx.obj or {}).get("json")):
        return
    emit_error("usage_error", f"{command} does not support --json: {reason}")
    raise SystemExit(2)


def emit(
    ctx: click.Context,
    data: Any,
    *,
    entity: str | None = None,
    total: int | None = None,
    legacy_data: Any | None = None,
    render: Callable[[Any], None] | None = None,
) -> None:
    """Single output funnel for `aq` commands (design §5.2).

    - ``--json`` (``ctx.obj["json"]``): prints the versioned envelope (or,
      under ``AQ_JSON_LEGACY=1``, the raw payload) as one JSON object on
      stdout. ``--brief`` (``ctx.obj["brief"]``) trims *data* via
      :func:`apply_brief` first when an *entity* is given.
    - default (human) mode: delegates to *render*. With ``--brief`` the
      callback receives the same projected payload as JSON mode; otherwise it
      receives *data* unchanged. If no *render* is supplied, falls back to an
      indented JSON dump (adequate for commands without a Rich formatter).
    """
    obj = ctx.obj or {}
    as_json = bool(obj.get("json"))
    payload = apply_brief(data, entity) if obj.get("brief") else data

    if as_json:
        if _json_legacy_active():
            click.echo(_LEGACY_WARNING, err=True)
            raw = payload if legacy_data is None else legacy_data
            click.echo(json.dumps(to_jsonable(raw), ensure_ascii=False))
        else:
            click.echo(json.dumps(to_jsonable(envelope(payload, total=total)), ensure_ascii=False))
        return

    if render is not None:
        render(payload)
        return

    click.echo(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
