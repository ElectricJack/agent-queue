"""Verify that all bus.emit() calls in the codebase provide required schema fields.

This test module statically analyzes the source code to extract emit() call
sites, then checks each one against the EVENT_SCHEMAS registry to ensure no
required fields are missing.  This catches regressions where a new emit() call
is added without including all fields required by the schema.

For emit() calls whose payloads are built dynamically (e.g. via Pydantic
``model_dump()`` or helper functions like ``_emit_task_event``), we verify
the *schemas* themselves are consistent with the models rather than trying
to trace runtime data flow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.event_schemas import (
    EVENT_SCHEMAS,
    META_FIELDS,
    validate_payload,
)

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Test: validate_payload helper
# ---------------------------------------------------------------------------


class TestValidatePayload:
    """Unit tests for the validate_payload() helper."""

    def test_valid_payload_returns_empty(self):
        errors = validate_payload(
            "task.completed",
            {
                "task_id": "t1",
                "project_id": "p1",
                "title": "My task",
            },
        )
        assert errors == []

    def test_missing_required_field(self):
        errors = validate_payload("task.completed", {"task_id": "t1"})
        assert any("project_id" in e for e in errors)
        assert any("title" in e for e in errors)

    def test_unregistered_event_type_is_valid(self):
        errors = validate_payload("nonexistent.event", {"anything": True})
        assert errors == []

    def test_extra_fields_allowed_by_default(self):
        errors = validate_payload(
            "task.completed",
            {
                "task_id": "t1",
                "project_id": "p1",
                "title": "My task",
                "extra_field": "value",
            },
        )
        assert errors == []

    def test_extra_fields_rejected_in_strict_mode(self):
        errors = validate_payload(
            "task.completed",
            {
                "task_id": "t1",
                "project_id": "p1",
                "title": "My task",
                "extra_field": "value",
            },
            strict_extras=True,
        )
        assert any("extra_field" in e for e in errors)

    def test_meta_fields_allowed_in_strict_mode(self):
        errors = validate_payload(
            "task.completed",
            {
                "task_id": "t1",
                "project_id": "p1",
                "title": "My task",
                "_plugin": "my-plugin",
            },
            strict_extras=True,
        )
        assert errors == []

    def test_optional_fields_are_allowed(self):
        errors = validate_payload(
            "task.started",
            {
                "task_id": "t1",
                "project_id": "p1",
                "title": "My task",
                "agent_id": "a1",
            },
            strict_extras=True,
        )
        assert errors == []


# ---------------------------------------------------------------------------
# Test: META_FIELDS
# ---------------------------------------------------------------------------


class TestMetaFields:
    """Validate META_FIELDS is properly defined."""

    def test_meta_fields_is_frozenset(self):
        assert isinstance(META_FIELDS, frozenset)

    def test_plugin_in_meta_fields(self):
        assert "_plugin" in META_FIELDS


# ---------------------------------------------------------------------------
# Test: hooks.py notify.text emits include required fields
# ---------------------------------------------------------------------------


class TestHooksNotifyTextEmits:
    """Verify hooks.py notify.text emits include severity and category.

    This is a regression test — hooks.py previously emitted notify.text
    payloads without the required ``severity`` and ``category`` fields.
    """

    def test_hooks_notify_text_payloads_have_required_fields(self):
        """Parse hooks.py and verify all notify.text dict literals include
        severity and category."""
        hooks_path = SRC_DIR / "hooks.py"
        source = hooks_path.read_text()

        # Find all notify.text emit blocks — they use raw dict literals
        # Pattern: emit("notify.text", { ... })
        # We extract the dict content between the braces
        pattern = re.compile(
            r'emit\(\s*"notify\.text"\s*,\s*\{([^}]+)\}',
            re.DOTALL,
        )
        matches = list(pattern.finditer(source))
        assert len(matches) > 0, "Expected to find notify.text emits in hooks.py"

        schema = EVENT_SCHEMAS["notify.text"]
        for match in matches:
            dict_content = match.group(1)
            # Extract all string keys from the dict literal
            keys = set(re.findall(r'"(\w+)"', dict_content))
            for req in schema["required"]:
                assert req in keys, (
                    f"hooks.py notify.text emit near offset {match.start()} "
                    f"is missing required field '{req}': found keys {keys}"
                )


# ---------------------------------------------------------------------------
# Test: Pydantic notification models satisfy schema requirements
# ---------------------------------------------------------------------------


class TestNotifyModelsMatchSchemas:
    """Verify that every Pydantic NotifyEvent subclass carries the fields
    declared in its EVENT_SCHEMAS entry.

    The orchestrator emits notify.* events via ``event.model_dump()``, so the
    Pydantic model *is* the payload.  If the model has defaults for all
    required fields, the schema is satisfied.
    """

    def test_notify_event_models_have_all_required_fields(self):
        """Each NotifyEvent subclass must define (or inherit) every field
        listed in the schema's ``required`` list."""
        from src.notifications.events import NotifyEvent

        # Collect all concrete NotifyEvent subclasses
        subclasses: list[type] = []
        queue = list(NotifyEvent.__subclasses__())
        while queue:
            cls = queue.pop()
            subclasses.append(cls)
            queue.extend(cls.__subclasses__())

        for cls in subclasses:
            # Get the event_type from the model's default
            model_fields = cls.model_fields
            et_field = model_fields.get("event_type")
            if et_field is None or et_field.default is None:
                continue  # abstract or no default event_type

            event_type = et_field.default
            schema = EVENT_SCHEMAS.get(event_type)
            if schema is None:
                continue  # no schema for this event type yet

            # model_dump() with all defaults should produce all required fields
            # Build a minimal instance using only defaults where possible
            all_field_names = set(model_fields.keys())
            for req in schema["required"]:
                assert req in all_field_names, (
                    f"{cls.__name__} (event_type={event_type!r}) is missing "
                    f"model field '{req}' which is required by EVENT_SCHEMAS"
                )


