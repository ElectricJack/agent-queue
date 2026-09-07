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
from typing import Any, Callable

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


def _json_legacy_active() -> bool:
    return os.environ.get(AQ_JSON_LEGACY_ENV) == "1"


def _project_item(item: Any, fields: tuple[str, ...]) -> Any:
    """Trim a single entity (dict or attribute-bearing object) to *fields*."""
    if isinstance(item, dict):
        return {k: item.get(k) for k in fields}
    return {k: getattr(item, k, None) for k in fields}


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
        return [_project_item(item, fields) for item in data]
    if isinstance(data, dict):
        return _project_item(data, fields)
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

    Known codes: ``command_error``, ``not_found``, ``out_of_scope``,
    ``daemon_unreachable``, ``paused``.

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
    click.echo(json.dumps(error_envelope(code, message, details), default=str))


def emit(
    ctx: click.Context,
    data: Any,
    *,
    entity: str | None = None,
    total: int | None = None,
    render: Callable[[Any], None] | None = None,
) -> None:
    """Single output funnel for `aq` commands (design §5.2).

    - ``--json`` (``ctx.obj["json"]``): prints the versioned envelope (or,
      under ``AQ_JSON_LEGACY=1``, the raw payload) as one JSON object on
      stdout. ``--brief`` (``ctx.obj["brief"]``) trims *data* via
      :func:`apply_brief` first when an *entity* is given.
    - default (human) mode: delegates to *render*, which always receives
      the untrimmed *data* — human-mode formatters keep full control over
      their columns/panels. If no *render* is supplied, falls back to an
      indented JSON dump (adequate for commands that don't have a Rich
      formatter yet).
    """
    obj = ctx.obj or {}
    as_json = bool(obj.get("json"))

    if as_json:
        payload = apply_brief(data, entity) if obj.get("brief") else data
        if _json_legacy_active():
            click.echo(_LEGACY_WARNING, err=True)
            click.echo(json.dumps(payload, default=str))
        else:
            click.echo(json.dumps(envelope(payload, total=total), default=str))
        return

    if render is not None:
        render(data)
        return

    click.echo(json.dumps(data, default=str, indent=2))
