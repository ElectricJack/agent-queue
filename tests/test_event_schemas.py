"""Tests for the event schema registry."""

from __future__ import annotations

from src.event_schemas import (
    EVENT_SCHEMAS,
    TIMER_SCHEMA,
    get_schema,
    registered_event_types,
    validate_payload,
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
                assert len(fields) == len(set(fields)), f"{event_type}.{key} has duplicate fields"


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

    def test_git_events(self):
        expected = ["git.commit", "git.push", "git.pr.created"]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_playbook_events(self):
        expected = ["playbook.run.completed", "playbook.run.failed"]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"

    def test_human_events(self):
        assert "human.review.completed" in EVENT_SCHEMAS

    def test_workflow_events(self):
        assert "workflow.stage.completed" in EVENT_SCHEMAS

    def test_timer_events_common_intervals(self):
        for interval in ("1m", "5m", "15m", "30m", "1h", "4h", "12h", "24h"):
            et = f"timer.{interval}"
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"


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
        assert "git.commit" in types
        assert "playbook.run.completed" in types
        assert "timer.30m" in types


class TestGitEventSchemas:
    """Validate specific field requirements for git events."""

    def test_git_commit_required_fields(self):
        schema = EVENT_SCHEMAS["git.commit"]
        for field in ("commit_hash", "branch", "changed_files", "project_id"):
            assert field in schema["required"], f"git.commit missing required field: {field}"

    def test_git_commit_optional_fields(self):
        schema = EVENT_SCHEMAS["git.commit"]
        for field in ("message", "author", "agent_id"):
            assert field in schema["optional"], f"git.commit missing optional field: {field}"

    def test_git_push_required_fields(self):
        schema = EVENT_SCHEMAS["git.push"]
        for field in ("branch", "remote", "project_id"):
            assert field in schema["required"], f"git.push missing required field: {field}"

    def test_git_push_optional_fields(self):
        schema = EVENT_SCHEMAS["git.push"]
        assert "commit_range" in schema["optional"]

    def test_git_pr_created_required_fields(self):
        schema = EVENT_SCHEMAS["git.pr.created"]
        for field in ("pr_url", "branch", "title", "project_id"):
            assert field in schema["required"], f"git.pr.created missing required field: {field}"

    def test_git_pr_created_no_optional_fields(self):
        schema = EVENT_SCHEMAS["git.pr.created"]
        assert schema["optional"] == []


class TestPlaybookEventSchemas:
    """Validate playbook event schemas."""

    def test_run_completed_required_fields(self):
        schema = EVENT_SCHEMAS["playbook.run.completed"]
        assert "playbook_id" in schema["required"]
        assert "run_id" in schema["required"]

    def test_run_completed_optional_fields(self):
        schema = EVENT_SCHEMAS["playbook.run.completed"]
        assert "final_context" in schema["optional"]

    def test_run_failed_required_fields(self):
        schema = EVENT_SCHEMAS["playbook.run.failed"]
        for field in ("playbook_id", "run_id", "failed_at_node"):
            assert field in schema["required"]

    def test_run_failed_optional_error(self):
        schema = EVENT_SCHEMAS["playbook.run.failed"]
        assert "error" in schema["optional"]


class TestHumanEventSchemas:
    """Validate human interaction event schemas."""

    def test_review_completed_required_fields(self):
        schema = EVENT_SCHEMAS["human.review.completed"]
        for field in ("playbook_id", "run_id", "node_id", "decision"):
            assert field in schema["required"]

    def test_review_completed_optional_edits(self):
        schema = EVENT_SCHEMAS["human.review.completed"]
        assert "edits" in schema["optional"]


class TestWorkflowEventSchemas:
    """Validate workflow event schemas."""

    def test_stage_completed_required_fields(self):
        schema = EVENT_SCHEMAS["workflow.stage.completed"]
        assert "workflow_id" in schema["required"]
        assert "stage" in schema["required"]

    def test_stage_completed_optional_task_ids(self):
        schema = EVENT_SCHEMAS["workflow.stage.completed"]
        assert "task_ids" in schema["optional"]


class TestTimerEventSchemas:
    """Validate timer event schemas and wildcard fallback."""

    def test_common_timer_events_share_same_schema(self):
        """All registered timer events use the canonical TIMER_SCHEMA."""
        for event_type, schema in EVENT_SCHEMAS.items():
            if event_type.startswith("timer."):
                assert schema is TIMER_SCHEMA, f"{event_type} does not reference TIMER_SCHEMA"

    def test_timer_schema_requires_tick_time_and_interval(self):
        assert "tick_time" in TIMER_SCHEMA["required"]
        assert "interval" in TIMER_SCHEMA["required"]

    def test_timer_schema_no_optional_fields(self):
        assert TIMER_SCHEMA["optional"] == []

    def test_get_schema_returns_timer_schema_for_arbitrary_interval(self):
        """Arbitrary timer.* events not in the registry still resolve."""
        schema = get_schema("timer.7m")
        assert schema is TIMER_SCHEMA

    def test_get_schema_returns_timer_schema_for_registered_interval(self):
        schema = get_schema("timer.30m")
        assert schema is TIMER_SCHEMA

    def test_get_schema_does_not_return_timer_for_non_timer(self):
        """Non-timer unknown events should still return None."""
        assert get_schema("nonexistent.event") is None

    def test_validate_payload_works_for_arbitrary_timer(self):
        """validate_payload should work for any timer.* event via fallback."""
        errors = validate_payload(
            "timer.42s", {"tick_time": "2026-01-01T00:00:00Z", "interval": "42s"}
        )
        assert errors == []

    def test_validate_payload_catches_missing_timer_fields(self):
        errors = validate_payload("timer.10m", {"tick_time": "2026-01-01T00:00:00Z"})
        assert any("interval" in e for e in errors)
