"""Comprehensive tests for event schema registry validation (Roadmap 0.2.6).

Covers all seven scenarios from the spec:
(a) Valid payloads pass silently
(b) Missing required fields trigger warning (prod) / error (dev)
(c) Extra fields beyond schema pass (forward compatibility)
(d) Wrong field types trigger validation errors
(e) Unregistered events pass through (graceful degradation)
(f) All registered event types have schemas and realistic payloads pass
(g) Error messages include event type, field name, and expected type

Tests exercise both ``validate_event()`` directly and the ``EventBus.emit()``
integration, ensuring schema validation behaves correctly end-to-end.
"""

from __future__ import annotations

import logging
import re

import pytest

from src.event_bus import EventBus, EventValidationError
from src.event_schemas import (
    EVENT_SCHEMAS,
    TIMER_SCHEMA,
    EventSchema,
    get_schema,
    validate_event,
)


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


def _make_bus(*, env="production", validate_events=True):
    return EventBus(env=env, validate_events=validate_events)


# A schema with explicit type constraints for type-checking tests
_TYPED_SCHEMA: EventSchema = {
    "required": ["project_id", "task_id", "count"],
    "optional": ["label", "tags"],
    "types": {
        "project_id": str,
        "task_id": str,
        "count": int,
        "label": str,
        "tags": list,
    },
}

# A schema with union types (accepts multiple types for a field)
_UNION_TYPED_SCHEMA: EventSchema = {
    "required": ["identifier"],
    "optional": [],
    "types": {"identifier": (str, int)},
}


@pytest.fixture(autouse=True)
def _register_typed_schemas(monkeypatch):
    """Register test schemas that include type constraints."""
    monkeypatch.setitem(EVENT_SCHEMAS, "test.typed_event", _TYPED_SCHEMA)
    monkeypatch.setitem(EVENT_SCHEMAS, "test.union_typed", _UNION_TYPED_SCHEMA)


# ---------------------------------------------------------------------------
# Canonical payloads for every event category — used by scenario (f)
# ---------------------------------------------------------------------------

