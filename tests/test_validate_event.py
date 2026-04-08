"""Tests for validate_event() — Phase 0.2.2 event payload validation.

Covers:
- Required field checking with informative error messages
- Type validation when schemas specify a ``types`` mapping
- Graceful pass-through for unregistered event types
- Strict-extras mode for unexpected fields
- Meta-field exemption in strict mode
- Backward compatibility of validate_payload() wrapper
- Error message formatting (includes event type, field name, expected type)
"""

from __future__ import annotations

import pytest

from src.event_schemas import (
    EVENT_SCHEMAS,
    META_FIELDS,
    EventSchema,
    validate_event,
    validate_payload,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A minimal schema with types for testing type validation
_TEST_SCHEMA_WITH_TYPES: EventSchema = {
    "required": ["name", "count"],
    "optional": ["label"],
    "types": {"name": str, "count": int, "label": str},
}

# Schema with tuple-of-types for union validation
_TEST_SCHEMA_UNION_TYPES: EventSchema = {
    "required": ["value"],
    "optional": [],
    "types": {"value": (str, int)},
}


@pytest.fixture(autouse=True)
def _register_test_schemas(monkeypatch):
    """Temporarily register test schemas in EVENT_SCHEMAS."""
    monkeypatch.setitem(EVENT_SCHEMAS, "test.typed", _TEST_SCHEMA_WITH_TYPES)
    monkeypatch.setitem(EVENT_SCHEMAS, "test.union", _TEST_SCHEMA_UNION_TYPES)


# ---------------------------------------------------------------------------
# Core validation: required fields
# ---------------------------------------------------------------------------


class TestRequiredFields:
    """validate_event checks that all required fields are present."""

    def test_all_required_present_returns_empty(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t1", "project_id": "p1", "title": "ok"},
        )
        assert errors == []

    def test_single_missing_required_field(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t1", "project_id": "p1"},
        )
        assert len(errors) == 1
        assert "title" in errors[0]

    def test_multiple_missing_required_fields(self):
        errors = validate_event("task.completed", {"task_id": "t1"})
        assert len(errors) == 2
        missing_fields = {e.split("'")[1] for e in errors}
        assert missing_fields == {"project_id", "title"}

    def test_all_required_missing(self):
        errors = validate_event("task.completed", {})
        assert len(errors) == 3

    def test_optional_fields_not_required(self):
        # task.started has optional agent_id — omitting it should be fine
        errors = validate_event(
            "task.started",
            {"task_id": "t1", "project_id": "p1", "title": "ok"},
        )
        assert errors == []

    def test_extra_fields_allowed_by_default(self):
        errors = validate_event(
            "task.completed",
            {
                "task_id": "t1",
                "project_id": "p1",
                "title": "ok",
                "bonus_field": True,
            },
        )
        assert errors == []


# ---------------------------------------------------------------------------
# Unregistered event types — graceful degradation
# ---------------------------------------------------------------------------


class TestUnregisteredEvents:
    """Unregistered event types pass through without validation."""

    def test_unknown_event_returns_empty(self):
        errors = validate_event("completely.unknown.event", {})
        assert errors == []

    def test_unknown_event_with_arbitrary_payload(self):
        errors = validate_event("no.such.event", {"x": 1, "y": "z"})
        assert errors == []

    def test_empty_event_type_string(self):
        errors = validate_event("", {"data": 42})
        assert errors == []


# ---------------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------------


class TestTypeValidation:
    """validate_event checks field types when the schema includes a ``types`` mapping."""

    def test_correct_types_returns_empty(self):
        errors = validate_event(
            "test.typed",
            {"name": "Alice", "count": 5},
        )
        assert errors == []

    def test_wrong_type_for_required_field(self):
        errors = validate_event(
            "test.typed",
            {"name": "Alice", "count": "not-a-number"},
        )
        assert len(errors) == 1
        assert "count" in errors[0]
        assert "int" in errors[0]
        assert "str" in errors[0]

    def test_wrong_type_for_multiple_fields(self):
        errors = validate_event(
            "test.typed",
            {"name": 123, "count": "bad"},
        )
        type_errors = [e for e in errors if "expected type" in e]
        assert len(type_errors) == 2

    def test_type_check_on_optional_field(self):
        errors = validate_event(
            "test.typed",
            {"name": "Alice", "count": 5, "label": 999},
        )
        assert len(errors) == 1
        assert "label" in errors[0]
        assert "str" in errors[0]

    def test_missing_field_not_type_checked(self):
        """Missing required fields get a 'missing' error, not a type error."""
        errors = validate_event(
            "test.typed",
            {"name": "Alice"},  # count missing
        )
        assert len(errors) == 1
        assert "missing" in errors[0]
        assert "count" in errors[0]

    def test_absent_optional_field_not_type_checked(self):
        """Absent optional fields are silently ignored for type checks."""
        errors = validate_event(
            "test.typed",
            {"name": "Alice", "count": 5},  # label absent — no error
        )
        assert errors == []

    def test_none_value_fails_type_check(self):
        errors = validate_event(
            "test.typed",
            {"name": None, "count": 5},
        )
        assert len(errors) == 1
        assert "NoneType" in errors[0]

    def test_union_types_accept_either(self):
        """A tuple-of-types in the schema accepts any matching type."""
        assert validate_event("test.union", {"value": "hello"}) == []
        assert validate_event("test.union", {"value": 42}) == []

    def test_union_types_reject_mismatch(self):
        errors = validate_event("test.union", {"value": [1, 2, 3]})
        assert len(errors) == 1
        assert "str | int" in errors[0]
        assert "list" in errors[0]

    def test_schema_without_types_skips_type_checking(self):
        """Schemas that don't include a 'types' key skip type validation entirely."""
        # task.completed has no types mapping
        errors = validate_event(
            "task.completed",
            {"task_id": 999, "project_id": 888, "title": 777},
        )
        # Only required-field check applies; all fields are present
        assert errors == []

    def test_bool_is_instance_of_int(self):
        """Python's bool is a subclass of int — isinstance(True, int) is True.
        This is expected behavior, not a bug."""
        errors = validate_event(
            "test.typed",
            {"name": "Alice", "count": True},
        )
        # True passes isinstance(True, int) — no error expected
        assert errors == []


# ---------------------------------------------------------------------------
# Error message formatting
# ---------------------------------------------------------------------------


class TestErrorMessages:
    """Error messages include event type, field name, and expected type."""

    def test_missing_field_error_includes_event_type(self):
        errors = validate_event("task.completed", {})
        for err in errors:
            assert err.startswith("[task.completed]"), f"Error missing event type prefix: {err}"

    def test_missing_field_error_includes_field_name(self):
        errors = validate_event("task.completed", {"task_id": "t1"})
        assert any("project_id" in e for e in errors)
        assert any("title" in e for e in errors)

    def test_type_error_includes_event_type(self):
        errors = validate_event(
            "test.typed",
            {"name": 123, "count": 5},
        )
        assert errors[0].startswith("[test.typed]")

    def test_type_error_includes_field_name(self):
        errors = validate_event(
            "test.typed",
            {"name": 123, "count": 5},
        )
        assert "'name'" in errors[0]

    def test_type_error_includes_expected_type(self):
        errors = validate_event(
            "test.typed",
            {"name": 123, "count": 5},
        )
        assert "'str'" in errors[0]

    def test_type_error_includes_actual_type(self):
        errors = validate_event(
            "test.typed",
            {"name": 123, "count": 5},
        )
        assert "'int'" in errors[0]

    def test_strict_extras_error_includes_event_type(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t", "project_id": "p", "title": "t", "rogue": True},
            strict_extras=True,
        )
        extra_errors = [e for e in errors if "unexpected" in e]
        assert len(extra_errors) == 1
        assert extra_errors[0].startswith("[task.completed]")

    def test_union_type_error_shows_all_accepted_types(self):
        errors = validate_event("test.union", {"value": 3.14})
        assert "str | int" in errors[0]


# ---------------------------------------------------------------------------
# Strict extras mode
# ---------------------------------------------------------------------------


class TestStrictExtras:
    """strict_extras=True rejects fields not in required + optional + META_FIELDS."""

    def test_strict_extras_rejects_unknown_field(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t", "project_id": "p", "title": "t", "rogue": True},
            strict_extras=True,
        )
        assert any("rogue" in e for e in errors)

    def test_strict_extras_allows_optional_fields(self):
        errors = validate_event(
            "task.started",
            {"task_id": "t", "project_id": "p", "title": "t", "agent_id": "a1"},
            strict_extras=True,
        )
        assert errors == []

    def test_strict_extras_allows_meta_fields(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t", "project_id": "p", "title": "t", "_plugin": "my-plugin"},
            strict_extras=True,
        )
        assert errors == []

    def test_strict_extras_off_by_default(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t", "project_id": "p", "title": "t", "rogue": True},
        )
        assert errors == []


# ---------------------------------------------------------------------------
# Meta fields
# ---------------------------------------------------------------------------


class TestMetaFieldsExemption:
    """META_FIELDS are always allowed and never cause validation errors."""

    def test_all_meta_fields_pass_strict_check(self):
        base_payload = {"task_id": "t", "project_id": "p", "title": "t"}
        for meta_field in META_FIELDS:
            payload = {**base_payload, meta_field: "value"}
            errors = validate_event("task.completed", payload, strict_extras=True)
            assert errors == [], f"META_FIELD {meta_field!r} was rejected"


# ---------------------------------------------------------------------------
# Combined error scenarios
# ---------------------------------------------------------------------------


class TestCombinedErrors:
    """Multiple kinds of errors can appear together."""

    def test_missing_and_type_errors_together(self):
        # count is missing (required), name has wrong type
        errors = validate_event(
            "test.typed",
            {"name": 123},
        )
        assert len(errors) == 2
        missing = [e for e in errors if "missing" in e]
        type_errors = [e for e in errors if "expected type" in e]
        assert len(missing) == 1
        assert len(type_errors) == 1
        assert "count" in missing[0]
        assert "name" in type_errors[0]

    def test_missing_type_and_strict_extras_together(self):
        errors = validate_event(
            "test.typed",
            {"name": 123, "rogue": True},
            strict_extras=True,
        )
        missing = [e for e in errors if "missing" in e]
        type_errors = [e for e in errors if "expected type" in e]
        extra_errors = [e for e in errors if "unexpected" in e]
        assert len(missing) == 1  # count missing
        assert len(type_errors) == 1  # name wrong type
        assert len(extra_errors) == 1  # rogue unexpected


# ---------------------------------------------------------------------------
# Real schema validation (smoke tests against actual EVENT_SCHEMAS)
# ---------------------------------------------------------------------------


class TestRealSchemas:
    """Smoke tests using actual registered schemas."""

    def test_valid_task_completed(self):
        errors = validate_event(
            "task.completed",
            {"task_id": "t1", "project_id": "p1", "title": "Done"},
        )
        assert errors == []

    def test_valid_notify_text(self):
        errors = validate_event(
            "notify.text",
            {"event_type": "notify.text", "severity": "info", "category": "system"},
        )
        assert errors == []

    def test_valid_plugin_loaded(self):
        errors = validate_event(
            "plugin.loaded",
            {"plugin": "my-plugin", "version": "1.0.0"},
        )
        assert errors == []

    def test_invalid_task_started_missing_fields(self):
        errors = validate_event("task.started", {"task_id": "t1"})
        assert len(errors) >= 2  # project_id and title missing
        assert all(e.startswith("[task.started]") for e in errors)

    def test_chat_message_all_required(self):
        errors = validate_event(
            "chat.message",
            {
                "channel_id": "c1",
                "project_id": "p1",
                "author": "user",
                "content": "hello",
                "timestamp": "2025-01-01T00:00:00Z",
                "is_bot": False,
            },
        )
        assert errors == []


# ---------------------------------------------------------------------------
# Backward compatibility: validate_payload wrapper
# ---------------------------------------------------------------------------


class TestValidatePayloadBackwardCompat:
    """validate_payload delegates to validate_event."""

    def test_validate_payload_returns_same_as_validate_event(self):
        payload = {"task_id": "t1"}
        errors_new = validate_event("task.completed", payload)
        errors_old = validate_payload("task.completed", payload)
        assert errors_new == errors_old

    def test_validate_payload_passes_strict_extras(self):
        payload = {
            "task_id": "t",
            "project_id": "p",
            "title": "t",
            "rogue": True,
        }
        errors = validate_payload("task.completed", payload, strict_extras=True)
        assert any("rogue" in e for e in errors)

    def test_validate_payload_unregistered_event(self):
        errors = validate_payload("unknown.event", {"anything": True})
        assert errors == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_payload_against_schema_with_no_required(self):
        """A schema with no required fields should validate an empty payload."""
        # Temporarily register a schema with no required fields
        EVENT_SCHEMAS["test.empty_req"] = {
            "required": [],
            "optional": ["maybe"],
        }
        try:
            errors = validate_event("test.empty_req", {})
            assert errors == []
        finally:
            del EVENT_SCHEMAS["test.empty_req"]

    def test_payload_with_none_values_counts_as_present(self):
        """A field set to None is considered present (not missing)."""
        errors = validate_event(
            "task.completed",
            {"task_id": None, "project_id": None, "title": None},
        )
        # No missing-field errors — all keys are present
        assert errors == []

    def test_deeply_nested_payload_values_accepted(self):
        """validate_event doesn't inspect nested structures — only top-level keys."""
        errors = validate_event(
            "task.completed",
            {
                "task_id": {"nested": {"deep": True}},
                "project_id": [1, [2, [3]]],
                "title": "ok",
            },
        )
        assert errors == []

    def test_type_check_with_subclass(self):
        """isinstance checks accept subclasses."""
        # dict is a subclass of... dict. But let's test with a custom subclass
        EVENT_SCHEMAS["test.subclass"] = {
            "required": ["data"],
            "optional": [],
            "types": {"data": dict},
        }
        try:
            from collections import OrderedDict

            errors = validate_event("test.subclass", {"data": OrderedDict()})
            assert errors == []
        finally:
            del EVENT_SCHEMAS["test.subclass"]
