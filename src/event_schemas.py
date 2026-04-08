"""Event Schema Registry — required and optional fields per event type.

Defines the canonical payload shape for every event emitted on the EventBus.
Used by ``validate_event()`` (Phase 0.2.2) to catch missing fields at emit
time — errors in dev mode, warnings in prod.

Structure::

    EVENT_SCHEMAS = {
        "event_type": {
            "required": ["field1", "field2"],
            "optional": ["field3", "field4"],
        },
    }

See docs/specs/design/playbooks.md Section 7 and docs/specs/design/roadmap.md
Phase 0.2 for the full specification.
"""

from __future__ import annotations

from typing import TypedDict


class EventSchema(TypedDict):
    """Schema definition for a single event type."""

    required: list[str]
    optional: list[str]


# Meta-fields injected by infrastructure (e.g. ``_plugin`` added by
# ``PluginContext.emit_event``).  Validators should ignore these when
# checking for unexpected extra fields — they are always allowed.
META_FIELDS: frozenset[str] = frozenset({"_plugin"})


# ---------------------------------------------------------------------------
# Task lifecycle events  (emitted via Orchestrator._emit_task_event)
# ---------------------------------------------------------------------------
#
# All task.* events include the base triple (task_id, project_id, title)
# via _emit_task_event, plus event-specific extras.