# Maps event_type -> minimal valid payload (all required fields present)
_CANONICAL_PAYLOADS: dict[str, dict] = {
    # Task lifecycle
    "task.started": {
        "task_id": "t-1",
        "project_id": "proj-1",
        "title": "Implement feature X",
    },
    "task.completed": {
        "task_id": "t-1",
        "project_id": "proj-1",
        "title": "Implement feature X",
    },
    "task.failed": {
        "task_id": "t-1",
        "project_id": "proj-1",
        "title": "Implement feature X",
        "status": "failed",
        "context": "max_retries",
    },
    "task.paused": {
        "task_id": "t-1",
        "project_id": "proj-1",
        "title": "Implement feature X",
        "reason": "rate_limited",
    },
    "task.waiting_input": {
        "task_id": "t-1",
        "project_id": "proj-1",
        "title": "Implement feature X",
        "question": "Which database should I use?",
    },
    # Note / knowledge
    "note.created": {
        "project_id": "proj-1",
        "task_id": "t-1",
        "note_path": "/notes/task-t-1.md",
    },
    "facts.extracted": {
        "project_id": "proj-1",
        "task_id": "t-1",
        "staging_path": "/staging/facts.json",
    },
    # File / folder
    "file.changed": {
        "path": "/workspace/src/main.py",
        "relative_path": "src/main.py",
        "project_id": "proj-1",
        "operation": "modified",
    },
    "folder.changed": {
        "path": "/workspace/src",
        "project_id": "proj-1",
        "changes": [{"file": "main.py", "op": "modified"}],
        "count": 1,
    },
    # Plugin
    "plugin.loaded": {"plugin": "my-plugin", "version": "1.0.0"},
    "plugin.unloaded": {"plugin": "my-plugin"},
    "plugin.installed": {
        "plugin": "my-plugin",
        "version": "1.0.0",
        "source": "https://github.com/example/plugin",
    },
    "plugin.updated": {"plugin": "my-plugin"},
    "plugin.removed": {"plugin": "my-plugin"},
    "plugin.reload_failed": {
        "plugin": "my-plugin",
        "task_id": "t-1",
        "error": "ImportError: module not found",
    },
    "plugin.auto_disabled": {
        "plugin": "my-plugin",
        "reason": "consecutive failures",
        "failures": 5,
    },
    # Config
    "config.reloaded": {
        "changed_sections": ["agents", "projects"],
        "config": {"agents": {}, "projects": {}},
    },
    "config.restart_needed": {
        "changed_sections": ["database"],
    },
    # Chat
    "chat.message": {
        "channel_id": "ch-1",
        "project_id": "proj-1",
        "author": "user123",
        "content": "Hello world",
        "timestamp": "2026-01-15T10:30:00Z",
        "is_bot": False,
    },
    "supervisor.chat.completed": {
        "project_id": "proj-1",
        "user_text": "What is the repo URL?",
        "response": "The repo URL is https://github.com/example/repo",
        "tools_used": ["get_project", "reply_to_user"],
    },
    # Git
    "git.commit": {
        "commit_hash": "abc123def456",
        "branch": "feature/new-thing",
        "changed_files": ["src/main.py", "tests/test_main.py"],
        "project_id": "proj-1",
    },
    "git.push": {
        "branch": "feature/new-thing",
        "remote": "origin",
        "project_id": "proj-1",
    },
    "git.pr.created": {
        "pr_url": "https://github.com/org/repo/pull/42",
        "branch": "feature/new-thing",
        "title": "Add new feature",
        "project_id": "proj-1",
    },
    # Playbook
    "playbook.run.completed": {
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
    },
    "playbook.run.failed": {
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
        "failed_at_node": "lint-check",
    },
    "playbook.run.paused": {
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
        "node_id": "review-step",
    },
    "playbook.run.resumed": {
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
        "node_id": "review-step",
        "decision": "approved",
    },
    # Human interaction
    "human.review.completed": {
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
        "node_id": "review-step",
        "decision": "approved",
    },
    # Workflow
    "workflow.stage.completed": {
        "workflow_id": "wf-001",
        "stage": "build",
    },
    # Notify — all share base fields
    "notify.task_started": {
        "event_type": "notify.task_started",
        "severity": "info",
        "category": "task",
        "task": "Implement feature X",
        "agent": "claude-1",
    },
    "notify.task_completed": {
        "event_type": "notify.task_completed",
        "severity": "info",
        "category": "task",
        "task": "Implement feature X",
        "agent": "claude-1",
    },
    "notify.task_failed": {
        "event_type": "notify.task_failed",
        "severity": "error",
        "category": "task",
        "task": "Implement feature X",
        "agent": "claude-1",
    },
    "notify.task_blocked": {
        "event_type": "notify.task_blocked",
        "severity": "warning",
        "category": "task",
        "task": "Implement feature X",
    },
    "notify.task_stopped": {
        "event_type": "notify.task_stopped",
        "severity": "info",
        "category": "task",
        "task": "Implement feature X",
    },
    "notify.agent_question": {
        "event_type": "notify.agent_question",
        "severity": "info",
        "category": "interaction",
        "task": "Implement feature X",
        "agent": "claude-1",
        "question": "Which approach should I take?",
    },
    "notify.plan_awaiting_approval": {
        "event_type": "notify.plan_awaiting_approval",
        "severity": "info",
        "category": "interaction",
        "task": "Implement feature X",
    },
    "notify.pr_created": {
        "event_type": "notify.pr_created",
        "severity": "info",
        "category": "vcs",
        "task": "Implement feature X",
        "pr_url": "https://github.com/org/repo/pull/42",
    },
    "notify.merge_conflict": {
        "event_type": "notify.merge_conflict",
        "severity": "warning",
        "category": "vcs",
        "task": "Implement feature X",
        "branch": "feature/new-thing",
        "target_branch": "main",
    },
    "notify.push_failed": {
        "event_type": "notify.push_failed",
        "severity": "error",
        "category": "vcs",
        "task": "Implement feature X",
    },
    "notify.budget_warning": {
        "event_type": "notify.budget_warning",
        "severity": "warning",
        "category": "budget",
        "project_name": "my-project",
        "usage": 85000,
        "limit": 100000,
        "percentage": 85.0,
    },
    "notify.chain_stuck": {
        "event_type": "notify.chain_stuck",
        "severity": "warning",
        "category": "system",
        "blocked_task": "t-3",
    },
    "notify.stuck_defined_task": {
        "event_type": "notify.stuck_defined_task",
        "severity": "warning",
        "category": "system",
        "task": "t-4",
    },
    "notify.system_online": {
        "event_type": "notify.system_online",
        "severity": "info",
        "category": "system",
    },
    "notify.task_thread_open": {
        "event_type": "notify.task_thread_open",
        "severity": "info",
        "category": "task",
    },
    "notify.task_message": {
        "event_type": "notify.task_message",
        "severity": "info",
        "category": "task",
    },
    "notify.task_thread_close": {
        "event_type": "notify.task_thread_close",
        "severity": "info",
        "category": "task",
    },
    "notify.text": {
        "event_type": "notify.text",
        "severity": "info",
        "category": "system",
    },
    "notify.profile_sync_failed": {
        "event_type": "notify.profile_sync_failed",
        "severity": "error",
        "category": "system",
    },
    "notify.playbook_run_started": {
        "event_type": "notify.playbook_run_started",
        "severity": "info",
        "category": "system",
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
    },
    "notify.playbook_run_completed": {
        "event_type": "notify.playbook_run_completed",
        "severity": "info",
        "category": "system",
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
    },
    "notify.playbook_run_failed": {
        "event_type": "notify.playbook_run_failed",
        "severity": "error",
        "category": "system",
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
        "failed_at_node": "lint-check",
    },
    "notify.playbook_run_resumed": {
        "event_type": "notify.playbook_run_resumed",
        "severity": "info",
        "category": "interaction",
        "playbook_id": "code-quality-gate",
        "run_id": "run-001",
        "node_id": "review-step",
    },
}

