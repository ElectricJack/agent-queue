"""Tests for EventBus emit-time payload validation (Phase 0.2.3).

Covers:
- Dev mode: raises EventValidationError on schema violations
- Prod mode: logs warning but still delivers the event
- Validation disabled: no validation at all
- Valid payloads: no errors in any mode
- Unregistered events: pass through without validation
- _event_type meta-field injected by emit() is excluded from validation
"""

import logging

import pytest

from src.event_bus import EventBus, EventValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus(*, env="production", validate_events=True):
    return EventBus(env=env, validate_events=validate_events)


# ---------------------------------------------------------------------------
# Dev mode — raises on validation failure
# ---------------------------------------------------------------------------


class TestDevModeValidation:
    """In dev mode, emit() raises EventValidationError for invalid payloads."""

    async def test_missing_required_field_raises(self):
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError, match="missing required field 'project_id'"):
            await bus.emit("task.completed", {"task_id": "t-1", "title": "Do stuff"})

    async def test_multiple_missing_fields_raises(self):
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError) as exc_info:
            await bus.emit("task.completed", {})
        msg = str(exc_info.value)
        assert "task_id" in msg
        assert "project_id" in msg
        assert "title" in msg

    async def test_valid_payload_no_error(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Do stuff"},
        )
        assert len(received) == 1

    async def test_valid_payload_with_optional_fields(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.started", lambda d: received.append(d))
        await bus.emit(
            "task.started",
            {"task_id": "t-1", "project_id": "p-1", "title": "Do stuff", "agent_id": "a-1"},
        )
        assert len(received) == 1

    async def test_handler_not_called_on_validation_error(self):
        """When validation raises, handlers must NOT be invoked."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with pytest.raises(EventValidationError):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert received == []


# ---------------------------------------------------------------------------
# Prod mode — logs warning but delivers the event
# ---------------------------------------------------------------------------


class TestProdModeValidation:
    """In prod mode, emit() logs a warning but still delivers the event."""

    async def test_invalid_payload_logs_warning(self, caplog):
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert "missing required field" in caplog.text

    async def test_valid_payload_no_warning(self, caplog):
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit(
                "task.completed",
                {"task_id": "t-1", "project_id": "p-1", "title": "Do stuff"},
            )
        assert len(received) == 1
        assert "validation" not in caplog.text.lower()

    async def test_event_still_delivered_on_warning(self):
        """Handlers fire even when the payload is invalid."""
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"

    async def test_multiple_errors_all_in_warning(self, caplog):
        bus = _make_bus(env="production")
        bus.subscribe("task.completed", lambda d: None)
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {})
        assert "task_id" in caplog.text
        assert "project_id" in caplog.text
        assert "title" in caplog.text


# ---------------------------------------------------------------------------
# Validation disabled
# ---------------------------------------------------------------------------


class TestValidationDisabled:
    """When validate_events=False, no validation is performed."""

    async def test_invalid_payload_no_error_dev(self):
        bus = _make_bus(env="dev", validate_events=False)
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        # Would raise in dev mode if validation were active
        await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1

    async def test_invalid_payload_no_warning_prod(self, caplog):
        bus = _make_bus(env="production", validate_events=False)
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert "validation" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# Unregistered events — pass through without validation
# ---------------------------------------------------------------------------


class TestUnregisteredEvents:
    """Events with no schema in the registry should pass through silently."""

    async def test_unregistered_event_dev_no_error(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("custom.unknown_event", lambda d: received.append(d))
        await bus.emit("custom.unknown_event", {"any": "payload"})
        assert len(received) == 1

    async def test_unregistered_event_prod_no_warning(self, caplog):
        bus = _make_bus(env="production")
        received = []
        bus.subscribe("custom.unknown_event", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("custom.unknown_event", {"any": "payload"})
        assert len(received) == 1
        assert caplog.text == ""


# ---------------------------------------------------------------------------
# Meta-field handling — _event_type added after validation
# ---------------------------------------------------------------------------


class TestMetaFieldHandling:
    """_event_type is injected by emit() AFTER validation, so it should not
    interfere with strict-extras or required-field checks."""

    async def test_event_type_injected_after_validation(self):
        """The _event_type meta-field should be present in the delivered data
        but should not trigger validation issues."""
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        await bus.emit(
            "task.completed",
            {"task_id": "t-1", "project_id": "p-1", "title": "Do stuff"},
        )
        assert received[0]["_event_type"] == "task.completed"


# ---------------------------------------------------------------------------
# Non-dev env variants (staging, etc.) — treated like prod
# ---------------------------------------------------------------------------


class TestNonDevEnvs:
    """Any env that is not 'dev' should log warnings instead of raising."""

    async def test_staging_logs_warning(self, caplog):
        bus = _make_bus(env="staging")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert "missing required field" in caplog.text

    async def test_empty_env_string_logs_warning(self, caplog):
        bus = _make_bus(env="")
        received = []
        bus.subscribe("task.completed", lambda d: received.append(d))
        with caplog.at_level(logging.WARNING, logger="src.event_bus"):
            await bus.emit("task.completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert "missing required field" in caplog.text


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------


class TestConstructorDefaults:
    """EventBus() with no args should use production env + validation enabled."""

    async def test_default_env_is_production(self):
        bus = EventBus()
        assert bus._env == "production"

    async def test_default_validation_enabled(self):
        bus = EventBus()
        assert bus._validate_events is True

    async def test_backward_compat_no_args(self):
        """EventBus() with no args works exactly like before for valid events."""
        bus = EventBus()
        received = []
        bus.subscribe("custom.event", lambda d: received.append(d))
        await bus.emit("custom.event", {"foo": "bar"})
        assert len(received) == 1


# ---------------------------------------------------------------------------
# Timer events — validated against the shared TIMER_SCHEMA
# ---------------------------------------------------------------------------


class TestTimerEventValidation:
    """Timer events use a shared schema via get_schema() fallback."""

    async def test_valid_timer_event_dev(self):
        bus = _make_bus(env="dev")
        received = []
        bus.subscribe("timer.5m", lambda d: received.append(d))
        await bus.emit("timer.5m", {"tick_time": "2026-01-01T00:00:00Z", "interval": "5m"})
        assert len(received) == 1

    async def test_invalid_timer_event_dev_raises(self):
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError, match="missing required field 'tick_time'"):
            await bus.emit("timer.5m", {"interval": "5m"})

    async def test_custom_timer_interval_validated(self):
        """Arbitrary timer intervals (e.g. timer.7m) use the TIMER_SCHEMA fallback."""
        bus = _make_bus(env="dev")
        with pytest.raises(EventValidationError):
            await bus.emit("timer.7m", {})