# ---------------------------------------------------------------------------
# Test: _emit_task_event helper builds correct payloads
# ---------------------------------------------------------------------------


class TestTaskEventPayloads:
    """Verify that _emit_task_event always provides the base required fields."""

    def test_emit_task_event_helper_provides_base_fields(self):
        """The _emit_task_event helper in orchestrator.py builds
        {task_id, project_id, title, **extra}.  Verify the schema
        for every task.* event includes those as required."""
        base_fields = {"task_id", "project_id", "title"}
        for event_type in EVENT_SCHEMAS:
            if event_type.startswith("task."):
                schema = EVENT_SCHEMAS[event_type]
                assert base_fields.issubset(set(schema["required"])), (
                    f"{event_type} schema required fields {schema['required']} "
                    f"don't include all task base fields {base_fields}"
                )


# ---------------------------------------------------------------------------
# Test: config event emits
# ---------------------------------------------------------------------------


class TestConfigEventEmits:
    """Verify config.py emit calls provide required fields."""

    def test_config_reloaded_emit_has_required_fields(self):
        """config.py emits config.reloaded with changed_sections and config."""
        config_path = SRC_DIR / "config.py"
        source = config_path.read_text()
        schema = EVENT_SCHEMAS["config.reloaded"]

        # Find the config.reloaded emit
        match = re.search(
            r'emit\(\s*"config\.reloaded"\s*,\s*\{([^}]+)\}',
            source,
            re.DOTALL,
        )
        assert match is not None, "config.reloaded emit not found in config.py"
        keys = set(re.findall(r'"(\w+)"', match.group(1)))
        for req in schema["required"]:
            assert req in keys, f"config.py config.reloaded emit missing required field '{req}'"

    def test_config_restart_needed_emit_has_required_fields(self):
        """config.py emits config.restart_needed with changed_sections."""
        config_path = SRC_DIR / "config.py"
        source = config_path.read_text()
        schema = EVENT_SCHEMAS["config.restart_needed"]

        match = re.search(
            r'emit\(\s*"config\.restart_needed"\s*,\s*\{([^}]+)\}',
            source,
            re.DOTALL,
        )
        assert match is not None, "config.restart_needed emit not found"
        keys = set(re.findall(r'"(\w+)"', match.group(1)))
        for req in schema["required"]:
            assert req in keys, (
                f"config.py config.restart_needed emit missing required field '{req}'"
            )


# ---------------------------------------------------------------------------
# Test: plugin registry emit calls
# ---------------------------------------------------------------------------


class TestPluginRegistryEmits:
    """Verify plugin registry emit calls provide required fields."""

    @pytest.mark.parametrize(
        "event_type",
        [
            "plugin.loaded",
            "plugin.unloaded",
            "plugin.installed",
            "plugin.updated",
            "plugin.removed",
            "plugin.auto_disabled",
            "plugin.reload_failed",
        ],
    )
    def test_plugin_event_schemas_require_plugin_name(self, event_type: str):
        """All plugin.* events must require the 'plugin' field."""
        schema = EVENT_SCHEMAS[event_type]
        assert "plugin" in schema["required"], f"{event_type} schema doesn't require 'plugin'"