# Timer schemas are dynamically generated; add common intervals
for _interval in ("1m", "5m", "15m", "30m", "1h", "4h", "12h", "24h"):
    _CANONICAL_PAYLOADS[f"timer.{_interval}"] = {
        "tick_time": "2026-01-15T10:00:00Z",
        "interval": _interval,
    }


# ═══════════════════════════════════════════════════════════════════════════
# (a) Valid payloads pass validation silently
# ═══════════════════════════════════════════════════════════════════════════


class TestValidPayloadPassesSilently:
    """(a) An event with all required fields passes validation silently."""

    def test_valid_task_completed_no_errors(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
        )
        assert errors == []

    def test_valid_payload_with_optional_fields(self):
        errors = validate_event(
            "task.started",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Starting",
                "agent_id": "a-1",
            },
        )
        assert errors == []

    def test_valid_payload_with_typed_schema(self):
        errors = validate_event(
            "test.typed_event",
            {"project_id": "p-1", "task_id": "t-1", "count": 5},
        )
        assert errors == []

    def test_valid_payload_with_typed_optionals(self):
        errors = validate_event(
            "test.typed_event",
            {
                "project_id": "p-1",
                "task_id": "t-1",
                "count": 5,
                "label": "my-label",
                "tags": ["tag1", "tag2"],
            },
        )
        assert errors == []

    async def test_valid_event_no_error_dev_mode(self):
        """Valid payload emits without raising in dev mode."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
        )
        assert len(received) == 1

    async def test_valid_event_no_warning_prod_mode(self, caplog):
        """Valid payload emits without warnings in prod mode."""
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit(
                "task.completed",
                {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
            )
        assert len(received) == 1
        assert "validation" not in caplog.text.lower()

    async def test_valid_complex_event_dev_mode(self):
        """Complex event with all required fields passes dev mode."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.failed", lambda d: received.append(d))
        await bus.emit(
            "task.failed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Build task",
                "status": "failed",
                "context": "max_retries",
                "error": "timeout",  # optional
            },
        )
        assert len(received) == 1

    def test_valid_timer_event(self):
        errors = validate_event(
            "timer.5m",
            {"tick_time": "2026-01-01T00:00:00Z", "interval": "5m"},
        )
        assert errors == []


# ═══════════════════════════════════════════════════════════════════════════
# (b) Missing required field triggers warning (prod) / error (dev)
# ═══════════════════════════════════════════════════════════════════════════


