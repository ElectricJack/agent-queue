"""Tests for the event schema registry."""

from __future__ import annotations

from src.event_schemas import (
    EVENT_SCHEMAS,
    get_schema,
    registered_event_types,
)


class TestEventSchemasStructure:
    """Validate the structure and completeness of EVENT_SCHEMAS."""

    def test_registry_is_nonempty(self):
        assert len(EVENT_SCHEMAS) > 0

    def test_all_entries_have_required_and_optional(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            assert "required" in schema, f"{event_type} missing 'required'"
            assert "optional" in schema, f"{event_type} missing 'optional'"

    def test_required_and_optional_are_lists_of_strings(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            for key in ("required", "optional"):
                fields = schema[key]
                assert isinstance(fields, list), f"{event_type}.{key} is not a list"
                for field in fields:
                    assert isinstance(field, str), (
                        f"{event_type}.{key} contains non-string: {field!r}"
                    )

    def test_no_overlap_between_required_and_optional(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            overlap = set(schema["required"]) & set(schema["optional"])
            assert not overlap, f"{event_type} has fields in both required and optional: {overlap}"

    def test_no_duplicate_fields_within_lists(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            for key in ("required", "optional"):
                fields = schema[key]
                assert len(fields) == len(set(fields)), (
                    f"{event_type}.{key} has duplicate fields"
                )


class TestExpectedEventTypes:
    """Verify that all known event types from the codebase are registered."""

    def test_task_lifecycle_events(self):
        expected = [
            "task.started",
            "task.completed",
            "task.failed",
            "task.paused",
            "task.waiting_input",
        ]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_note_events(self):
        assert "note.created" in EVENT_SCHEMAS
        assert "facts.extracted" in EVENT_SCHEMAS

    def test_file_events(self):
        assert "file.changed" in EVENT_SCHEMAS
        assert "folder.changed" in EVENT_SCHEMAS

    def test_plugin_events(self):
        expected = [
            "plugin.loaded",
            "plugin.unloaded",
            "plugin.installed",
            "plugin.updated",
            "plugin.removed",
        ]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_config_events(self):
        assert "config.reloaded" in EVENT_SCHEMAS
        assert "config.restart_needed" in EVENT_SCHEMAS

    def test_notify_events(self):
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

    def test_chat_events(self):
        assert "chat.message" in EVENT_SCHEMAS


class TestTaskEventSchemas:
    """Validate specific field requirements for task events."""

    def test_task_started_requires_base_fields(self):
        schema = EVENT_SCHEMAS["task.started"]
        assert "task_id" in schema["required"]
        assert "project_id" in schema["required"]
        assert "title" in schema["required"]

    def test_task_completed_requires_base_fields(self):
        schema = EVENT_SCHEMAS["task.completed"]
        assert "task_id" in schema["required"]
        assert "project_id" in schema["required"]

    def test_task_failed_includes_context(self):
        schema = EVENT_SCHEMAS["task.failed"]
        assert "status" in schema["required"]
        assert "context" in schema["required"]
        assert "error" in schema["optional"]

    def test_task_paused_includes_reason(self):
        schema = EVENT_SCHEMAS["task.paused"]
        assert "reason" in schema["required"]
        assert "resume_after" in schema["optional"]

    def test_task_waiting_input_includes_question(self):
        schema = EVENT_SCHEMAS["task.waiting_input"]
        assert "question" in schema["required"]


class TestFileEventSchemas:
    """Validate file/folder watch event schemas."""

    def test_file_changed_fields(self):
        schema = EVENT_SCHEMAS["file.changed"]
        assert "path" in schema["required"]
        assert "relative_path" in schema["required"]
        assert "project_id" in schema["required"]
        assert "operation" in schema["required"]
        assert "watch_id" in schema["optional"]

    def test_folder_changed_fields(self):
        schema = EVENT_SCHEMAS["folder.changed"]
        assert "path" in schema["required"]
        assert "project_id" in schema["required"]
        assert "changes" in schema["required"]
        assert "count" in schema["required"]


class TestNotifyEventSchemas:
    """Validate that notify events carry the base fields."""

    def test_all_notify_events_require_base_fields(self):
        base_required = {"event_type", "severity", "category"}
        for event_type, schema in EVENT_SCHEMAS.items():
            if event_type.startswith("notify."):
                assert base_required.issubset(set(schema["required"])), (
                    f"{event_type} missing notify base fields: "
                    f"{base_required - set(schema['required'])}"
                )

    def test_all_notify_events_have_optional_project_id(self):
        for event_type, schema in EVENT_SCHEMAS.items():
            if event_type.startswith("notify."):
                assert "project_id" in schema["optional"], (
                    f"{event_type} missing optional project_id"
                )


class TestHelperFunctions:
    """Test the convenience functions."""

    def test_get_schema_returns_schema(self):
        schema = get_schema("task.completed")
        assert schema is not None
        assert "required" in schema

    def test_get_schema_returns_none_for_unknown(self):
        assert get_schema("nonexistent.event") is None

    def test_registered_event_types_returns_sorted_list(self):
        types = registered_event_types()
        assert isinstance(types, list)
        assert types == sorted(types)
        assert len(types) == len(EVENT_SCHEMAS)

    def test_registered_event_types_contains_known_types(self):
        types = registered_event_types()
        assert "task.completed" in types
        assert "notify.text" in types
        assert "plugin.loaded" in types