# ---------------------------------------------------------------------------
# Test: file/folder watcher emits
# ---------------------------------------------------------------------------


class TestFileWatcherEmits:
    """Verify file_watcher.py emit calls provide required fields."""

    def test_file_changed_emit_has_required_fields(self):
        fw_path = SRC_DIR / "file_watcher.py"
        source = fw_path.read_text()
        schema = EVENT_SCHEMAS["file.changed"]

        match = re.search(
            r'emit\(\s*"file\.changed"\s*,\s*\{([^}]+)\}',
            source,
            re.DOTALL,
        )
        assert match is not None, "file.changed emit not found"
        keys = set(re.findall(r'"(\w+)"', match.group(1)))
        for req in schema["required"]:
            assert req in keys, f"file_watcher.py file.changed emit missing required field '{req}'"

    def test_folder_changed_emit_has_required_fields(self):
        fw_path = SRC_DIR / "file_watcher.py"
        source = fw_path.read_text()
        schema = EVENT_SCHEMAS["folder.changed"]

        # The folder.changed payload contains nested dicts (changes list),
        # so we grab a larger block around the emit and look for top-level keys.
        match = re.search(
            r'emit\(\s*"folder\.changed"\s*,\s*(\{.+?\},)\s*\)',
            source,
            re.DOTALL,
        )
        assert match is not None, "folder.changed emit not found"
        block = match.group(1)
        # Extract top-level string keys (those preceded by newline + whitespace
        # and followed by a colon, which are the dict's top-level keys).
        keys = set(re.findall(r'"(\w+)"\s*:', block))
        for req in schema["required"]:
            assert req in keys, (
                f"file_watcher.py folder.changed emit missing required field '{req}'"
            )


# ---------------------------------------------------------------------------
# Test: chat.message emit
# ---------------------------------------------------------------------------


class TestChatMessageEmit:
    """Verify discord/bot.py chat.message emit has all required fields."""

    def test_chat_message_emit_has_required_fields(self):
        bot_path = SRC_DIR / "discord" / "bot.py"
        source = bot_path.read_text()
        schema = EVENT_SCHEMAS["chat.message"]

        match = re.search(
            r'emit\(\s*"chat\.message"\s*,\s*\{([^}]+)\}',
            source,
            re.DOTALL,
        )
        assert match is not None, "chat.message emit not found in bot.py"
        keys = set(re.findall(r'"(\w+)"', match.group(1)))
        for req in schema["required"]:
            assert req in keys, f"bot.py chat.message emit missing required field '{req}'"


# ---------------------------------------------------------------------------
# Test: all schemas have the fields required by the task spec
# ---------------------------------------------------------------------------


class TestSpecRequirements:
    """Verify schemas match the requirements from the task spec:

    - task.* require task_id, project_id
    - note.created requires project_id, note_path
    - file.changed requires path, project_id, operation
    - folder.changed requires path, project_id, changes
    - plugin.* requires plugin name
    - config.reloaded requires changed_sections
    """

    def test_task_events_require_task_id_and_project_id(self):
        for et in [
            "task.started",
            "task.completed",
            "task.failed",
            "task.paused",
            "task.waiting_input",
        ]:
            schema = EVENT_SCHEMAS[et]
            assert "task_id" in schema["required"], f"{et} missing task_id"
            assert "project_id" in schema["required"], f"{et} missing project_id"

    def test_note_created_requires_project_id_and_note_path(self):
        schema = EVENT_SCHEMAS["note.created"]
        assert "project_id" in schema["required"]
        assert "note_path" in schema["required"]

    def test_file_changed_requires_path_project_id_operation(self):
        schema = EVENT_SCHEMAS["file.changed"]
        assert "path" in schema["required"]
        assert "project_id" in schema["required"]
        assert "operation" in schema["required"]

    def test_folder_changed_requires_path_project_id_changes(self):
        schema = EVENT_SCHEMAS["folder.changed"]
        assert "path" in schema["required"]
        assert "project_id" in schema["required"]
        assert "changes" in schema["required"]

    def test_plugin_events_require_plugin_name(self):
        for et in EVENT_SCHEMAS:
            if et.startswith("plugin."):
                schema = EVENT_SCHEMAS[et]
                assert "plugin" in schema["required"], f"{et} missing plugin"

    def test_config_reloaded_requires_changed_sections(self):
        schema = EVENT_SCHEMAS["config.reloaded"]
        assert "changed_sections" in schema["required"]