class TestMissingRequiredFieldBehavior:
    """(b) Missing a required field triggers warning in prod and error in dev."""

    # --- Dev mode: raises EventValidationError ---

    async def test_dev_missing_single_field_raises(self):
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError, match="missing required field 'project_id'"):
            await bus.emit("task.completed", {"task_id": "t-1", "title": "X"})

    async def test_dev_missing_multiple_fields_raises(self):
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError) as exc_info:
            await bus.emit("task.completed", {})
        msg = str(exc_info.value)
        assert "task_id" in msg
        assert "project_id" in msg
        assert "title" in msg

    async def test_dev_handlers_not_called_on_error(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with pytest.raises(EventValidationError):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert received == [], "Handler must not fire when validation raises"

    async def test_dev_missing_extra_required_field(self):
        """task.failed requires status and context beyond the base triple."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError) as exc_info:
            await bus.emit(
                "task.failed",
                {"task_id": "t-1", "project_id": "p-1", "title": "X"},
            )
        msg = str(exc_info.value)
        assert "status" in msg
        assert "context" in msg

    # --- Prod mode: logs warning, delivers event ---

    async def test_prod_missing_field_logs_warning(self, caplog):
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert "missing required field" in caplog.text

    async def test_prod_event_still_delivered(self):
        """In prod mode, event is delivered even when validation fails."""
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"

    async def test_prod_all_missing_fields_in_warning(self, caplog):
        bus = _make_bus(env="production")
        bus.subscribe("task.completed", lambda d: None)
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {})
        assert "task_id" in caplog.text
        assert "project_id" in caplog.text
        assert "title" in caplog.text

    # --- Direct validate_event ---

    def test_validate_single_missing_field(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1"},
        )
        assert len(errors) == 1
        assert "title" in errors[0]

    def test_validate_all_missing_fields(self):
        errors = validate_event("task.completed", {})
        assert len(errors) == 3

    def test_validate_missing_notify_base_fields(self):
        """Notify events require event_type, severity, category."""
        errors = validate_event("notify.text", {})
        missing_fields = {e.split("'")[1] for e in errors}
        assert {"event_type", "severity", "category"}.issubset(missing_fields)


# ═══════════════════════════════════════════════════════════════════════════
# (c) Extra fields beyond schema pass (forward compatibility)
# ═══════════════════════════════════════════════════════════════════════════


class TestExtraFieldsForwardCompatibility:
    """(c) Events with extra fields beyond the schema pass validation."""

    def test_extra_fields_pass_validation(self):
        errors = validate_event(
            "task.completed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Done",
                "extra_field": "value",
                "another_extra": 42,
            },
        )
        assert errors == []

    def test_extra_fields_pass_with_typed_schema(self):
        """Extra fields are allowed even when schema has type constraints."""
        errors = validate_event(
            "test.typed_event",
            {
                "project_id": "p-1",
                "task_id": "t-1",
                "count": 5,
                "future_field": {"nested": True},
            },
        )
        assert errors == []

    async def test_extra_fields_pass_dev_mode(self):
        """Dev mode does not reject extra fields (not strict by default)."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Done",
                "extra": "forward-compat",
            },
        )
        assert len(received) == 1

    async def test_extra_fields_pass_prod_mode(self, caplog):
        """Prod mode does not warn about extra fields."""
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("plugin.loaded", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit(
                "plugin.loaded",
                {
                    "plugin": "my-plugin",
                    "version": "2.0",
                    "new_field_v2": True,
                },
            )
        assert len(received) == 1
        assert "validation" not in caplog.text.lower()

    def test_meta_fields_always_allowed(self):
        """Infrastructure meta-fields like _plugin are allowed."""
        errors = validate_event(
            "task.completed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Done",
                "_plugin": "my-plugin",
            },
        )
        assert errors == []

    def test_strict_mode_rejects_extra_fields(self):
        """When strict_extras=True, extra fields ARE rejected (opt-in)."""
        errors = validate_event(
            "task.completed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Done",
                "rogue_field": True,
            },
            strict_extras=True,
        )
        assert any("unexpected field 'rogue_field'" in e for e in errors)

    def test_strict_mode_still_allows_meta_fields(self):
        """Even strict mode allows META_FIELDS."""
        errors = validate_event(
            "task.completed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Done",
                "_plugin": "my-plugin",
            },
            strict_extras=True,
        )
        assert errors == []


# ═══════════════════════════════════════════════════════════════════════════
# (d) Wrong field type triggers validation error
# ═══════════════════════════════════════════════════════════════════════════