_TASK_SCHEMAS: dict[str, EventSchema] = {
    "task.started": {
        "required": ["task_id", "project_id", "title"],
        "optional": ["agent_id"],
    },
    "task.completed": {
        "required": ["task_id", "project_id", "title"],
        "optional": [],
    },
    "task.failed": {
        "required": ["task_id", "project_id", "title", "status", "context"],
        "optional": ["error"],
    },
    "task.paused": {
        "required": ["task_id", "project_id", "title", "reason"],
        "optional": ["resume_after"],
    },
    "task.waiting_input": {
        "required": ["task_id", "project_id", "title", "question"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# Note / knowledge events
# ---------------------------------------------------------------------------

_NOTE_SCHEMAS: dict[str, EventSchema] = {
    "note.created": {
        "required": ["project_id", "task_id", "note_path"],
        "optional": [],
    },
    "facts.extracted": {
        "required": ["project_id", "task_id", "staging_path"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# File & folder watch events  (emitted by FileWatcher)
# ---------------------------------------------------------------------------

_FILE_SCHEMAS: dict[str, EventSchema] = {
    "file.changed": {
        "required": ["path", "relative_path", "project_id", "operation"],
        "optional": ["old_mtime", "new_mtime", "size", "watch_id"],
    },
    "folder.changed": {
        "required": ["path", "project_id", "changes", "count"],
        "optional": ["watch_id"],
    },
}

# ---------------------------------------------------------------------------
# Plugin events  (emitted by PluginRegistry)
# ---------------------------------------------------------------------------

_PLUGIN_SCHEMAS: dict[str, EventSchema] = {
    "plugin.loaded": {
        "required": ["plugin", "version"],
        "optional": [],
    },
    "plugin.unloaded": {
        "required": ["plugin"],
        "optional": [],
    },
    "plugin.installed": {
        "required": ["plugin", "version", "source"],
        "optional": [],
    },
    "plugin.updated": {
        "required": ["plugin"],
        "optional": ["version", "rev"],
    },
    "plugin.removed": {
        "required": ["plugin"],
        "optional": [],
    },
    "plugin.reload_failed": {
        "required": ["plugin", "task_id", "error"],
        "optional": [],
    },
    "plugin.auto_disabled": {
        "required": ["plugin", "reason", "failures"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# Configuration events  (emitted by ConfigManager)
# ---------------------------------------------------------------------------

_CONFIG_SCHEMAS: dict[str, EventSchema] = {
    "config.reloaded": {
        "required": ["changed_sections", "config"],
        "optional": [],
    },
    "config.restart_needed": {
        "required": ["changed_sections"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# Notification events  (notify.*)
#
# All notify.* events share the NotifyEvent base fields (event_type,
# severity, category, project_id).  Per-event required/optional lists
# below include only the *additional* fields beyond the base.
# ---------------------------------------------------------------------------

_NOTIFY_BASE_FIELDS = ["event_type", "severity", "category"]
_NOTIFY_BASE_OPTIONAL = ["project_id"]

_NOTIFY_SCHEMAS: dict[str, EventSchema] = {
    # -- Task lifecycle notifications --
    "notify.task_started": {
        "required": [*_NOTIFY_BASE_FIELDS, "task", "agent"],
        "optional": [
            *_NOTIFY_BASE_OPTIONAL,
            "workspace_path",
            "workspace_name",
            "is_reopened",
            "task_description",
            "task_contexts",
        ],
    },
    "notify.task_completed": {
        "required": [*_NOTIFY_BASE_FIELDS, "task", "agent"],
        "optional": [
            *_NOTIFY_BASE_OPTIONAL,
            "summary",
            "files_changed",
            "tokens_used",
        ],
    },
    "notify.task_failed": {
        "required": [*_NOTIFY_BASE_FIELDS, "task", "agent"],
        "optional": [
            *_NOTIFY_BASE_OPTIONAL,
            "error_label",
            "error_detail",
            "fix_suggestion",
            "retry_count",
            "max_retries",
        ],
    },
    "notify.task_blocked": {
        "required": [*_NOTIFY_BASE_FIELDS, "task"],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "last_error"],
    },
    "notify.task_stopped": {
        "required": [*_NOTIFY_BASE_FIELDS, "task"],
        "optional": [*_NOTIFY_BASE_OPTIONAL],
    },
    # -- Interaction notifications --
    "notify.agent_question": {
        "required": [*_NOTIFY_BASE_FIELDS, "task", "agent", "question"],
        "optional": [*_NOTIFY_BASE_OPTIONAL],
    },
    "notify.plan_awaiting_approval": {
        "required": [*_NOTIFY_BASE_FIELDS, "task"],
        "optional": [
            *_NOTIFY_BASE_OPTIONAL,
            "subtasks",
            "plan_url",
            "raw_content",
            "thread_url",
        ],
    },
    # -- VCS notifications --
    "notify.pr_created": {
        "required": [*_NOTIFY_BASE_FIELDS, "task", "pr_url"],
        "optional": [*_NOTIFY_BASE_OPTIONAL],
    },
    "notify.merge_conflict": {
        "required": [*_NOTIFY_BASE_FIELDS, "task", "branch", "target_branch"],
        "optional": [*_NOTIFY_BASE_OPTIONAL],
    },
    "notify.push_failed": {
        "required": [*_NOTIFY_BASE_FIELDS, "task"],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "branch", "error_detail"],
    },
    # -- Budget & system notifications --
    "notify.budget_warning": {
        "required": [*_NOTIFY_BASE_FIELDS, "project_name", "usage", "limit", "percentage"],
        "optional": [*_NOTIFY_BASE_OPTIONAL],
    },
    "notify.chain_stuck": {
        "required": [*_NOTIFY_BASE_FIELDS, "blocked_task"],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "stuck_task_ids", "stuck_task_titles"],
    },
    "notify.stuck_defined_task": {
        "required": [*_NOTIFY_BASE_FIELDS, "task"],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "blocking_deps", "stuck_hours"],
    },
    "notify.system_online": {
        "required": [*_NOTIFY_BASE_FIELDS],
        "optional": [*_NOTIFY_BASE_OPTIONAL],
    },
    # -- Thread / streaming notifications --
    "notify.task_thread_open": {
        "required": [*_NOTIFY_BASE_FIELDS],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "task_id", "thread_name", "initial_message"],
    },
    "notify.task_message": {
        "required": [*_NOTIFY_BASE_FIELDS],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "task_id", "message", "message_type"],
    },
    "notify.task_thread_close": {
        "required": [*_NOTIFY_BASE_FIELDS],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "task_id", "final_status", "final_message"],
    },
    # -- Generic text notification --
    "notify.text": {
        "required": [*_NOTIFY_BASE_FIELDS],
        "optional": [*_NOTIFY_BASE_OPTIONAL, "message", "embed_data"],
    },
}

# ---------------------------------------------------------------------------
# Chat events  (emitted by Discord bot)
# ---------------------------------------------------------------------------

_CHAT_SCHEMAS: dict[str, EventSchema] = {
    "chat.message": {
        "required": ["channel_id", "project_id", "author", "content", "timestamp", "is_bot"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# Git events  (emitted by GitManager — Phase 0.2.5 / playbooks)
#
# These events will be emitted by GitManager once the playbook system is
# wired up.  Schemas are defined now so validation and tooling can reference
# them ahead of time.
# ---------------------------------------------------------------------------

_GIT_SCHEMAS: dict[str, EventSchema] = {
    "git.commit": {
        "required": ["commit_hash", "branch", "changed_files", "project_id"],
        "optional": ["message", "author", "agent_id"],
    },
    "git.push": {
        "required": ["branch", "remote", "project_id"],
        "optional": ["commit_range"],
    },
    "git.pr.created": {
        "required": ["pr_url", "branch", "title", "project_id"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# Playbook events  (emitted by PlaybookExecutor — Phase 0.2.5)
# ---------------------------------------------------------------------------

_PLAYBOOK_SCHEMAS: dict[str, EventSchema] = {
    "playbook.run.completed": {
        "required": ["playbook_id", "run_id"],
        "optional": ["final_context"],
    },
    "playbook.run.failed": {
        "required": ["playbook_id", "run_id", "failed_at_node"],
        "optional": ["error"],
    },
}

# ---------------------------------------------------------------------------
# Human interaction events  (emitted by Dashboard / Discord — Phase 0.2.5)
# ---------------------------------------------------------------------------

_HUMAN_SCHEMAS: dict[str, EventSchema] = {
    "human.review.completed": {
        "required": ["playbook_id", "run_id", "node_id", "decision"],
        "optional": ["edits"],
    },
}

# ---------------------------------------------------------------------------
# Workflow events  (Phase 0.2.5)
# ---------------------------------------------------------------------------

_WORKFLOW_SCHEMAS: dict[str, EventSchema] = {
    "workflow.stage.completed": {
        "required": ["workflow_id", "stage"],
        "optional": ["task_ids"],
    },
}

# ---------------------------------------------------------------------------
# Timer events  (synthetic events emitted by the timer service)
#
# Timer events follow the pattern ``timer.{interval}`` (e.g. ``timer.30m``,
# ``timer.4h``, ``timer.24h``).  Since arbitrary intervals are supported, we
# cannot enumerate all possible event types.  Instead we store the canonical
# timer schema separately and register a few common intervals explicitly.
# ``get_schema()`` falls back to ``TIMER_SCHEMA`` for any ``timer.*`` event
# not explicitly listed.
# ---------------------------------------------------------------------------

TIMER_SCHEMA: EventSchema = {
    "required": ["tick_time", "interval"],
    "optional": [],
}
"""Canonical schema shared by all ``timer.*`` events."""

_TIMER_SCHEMAS: dict[str, EventSchema] = {
    f"timer.{interval}": TIMER_SCHEMA
    for interval in ("1m", "5m", "15m", "30m", "1h", "4h", "12h", "24h")
}

# ---------------------------------------------------------------------------
# Combined registry
# ---------------------------------------------------------------------------

EVENT_SCHEMAS: dict[str, EventSchema] = {
    **_TASK_SCHEMAS,
    **_NOTE_SCHEMAS,
    **_FILE_SCHEMAS,
    **_PLUGIN_SCHEMAS,
    **_CONFIG_SCHEMAS,
    **_NOTIFY_SCHEMAS,
    **_CHAT_SCHEMAS,
    **_GIT_SCHEMAS,
    **_PLAYBOOK_SCHEMAS,
    **_HUMAN_SCHEMAS,
    **_WORKFLOW_SCHEMAS,
    **_TIMER_SCHEMAS,
}
"""Master registry of all event schemas.

Keys are event type strings (e.g., ``"task.completed"``).  Values are dicts
with ``"required"`` and ``"optional"`` field lists.  Used by the validation
layer (Phase 0.2.2) to check payloads at emit time.
"""


def get_schema(event_type: str) -> EventSchema | None:
    """Return the schema for *event_type*, or ``None`` if unregistered.

    For ``timer.*`` events that are not explicitly registered, falls back to
    the canonical :data:`TIMER_SCHEMA` so that arbitrary intervals (e.g.
    ``timer.7m``, ``timer.2h``) are validated correctly.
    """
    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None and event_type.startswith("timer."):
        return TIMER_SCHEMA
    return schema


def registered_event_types() -> list[str]:
    """Return a sorted list of all registered event type strings."""
    return sorted(EVENT_SCHEMAS)


def validate_payload(
    event_type: str,
    payload: dict,
    *,
    strict_extras: bool = False,
) -> list[str]:
    """Check *payload* against the schema for *event_type*.

    Returns a list of error strings (empty == valid).  Checks:

    1. All ``required`` fields are present.
    2. (Optional, when *strict_extras* is ``True``) No fields beyond
       ``required`` + ``optional`` + ``META_FIELDS`` are present.

    If no schema is registered for *event_type* the payload is considered
    valid (unregistered events are allowed).
    """
    schema = get_schema(event_type)
    if schema is None:
        return []

    errors: list[str] = []
    for field in schema["required"]:
        if field not in payload:
            errors.append(f"missing required field '{field}'")

    if strict_extras:
        allowed = set(schema["required"]) | set(schema["optional"]) | META_FIELDS
        for field in payload:
            if field not in allowed:
                errors.append(f"unexpected field '{field}'")

    return errors