class TestWrongFieldType:
    """(d) A field with the wrong type triggers a validation error."""

    def test_project_id_as_int_instead_of_str(self):
        """project_id should be str; passing int triggers type error."""
        errors = validate_event(
            "test.typed_event",
            {"project_id": 123, "task_id": "t-1", "count": 5},
        )
        assert len(errors) == 1
        assert "project_id" in errors[0]
        assert "str" in errors[0]
        assert "int" in errors[0]

    def test_count_as_str_instead_of_int(self):
        errors = validate_event(
            "test.typed_event",
            {"project_id": "p-1", "task_id": "t-1", "count": "five"},
        )
        assert len(errors) == 1
        assert "count" in errors[0]
        assert "int" in errors[0]
        assert "str" in errors[0]

    def test_multiple_type_errors(self):
        errors = validate_event(
            "test.typed_event",
            {"project_id": 123, "task_id": 456, "count": "bad"},
        )
        type_errors = [e for e in errors if "expected type" in e]
        assert len(type_errors) == 3

    def test_optional_field_type_checked(self):
        """Optional fields are type-checked when present."""
        errors = validate_event(
            "test.typed_event",
            {
                "project_id": "p-1",
                "task_id": "t-1",
                "count": 5,
                "label": 999,
            },
        )
        assert len(errors) == 1
        assert "label" in errors[0]
        assert "str" in errors[0]
        assert "int" in errors[0]

    def test_tags_as_str_instead_of_list(self):
        errors = validate_event(
            "test.typed_event",
            {
                "project_id": "p-1",
                "task_id": "t-1",
                "count": 5,
                "tags": "not-a-list",
            },
        )
        assert len(errors) == 1
        assert "tags" in errors[0]
        assert "list" in errors[0]

    def test_none_value_fails_type_check(self):
        errors = validate_event(
            "test.typed_event",
            {"project_id": None, "task_id": "t-1", "count": 5},
        )
        assert len(errors) == 1
        assert "NoneType" in errors[0]

    def test_union_type_accepts_either(self):
        """Schema with (str, int) union accepts both types."""
        assert validate_event("test.union_typed", {"identifier": "abc"}) == []
        assert validate_event("test.union_typed", {"identifier": 42}) == []

    def test_union_type_rejects_mismatch(self):
        """Schema with (str, int) union rejects other types."""
        errors = validate_event("test.union_typed", {"identifier": [1, 2]})
        assert len(errors) == 1
        assert "str | int" in errors[0]
        assert "list" in errors[0]

    async def test_type_error_raises_in_dev_mode(self):
        """Type errors raise EventValidationError in dev mode."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError, match="expected type"):
            await bus.emit(
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
            )

    async def test_type_error_warns_in_prod_mode(self, caplog):
        """Type errors log a warning in prod mode."""
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("test.typed_event", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit(
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
            )
        assert "expected type" in caplog.text
        # Event is still delivered in prod mode
        assert len(received) == 1

    async def test_type_error_handler_not_called_dev(self):
        """In dev mode, handlers must not fire on type errors."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("test.typed_event", lambda d: received.append(d))
        with pytest.raises(EventValidationError):
            await bus.emit(
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
            )
        assert received == []

    def test_schema_without_types_skips_type_checking(self):
        """Schemas without a 'types' key skip type validation entirely."""
        # task.completed has no types mapping — any value type is accepted
        errors = validate_event(
            "task.completed",
            {"task_id": 999, "project_id": 888, "title": 777},
        )
        assert errors == []

    def test_bool_subclass_of_int_accepted(self):
        """Python bool is a subclass of int — isinstance(True, int) is True."""
        errors = validate_event(
            "test.typed_event",
            {"project_id": "p-1", "task_id": "t-1", "count": True},
        )
        assert errors == []

    def test_combined_missing_and_type_errors(self):
        """Missing field errors and type errors can appear together."""
        errors = validate_event(
            "test.typed_event",
            {"project_id": 123},  # task_id missing, count missing, project_id wrong type
        )
        missing = [e for e in errors if "missing" in e]
        type_errors = [e for e in errors if "expected type" in e]
        assert len(missing) == 2  # task_id and count
        assert len(type_errors) == 1  # project_id


# ═══════════════════════════════════════════════════════════════════════════
# (e) Unregistered event type passes through (graceful degradation)
# ═══════════════════════════════════════════════════════════════════════════


class TestUnregisteredEventPassThrough:
    """(e) Unregistered event types pass through without validation."""

    def test_unregistered_event_returns_empty_errors(self):
        errors = validate_event("completely.unknown.event", {})
        assert errors == []

    def test_unregistered_event_with_arbitrary_payload(self):
        errors = validate_event("no.schema.here", {"any": "payload", "x": 42})
        assert errors == []

    def test_empty_event_type_string(self):
        errors = validate_event("", {"data": True})
        assert errors == []

    async def test_unregistered_event_dev_mode_no_error(self):
        """Dev mode does not raise for unregistered events."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("custom.brand_new_event", lambda d: received.append(d))
        await bus.emit("custom.brand_new_event", {"any": "payload"})
        assert len(received) == 1

    async def test_unregistered_event_prod_mode_no_warning(self, caplog):
        """Prod mode does not warn for unregistered events."""
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("custom.brand_new_event", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("custom.brand_new_event", {"any": "payload"})
        assert len(received) == 1
        assert caplog.text == ""

    def test_get_schema_returns_none_for_unregistered(self):
        assert get_schema("totally.unknown") is None

    def test_arbitrary_timer_interval_uses_fallback(self):
        """Unregistered timer.* events use the TIMER_SCHEMA fallback."""
        schema = get_schema("timer.42s")
        assert schema is TIMER_SCHEMA

    async def test_arbitrary_timer_validates_via_fallback(self):
        """Arbitrary timer intervals get validated via TIMER_SCHEMA fallback."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("timer.7m", lambda d: received.append(d))
        await bus.emit("timer.7m", {"tick_time": "2026-01-01T00:00:00Z", "interval": "7m"})
        assert len(received) == 1

    async def test_arbitrary_timer_missing_field_raises_dev(self):
        """Arbitrary timer intervals are still validated for required fields."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError, match="tick_time"):
            await bus.emit("timer.7m", {"interval": "7m"})


# ═══════════════════════════════════════════════════════════════════════════
# (f) All existing event types have schemas and realistic payloads pass
# ═══════════════════════════════════════════════════════════════════════════


class TestAllEventTypesHaveSchemas:
    """(f) All event types (task.*, note.*, file.*, plugin.*, config.*, etc.)
    have schemas and realistic/current emissions pass validation."""

    def test_all_task_events_registered(self):
        expected = [
            "task.started",
            "task.completed",
            "task.failed",
            "task.paused",
            "task.waiting_input",
        ]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_all_note_events_registered(self):
        assert "note.created" in EVENT_SCHEMAS
        assert "facts.extracted" in EVENT_SCHEMAS

    def test_all_file_events_registered(self):
        assert "file.changed" in EVENT_SCHEMAS
        assert "folder.changed" in EVENT_SCHEMAS

    def test_all_plugin_events_registered(self):
        expected = [
            "plugin.loaded",
            "plugin.unloaded",
            "plugin.installed",
            "plugin.updated",
            "plugin.removed",
            "plugin.reload_failed",
            "plugin.auto_disabled",
        ]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_all_config_events_registered(self):
        assert "config.reloaded" in EVENT_SCHEMAS
        assert "config.restart_needed" in EVENT_SCHEMAS

    def test_all_notify_events_registered(self):
        expected = [
            "notify.task_started",
            "notify.task_completed",
            "notify.task_failed",
            "notify.task_blocked",
            "notify.task_stopped",
            "notify.agent_question",
            "notify.plan_awaiting_approval",
            "notify.pr_created",
            "notify.merge_conflict",
            "notify.push_failed",
            "notify.budget_warning",
            "notify.chain_stuck",
            "notify.stuck_defined_task",
            "notify.system_online",
            "notify.task_thread_open",
            "notify.task_message",
            "notify.task_thread_close",
            "notify.text",
        ]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_all_chat_events_registered(self):
        assert "chat.message" in EVENT_SCHEMAS

    def test_all_git_events_registered(self):
        expected = ["git.commit", "git.push", "git.pr.created"]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_all_playbook_events_registered(self):
        expected = ["playbook.run.completed", "playbook.run.failed"]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_all_human_events_registered(self):
        assert "human.review.completed" in EVENT_SCHEMAS

    def test_all_workflow_events_registered(self):
        assert "workflow.stage.completed" in EVENT_SCHEMAS

    def test_timer_common_intervals_registered(self):
        for interval in ("1m", "5m", "15m", "30m", "1h", "4h", "12h", "24h"):
            assert f"timer.{interval}" in EVENT_SCHEMAS


class TestAllCanonicalPayloadsPassValidation:
    """Every canonical payload for every registered event type passes validation."""

    @pytest.mark.parametrize(
        "event_type",
        sorted(_CANONICAL_PAYLOADS.keys()),
        ids=lambda et: et,
    )
    def test_canonical_payload_passes(self, event_type: str):
        """Canonical payload for {event_type} passes validate_event()."""
        payload = _CANONICAL_PAYLOADS[event_type]
        errors = validate_event(event_type, payload)
        assert errors == [], f"Canonical payload for {event_type} failed validation: {errors}"

    @pytest.mark.parametrize(
        "event_type",
        sorted(_CANONICAL_PAYLOADS.keys()),
        ids=lambda et: et,
    )
    async def test_canonical_payload_passes_dev_bus(self, event_type: str):
        """Canonical payload for {event_type} does not raise on dev EventBus."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe(event_type, lambda d: received.append(d))
        payload = _CANONICAL_PAYLOADS[event_type]
        await bus.emit(event_type, dict(payload))  # copy to avoid mutation
        assert len(received) == 1

    def test_coverage_every_registered_schema_has_canonical_payload(self):
        """Verify _CANONICAL_PAYLOADS covers every real schema in EVENT_SCHEMAS."""
        # Exclude test-only schemas injected by the monkeypatch fixture
        real_schemas = {k for k in EVENT_SCHEMAS if not k.startswith("test.")}
        missing = real_schemas - set(_CANONICAL_PAYLOADS.keys())
        assert missing == set(), f"Missing canonical payloads for: {sorted(missing)}"


class TestSchemaStructureConsistency:
    """Structural invariants that all schemas must satisfy."""

    def test_all_schemas_have_required_and_optional(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            assert "required" in schema, f"{event_type} missing 'required'"
            assert "optional" in schema, f"{event_type} missing 'optional'"

    def test_no_overlap_required_and_optional(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            overlap = set(schema["required"]) & set(schema["optional"])
            assert not overlap, f"{event_type} has fields in both required and optional: {overlap}"

    def test_no_duplicate_fields(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            for key in ("required", "optional"):
                fields = schema[key]
                assert len(fields) == len(set(fields)), f"{event_type}.{key} has duplicate fields"

    def test_task_events_share_base_triple(self):
        """All task.* events require task_id, project_id, title."""
        base = {"task_id", "project_id", "title"}
        for event_type in EVENT_SCHEMAS:
            if event_type.startswith("task."):
                required = set(EVENT_SCHEMAS[event_type]["required"])
                assert base.issubset(required), (
                    f"{event_type} missing base fields: {base - required}"
                )

    def test_notify_events_share_base_fields(self):
        """All notify.* events require event_type, severity, category."""
        base = {"event_type", "severity", "category"}
        for event_type in EVENT_SCHEMAS:
            if event_type.startswith("notify."):
                required = set(EVENT_SCHEMAS[event_type]["required"])
                assert base.issubset(required), (
                    f"{event_type} missing notify base fields: {base - required}"
                )

    def test_notify_events_have_optional_project_id(self):
        """All notify.* events have project_id as optional."""
        for event_type in EVENT_SCHEMAS:
            if event_type.startswith("notify."):
                assert "project_id" in EVENT_SCHEMAS[event_type]["optional"], (
                    f"{event_type} missing optional project_id"
                )

    def test_plugin_events_require_plugin_name(self):
        """All plugin.* events require the 'plugin' field."""
        for event_type in EVENT_SCHEMAS:
            if event_type.startswith("plugin."):
                assert "plugin" in EVENT_SCHEMAS[event_type]["required"], (
                    f"{event_type} missing required 'plugin' field"
                )

    def test_timer_events_share_canonical_schema(self):
        """All registered timer events reference the canonical TIMER_SCHEMA."""
        for event_type in EVENT_SCHEMAS:
            if event_type.startswith("timer."):
                assert EVENT_SCHEMAS[event_type] is TIMER_SCHEMA, (
                    f"{event_type} does not reference TIMER_SCHEMA"
                )


# ═══════════════════════════════════════════════════════════════════════════
# (g) Error messages include event type, field name, and expected type
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorMessageFormatting:
    """(g) Validation error messages include event type, field name, and
    expected type to aid debugging."""

    def test_missing_field_error_format(self):
        """Missing field error: [event_type] missing required field 'field'."""
        errors = validate_event("task.completed", {})
        for err in errors:
            # Starts with [event_type]
            assert err.startswith("[task.completed]"), f"Missing event type prefix: {err}"
            # Contains "missing required field"
            assert "missing required field" in err
            # Contains quoted field name
            assert re.search(r"'[a-z_]+'", err), f"Missing quoted field name: {err}"

    def test_missing_field_includes_field_name(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t-1"},
        )
        field_names = set()
        for err in errors:
            match = re.search(r"'(\w+)'", err)
            assert match
            field_names.add(match.group(1))
        assert "project_id" in field_names
        assert "title" in field_names

    def test_type_error_format(self):
        """Type error: [event_type] field 'name' expected type 'str', got 'int'."""
        errors = validate_event(
            "test.typed_event",
            {"project_id": 123, "task_id": "t-1", "count": 5},
        )
        assert len(errors) == 1
        err = errors[0]
        # Starts with [event_type]
        assert err.startswith("[test.typed_event]")
        # Contains field name
        assert "'project_id'" in err
        # Contains expected type
        assert "expected type" in err
        assert "'str'" in err
        # Contains actual type
        assert "got" in err
        assert "'int'" in err

    def test_type_error_with_union_shows_all_types(self):
        """Union type errors show 'str | int', not just one type."""
        errors = validate_event("test.union_typed", {"identifier": 3.14})
        assert len(errors) == 1
        err = errors[0]
        assert "[test.union_typed]" in err
        assert "'identifier'" in err
        assert "str | int" in err
        assert "'float'" in err

    def test_strict_extras_error_format(self):
        """Strict extras error: [event_type] unexpected field 'field'."""
        errors = validate_event(
            "task.completed",
            {
                "task_id": "t-1",
                "project_id": "p-1",
                "title": "Done",
                "rogue_field": True,
            },
            strict_extras=True,
        )
        extra_errors = [e for e in errors if "unexpected" in e]
        assert len(extra_errors) == 1
        err = extra_errors[0]
        assert err.startswith("[task.completed]")
        assert "'rogue_field'" in err

    async def test_error_message_propagated_to_event_validation_error(self):
        """EventValidationError message includes the full formatted error."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError) as exc_info:
            await bus.emit(
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
            )
        msg = str(exc_info.value)
        assert "[test.typed_event]" in msg
        assert "project_id" in msg

    async def test_dev_error_message_contains_all_details(self):
        """EventValidationError raised in dev mode includes full context."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError) as exc_info:
            await bus.emit(
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
            )
        msg = str(exc_info.value)
        assert "test.typed_event" in msg
        assert "project_id" in msg
        assert "expected type" in msg
        assert "str" in msg
        assert "int" in msg

    async def test_dev_multiple_errors_semicolon_separated(self):
        """Multiple errors are joined with semicolons in the exception."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError) as exc_info:
            await bus.emit("task.completed", {})
        msg = str(exc_info.value)
        # Multiple errors joined by semicolons
        assert ";" in msg
        assert msg.count("missing required field") == 3

    async def test_prod_warning_contains_all_error_details(self, caplog):
        """Prod warning message includes full error context."""
        bus = _make_bus(env="production")
        bus.subscribe("test.typed_event", lambda d: None)
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit(
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
            )
        assert "test.typed_event" in caplog.text
        assert "project_id" in caplog.text
        assert "expected type" in caplog.text

    @pytest.mark.parametrize(
        ("event_type", "payload", "expected_fragments"),
        [
            (
                "task.completed",
                {},
                ["[task.completed]", "missing required field", "'task_id'"],
            ),
            (
                "plugin.loaded",
                {"version": "1.0"},
                ["[plugin.loaded]", "missing required field", "'plugin'"],
            ),
            (
                "config.reloaded",
                {},
                ["[config.reloaded]", "'changed_sections'", "'config'"],
            ),
            (
                "test.typed_event",
                {"project_id": 123, "task_id": "t-1", "count": 5},
                ["[test.typed_event]", "'project_id'", "expected type", "'str'", "'int'"],
            ),
        ],
        ids=["task-missing", "plugin-missing", "config-missing", "type-error"],
    )
    def test_error_message_fragments_parametrized(
        self, event_type: str, payload: dict, expected_fragments: list[str]
    ):
        """Error messages contain all expected fragments for debugging."""
        errors = validate_event(event_type, payload)
        assert errors, f"Expected validation errors for {event_type}"
        combined = "; ".join(errors)
        for fragment in expected_fragments:
            assert fragment in combined, (
                f"Expected fragment {fragment!r} not in error message: {combined}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Integration: end-to-end through EventBus
# ═══════════════════════════════════════════════════════════════════════════


class TestEndToEndEventBusIntegration:
    """Integration tests exercising the full emit → validate → deliver pipeline."""

    async def test_valid_event_delivered_to_handler(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
        )
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"
        assert received[0]["_event_type"] == "task.completed"

    async def test_event_type_injected_after_validation(self):
        """_event_type is added after validation, not before."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
        )
        assert "_event_type" in received[0]
        assert received[0]["_event_type"] == "task.completed"

    async def test_wildcard_handler_receives_validated_event(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("*", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
        )
        assert len(received) == 1
        assert received[0]["_event_type"] == "task.completed"

    async def test_validation_disabled_skips_all_checks(self):
        """With validate_events=False, even invalid payloads are delivered."""
        bus = _make_bus(env="dev", validate_events=False)
        received = []
        bus.subscribe("test.typed_event", lambda d: received.append(d))
        # Would raise in dev mode if validation were active
        await bus.emit("test.typed_event", {"project_id": 123})
        assert len(received) == 1

    async def test_staging_env_behaves_like_prod(self, caplog):
        """Non-dev environments (staging, etc.) behave like prod."""
        bus = _make_bus(env="staging")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert "missing required field" in caplog.text

    async def test_empty_env_string_behaves_like_prod(self, caplog):
        bus = _make_bus(env="")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert "missing required field" in caplog.text

    async def test_filter_still_works_with_validation(self):
        """Payload filtering works alongside validation."""
        bus = _make_bus(env="dev")
        matched = []
        unmatched = []
        bus.subscribe(
            "task.completed",
            lambda d: matched.append(d),
            filter={"project_id": "p-1"},
        )
        bus.subscribe(
            "task.completed",
            lambda d: unmatched.append(d),
            filter={"project_id": "p-other"},
        )
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Done"},
        )
        assert len(matched) == 1
        assert len(unmatched) == 0
